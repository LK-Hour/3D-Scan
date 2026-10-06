"""Minimal reader/writer for COLMAP's text model format (cameras.txt, images.txt, points3D.txt)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .poses import quat_xyzw_to_R  # noqa: F401  (re-exported for convenience)


@dataclass
class Camera:
    id: int
    model: str
    width: int
    height: int
    params: np.ndarray

    def K_and_dist(self) -> tuple[np.ndarray, np.ndarray]:
        """OpenCV camera matrix and distortion vector for this COLMAP camera."""
        p, m = self.params, self.model
        if m == "SIMPLE_PINHOLE":
            f, cx, cy = p; return np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]]), np.zeros(4)
        if m == "PINHOLE":
            fx, fy, cx, cy = p; return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]]), np.zeros(4)
        if m == "SIMPLE_RADIAL":
            f, cx, cy, k = p; return np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]]), np.array([k, 0, 0, 0.0])
        if m == "RADIAL":
            f, cx, cy, k1, k2 = p; return np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]]), np.array([k1, k2, 0, 0.0])
        if m == "OPENCV":
            fx, fy, cx, cy, k1, k2, p1, p2 = p
            return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]]), np.array([k1, k2, p1, p2])
        raise ValueError(f"unsupported COLMAP camera model {m}")

    @property
    def focal(self) -> float:
        return float(self.params[0])


@dataclass
class Image:
    id: int
    qvec: np.ndarray  # w x y z, world-to-camera
    tvec: np.ndarray
    camera_id: int
    name: str
    xys: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    point3d_ids: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))

    def R(self) -> np.ndarray:
        w, x, y, z = self.qvec / np.linalg.norm(self.qvec)
        return np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ])

    def center(self) -> np.ndarray:
        return -self.R().T @ self.tvec


@dataclass
class Points:
    ids: np.ndarray
    xyz: np.ndarray
    rgb: np.ndarray
    error: np.ndarray
    track_len: np.ndarray


@dataclass
class Model:
    cameras: dict[int, Camera]
    images: dict[int, Image]
    points: Points

    def by_name(self) -> dict[str, Image]:
        return {im.name: im for im in self.images.values()}

    def mean_reprojection_error(self) -> float:
        return float(self.points.error.mean()) if len(self.points.error) else float("nan")


def _data_lines(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip() and not line.startswith("#"):
                yield line.rstrip("\n")


def read_text_model(folder: str | Path) -> Model:
    folder = Path(folder)
    cams: dict[int, Camera] = {}
    for line in _data_lines(folder / "cameras.txt"):
        e = line.split()
        cams[int(e[0])] = Camera(int(e[0]), e[1], int(e[2]), int(e[3]), np.array(e[4:], dtype=float))

    images: dict[int, Image] = {}
    # images.txt alternates a header line and a POINTS2D line; the latter may be empty, so do not skip blanks
    raw = (folder / "images.txt").read_text(encoding="utf-8").splitlines()
    raw = [r for r in raw if not r.startswith("#")]
    for k in range(0, len(raw) - 1, 2):
        e = raw[k].split()
        if len(e) < 10:
            continue
        pts = raw[k + 1].split()
        arr = np.array(pts, dtype=float).reshape(-1, 3) if pts else np.zeros((0, 3))
        images[int(e[0])] = Image(
            int(e[0]), np.array(e[1:5], dtype=float), np.array(e[5:8], dtype=float), int(e[8]), e[9],
            arr[:, :2].copy(), arr[:, 2].astype(np.int64))

    ids, xyz, rgb, err, tl = [], [], [], [], []
    p3 = folder / "points3D.txt"
    if p3.exists():
        for line in _data_lines(p3):
            e = line.split()
            ids.append(int(e[0])); xyz.append([float(v) for v in e[1:4]])
            rgb.append([int(v) for v in e[4:7]]); err.append(float(e[7])); tl.append((len(e) - 8) // 2)
    pts = Points(np.array(ids, dtype=np.int64), np.array(xyz, dtype=float).reshape(-1, 3),
                 np.array(rgb, dtype=np.uint8).reshape(-1, 3), np.array(err, dtype=float),
                 np.array(tl, dtype=np.int64))
    return Model(cams, images, pts)
