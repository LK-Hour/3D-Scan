import json

import cv2
import numpy as np
import pytest

from scanpc.filtering import FilterConfig, select_keyframes, sharpness
from scanpc.session import SessionError, load_session, validate_session
from tests.conftest import write_tiny_session


def test_valid_session_loads_and_validates(tiny_session):
    s = load_session(tiny_session)
    assert len(s.poses) == 12 and s.intrinsics.fx == 50
    rep = validate_session(tiny_session)
    assert rep.ok, rep.errors
    assert any("no laser measurements" in w for w in rep.warnings)


def test_missing_required_file(tiny_session):
    (tiny_session / "intrinsics.json").unlink()
    assert "intrinsics.json" in validate_session(tiny_session).errors[0]


def test_wrong_format_version(tiny_session):
    meta = json.loads((tiny_session / "session.json").read_text()); meta["format_version"] = 2
    (tiny_session / "session.json").write_text(json.dumps(meta))
    with pytest.raises(SessionError):
        load_session(tiny_session)


def test_bad_quaternion_and_unsafe_frame_names(tiny_session):
    lines = (tiny_session / "poses.jsonl").read_text().splitlines()
    d = json.loads(lines[0]); d["q"] = [0, 0, 0, 0]
    (tiny_session / "poses.jsonl").write_text(json.dumps(d) + "\n")
    assert not validate_session(tiny_session).ok
    d = json.loads(lines[1]); d["frame"] = "../../etc/passwd"
    (tiny_session / "poses.jsonl").write_text("\n".join(lines[2:] + [json.dumps(d)]) + "\n")
    assert any("unsafe" in e for e in validate_session(tiny_session).errors)


def test_intrinsics_must_match_image_size(tiny_session):
    (tiny_session / "intrinsics.json").write_text(json.dumps({"width": 640, "height": 480, "fx": 1, "fy": 1, "cx": 1, "cy": 1}))
    assert any("intrinsics.json says" in e for e in validate_session(tiny_session).errors)


def test_too_few_tracking_frames(tmp_path):
    s = write_tiny_session(tmp_path / "lost", tracking="PAUSED")
    assert any("TRACKING" in e for e in validate_session(s).errors)


def test_measurements_validation(tiny_session):
    (tiny_session / "measurements.json").write_text(json.dumps({"distances": [
        {"a": {"tag": 1, "point": "center"}, "b": {"tag": 2, "point": "corner9"}, "meters": 3}]}))
    with pytest.raises(SessionError):
        load_session(tiny_session)


def _textured(rng, n=240):
    return cv2.resize(rng.integers(0, 255, (n // 8, n // 8), dtype=np.uint8), (n, n), interpolation=cv2.INTER_CUBIC)


def test_filter_drops_blur_untracked_and_duplicates(tmp_path):
    rng = np.random.default_rng(0)
    root = tmp_path / "f"; (root / "frames").mkdir(parents=True)
    base = _textured(rng)
    lines = []
    plan = []  # (tracking, blurred, x position)
    for k in range(12):
        plan.append(("TRACKING", k == 4, 0.2 * k if k != 6 else 0.2 * 5))   # frame 6 stays where frame 5 was (duplicate)
    plan[8] = ("PAUSED", False, 1.6)
    for k, (tr, blur, x) in enumerate(plan):
        img = cv2.GaussianBlur(base, (0, 0), 6) if blur else base
        name = f"{k + 1:06d}.jpg"
        cv2.imwrite(str(root / "frames" / name), img)
        lines.append(json.dumps({"frame": name, "t_ns": k, "tracking": tr, "t": [x, 1, 0], "q": [0, 0, 0, 1]}))
    (root / "poses.jsonl").write_text("\n".join(lines) + "\n")
    (root / "intrinsics.json").write_text(json.dumps({"width": 240, "height": 240, "fx": 200, "fy": 200, "cx": 120, "cy": 120}))
    (root / "session.json").write_text(json.dumps({"format_version": 1}))
    res = select_keyframes(load_session(root), FilterConfig(min_translation_m=0.05))
    assert res.dropped["000009.jpg"] == "tracking"
    assert res.dropped["000005.jpg"] == "blur"
    assert res.dropped["000007.jpg"] == "duplicate"
    assert sharpness(root / "frames" / "000001.jpg") > sharpness(root / "frames" / "000005.jpg")
