"""Synthetic capture session: a textured room with AprilTags, rendered by ray casting.

Used to test the whole PC pipeline without the Android app. The "ARCore" poses deliberately contain a
scale error, a yaw offset and drift, so a passing test proves that tag/laser scaling fixes them.
Ground truth is written to gt.json next to the session (NOT inside it, so the pipeline never sees it).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from .poses import R_to_quat_wxyz

ROOM = np.array([5.0, 2.6, 4.0])  # x, y (up), z in metres


def _texture(rng: np.random.Generator, S: int = 2048) -> np.ndarray:
    img = np.zeros((S, S, 3), np.float32)
    for cells, amp in [(8, 0.9), (16, 0.8), (32, 0.6), (64, 0.5), (128, 0.4), (256, 0.35), (512, 0.3)]:
        n = rng.random((cells, cells, 3)).astype(np.float32)
        img += amp * cv2.resize(n, (S, S), interpolation=cv2.INTER_CUBIC)
    img = (img - img.min()) / (img.max() - img.min())
    img = (img * 255).astype(np.uint8)
    for _ in range(60):
        c = tuple(int(v) for v in rng.integers(0, 255, 3))
        x, y = (int(v) for v in rng.integers(0, S, 2))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(20, 160)), y + int(rng.integers(20, 160))), c, -1)
        else:
            cv2.circle(img, (x, y), int(rng.integers(10, 80)), c, -1)
    return img


# face: origin p0, axes u, v (full-length vectors). Normals (u x v) point INTO the room so tags are not mirrored.
FACES = {
    "floor":   (np.array([0, 0, 0.0]),   np.array([5.0, 0, 0]),  np.array([0, 0, 4.0])),
    "ceiling": (np.array([0, 2.6, 0.0]), np.array([5.0, 0, 0]),  np.array([0, 0, 4.0])),
    "wall_z0": (np.array([0, 0, 0.0]),   np.array([5.0, 0, 0]),  np.array([0, 2.6, 0])),
    "wall_z4": (np.array([5, 0, 4.0]),   np.array([-5.0, 0, 0]), np.array([0, 2.6, 0])),
    "wall_x0": (np.array([0, 0, 4.0]),   np.array([0, 0, -4.0]), np.array([0, 2.6, 0])),
    "wall_x5": (np.array([5, 0, 0.0]),   np.array([0, 0, 4.0]),  np.array([0, 2.6, 0])),
}
# tag id -> (face, distance along u in m, height along v in m)
TAGS = {1: ("wall_z0", 1.0, 1.5), 2: ("wall_z0", 4.0, 1.2), 3: ("wall_x5", 1.0, 1.4), 4: ("wall_x5", 3.0, 1.1),
        5: ("wall_z4", 1.2, 1.6), 6: ("wall_x0", 1.5, 1.3), 7: ("wall_x0", 3.2, 1.0), 8: ("wall_z4", 3.8, 1.2)}


def tag_corners_world(tag_id: int, size: float) -> np.ndarray:
    face, cu, cv_ = TAGS[tag_id]
    p0, u, v = FACES[face]
    uh, vh = u / np.linalg.norm(u), v / np.linalg.norm(v)
    c = p0 + cu * uh + cv_ * vh
    h = size / 2
    return np.array([c - h * uh + h * vh, c + h * uh + h * vh, c + h * uh - h * vh, c - h * uh - h * vh])


def _lookat(pos: np.ndarray, target: np.ndarray) -> np.ndarray:
    f = target - pos; f /= np.linalg.norm(f)
    right = np.cross(f, [0, 1.0, 0]); right /= np.linalg.norm(right)
    up = np.cross(right, f)
    return np.stack([right, up, -f], axis=1)  # OpenGL camera-to-world rotation


def _Ry(deg: float) -> np.ndarray:
    a = np.radians(deg)
    return np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])


def render(R: np.ndarray, pos: np.ndarray, textures: dict, markers: dict, size: float,
           W: int, H: int, fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    xs, ys = np.meshgrid(np.arange(W, dtype=np.float32) + 0.5, np.arange(H, dtype=np.float32) + 0.5)
    d_cam = np.stack([(xs - cx) / fx, -(ys - cy) / fy, -np.ones_like(xs)], axis=-1).reshape(-1, 3)
    d = d_cam @ R.T
    out = np.zeros((H * W, 3), np.float32)
    best = np.full(H * W, np.inf, np.float32)
    S = next(iter(textures.values())).shape[0]
    cell = size / 8.0
    for name, (p0, u, v) in FACES.items():
        n = np.cross(u, v); n /= np.linalg.norm(n)
        den = d @ n
        with np.errstate(divide="ignore", invalid="ignore"):
            t = ((p0 - pos) @ n) / den
        hit = pos + t[:, None] * d
        Lu, Lv = np.linalg.norm(u), np.linalg.norm(v)
        a = ((hit - p0) @ u) / Lu ** 2
        b = ((hit - p0) @ v) / Lv ** 2
        ok = (den < 0) & (t > 0.05) & (t < best) & (a >= 0) & (a <= 1) & (b >= 0) & (b <= 1)
        if not ok.any():
            continue
        mapx = np.where(ok, a * (S - 1), -1).astype(np.float32).reshape(H, W)
        mapy = np.where(ok, (1 - b) * (S - 1), -1).astype(np.float32).reshape(H, W)
        col = cv2.remap(textures[name], mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT).astype(np.float32).reshape(-1, 3)
        for tid, (face, cu, cv_) in TAGS.items():
            if face != name:
                continue
            ua, vb = a * Lu - cu, b * Lv - cv_
            inside = ok & (np.abs(ua) <= 5 * cell) & (np.abs(vb) <= 5 * cell)
            if not inside.any():
                continue
            Sm = markers[tid].shape[0]
            mx = np.where(inside, (ua / size + 0.5) * Sm, -1).astype(np.float32).reshape(H, W)
            my = np.where(inside, (0.5 - vb / size) * Sm, -1).astype(np.float32).reshape(H, W)
            m = cv2.remap(markers[tid], mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255).reshape(-1).astype(np.float32)
            col[inside] = m[inside, None]
        shade = np.clip(1.15 - 0.1 * t, 0.55, 1.1)
        out[ok] = col[ok] * shade[ok, None]
        best[ok] = t[ok]
    return np.clip(out, 0, 255).astype(np.uint8).reshape(H, W, 3)


def make_session(out_dir: str | Path, n_frames: int = 60, seed: int = 0, size=(1024, 768), tag_size: float = 0.20,
                 arcore_scale: float = 1.035, arcore_yaw_deg: float = 35.0, drift_deg: float = 1.5,
                 laser_sigma: float = 0.003) -> Path:
    out_dir = Path(out_dir)
    rng = np.random.default_rng(seed)
    W, H = size
    fx = fy = 0.78 * W
    cx, cy = W / 2, H / 2
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)

    textures = {k: _texture(rng) for k in FACES}
    dic = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    markers = {tid: cv2.aruco.generateImageMarker(dic, tid, 256) for tid in TAGS}

    poses_lines = []
    p_first = None
    pos_noise = np.zeros(3)
    for k in range(n_frames):
        th = 2 * np.pi * k / n_frames
        pos = np.array([2.5 + 1.4 * np.cos(th), 1.4 + 0.1 * np.sin(3 * th), 2.0 + 0.9 * np.sin(th)])
        phi = 1.5 * th + 0.5
        target = np.array([2.5 + 2.2 * np.cos(phi), 1.3 + 0.3 * np.sin(2 * th), 2.0 + 1.8 * np.sin(phi)])
        R = _lookat(pos, target)
        img = render(R, pos, textures, markers, tag_size, W, H, fx, fy, cx, cy)
        img = cv2.GaussianBlur(img, (0, 0), 0.8)
        if rng.random() < 0.08 and k not in (0, 1):  # motion-blurred frame
            kern = np.zeros((1, 17), np.float32); kern[0, :] = 1 / 17
            img = cv2.filter2D(img, -1, kern)
        img = np.clip(img.astype(np.float32) + rng.normal(0, 2.0, img.shape), 0, 255).astype(np.uint8)
        name = f"{k + 1:06d}.jpg"
        cv2.imwrite(str(out_dir / "frames" / name), img, [cv2.IMWRITE_JPEG_QUALITY, 92])

        # "ARCore" world: origin at first camera, arbitrary heading, scale error, yaw drift, jitter
        if p_first is None:
            p_first = pos.copy()
        pos_noise += rng.normal(0, 0.004, 3)
        yaw = arcore_yaw_deg + drift_deg * k / max(1, n_frames - 1)
        Ra = _Ry(yaw)
        t_a = arcore_scale * (Ra @ (pos - p_first)) + pos_noise
        R_a = Ra @ R
        q_wxyz = R_to_quat_wxyz(R_a)
        q = [q_wxyz[1], q_wxyz[2], q_wxyz[3], q_wxyz[0]]
        poses_lines.append(json.dumps({"frame": name, "t_ns": 33_000_000 * k,
                                       "tracking": "PAUSED" if k in (n_frames // 2, n_frames // 2 + 1) else "TRACKING",
                                       "t": [round(float(v), 5) for v in t_a], "q": [round(float(v), 6) for v in q]}))
    (out_dir / "poses.jsonl").write_text("\n".join(poses_lines) + "\n")
    (out_dir / "intrinsics.json").write_text(json.dumps({"width": W, "height": H, "fx": fx, "fy": fy, "cx": cx, "cy": cy}))
    (out_dir / "session.json").write_text(json.dumps({
        "format_version": 1, "session_id": out_dir.name, "room_name": "Synthetic room",
        "device": {"model": "synthetic"}, "image_width": W, "image_height": H,
        "tag_family": "tag36h11", "tag_size_m": tag_size}, indent=1))

    # laser distances between tag centres (true distance + noise); two held out as checks
    centres = {tid: tag_corners_world(tid, tag_size).mean(0) for tid in TAGS}
    pairs = [(1, 2, "fit"), (3, 4, "fit"), (6, 7, "fit"), (1, 5, "fit"), (2, 3, "fit"), (4, 8, "check"), (7, 5, "check")]
    dist = []
    for a, b, use in pairs:
        true = float(np.linalg.norm(centres[a] - centres[b]))
        dist.append({"a": {"tag": a, "point": "center"}, "b": {"tag": b, "point": "center"},
                     "meters": round(true + float(rng.normal(0, laser_sigma)), 3), "sigma_m": laser_sigma, "use": use})
    (out_dir / "measurements.json").write_text(json.dumps({"distances": dist}, indent=1))

    gt = {"tag_size_m": tag_size, "arcore_scale": arcore_scale, "room_m": ROOM.tolist(),
          "tag_corners_world": {str(t): tag_corners_world(t, tag_size).tolist() for t in TAGS}}
    (out_dir.parent / f"{out_dir.name}.gt.json").write_text(json.dumps(gt))
    return out_dir


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate a synthetic capture session for testing")
    ap.add_argument("out"); ap.add_argument("--frames", type=int, default=60); ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    print(make_session(a.out, a.frames, a.seed))
