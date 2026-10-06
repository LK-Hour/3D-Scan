"""Whole system on a synthetic room: upload over HTTP -> job queue -> COLMAP (CPU) -> metric model -> download.

Compares against ground truth that the pipeline never sees. Run with: python -m pytest --runslow
"""
import hashlib
import io
import itertools
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from scanpc import synth
from scanpc.server import create_app
from tests.conftest import write_tiny_session

pytestmark = pytest.mark.slow
TOKEN = "e" * 40
H = {"Authorization": f"Bearer {TOKEN}"}


def _manifest(root):
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and "work" not in p.relative_to(root).parts:
            d = p.read_bytes()
            out.append({"path": p.relative_to(root).as_posix(), "size": len(d), "sha256": hashlib.sha256(d).hexdigest()})
    return out


def _upload(c, root, sid):
    assert c.post("/v1/sessions", json={"session_id": sid, "files": _manifest(root)}, headers=H).status_code == 200
    for f in _manifest(root):
        r = c.put(f"/v1/sessions/{sid}/files/{f['path']}", content=(root / f["path"]).read_bytes(), headers={**H, "Upload-Offset": "0"})
        assert r.headers["Upload-Complete"] == "1", r.text


def _wait(c, job_id, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/v1/jobs/{job_id}", headers=H).json()
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(1.0)
    raise TimeoutError(job_id)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    base = tmp_path_factory.mktemp("e2e")
    room = synth.make_session(base / "room", n_frames=48, seed=0)
    gt = json.loads((base / "room.gt.json").read_text())
    app = create_app(base / "data", TOKEN, start_worker=True,
                     default_options={"dense": False, "use_gpu": False, "max_image_size": 1024, "max_features": 4096})
    with TestClient(app, client=("127.0.0.1", 5)) as c:
        _upload(c, room, "synthetic-room")
        job = c.post("/v1/sessions/synthetic-room/finish", json={}, headers=H).json()
        done = _wait(c, job["id"])
        yield {"client": c, "job": done, "gt": gt, "base": base, "app": app}
    app.state.jobs.stop()


def test_job_completes_with_high_grade(run):
    j = run["job"]
    assert j["state"] == "done", j.get("error")
    rep = j["report"]
    assert rep["frames"]["registered"] >= 40
    assert rep["sparse"]["mean_reprojection_px"] < 1.0
    assert rep["scale"]["source"] == "laser" and rep["accuracy"]["grade"] == "high"
    assert rep["frames"]["dropped_reasons"].get("tracking") == 2          # the two PAUSED frames


def test_metric_accuracy_against_ground_truth(run):
    """Independent of the pipeline's own report: inter-tag distances and the vertical axis vs the true room."""
    from pathlib import Path
    from scanpc.colmap_io import read_text_model
    from scanpc.tags import detect_frames, triangulate_tags
    root = run["app"].state.sessions_root / "synthetic-room"
    al = json.loads((root / "work" / "alignment.json").read_text())["sim3_model_to_world"]
    s, R, t = al["scale"], np.array(al["R"]), np.array(al["t"])
    model = read_text_model(root / "work" / "sparse_best")
    dets = detect_frames({im.name: root / "frames" / im.name for im in model.images.values()}, root / "work" / "tags_detected.json")
    cen = {k: s * (R @ tm.center) + t for k, tm in triangulate_tags(model, dets).items()}
    assert len(cen) >= 6
    gtc = {int(k): np.array(v).mean(0) for k, v in run["gt"]["tag_corners_world"].items()}
    err = np.array([1000 * (np.linalg.norm(cen[a] - cen[b]) - np.linalg.norm(gtc[a] - gtc[b])) for a, b in itertools.combinations(sorted(cen), 2)])
    dy = np.array([1000 * ((cen[a][1] - cen[b][1]) - (gtc[a][1] - gtc[b][1])) for a, b in itertools.combinations(sorted(cen), 2)])
    assert np.sqrt((err ** 2).mean()) < 15 and abs(err).max() < 30           # mm (observed ~4 / ~8)
    assert np.sqrt((dy ** 2).mean()) < 3                                    # gravity / up axis correct


def test_downloaded_outputs_are_metric_and_y_up(run):
    import trimesh
    c = run["client"]
    names = {x["name"] for x in c.get("/v1/sessions/synthetic-room/results", headers=H).json()}
    assert {"report.json", "report.md", "model.glb", "sparse.ply", "cameras.json"} <= names
    ply = trimesh.load(io.BytesIO(c.get("/v1/sessions/synthetic-room/result/sparse.ply", headers=H).content), file_type="ply")
    v = np.asarray(ply.vertices)
    lo, hi = np.percentile(v, 1, axis=0), np.percentile(v, 99, axis=0)
    assert 2.3 < hi[1] - lo[1] < 2.9                                        # room height 2.6 m along +Y (up)
    sub = v[np.random.default_rng(0).choice(len(v), min(1500, len(v)), replace=False)]
    horiz = np.linalg.norm(sub[:, None, [0, 2]] - sub[None, :, [0, 2]], axis=-1).max()
    assert 5.6 < horiz < 6.6                                                # room diagonal sqrt(5^2+4^2)=6.4 m
    glb = trimesh.load(io.BytesIO(c.get("/v1/sessions/synthetic-room/result/model.glb", headers=H).content), file_type="glb")
    assert len(glb.geometry) >= 1
    cams = c.get("/v1/sessions/synthetic-room/result/cameras.json", headers=H).json()["cameras"]
    assert len(cams) == run["job"]["report"]["frames"]["registered"]


def test_unreconstructable_session_fails_with_a_readable_error(run, tmp_path):
    c = run["client"]
    bad = write_tiny_session(tmp_path / "noise-session", n=14)
    _upload(c, bad, "noise-session")
    job = c.post("/v1/sessions/noise-session/finish", json={}, headers=H).json()
    j = _wait(c, job["id"], timeout=300)
    assert j["state"] == "failed" and j["message"] and (run["app"].state.sessions_root / "noise-session" / "work" / "error.txt").exists()
