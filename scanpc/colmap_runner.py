"""COLMAP back-ends.

* `ColmapCli`   - drives the official COLMAP binary (Windows CUDA zip, Linux build). Needed for dense
                  reconstruction on your GTX 1650. It probes `colmap <cmd> -h` and only passes option names
                  that exist, because COLMAP renamed many options between 3.11 / 3.12 / 4.x.
* `PyColmap`    - the `pycolmap` Python package. Sparse stages only unless that build has CUDA; used for tests
                  and as a no-install fallback on Linux.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional


class ColmapError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def find_colmap(explicit: str | None = None) -> str | None:
    cands = [explicit, os.environ.get("COLMAP_EXE"), "colmap", "COLMAP.bat", "colmap.exe", "colmap.bat"]
    for c in cands:
        if c and (shutil.which(c) or Path(c).is_file()):
            return shutil.which(c) or str(c)
    return None


class ColmapCli:
    name = "cli"
    has_dense = True

    def __init__(self, exe: str, log: Path | None = None, cancel: threading.Event | None = None):
        self.exe = exe
        self.log = log
        self.cancel = cancel or threading.Event()
        self._help: dict[str, str] = {}
        top = self._run_capture(["-h"])
        self.version_line = top.strip().splitlines()[0] if top.strip() else "COLMAP (version unknown)"
        self.has_cuda = "with CUDA" in top and "without CUDA" not in top

    # -- plumbing -------------------------------------------------------------------------------------------
    def _run_capture(self, args: list[str]) -> str:
        p = subprocess.run([self.exe, *args], capture_output=True, text=True, errors="replace", timeout=60)
        return (p.stdout or "") + (p.stderr or "")

    def _opts(self, cmd: str) -> str:
        if cmd not in self._help:
            self._help[cmd] = self._run_capture([cmd, "-h"])
        return self._help[cmd]

    def _pick(self, cmd: str, *candidates: str) -> Optional[str]:
        """First option name that this COLMAP version knows for `cmd` (None if none)."""
        text = self._opts(cmd)
        for c in candidates:
            if re.search(re.escape(c) + r"(\s|=|$)", text):
                return c
        return None

    def _flags(self, cmd: str, wanted: list[tuple[tuple[str, ...], object]]) -> list[str]:
        out: list[str] = []
        for names, value in wanted:
            opt = self._pick(cmd, *names)
            if opt is not None:
                out += [opt, str(value)]
        return out

    def run(self, args: list[str], on_line: Callable[[str], None] | None = None) -> None:
        if self.log:
            self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a", encoding="utf-8") if self.log else open(os.devnull, "w") as lf:
            lf.write("\n$ " + " ".join([self.exe, *args]) + "\n"); lf.flush()
            proc = subprocess.Popen([self.exe, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace")
            assert proc.stdout is not None
            for line in proc.stdout:
                lf.write(line)
                if on_line:
                    on_line(line)
                if self.cancel.is_set():
                    proc.terminate()
                    proc.wait(timeout=20)
                    raise Cancelled()
            rc = proc.wait()
        if rc != 0:
            tail = ""
            if self.log and self.log.exists():
                tail = "\n".join(self.log.read_text(errors="replace").splitlines()[-15:])
            raise ColmapError(f"colmap {args[0]} failed with exit code {rc}\n{tail}")

    # -- sparse ---------------------------------------------------------------------------------------------
    def extract_features(self, db: Path, images: Path, names: list[str], *, camera_model: str, camera_params: list[float],
                         max_image_size: int, max_features: int, use_gpu: bool) -> None:
        lst = db.parent / "image_list.txt"
        lst.write_text("\n".join(names) + "\n")
        args = ["feature_extractor", "--database_path", str(db), "--image_path", str(images),
                "--image_list_path", str(lst)]
        args += self._flags("feature_extractor", [
            (("--ImageReader.camera_model",), camera_model),
            (("--ImageReader.single_camera",), 1),
            (("--ImageReader.camera_params",), ",".join(f"{v:.10g}" for v in camera_params)),
            (("--FeatureExtraction.use_gpu", "--SiftExtraction.use_gpu"), int(use_gpu)),
            (("--FeatureExtraction.max_image_size", "--SiftExtraction.max_image_size"), max_image_size),
            (("--SiftExtraction.max_num_features",), max_features),
        ])
        self.run(args)

    def match_pairs(self, db: Path, pairs_file: Path, *, use_gpu: bool) -> None:
        args = ["matches_importer", "--database_path", str(db), "--match_list_path", str(pairs_file)]
        args += self._flags("matches_importer", [
            (("--match_type",), "pairs"),
            (("--FeatureMatching.use_gpu", "--SiftMatching.use_gpu"), int(use_gpu)),
        ])
        self.run(args)

    def map(self, db: Path, images: Path, out_dir: Path) -> list[Path]:
        sparse = out_dir / "sparse"
        sparse.mkdir(parents=True, exist_ok=True)
        self.run(["mapper", "--database_path", str(db), "--image_path", str(images), "--output_path", str(sparse)])
        models = sorted(p for p in sparse.iterdir() if p.is_dir())
        txts = []
        for m in models:
            t = out_dir / "sparse_txt" / m.name
            t.mkdir(parents=True, exist_ok=True)
            self.run(["model_converter", "--input_path", str(m), "--output_path", str(t), "--output_type", "TXT"])
            txts.append(t)
        return txts

    # -- dense (needs CUDA) ---------------------------------------------------------------------------------
    def dense(self, sparse_bin_or_txt: Path, images: Path, out_dir: Path, *, max_image_size: int, geom_consistency: bool = True) -> Path:
        if not self.has_cuda:
            raise ColmapError("this COLMAP build has no CUDA; dense reconstruction (patch_match_stereo) needs the CUDA build")
        out_dir.mkdir(parents=True, exist_ok=True)
        self.run(["image_undistorter", "--image_path", str(images), "--input_path", str(sparse_bin_or_txt),
                  "--output_path", str(out_dir), "--output_type", "COLMAP", "--max_image_size", str(max_image_size)])
        pm = ["patch_match_stereo", "--workspace_path", str(out_dir), "--workspace_format", "COLMAP"]
        pm += self._flags("patch_match_stereo", [
            (("--PatchMatchStereo.geom_consistency",), str(geom_consistency).lower()),
            (("--PatchMatchStereo.max_image_size",), max_image_size),
            (("--PatchMatchStereo.cache_size",), 16),
        ])
        self.run(pm)
        fused = out_dir / "fused.ply"
        fu = ["stereo_fusion", "--workspace_path", str(out_dir), "--workspace_format", "COLMAP", "--output_path", str(fused)]
        fu += self._flags("stereo_fusion", [(("--input_type",), "geometric" if geom_consistency else "photometric")])
        self.run(fu)
        return fused

    def poisson(self, fused: Path, out_ply: Path, depth: int = 11, trim: float = 7.0) -> Path:
        args = ["poisson_mesher", "--input_path", str(fused), "--output_path", str(out_ply)]
        args += self._flags("poisson_mesher", [(("--PoissonMeshing.depth",), depth), (("--PoissonMeshing.trim",), trim)])
        self.run(args)
        return out_ply


class PyColmap:
    name = "pycolmap"

    def __init__(self, log: Path | None = None, cancel: threading.Event | None = None):
        import pycolmap  # noqa: F401
        self.pc = pycolmap
        self.has_cuda = bool(getattr(pycolmap, "has_cuda", False))
        self.has_dense = False
        self.cancel = cancel or threading.Event()
        self.version_line = f"pycolmap {pycolmap.__version__} ({'with' if self.has_cuda else 'without'} CUDA)"

    def _device(self, use_gpu: bool):
        return self.pc.Device.auto if (use_gpu and self.has_cuda) else self.pc.Device.cpu

    def extract_features(self, db: Path, images: Path, names: list[str], *, camera_model: str, camera_params: list[float],
                         max_image_size: int, max_features: int, use_gpu: bool) -> None:
        pc = self.pc
        reader = pc.ImageReaderOptions()
        reader.camera_model = camera_model
        reader.camera_params = ",".join(f"{v:.10g}" for v in camera_params)
        ext = pc.FeatureExtractionOptions()
        ext.max_image_size = max_image_size
        ext.sift.max_num_features = max_features
        pc.extract_features(str(db), str(images), image_names=names, camera_mode=pc.CameraMode.SINGLE,
                            reader_options=reader, extraction_options=ext, device=self._device(use_gpu))

    def match_pairs(self, db: Path, pairs_file: Path, *, use_gpu: bool) -> None:
        pc = self.pc
        pairing = pc.ImportedPairingOptions()
        pairing.match_list_path = str(pairs_file)
        pc.match_image_pairs(str(db), pairing_options=pairing, device=self._device(use_gpu))

    def map(self, db: Path, images: Path, out_dir: Path) -> list[Path]:
        sparse = out_dir / "sparse"
        sparse.mkdir(parents=True, exist_ok=True)
        recs = self.pc.incremental_mapping(str(db), str(images), str(sparse))
        txts = []
        for k, rec in recs.items():
            t = out_dir / "sparse_txt" / str(k)
            t.mkdir(parents=True, exist_ok=True)
            rec.write_text(str(t))
            txts.append(t)
        return txts

    def dense(self, *a, **k):  # pragma: no cover
        raise ColmapError("dense reconstruction needs the COLMAP CUDA binary (set COLMAP_EXE)")


def make_backend(kind: str = "auto", exe: str | None = None, log: Path | None = None, cancel: threading.Event | None = None):
    """kind: 'cli' | 'pycolmap' | 'auto' (prefers the CLI because only it can do dense on a GPU)."""
    if kind in ("cli", "auto"):
        found = find_colmap(exe)
        if found:
            return ColmapCli(found, log, cancel)
        if kind == "cli":
            raise ColmapError("COLMAP executable not found. Install it and put it on PATH, or set COLMAP_EXE. See README.")
    try:
        return PyColmap(log, cancel)
    except ImportError as e:
        raise ColmapError("neither the COLMAP executable nor the pycolmap package is available. See README.") from e
