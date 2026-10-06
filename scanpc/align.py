"""Metric alignment: ARCore gives gravity and a rough scale, tags + laser distances give the real scale."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .colmap_io import Model
from .poses import chordal_mean_rotation, cv_w2c_to_c2w_gl, quat_xyzw_to_R
from .session import Measurement, Session
from .tags import TagModel

# Y-up (ARCore / glTF)  ->  Z-up (CAD / point-cloud tools)
R_Y_TO_Z_UP = np.array([[1.0, 0, 0], [0, 0, -1.0], [0, 1.0, 0]])


@dataclass
class Sim3:
    s: float
    R: np.ndarray
    t: np.ndarray

    def apply(self, X: np.ndarray) -> np.ndarray:
        return self.s * (np.asarray(X) @ self.R.T) + self.t

    def to_dict(self) -> dict:
        return {"scale": self.s, "R": self.R.tolist(), "t": self.t.tolist()}

    @classmethod
    def identity(cls) -> "Sim3":
        return cls(1.0, np.eye(3), np.zeros(3))

    def then(self, other: "Sim3") -> "Sim3":
        """Compose: first self, then other."""
        return Sim3(other.s * self.s, other.R @ self.R, other.s * (other.R @ self.t) + other.t)


def umeyama(src: np.ndarray, dst: np.ndarray, with_scale: bool = True) -> Sim3:
    """Least-squares similarity transform mapping src -> dst (both (n,3))."""
    n = len(src)
    mu_s, mu_d = src.mean(0), dst.mean(0)
    xs, xd = src - mu_s, dst - mu_d
    cov = xd.T @ xs / n
    U, S, Vt = np.linalg.svd(cov)
    D = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        D[2, 2] = -1
    R = U @ D @ Vt
    var_s = (xs ** 2).sum() / n
    s = float(np.trace(np.diag(S) @ D) / var_s) if with_scale and var_s > 0 else 1.0
    return Sim3(s, R, mu_d - s * R @ mu_s)


@dataclass
class RoughAlignment:
    sim3: Sim3
    n_images: int
    rot_spread_deg: float     # how consistently ARCore and COLMAP orientations agree (drift indicator)
    centre_rms_m: float       # residual of camera centres after alignment (ARCore drift + scale error)


def align_to_arcore(model: Model, session: Session) -> RoughAlignment:
    """Rotation (=> gravity/up), translation and a *rough* scale from ARCore's own camera poses.

    Rotation uses camera orientations (robust even if you walked in a straight line); scale uses
    camera-centre spread. ARCore's scale is only good to a few percent, so the final scale is
    replaced by tag / laser scale in `estimate_scale`.
    """
    poses = {p.frame: p for p in session.poses}
    Rs, A, B = [], [], []
    for im in model.images.values():
        p = poses.get(im.name)
        if p is None:
            continue
        R_gl_c2w, C = cv_w2c_to_c2w_gl(im.R(), im.tvec)
        Rs.append(quat_xyzw_to_R(p.q) @ R_gl_c2w.T)
        A.append(p.t); B.append(C)
    if len(Rs) < 3:
        raise ValueError("fewer than 3 registered images match poses.jsonl; cannot align to ARCore")
    Rs = np.array(Rs); A = np.array(A); B = np.array(B)
    R = chordal_mean_rotation(Rs)
    ang = [np.degrees(np.arccos(np.clip((np.trace(R.T @ r) - 1) / 2, -1, 1))) for r in Rs]
    a0, b0 = A.mean(0), B.mean(0)
    BR = (B - b0) @ R.T
    denom = float((BR ** 2).sum())
    s = float(((A - a0) * BR).sum() / denom) if denom > 0 else 1.0
    t = a0 - s * R @ b0
    sim = Sim3(s, R, t)
    rms = float(np.sqrt(np.mean(np.sum((sim.apply(B) - A) ** 2, axis=1))))
    return RoughAlignment(sim, len(Rs), float(np.sqrt(np.mean(np.square(ang)))), rms)


@dataclass
class ScaleEstimate:
    scale: float                  # metres per model unit
    source: str                   # "laser" | "tags" | "arcore"
    expected_error_pct: float
    scale_tags: float | None = None
    scale_laser: float | None = None
    scale_arcore: float | None = None
    per_tag_scale: dict[int, float] = field(default_factory=dict)
    laser_residuals: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _point(tags: dict[int, TagModel], tag: int, point: str):
    tm = tags.get(tag)
    return None if tm is None else tm.point(point)


def estimate_scale(session: Session, tags: dict[int, TagModel], scale_arcore: float) -> ScaleEstimate:
    warnings: list[str] = []

    # 1) tag edge lengths (each tag gives a short-baseline scale; the median over tags is robust)
    per_tag: dict[int, float] = {}
    if session.has_tag_size:
        for tid, tm in tags.items():
            try:
                true = session.tag_size(tid)
            except Exception:
                continue
            per_tag[tid] = float(true / np.mean(tm.edge_lengths))
    s_tags = float(np.median(list(per_tag.values()))) if per_tag else None
    tag_spread_pct = None
    if len(per_tag) >= 2:
        v = np.array(list(per_tag.values()))
        tag_spread_pct = float(100 * v.std(ddof=1) / v.mean() / np.sqrt(len(v)))

    # 2) laser distances between tag points (long baselines => far more accurate than tag edges)
    def model_dist(m: Measurement):
        pa, pb = _point(tags, m.a_tag, m.a_point), _point(tags, m.b_tag, m.b_point)
        return None if pa is None or pb is None else float(np.linalg.norm(pa - pb))

    usable = [(m, d) for m in session.measurements if (d := model_dist(m)) is not None]
    skipped = len(session.measurements) - len(usable)
    if skipped:
        warnings.append(f"{skipped} laser measurement(s) skipped: a tag was not triangulated in this model")
    fit = [(m, d) for m, d in usable if m.use == "fit"]
    check = [(m, d) for m, d in usable if m.use == "check"]

    def fit_scale(items):
        w = np.array([1.0 / m.sigma_m ** 2 for m, _ in items])
        meas = np.array([m.meters for m, _ in items]); dm = np.array([d for _, d in items])
        return float((w * meas * dm).sum() / (w * dm * dm).sum())

    s_laser = fit_scale(fit) if fit else None
    residuals: list[dict] = []
    if s_laser is not None:
        for m, d in check:
            residuals.append(_resid(m, d, s_laser, "check"))
        if not check and len(fit) >= 3:
            for k, (m, d) in enumerate(fit):
                rest = fit[:k] + fit[k + 1:]
                residuals.append(_resid(m, d, fit_scale(rest), "leave-one-out"))
        if not residuals:
            for m, d in fit:
                residuals.append(_resid(m, d, s_laser, "fit"))

    # choose
    if s_laser is not None:
        scale, source = s_laser, "laser"
        honest = [r for r in residuals if r["kind"] in ("check", "leave-one-out")]
        if honest:
            rel = np.array([abs(r["residual_mm"]) / (1000 * r["measured_m"]) for r in honest])
            err = float(100 * np.sqrt(np.mean(rel ** 2)))
        else:
            err = float(100 * np.median([m.sigma_m / m.meters for m, _ in fit]))
            warnings.append("only one or two laser distances, so the scale error cannot be cross-checked; "
                            "measure at least 3 (ideally along different directions)")
    elif s_tags is not None:
        scale, source = s_tags, "tags"
        err = max(tag_spread_pct or 0.0, 0.5)
        warnings.append("scale comes from printed tag size only; add laser distances (measurements.json) for best accuracy")
    else:
        scale, source = scale_arcore, "arcore"
        err = 3.0
        warnings.append("no usable tags/laser distances: absolute scale comes from ARCore only (expect ~2-5 % error)")

    if s_laser is not None and s_tags is not None and abs(s_laser / s_tags - 1) > 0.02:
        warnings.append(f"laser scale and tag-size scale disagree by {100 * (s_laser / s_tags - 1):+.1f} %: "
                        "re-measure the printed tag size, or check the tag was printed at 100 %")
    if abs(scale / scale_arcore - 1) > 0.15:
        warnings.append(f"final scale differs from ARCore's by {100 * (scale / scale_arcore - 1):+.1f} %: "
                        "unusual; check tag sizes / measurements")

    return ScaleEstimate(scale, source, err, s_tags, s_laser, scale_arcore, per_tag, residuals, warnings)


def _resid(m: Measurement, d_model: float, s: float, kind: str) -> dict:
    pred = s * d_model
    return {"a": f"tag{m.a_tag}.{m.a_point}", "b": f"tag{m.b_tag}.{m.b_point}", "kind": kind,
            "measured_m": m.meters, "model_m": pred, "residual_mm": 1000 * (pred - m.meters)}


def finalize_transform(rough: RoughAlignment, scale: float, model: Model, up: str = "y") -> Sim3:
    """Metric transform: ARCore-derived rotation (gravity), the trusted scale, centroid kept at ARCore's."""
    R = rough.sim3.R
    # keep the model centroid where ARCore puts it, but at the trusted scale
    B = np.array([im.center() for im in model.images.values()])
    centroid_world = rough.sim3.apply(B.mean(0, keepdims=True))[0]
    t = centroid_world - scale * R @ B.mean(0)
    sim = Sim3(scale, R, t)
    if up == "z":
        sim = sim.then(Sim3(1.0, R_Y_TO_Z_UP, np.zeros(3)))
    return sim
