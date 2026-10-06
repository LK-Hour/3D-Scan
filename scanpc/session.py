"""Capture-session format shared by the Android app and the PC pipeline (see docs/SESSION_FORMAT.md)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FORMAT_VERSION = 1
FRAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.(jpg|jpeg|png)$", re.IGNORECASE)
TRACKING_OK = "TRACKING"


class SessionError(ValueError):
    """The session folder is missing something or contains inconsistent data."""


@dataclass
class Intrinsics:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    def K(self) -> np.ndarray:
        return np.array([[self.fx, 0, self.cx], [0, self.fy, self.cy], [0, 0, 1.0]])

    @classmethod
    def from_dict(cls, d: dict) -> "Intrinsics":
        try:
            return cls(int(d["width"]), int(d["height"]), float(d["fx"]), float(d["fy"]),
                       float(d["cx"]), float(d["cy"]))
        except (KeyError, TypeError, ValueError) as e:
            raise SessionError(f"intrinsics.json is invalid: {e}") from e


@dataclass
class PoseRecord:
    frame: str
    t_ns: int
    tracking: str
    t: np.ndarray  # camera position in ARCore world (3,)
    q: np.ndarray  # quaternion x, y, z, w (4,), unit length


@dataclass
class Measurement:
    a_tag: int
    a_point: str
    b_tag: int
    b_point: str
    meters: float
    sigma_m: float = 0.003
    use: str = "fit"  # "fit" or "check"


@dataclass
class Session:
    root: Path
    meta: dict
    intrinsics: Intrinsics
    poses: list[PoseRecord]
    measurements: list[Measurement] = field(default_factory=list)

    @property
    def frames_dir(self) -> Path:
        return self.root / "frames"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    def frame_path(self, name: str) -> Path:
        return self.frames_dir / name

    @property
    def session_id(self) -> str:
        return str(self.meta.get("session_id", self.root.name))

    def tag_size(self, tag_id: int) -> float:
        sizes = self.meta.get("tag_sizes_m") or {}
        if str(tag_id) in sizes:
            return float(sizes[str(tag_id)])
        if "tag_size_m" in self.meta:
            return float(self.meta["tag_size_m"])
        raise SessionError("session.json has no tag_size_m, cannot use tags for scale")

    @property
    def has_tag_size(self) -> bool:
        return "tag_size_m" in self.meta or bool(self.meta.get("tag_sizes_m"))


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise SessionError(f"missing required file: {path.name}") from e
    except json.JSONDecodeError as e:
        raise SessionError(f"{path.name} is not valid JSON: {e}") from e


def _read_poses(path: Path) -> list[PoseRecord]:
    if not path.exists():
        raise SessionError("missing required file: poses.jsonl")
    poses: list[PoseRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                t = np.asarray(d["t"], dtype=float)
                q = np.asarray(d["q"], dtype=float)
                if t.shape != (3,) or q.shape != (4,):
                    raise ValueError("t must have 3 and q 4 numbers")
                norm = np.linalg.norm(q)
                if not 0.9 < norm < 1.1:
                    raise ValueError(f"quaternion norm {norm:.3f} is not ~1")
                poses.append(PoseRecord(str(d["frame"]), int(d.get("t_ns", n)),
                                        str(d.get("tracking", TRACKING_OK)), t, q / norm))
            except (KeyError, ValueError, TypeError) as e:
                raise SessionError(f"poses.jsonl line {n}: {e}") from e
    if not poses:
        raise SessionError("poses.jsonl is empty")
    poses.sort(key=lambda p: p.t_ns)
    return poses


def _read_measurements(path: Path) -> list[Measurement]:
    if not path.exists():
        return []
    d = _read_json(path)
    out: list[Measurement] = []
    for i, m in enumerate(d.get("distances", [])):
        try:
            out.append(Measurement(
                int(m["a"]["tag"]), str(m["a"].get("point", "center")),
                int(m["b"]["tag"]), str(m["b"].get("point", "center")),
                float(m["meters"]), float(m.get("sigma_m", 0.003)), str(m.get("use", "fit"))))
        except (KeyError, TypeError, ValueError) as e:
            raise SessionError(f"measurements.json entry {i}: {e}") from e
        for p in (out[-1].a_point, out[-1].b_point):
            if p != "center" and not re.fullmatch(r"corner[0-3]", p):
                raise SessionError(f"measurements.json entry {i}: bad point '{p}'")
        if out[-1].meters <= 0 or out[-1].use not in ("fit", "check"):
            raise SessionError(f"measurements.json entry {i}: bad meters/use")
    return out


def load_session(root: str | Path) -> Session:
    root = Path(root)
    if not root.is_dir():
        raise SessionError(f"not a folder: {root}")
    meta = _read_json(root / "session.json")
    if int(meta.get("format_version", 0)) != FORMAT_VERSION:
        raise SessionError(f"unsupported format_version {meta.get('format_version')!r}, expected {FORMAT_VERSION}")
    intr = Intrinsics.from_dict(_read_json(root / "intrinsics.json"))
    poses = _read_poses(root / "poses.jsonl")
    meas = _read_measurements(root / "measurements.json")
    return Session(root, meta, intr, poses, meas)


def validate_session(root: str | Path, check_images: int = 5) -> ValidationReport:
    """Cheap structural checks. Returns a report instead of raising so the server can show all problems."""
    rep = ValidationReport()
    try:
        s = load_session(root)
    except SessionError as e:
        rep.errors.append(str(e))
        return rep

    names_seen: set[str] = set()
    missing = []
    for p in s.poses:
        if not FRAME_RE.match(p.frame):
            rep.errors.append(f"unsafe or odd frame name in poses.jsonl: {p.frame!r}")
            continue
        if p.frame in names_seen:
            rep.errors.append(f"duplicate pose for frame {p.frame}")
        names_seen.add(p.frame)
        if not s.frame_path(p.frame).is_file():
            missing.append(p.frame)
    if missing:
        rep.errors.append(f"{len(missing)} frames listed in poses.jsonl are missing, e.g. {missing[:3]}")

    tracked = sum(1 for p in s.poses if p.tracking == TRACKING_OK)
    if tracked < 10:
        rep.errors.append(f"only {tracked} frames have TRACKING state, need at least 10")
    elif tracked < 0.6 * len(s.poses):
        rep.warnings.append(f"only {tracked}/{len(s.poses)} frames have good tracking; scan slower / with more light")

    try:  # image size vs intrinsics, on a few frames
        from PIL import Image
        existing = [p.frame for p in s.poses if s.frame_path(p.frame).is_file()]
        step = max(1, len(existing) // max(1, check_images))
        for name in existing[::step][:check_images]:
            with Image.open(s.frame_path(name)) as im:
                if im.size != (s.intrinsics.width, s.intrinsics.height):
                    rep.errors.append(
                        f"{name} is {im.size[0]}x{im.size[1]} but intrinsics.json says "
                        f"{s.intrinsics.width}x{s.intrinsics.height}")
                    break
    except Exception as e:  # unreadable image
        rep.errors.append(f"could not read a frame: {e}")

    if s.measurements and not s.has_tag_size:
        rep.warnings.append("measurements.json present but session.json has no tag_size_m")
    if not s.has_tag_size:
        rep.warnings.append("no tag_size_m: absolute scale will come from ARCore only (a few % error)")
    elif not s.measurements:
        rep.warnings.append("no laser measurements: scale comes from tag size only; add measurements.json for best accuracy")
    return rep
