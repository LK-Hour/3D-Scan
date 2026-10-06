"""The reconstruction pipeline: capture session folder -> metric 3D model + accuracy report.

Stages (each cached in <session>/work/state.json, so a crash or a changed setting only re-runs what's needed):
  filter -> features -> matching -> sparse -> tags -> align -> dense -> export
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import __version__
from .align import (Sim3, align_to_arcore, estimate_scale, finalize_transform)
from .colmap_io import Model, read_text_model
from .colmap_runner import Cancelled, ColmapError, make_backend
from .filtering import FilterConfig, select_keyframes
from .poses import select_pairs, view_direction, quat_xyzw_to_R
from .session import SessionError, load_session, validate_session
from .tags import detect_frames, triangulate_tags

ProgressFn = Callable[[str, float, str], None]

STAGES = ["filter", "features", "matching", "sparse", "tags", "align", "dense", "export"]
_WEIGHTS = {"filter": 3, "features": 18, "matching": 14, "sparse": 18, "tags": 5, "align": 2, "dense": 30, "export": 10}


@dataclass
class PipelineConfig:
    # frame selection
    min_translation_m: float = 0.05
    min_rotation_deg: float = 4.0
    blur_rel: float = 0.5
    max_frames: int = 0
    # COLMAP
    backend: str = "auto"            # auto | cli | pycolmap
    colmap_exe: str | None = None
    camera_model: str = "OPENCV"     # OPENCV | PINHOLE | SIMPLE_RADIAL | RADIAL | SIMPLE_PINHOLE
    max_image_size: int = 1600       # GTX 1650 (4 GB): 1600 is safe, 2000 may run out of memory
    max_features: int = 8192
    use_gpu: bool = True
    # pair selection from ARCore poses
    pair_radius_m: float = 3.0
    pair_max_angle_deg: float = 60.0
    pair_max_neighbors: int = 40
    pair_sequential: int = 8
    # dense / mesh
    dense: bool = True
    poisson_depth: int = 11
    # output
    output_up: str = "y"             # y (ARCore/glTF) | z (CAD)
    glb_max_faces: int = 300_000
    min_registered_ratio: float = 0.5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PipelineConfig":
        d = dict(d or {})
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _h(*parts) -> str:
    m = hashlib.sha1()
    for p in parts:
        m.update(json.dumps(p, sort_keys=True, default=str).encode())
    return m.hexdigest()[:16]


class _State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = json.loads(path.read_text()) if path.exists() else {}

    def done(self, name: str, key: str, outputs: list[Path]) -> bool:
        return self.data.get(name) == key and all(p.exists() for p in outputs)

    def mark(self, name: str, key: str) -> None:
        self.data[name] = key
        self.path.write_text(json.dumps(self.data, indent=1))


def _camera_params(model: str, intr) -> list[float]:
    f = (intr.fx + intr.fy) / 2
    return {"OPENCV": [intr.fx, intr.fy, intr.cx, intr.cy, 0, 0, 0, 0],
            "PINHOLE": [intr.fx, intr.fy, intr.cx, intr.cy],
            "SIMPLE_RADIAL": [f, intr.cx, intr.cy, 0],
            "RADIAL": [f, intr.cx, intr.cy, 0, 0],
            "SIMPLE_PINHOLE": [f, intr.cx, intr.cy]}[model]


class Pipeline:
    def __init__(self, session_root: str | Path, cfg: PipelineConfig | None = None,
                 progress: ProgressFn | None = None, cancel: threading.Event | None = None):
        self.root = Path(session_root)
        self.cfg = cfg or PipelineConfig()
        self._progress = progress or (lambda stage, frac, msg: None)
        self.cancel = cancel or threading.Event()
        self.warnings: list[str] = []
        self.timings: dict[str, float] = {}

    # -- helpers ----------------------------------------------------------------------------------------------
    def _p(self, stage: str, within: float, msg: str) -> None:
        done = 0.0
        total = float(sum(_WEIGHTS[s] for s in STAGES if s != "dense" or self.cfg.dense))
        for s in STAGES:
            if s == stage:
                break
            if s != "dense" or self.cfg.dense:
                done += _WEIGHTS[s]
        w = _WEIGHTS[stage] if (stage != "dense" or self.cfg.dense) else 0
        self._progress(stage, min(1.0, (done + w * within) / total), msg)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    # -- main entry -------------------------------------------------------------------------------------------
    def run(self) -> dict:
        cfg = self.cfg
        t_start = time.time()
        rep = validate_session(self.root)
        if not rep.ok:
            raise SessionError("; ".join(rep.errors))
        self.warnings += rep.warnings
        sess = load_session(self.root)
        work = sess.work_dir
        work.mkdir(parents=True, exist_ok=True)
        state = _State(work / "state.json")
        backend = make_backend(cfg.backend, cfg.colmap_exe, log=work / "colmap.log", cancel=self.cancel)
        self._p("filter", 0, f"backend: {backend.version_line}")

        base = _h(hashlib.sha1((self.root / "poses.jsonl").read_bytes()).hexdigest(), sess.intrinsics.__dict__,
                  sorted(p.name for p in sess.frames_dir.iterdir()))

        # ---------- filter
        t = time.time()
        kf_file = work / "keyframes.json"
        k_filter = _h(base, cfg.min_translation_m, cfg.min_rotation_deg, cfg.blur_rel, cfg.max_frames)
        if not state.done("filter", k_filter, [kf_file]):
            self._p("filter", 0.1, "selecting sharp, well-spaced frames")
            fr = select_keyframes(sess, FilterConfig(blur_rel=cfg.blur_rel, min_translation_m=cfg.min_translation_m,
                                                     min_rotation_deg=cfg.min_rotation_deg, max_frames=cfg.max_frames))
            kf_file.write_text(json.dumps({"kept": fr.kept, "dropped": fr.dropped, "stats": fr.stats()}))
            state.mark("filter", k_filter)
        kf = json.loads(kf_file.read_text())
        kept: list[str] = kf["kept"]
        if len(kept) < 10:
            raise SessionError(f"only {len(kept)} usable frames after filtering ({kf['stats']['reasons']}); rescan slower with more light")
        self.timings["filter"] = time.time() - t
        self._check_cancel()

        colmap_dir = work / "colmap"
        db = colmap_dir / "database.db"
        poses = {p.frame: p for p in sess.poses}

        # ---------- features
        t = time.time()
        k_feat = _h(k_filter, cfg.camera_model, cfg.max_image_size, cfg.max_features, cfg.use_gpu, backend.name)
        if not state.done("features", k_feat, [db]):
            self._p("features", 0.0, f"extracting features from {len(kept)} frames")
            shutil.rmtree(colmap_dir, ignore_errors=True)
            colmap_dir.mkdir(parents=True)
            backend.extract_features(db, sess.frames_dir, kept, camera_model=cfg.camera_model,
                                     camera_params=_camera_params(cfg.camera_model, sess.intrinsics),
                                     max_image_size=cfg.max_image_size, max_features=cfg.max_features, use_gpu=cfg.use_gpu)
            state.mark("features", k_feat)
        self.timings["features"] = time.time() - t
        self._check_cancel()

        # ---------- matching (pairs chosen from ARCore poses)
        t = time.time()
        pairs_file = colmap_dir / "pairs.txt"
        k_match = _h(k_feat, cfg.pair_radius_m, cfg.pair_max_angle_deg, cfg.pair_max_neighbors, cfg.pair_sequential)
        if not state.done("matching", k_match, [pairs_file]):
            C = np.array([poses[f].t for f in kept])
            D = np.array([view_direction(quat_xyzw_to_R(poses[f].q)) for f in kept])
            pairs = select_pairs(C, D, radius=cfg.pair_radius_m, max_angle_deg=cfg.pair_max_angle_deg,
                                 max_neighbors=cfg.pair_max_neighbors, sequential=cfg.pair_sequential)
            pairs_file.write_text("".join(f"{kept[i]} {kept[j]}\n" for i, j in pairs))
            self._p("matching", 0.0, f"matching {len(pairs)} image pairs")
            backend.match_pairs(db, pairs_file, use_gpu=cfg.use_gpu)
            state.mark("matching", k_match)
        self.timings["matching"] = time.time() - t
        self._check_cancel()

        # ---------- sparse reconstruction
        t = time.time()
        best_txt = work / "sparse_best"
        k_sparse = _h(k_match, "mapper")
        if not state.done("sparse", k_sparse, [best_txt / "images.txt"]):
            self._p("sparse", 0.0, "structure-from-motion + bundle adjustment")
            for d in (colmap_dir / "sparse", colmap_dir / "sparse_txt", best_txt):
                shutil.rmtree(d, ignore_errors=True)
            txts = backend.map(db, sess.frames_dir, colmap_dir)
            if not txts:
                raise ColmapError("COLMAP could not reconstruct anything. Usually: too little overlap, blur, or blank walls. "
                                  "Rescan with slower movement and more texture in view (see the capture tips).")
            models = [(len(read_text_model(p).images), p) for p in txts]
            n_best, src = max(models, key=lambda x: x[0])
            if len(models) > 1:
                self.warnings.append(f"COLMAP produced {len(models)} disconnected models; using the largest ({n_best} images). "
                                     "Rescan with overlap between the parts, or add shared tags.")
            shutil.copytree(src, best_txt)
            state.mark("sparse", k_sparse)
        model = read_text_model(best_txt)
        n_reg = len(model.images)
        if n_reg < max(10, cfg.min_registered_ratio * len(kept)):
            self.warnings.append(f"only {n_reg}/{len(kept)} frames registered; result may be incomplete")
        if n_reg < 10:
            raise ColmapError(f"only {n_reg} frames registered, cannot continue")
        self.timings["sparse"] = time.time() - t
        self._check_cancel()

        # ---------- tags
        t = time.time()
        self._p("tags", 0.0, "detecting tags and triangulating corners")
        reg_names = {im.name: sess.frame_path(im.name) for im in model.images.values()}
        dets = detect_frames(reg_names, work / "tags_detected.json")
        tag_models = triangulate_tags(model, dets)
        self.timings["tags"] = time.time() - t

        # ---------- align + scale
        self._p("align", 0.0, "scaling to metres")
        rough = align_to_arcore(model, sess)
        scale = estimate_scale(sess, tag_models, rough.sim3.s)
        sim = finalize_transform(rough, scale.scale, model, cfg.output_up)
        self.warnings += scale.warnings
        if rough.rot_spread_deg > 5:
            self.warnings.append(f"ARCore and COLMAP orientations disagree by {rough.rot_spread_deg:.1f} deg RMS; "
                                 "'up' direction may be slightly off or ARCore tracking drifted")
        (work / "alignment.json").write_text(json.dumps({
            "sim3_model_to_world": sim.to_dict(), "scale": scale.scale, "scale_source": scale.source,
            "up": cfg.output_up}, indent=1))
        self._check_cancel()

        # ---------- dense + mesh
        t = time.time()
        fused = mesh = None
        dense_dir = work / "dense"
        k_dense = _h(k_sparse, cfg.dense, cfg.max_image_size, cfg.poisson_depth)
        if cfg.dense:
            if not getattr(backend, "has_dense", False) or not getattr(backend, "has_cuda", False):
                self.warnings.append("dense reconstruction skipped: needs the COLMAP binary built with CUDA "
                                     f"(current backend: {backend.version_line}). Showing the sparse model only.")
            else:
                fused_p, mesh_p = dense_dir / "fused.ply", dense_dir / "meshed.ply"
                if not state.done("dense", k_dense, [fused_p]):
                    self._p("dense", 0.0, "dense depth maps (this is the slow, GPU-heavy part)")
                    shutil.rmtree(dense_dir, ignore_errors=True)
                    size = cfg.max_image_size
                    try:
                        backend.dense(best_txt, sess.frames_dir, dense_dir, max_image_size=size)
                    except ColmapError as e:
                        if size <= 1000:
                            raise
                        self.warnings.append(f"dense at {size}px failed ({str(e).splitlines()[0]}); retried at 1000px (GPU memory?)")
                        shutil.rmtree(dense_dir, ignore_errors=True)
                        backend.dense(best_txt, sess.frames_dir, dense_dir, max_image_size=1000)
                    state.mark("dense", k_dense)
                fused = fused_p
                if not mesh_p.exists():
                    self._p("dense", 0.95, "meshing")
                    try:
                        backend.poisson(fused_p, mesh_p, depth=cfg.poisson_depth)
                    except ColmapError as e:
                        self.warnings.append(f"meshing failed: {str(e).splitlines()[0]}; point cloud only")
                if mesh_p.exists():
                    mesh = mesh_p
        self.timings["dense"] = time.time() - t
        self._check_cancel()

        # ---------- export
        t = time.time()
        self._p("export", 0.0, "writing results")
        from .export import export_results  # local import keeps trimesh optional for the other stages
        out = export_results(work / "result", model, sim, fused, mesh, glb_max_faces=cfg.glb_max_faces)
        self.timings["export"] = time.time() - t
        self.timings["total"] = time.time() - t_start

        report = self._report(sess, kf, model, tag_models, rough, scale, sim, backend, out)
        (work / "result" / "report.json").write_text(json.dumps(report, indent=1))
        (work / "result" / "report.md").write_text(render_report_md(report))
        self._p("export", 1.0, "done")
        return report

    # -- report -----------------------------------------------------------------------------------------------
    def _report(self, sess, kf, model: Model, tags, rough, scale, sim: Sim3, backend, outputs) -> dict:
        reproj = model.mean_reprojection_error()
        ratio = len(model.images) / max(1, len(kf["kept"]))
        tag_rows = []
        for tid, tm in sorted(tags.items()):
            row = {"id": tid, "views": tm.n_views, "reproj_rms_px": round(tm.rms_px, 3)}
            if sess.has_tag_size:
                try:
                    true = sess.tag_size(tid)
                    edges_mm = 1000 * sim.s * tm.edge_lengths
                    row["printed_edge_mm"] = round(1000 * true, 2)
                    row["reconstructed_edge_mm"] = [round(float(e), 2) for e in edges_mm]
                    row["edge_error_mm"] = round(float(np.mean(edges_mm) - 1000 * true), 2)
                except Exception:
                    pass
            tag_rows.append(row)

        validated = any(r["kind"] in ("check", "leave-one-out") for r in scale.laser_residuals)
        if scale.source == "laser" and validated and scale.expected_error_pct <= 0.5 and reproj <= 1.0 and ratio >= 0.85:
            grade = "high"
        elif scale.source in ("laser", "tags") and reproj <= 1.5 and ratio >= 0.7:
            grade = "medium"
        else:
            grade = "low"
        five_m_mm = 50.0 * scale.expected_error_pct  # % of 5000 mm
        summary = {
            "high": "Scale was independently validated against laser distances and the reconstruction fits the images tightly.",
            "medium": "Scale is anchored to tags/laser but is not fully cross-validated, or fit quality is only moderate.",
            "low": "Scale or fit is weak: treat measurements from this model as approximate.",
        }[grade]
        return {
            "tool_version": __version__, "session": sess.session_id, "room": sess.meta.get("room_name"),
            "backend": backend.version_line,
            "frames": {"in_session": len(sess.poses), "kept_after_filter": len(kf["kept"]),
                       "registered": len(model.images), "dropped_reasons": kf["stats"]["reasons"]},
            "sparse": {"points": int(len(model.points.ids)), "mean_reprojection_px": round(reproj, 3),
                       "mean_track_length": round(float(model.points.track_len.mean()), 2) if len(model.points.ids) else 0},
            "scale": {"source": scale.source, "metres_per_model_unit": scale.scale,
                      "expected_scale_error_percent": round(scale.expected_error_pct, 3),
                      "expected_error_on_5m_mm_from_scale_only": round(five_m_mm, 1),
                      "from_tags": scale.scale_tags, "from_laser": scale.scale_laser, "from_arcore_rough": scale.scale_arcore,
                      "laser_residuals": [{**r, "residual_mm": round(r["residual_mm"], 2), "model_m": round(r["model_m"], 4)}
                                          for r in scale.laser_residuals]},
            "tags": tag_rows,
            "arcore_alignment": {"orientation_rms_deg": round(rough.rot_spread_deg, 2),
                                 "camera_centre_rms_m_after_alignment": round(rough.centre_rms_m, 4)},
            "accuracy": {"grade": grade, "summary": summary,
                         "note": "expected_scale_error covers absolute scale only; local surface noise is not included "
                                 "(see reprojection error and tag edge errors)."},
            "warnings": self.warnings, "timings_s": {k: round(v, 1) for k, v in self.timings.items()},
            "outputs": {k: str(v) for k, v in outputs.items()},
        }


def render_report_md(r: dict) -> str:
    L = [f"# Scan report: {r.get('room') or r['session']}", "",
         f"**Accuracy grade: {r['accuracy']['grade'].upper()}**: {r['accuracy']['summary']}", "",
         "## Reconstruction",
         f"- Frames: {r['frames']['in_session']} captured, {r['frames']['kept_after_filter']} kept, {r['frames']['registered']} registered",
         f"- Sparse points: {r['sparse']['points']}, mean reprojection error {r['sparse']['mean_reprojection_px']} px",
         "", "## Scale",
         f"- Source: **{r['scale']['source']}**, expected scale error ~{r['scale']['expected_scale_error_percent']} % "
         f"(~{r['scale']['expected_error_on_5m_mm_from_scale_only']} mm over 5 m, scale only)"]
    for x in r["scale"]["laser_residuals"]:
        L.append(f"  - {x['a']} to {x['b']}: measured {x['measured_m']} m, model {x['model_m']} m, "
                 f"residual {x['residual_mm']:+} mm ({x['kind']})")
    if r["tags"]:
        L += ["", "## Tags"]
        for t in r["tags"]:
            extra = f", edge error {t['edge_error_mm']:+} mm" if "edge_error_mm" in t else ""
            L.append(f"- Tag {t['id']}: {t['views']} views, reprojection {t['reproj_rms_px']} px{extra}")
    if r["warnings"]:
        L += ["", "## Warnings"] + [f"- {w}" for w in r["warnings"]]
    L += ["", f"_{r['accuracy']['note']}_", ""]
    return "\n".join(L)


def process_folder(root: str | Path, cfg: PipelineConfig | None = None, progress: ProgressFn | None = None) -> dict:
    return Pipeline(root, cfg, progress).run()
