"""AprilTag (tag36h11) detection and multi-view triangulation of tag corners.

Tags are the ground-truth anchors of the whole system: their printed size fixes the metric
scale, laser distances between them validate it, and shared tags stitch rooms together.
Detection runs on the full-resolution saved frames (not on what the Android app saw live).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .colmap_io import Model


@dataclass
class TagDetection:
    frame: str
    tag_id: int
    corners: np.ndarray  # (4,2) pixels, order: top-left, top-right, bottom-right, bottom-left of the upright tag


@dataclass
class TagModel:
    tag_id: int
    corners: np.ndarray        # (4,3) triangulated corner positions in the model frame
    n_views: int               # views used for the worst-observed corner
    rms_px: float              # reprojection RMS over all used observations
    edge_lengths: np.ndarray   # (4,) in model units

    @property
    def center(self) -> np.ndarray:
        return self.corners.mean(axis=0)

    def point(self, name: str) -> np.ndarray:
        if name == "center":
            return self.center
        return self.corners[int(name[-1])]


def make_detector() -> "cv2.aruco.ArucoDetector":
    dic = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    params = cv2.aruco.DetectorParameters()
    refine = getattr(cv2.aruco, "CORNER_REFINE_APRILTAG", cv2.aruco.CORNER_REFINE_SUBPIX)
    params.cornerRefinementMethod = refine
    return cv2.aruco.ArucoDetector(dic, params)


def detect_frames(frame_paths: dict[str, Path], cache_file: Path | None = None) -> dict[str, list[TagDetection]]:
    """Detect tags in every frame. Results are cached as JSON because detection on 2000 px frames is not free."""
    if cache_file and cache_file.exists():
        raw = json.loads(cache_file.read_text())
        if set(raw["frames"]) == set(frame_paths):
            return {f: [TagDetection(f, d["id"], np.array(d["corners"], float)) for d in dets]
                    for f, dets in raw["detections"].items()}
    detector = make_detector()
    out: dict[str, list[TagDetection]] = {}
    for name, path in frame_paths.items():
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            out[name] = []
            continue
        corners, ids, _ = detector.detectMarkers(gray)
        dets = []
        if ids is not None:
            for c, i in zip(corners, ids.flatten()):
                dets.append(TagDetection(name, int(i), c.reshape(4, 2).astype(float)))
        out[name] = dets
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "frames": sorted(frame_paths),
            "detections": {f: [{"id": d.tag_id, "corners": d.corners.tolist()} for d in dets] for f, dets in out.items()},
        }))
    return out


def _triangulate_rays(C: np.ndarray, D: np.ndarray) -> tuple[np.ndarray, float]:
    """Point closest (least squares) to rays C + s*D. Returns (X, smallest eigenvalue of normal matrix / n)."""
    A = np.zeros((3, 3)); b = np.zeros(3)
    for c, d in zip(C, D):
        P = np.eye(3) - np.outer(d, d)
        A += P; b += P @ c
    X = np.linalg.solve(A, b)
    return X, float(np.linalg.eigvalsh(A)[0] / len(C))


def triangulate_tags(model: Model, detections: dict[str, list[TagDetection]], *, min_views: int = 3,
                     min_parallax_deg: float = 2.0, max_reproj_px: float = 3.0) -> dict[int, TagModel]:
    """Triangulate every tag corner seen in >= min_views registered images (with outlier rejection)."""
    by_name = model.by_name()
    # tag -> corner -> list of (image, pixel)
    obs: dict[int, list[list[tuple]]] = {}
    for fname, dets in detections.items():
        im = by_name.get(fname)
        if im is None:
            continue
        for d in dets:
            slots = obs.setdefault(d.tag_id, [[], [], [], []])
            for k in range(4):
                slots[k].append((im, d.corners[k]))

    result: dict[int, TagModel] = {}
    for tag_id, slots in obs.items():
        pts3, nviews, sq_err, n_err = [], [], 0.0, 0
        ok = True
        for k in range(4):
            res = _triangulate_corner(model, slots[k], min_views, min_parallax_deg, max_reproj_px)
            if res is None:
                ok = False
                break
            X, n_used, errs = res
            pts3.append(X); nviews.append(n_used); sq_err += float(np.sum(errs ** 2)); n_err += len(errs)
        if not ok:
            continue
        P = np.array(pts3)
        edges = np.array([np.linalg.norm(P[(k + 1) % 4] - P[k]) for k in range(4)])
        result[tag_id] = TagModel(tag_id, P, min(nviews), float(np.sqrt(sq_err / max(n_err, 1))), edges)
    return result


def _triangulate_corner(model: Model, observations, min_views, min_parallax_deg, max_reproj_px):
    cams = model.cameras
    C, D, imgs, px = [], [], [], []
    for im, pix in observations:
        cam = cams[im.camera_id]
        K, dist = cam.K_and_dist()
        xn = cv2.undistortPoints(np.array([[pix]], dtype=np.float64), K, dist).reshape(2)
        d_cam = np.array([xn[0], xn[1], 1.0]); d_cam /= np.linalg.norm(d_cam)
        R = im.R()
        C.append(im.center()); D.append(R.T @ d_cam); imgs.append(im); px.append(pix)
    C, D = np.array(C), np.array(D)
    keep = np.ones(len(C), bool)
    for _ in range(4):
        if keep.sum() < min_views:
            return None
        X, _ = _triangulate_rays(C[keep], D[keep])
        errs = np.array([_reproj_err(model, im, X, p) for im, p in zip(imgs, px)])
        new_keep = errs <= max_reproj_px
        if new_keep.sum() < min_views:
            return None
        if (new_keep == keep).all():
            break
        keep = new_keep
    Dk = D[keep]
    cosang = np.clip(Dk @ Dk.T, -1, 1)
    if np.degrees(np.arccos(cosang.min())) < min_parallax_deg:
        return None
    X, _ = _triangulate_rays(C[keep], D[keep])
    errs = np.array([_reproj_err(model, im, X, p) for im, p, k in zip(imgs, px, keep) if k])
    return X, int(keep.sum()), errs


def _reproj_err(model: Model, im, X: np.ndarray, pix: np.ndarray) -> float:
    cam = model.cameras[im.camera_id]
    K, dist = cam.K_and_dist()
    R = im.R()
    Xc = R @ X + im.tvec
    if Xc[2] <= 1e-9:
        return 1e9
    proj, _ = cv2.projectPoints(X.reshape(1, 1, 3), cv2.Rodrigues(R)[0], im.tvec.reshape(3, 1), K, dist)
    return float(np.linalg.norm(proj.reshape(2) - pix))
