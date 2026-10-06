import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from scanpc.server import create_app, safe_rel_path, sanitize_options
from tests.conftest import write_tiny_session

TOKEN = "t" * 40
H = {"Authorization": f"Bearer {TOKEN}"}


def manifest_for(root):
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and "work" not in p.relative_to(root).parts:
            data = p.read_bytes()
            files.append({"path": p.relative_to(root).as_posix(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return files


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", TOKEN, start_worker=False, pair_host="192.168.1.50")
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def upload_all(client, root, sid, skip=()):
    for f in manifest_for(root):
        if f["path"] in skip:
            continue
        r = client.put(f"/v1/sessions/{sid}/files/{f['path']}", content=(root / f["path"]).read_bytes(),
                       headers={**H, "Upload-Offset": "0"})
        assert r.status_code == 200 and r.headers["Upload-Complete"] == "1", r.text


def test_health_open_everything_else_needs_token(client):
    assert client.get("/v1/health").json()["protocol"] == 1
    assert client.get("/v1/sessions").status_code == 401
    assert client.get("/v1/sessions", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/sessions", headers=H).status_code == 200


def test_path_whitelist():
    for ok in ["session.json", "poses.jsonl", "frames/000001.jpg", "frames/a_b-1.PNG"]:
        assert safe_rel_path(ok) == ok
    for bad in ["../x", "frames/../../x", "/etc/passwd", "frames/x.exe", "frames/CON.jpg", "work/state.json",
                "frames/sub/000001.jpg", "frames/..jpg", "a\\b", "frames/%2e%2e", ""]:
        with pytest.raises(ValueError):
            safe_rel_path(bad)


def test_option_whitelist_blocks_executables():
    assert sanitize_options({"dense": False, "max_image_size": 1200})["max_image_size"] == 1200
    for bad in [{"colmap_exe": "calc.exe"}, {"backend": "cli"}, {"max_image_size": 99999}, {"dense": "yes"},
                {"output_up": "x"}, {"max_image_size": True}]:
        with pytest.raises(ValueError):
            sanitize_options(bad)


def test_full_upload_and_finish_queues_a_job(client, tmp_path):
    root = write_tiny_session(tmp_path / "src-session")
    sid = "room-1"
    r = client.post("/v1/sessions", json={"session_id": sid, "files": manifest_for(root)}, headers=H)
    assert r.status_code == 200 and r.json()["state"] == "uploading"
    upload_all(client, root, sid)
    st = client.get(f"/v1/sessions/{sid}", headers=H).json()
    assert st["files_complete"] == st["files_total"] and st["state"] == "ready"
    r = client.post(f"/v1/sessions/{sid}/finish", json={"options": {"dense": False}}, headers=H)
    assert r.status_code == 200 and r.json()["state"] == "queued"
    again = client.post(f"/v1/sessions/{sid}/finish", json={}, headers=H).json()
    assert again["id"] == r.json()["id"]                       # idempotent while queued
    assert client.get(f"/v1/jobs/{r.json()['id']}", headers=H).json()["options"] == {"dense": False}
    assert client.delete(f"/v1/sessions/{sid}", headers=H).status_code == 409    # busy
    assert client.post(f"/v1/jobs/{r.json()['id']}/cancel", headers=H).status_code == 200


def test_resumable_upload_offsets_and_checksum(client, tmp_path):
    root = write_tiny_session(tmp_path / "s")
    sid = "resume-test"
    client.post("/v1/sessions", json={"session_id": sid, "files": manifest_for(root)}, headers=H)
    path = "poses.jsonl"
    data = (root / path).read_bytes()
    url = f"/v1/sessions/{sid}/files/{path}"
    cut = len(data) // 2
    r = client.put(url, content=data[:cut], headers={**H, "Upload-Offset": "0"})
    assert r.headers["Upload-Complete"] == "0" and r.headers["Upload-Offset"] == str(cut)
    assert client.head(url, headers=H).headers["Upload-Offset"] == str(cut)            # app asks where to resume
    r = client.put(url, content=data[cut:], headers={**H, "Upload-Offset": "3"})        # wrong offset
    assert r.status_code == 409 and r.headers["Upload-Offset"] == str(cut)
    r = client.put(url, content=data[cut:], headers={**H, "Upload-Offset": str(cut)})
    assert r.status_code == 200 and r.headers["Upload-Complete"] == "1"
    # corrupted content is rejected and the partial file discarded
    url2 = f"/v1/sessions/{sid}/files/intrinsics.json"
    bad = (root / "intrinsics.json").read_bytes()[:-1] + b"X"
    r = client.put(url2, content=bad, headers={**H, "Upload-Offset": "0"})
    assert r.status_code == 422 and r.headers["Upload-Offset"] == "0"
    # more data than declared is refused
    r = client.put(url2, content=bad + b"extra", headers={**H, "Upload-Offset": "0"})
    assert r.status_code == 413


def test_finish_requires_complete_upload(client, tmp_path):
    root = write_tiny_session(tmp_path / "s2")
    sid = "partial-1"
    client.post("/v1/sessions", json={"session_id": sid, "files": manifest_for(root)}, headers=H)
    upload_all(client, root, sid, skip=("frames/000003.jpg",))
    r = client.post(f"/v1/sessions/{sid}/finish", json={}, headers=H)
    assert r.status_code == 409 and "frames/000003.jpg" in r.json()["detail"]["missing"]


def test_manifest_rejects_traversal_and_bad_ids(client, tmp_path):
    root = write_tiny_session(tmp_path / "s3")
    files = manifest_for(root)
    evil = files + [{"path": "../../evil.py", "size": 1, "sha256": "0" * 64}]
    assert client.post("/v1/sessions", json={"session_id": "ok-id", "files": evil}, headers=H).status_code == 400
    for sid in ["..", ".hidden", "a/b", "x"]:
        assert client.post("/v1/sessions", json={"session_id": sid, "files": files}, headers=H).status_code in (400, 404, 405)
    # the HTTP client may collapse '../' before sending (-> 405 no route); encoded variants reach the server (-> 400/404)
    for url in ["/v1/sessions/ok-id/files/../../x", "/v1/sessions/ok-id/files/%2e%2e/%2e%2e/x",
                "/v1/sessions/ok-id/files/frames%2f..%2f..%2fx"]:
        r = client.put(url, content=b"1", headers={**H, "Upload-Offset": "0"})
        assert r.status_code in (400, 404, 405), (url, r.status_code)
    assert not (client.app.state.sessions_root.parent / "x").exists()


def test_invalid_session_is_rejected_at_finish(client, tmp_path):
    root = write_tiny_session(tmp_path / "s4")
    (root / "session.json").write_text(json.dumps({"format_version": 7}))
    sid = "bad-format"
    client.post("/v1/sessions", json={"session_id": sid, "files": manifest_for(root)}, headers=H)
    upload_all(client, root, sid)
    r = client.post(f"/v1/sessions/{sid}/finish", json={}, headers=H)
    assert r.status_code == 422 and "format_version" in json.dumps(r.json())


def test_results_only_whitelisted_names(client, tmp_path):
    root = write_tiny_session(tmp_path / "s5")
    sid = "results-1"
    client.post("/v1/sessions", json={"session_id": sid, "files": manifest_for(root)}, headers=H)
    res = client.app.state.sessions_root / sid / "work" / "result"
    res.mkdir(parents=True)
    (res / "report.json").write_text("{}")
    (res / "secret.txt").write_text("nope")
    assert [x["name"] for x in client.get(f"/v1/sessions/{sid}/results", headers=H).json()] == ["report.json"]
    assert client.get(f"/v1/sessions/{sid}/result/report.json", headers=H).status_code == 200
    assert client.get(f"/v1/sessions/{sid}/result/secret.txt", headers=H).status_code == 400
    assert client.get(f"/v1/sessions/{sid}/result/..%2Fmanifest.json", headers=H).status_code in (400, 404)
    assert client.get(f"/v1/sessions/{sid}/result/report.json", headers={"Range": "bytes=0-0", **H}).status_code in (200, 206)


def test_pair_page_is_localhost_only(tmp_path):
    app = create_app(tmp_path / "d", TOKEN, start_worker=False, pair_host="192.168.1.50")
    with TestClient(app, client=("192.168.1.77", 1234)) as remote:
        assert remote.get("/pair").status_code == 403 and remote.get("/pair.png").status_code == 403
    with TestClient(app, client=("127.0.0.1", 1234)) as local:
        r = local.get("/pair.png")
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
