"""Pose maths: ARCore (OpenGL camera, camera-to-world) <-> COLMAP (OpenCV camera, world-to-camera)."""
from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

# OpenGL camera (x right, y up, looks -z)  ->  OpenCV camera (x right, y down, looks +z)
GL_TO_CV = np.diag([1.0, -1.0, -1.0])


def quat_xyzw_to_R(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q / np.linalg.norm(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def R_to_quat_wxyz(R: np.ndarray) -> np.ndarray:
    """Rotation matrix -> unit quaternion (w, x, y, z) with w >= 0 (COLMAP order)."""
    m = R
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = np.sqrt(tr + 1.0) * 2
        q = np.array([0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s])
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = np.array([(m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s])
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = np.array([(m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s])
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = np.array([(m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s])
    q /= np.linalg.norm(q)
    return -q if q[0] < 0 else q


def arcore_c2w_gl(t: np.ndarray, q_xyzw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """ARCore pose -> (R_c2w with OpenGL camera axes, camera centre)."""
    return quat_xyzw_to_R(q_xyzw), np.asarray(t, float)


def c2w_gl_to_cv_w2c(R_c2w_gl: np.ndarray, C: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """OpenGL camera-to-world -> OpenCV/COLMAP world-to-camera (R, tvec)."""
    R_c2w_cv = R_c2w_gl @ GL_TO_CV
    R_w2c = R_c2w_cv.T
    return R_w2c, -R_w2c @ C


def cv_w2c_to_c2w_gl(R_w2c: np.ndarray, tvec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of c2w_gl_to_cv_w2c."""
    R_c2w_cv = R_w2c.T
    C = -R_c2w_cv @ tvec
    return R_c2w_cv @ GL_TO_CV, C


def view_direction(R_c2w_gl: np.ndarray) -> np.ndarray:
    """World-space unit vector the (OpenGL) camera looks along (-Z axis)."""
    return -R_c2w_gl[:, 2]


def select_pairs(centers: np.ndarray, dirs: np.ndarray, *, radius: float = 3.0, max_angle_deg: float = 60.0,
                 max_neighbors: int = 40, sequential: int = 8) -> list[tuple[int, int]]:
    """Choose image pairs worth matching, using ARCore poses (frames are in time order).

    A pair qualifies if the cameras are within `radius` metres and look in similar directions,
    or if they are within `sequential` frames of each other. Each frame keeps at most
    `max_neighbors` spatial partners (nearest first), so matching cost stays ~linear in frames.
    Pose drift of a few percent does not matter here: we only need *plausible* overlap.
    """
    n = len(centers)
    pairs: set[tuple[int, int]] = set()
    for i in range(n):
        for j in range(i + 1, min(n, i + 1 + sequential)):
            pairs.add((i, j))
    if n > 1:
        cos_min = np.cos(np.deg2rad(max_angle_deg))
        tree = cKDTree(centers)
        for i in range(n):
            idx = tree.query_ball_point(centers[i], radius)
            cand = [j for j in idx if j != i and float(dirs[i] @ dirs[j]) >= cos_min]
            cand.sort(key=lambda j: float(np.linalg.norm(centers[j] - centers[i])))
            for j in cand[:max_neighbors]:
                pairs.add((min(i, j), max(i, j)))
    return sorted(pairs)


def chordal_mean_rotation(rotations: np.ndarray) -> np.ndarray:
    """Average of rotation matrices (n,3,3) via SVD projection onto SO(3)."""
    M = rotations.sum(axis=0)
    U, _, Vt = np.linalg.svd(M)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt
    return R
