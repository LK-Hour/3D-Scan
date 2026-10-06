"""Frame selection: drop frames with bad tracking, motion blur and near-duplicates."""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .poses import quat_xyzw_to_R
from .session import Session, TRACKING_OK


@dataclass
class FilterConfig:
    blur_rel: float = 0.5        # drop frame if sharpness < blur_rel * best sharpness among neighbours
    blur_window: int = 4         # neighbours on each side (in tracked-frame order)
    blur_abs_min: float = 3.0    # drop frames that are essentially featureless/black
    min_translation_m: float = 0.05
    min_rotation_deg: float = 4.0
    max_frames: int = 0          # 0 = no cap


@dataclass
class FilterResult:
    kept: list[str]
    dropped: dict[str, str] = field(default_factory=dict)
    sharpness: dict[str, float] = field(default_factory=dict)

    def stats(self) -> dict:
        reasons: dict[str, int] = {}
        for r in self.dropped.values():
            reasons[r] = reasons.get(r, 0) + 1
        return {"kept": len(self.kept), "dropped": len(self.dropped), "reasons": reasons}


def sharpness(path, width: int = 640) -> float:
    """Variance of the Laplacian on a fixed-width grey image (comparable across resolutions)."""
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0
    h, w = img.shape
    if w > width:
        img = cv2.resize(img, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def _rot_angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    c = (np.trace(Ra.T @ Rb) - 1) / 2
    return float(np.degrees(np.arccos(np.clip(c, -1, 1))))


def select_keyframes(session: Session, cfg: FilterConfig | None = None) -> FilterResult:
    cfg = cfg or FilterConfig()
    res = FilterResult(kept=[])

    tracked = []
    for p in session.poses:
        if p.tracking != TRACKING_OK:
            res.dropped[p.frame] = "tracking"
        elif not session.frame_path(p.frame).is_file():
            res.dropped[p.frame] = "missing"
        else:
            tracked.append(p)

    for p in tracked:
        res.sharpness[p.frame] = sharpness(session.frame_path(p.frame))

    # 1) blur: keep a frame unless a neighbour is clearly sharper
    sharp_ok = []
    n = len(tracked)
    for i, p in enumerate(tracked):
        lo, hi = max(0, i - cfg.blur_window), min(n, i + cfg.blur_window + 1)
        best = max(res.sharpness[q.frame] for q in tracked[lo:hi])
        s = res.sharpness[p.frame]
        if s < cfg.blur_abs_min:
            res.dropped[p.frame] = "featureless"
        elif s < cfg.blur_rel * best:
            res.dropped[p.frame] = "blur"
        else:
            sharp_ok.append(p)

    # 2) motion-based decimation: keep a frame only if the camera moved or turned enough
    last = None
    for p in sharp_ok:
        R = quat_xyzw_to_R(p.q)
        if last is not None:
            moved = float(np.linalg.norm(p.t - last[0]))
            turned = _rot_angle_deg(last[1], R)
            if moved < cfg.min_translation_m and turned < cfg.min_rotation_deg:
                res.dropped[p.frame] = "duplicate"
                continue
        res.kept.append(p.frame)
        last = (p.t, R)

    # 3) optional cap: thin evenly
    if cfg.max_frames and len(res.kept) > cfg.max_frames:
        idx = np.linspace(0, len(res.kept) - 1, cfg.max_frames).round().astype(int)
        keep = {res.kept[i] for i in idx}
        for f in res.kept:
            if f not in keep:
                res.dropped[f] = "cap"
        res.kept = [f for f in res.kept if f in keep]
    return res
