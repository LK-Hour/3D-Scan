import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def write_tiny_session(root: Path, n: int = 12, size=(64, 48), tracking="TRACKING") -> Path:
    """A structurally valid session with noise images (not reconstructable; for validation/server tests)."""
    rng = np.random.default_rng(0)
    (root / "frames").mkdir(parents=True, exist_ok=True)
    lines = []
    for k in range(n):
        name = f"{k + 1:06d}.jpg"
        cv2.imwrite(str(root / "frames" / name), rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8))
        lines.append(json.dumps({"frame": name, "t_ns": k * 1000, "tracking": tracking,
                                 "t": [0.1 * k, 1.4, 0.0], "q": [0, 0, 0, 1]}))
    (root / "poses.jsonl").write_text("\n".join(lines) + "\n")
    (root / "intrinsics.json").write_text(json.dumps({"width": size[0], "height": size[1], "fx": 50, "fy": 50, "cx": size[0] / 2, "cy": size[1] / 2}))
    (root / "session.json").write_text(json.dumps({"format_version": 1, "session_id": root.name, "tag_size_m": 0.16}))
    return root


@pytest.fixture
def tiny_session(tmp_path):
    return write_tiny_session(tmp_path / "tiny-session")


def pytest_addoption(parser):
    parser.addoption("--runslow", action="store_true", help="run slow end-to-end tests (COLMAP on CPU, ~3 min)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runslow"):
        return
    skip = pytest.mark.skip(reason="slow; use --runslow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
