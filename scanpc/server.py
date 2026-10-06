"""Upload server for the Android app: token auth, resumable chunked uploads with checksums, job status, result download.

Local-network use only (plain HTTP + bearer token from the pairing QR). Do not expose the port to the internet.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import shutil
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .jobs import JobStore
from .pairing import lan_addresses, pairing_payload, qr_png
from .pipeline import PipelineConfig
from .session import FRAME_RE, validate_session

PROTOCOL = 1
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\.]{2,99}$")
ROOT_FILES = {"session.json", "intrinsics.json", "poses.jsonl", "measurements.json", "imu.csv"}
RESULT_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.(ply|glb|json|md|obj|stl)$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_FILE_BYTES = 4 * 1024 ** 3
SAFE_OPTIONS = {  # what a remote client may change; deliberately excludes backend/colmap_exe
    "dense": (bool,), "use_gpu": (bool,), "max_image_size": (int, 640, 3200), "max_features": (int, 1000, 20000),
    "camera_model": (str, {"OPENCV", "PINHOLE", "SIMPLE_RADIAL", "RADIAL", "SIMPLE_PINHOLE"}),
    "output_up": (str, {"y", "z"}), "max_frames": (int, 0, 5000), "poisson_depth": (int, 8, 13),
    "min_translation_m": (float, 0.0, 1.0), "min_rotation_deg": (float, 0.0, 45.0), "blur_rel": (float, 0.0, 1.0),
    "pair_radius_m": (float, 0.5, 20.0), "pair_max_angle_deg": (float, 10.0, 180.0),
    "pair_max_neighbors": (int, 5, 200), "pair_sequential": (int, 0, 50),
}


def safe_rel_path(rel: str) -> str:
    """Whitelist of paths a client may write inside a session. Raises ValueError for anything else."""
    parts = rel.split("/")
    if len(parts) == 1 and parts[0] in ROOT_FILES:
        return rel
    if len(parts) == 2 and parts[0] == "frames" and FRAME_RE.match(parts[1]) and parts[1].split(".")[0].upper() not in RESERVED:
        return rel
    raise ValueError(f"path not allowed: {rel!r}")


def sanitize_options(opts: dict | None) -> dict:
    out: dict = {}
    for k, v in (opts or {}).items():
        spec = SAFE_OPTIONS.get(k)
        if spec is None:
            raise ValueError(f"option not allowed: {k}")
        typ = spec[0]
        if typ is bool:
            if not isinstance(v, bool):
                raise ValueError(f"option {k} must be true/false")
        elif typ is str:
            if v not in spec[1]:
                raise ValueError(f"option {k} must be one of {sorted(spec[1])}")
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"option {k} must be a number")
            v = typ(v)
            if not spec[1] <= v <= spec[2]:
                raise ValueError(f"option {k} must be between {spec[1]} and {spec[2]}")
        out[k] = v
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_app(data_dir: str | Path, token: str, *, name: str = "scanpc", start_worker: bool = True,
               default_options: dict | None = None, pair_host: str | None = None, pair_port: int = 8765) -> FastAPI:
    data = Path(data_dir)
    sessions_root = data / "sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)
    jobs = JobStore(data, sessions_root)
    if start_worker:
        jobs.start()
    defaults = sanitize_options(default_options)
    locks: dict[tuple[str, str], asyncio.Lock] = {}

    app = FastAPI(title="scanpc", version=__version__)
    app.state.jobs = jobs
    app.state.sessions_root = sessions_root

    def auth(request: Request) -> None:
        h = request.headers.get("authorization", "")
        if not h.startswith("Bearer ") or not secrets.compare_digest(h[7:].strip(), token):
            raise HTTPException(401, "missing or wrong token; re-pair by scanning the QR code on the PC")

    # -- helpers ----------------------------------------------------------------------------------------------
    def sdir(session_id: str) -> Path:
        if not SESSION_ID_RE.match(session_id):
            raise HTTPException(400, "bad session id (use 3-100 letters, digits, - _ .)")
        d = sessions_root / session_id
        if d.resolve().parent != sessions_root.resolve():
            raise HTTPException(400, "bad session id")
        return d

    def manifest(session_id: str) -> dict:
        f = sdir(session_id) / "manifest.json"
        if not f.exists():
            raise HTTPException(404, "unknown session; POST /v1/sessions first")
        return json.loads(f.read_text())

    def file_state(d: Path, rel: str, meta: dict) -> dict:
        final = d / rel
        part = d / (rel + ".part")
        if final.exists() and final.stat().st_size == meta["size"]:
            return {"received": meta["size"], "complete": True}
        return {"received": part.stat().st_size if part.exists() else 0, "complete": False}

    def session_status(session_id: str) -> dict:
        d, m = sdir(session_id), manifest(session_id)
        st = {rel: file_state(d, rel, meta) for rel, meta in m["files"].items()}
        done = sum(1 for s in st.values() if s["complete"])
        job = next((j for j in reversed(jobs.list()) if j["session_id"] == session_id), None)
        state = "uploading" if done < len(st) else "ready"
        if job:
            state = {"queued": "processing", "running": "processing", "done": "done", "failed": "failed",
                     "cancelled": "ready"}[job["state"]] if done == len(st) else state
        return {"session_id": session_id, "state": state, "files_complete": done, "files_total": len(st),
                "bytes_received": sum(s["received"] for s in st.values()),
                "bytes_total": sum(meta["size"] for meta in m["files"].values()),
                "job_id": job["id"] if job else None, "files": st}

    # -- open endpoints ---------------------------------------------------------------------------------------
    @app.get("/v1/health")
    def health():
        return {"name": name, "version": __version__, "protocol": PROTOCOL}

    def _local_only(request: Request) -> None:
        if (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(403, "the pairing page is only available on the PC itself")

    def _payload() -> dict:
        host = pair_host or lan_addresses()[0]
        return pairing_payload(host, pair_port, token, name)

    @app.get("/pair.png")
    def pair_png(request: Request):
        _local_only(request)
        return Response(qr_png(_payload()), media_type="image/png")

    @app.get("/pair", response_class=HTMLResponse)
    def pair_page(request: Request):
        _local_only(request)
        p = _payload()
        others = ", ".join(a for a in lan_addresses() if a != p["host"]) or "none"
        return (f"<html><body style='font-family:sans-serif;text-align:center'><h2>Scan with the ScanPC app</h2>"
                f"<img src='/pair.png' width=320><p>{p['host']}:{p['port']}</p><p>Other addresses: {others}</p></body></html>")

    # -- sessions ---------------------------------------------------------------------------------------------
    @app.post("/v1/sessions", dependencies=[Depends(auth)])
    async def create_session(body: dict):
        sid = str(body.get("session_id", ""))
        d = sdir(sid)
        files = body.get("files")
        if not isinstance(files, list) or not files:
            raise HTTPException(400, "files: [{path,size,sha256}] required")
        new: dict[str, dict] = {}
        for f in files:
            try:
                rel = safe_rel_path(str(f["path"]))
                size, sha = int(f["size"]), str(f["sha256"]).lower()
            except (KeyError, ValueError, TypeError) as e:
                raise HTTPException(400, f"bad manifest entry {f!r}: {e}") from e
            if not 0 <= size <= MAX_FILE_BYTES or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise HTTPException(400, f"bad size/sha256 for {rel}")
            new[rel] = {"size": size, "sha256": sha}
        for req in ("session.json", "intrinsics.json", "poses.jsonl"):
            if req not in new:
                raise HTTPException(400, f"manifest must include {req}")
        total = sum(v["size"] for v in new.values())
        free = shutil.disk_usage(sessions_root).free
        if free < total * 1.3 + 2 * 1024 ** 3:
            raise HTTPException(507, f"not enough disk space on the PC (need ~{total * 1.3 / 1e9:.1f} GB, free {free / 1e9:.1f} GB)")
        d.mkdir(parents=True, exist_ok=True)
        old = json.loads((d / "manifest.json").read_text())["files"] if (d / "manifest.json").exists() else {}
        for rel, meta in old.items():  # re-posting a manifest: drop files whose content changed
            if rel not in new or new[rel] != meta:
                for p in (d / rel, d / (rel + ".part")):
                    p.unlink(missing_ok=True)
        (d / "manifest.json").write_text(json.dumps({"files": new, "created": time.time()}))
        return session_status(sid)

    @app.get("/v1/sessions", dependencies=[Depends(auth)])
    def list_sessions():
        out = []
        for d in sorted(p for p in sessions_root.iterdir() if p.is_dir() and (p / "manifest.json").exists()):
            s = session_status(d.name)
            out.append({k: s[k] for k in ("session_id", "state", "files_complete", "files_total", "job_id")})
        return out

    @app.get("/v1/sessions/{sid}", dependencies=[Depends(auth)])
    def get_session(sid: str, files: bool = False):
        s = session_status(sid)
        if not files:
            s.pop("files")
        return s

    @app.delete("/v1/sessions/{sid}", dependencies=[Depends(auth)])
    def delete_session(sid: str):
        d = sdir(sid)
        manifest(sid)
        if any(j["session_id"] == sid and j["state"] in ("queued", "running") for j in jobs.list()):
            raise HTTPException(409, "a job for this session is queued or running; cancel it first")
        shutil.rmtree(d)
        return {"deleted": sid}

    @app.head("/v1/sessions/{sid}/files/{rel:path}", dependencies=[Depends(auth)])
    def file_head(sid: str, rel: str):
        try:
            rel = safe_rel_path(rel)
        except ValueError as e:
            raise HTTPException(400, str(e))
        m = manifest(sid)
        if rel not in m["files"]:
            raise HTTPException(404, "file not in manifest")
        st = file_state(sdir(sid), rel, m["files"][rel])
        return Response(headers={"Upload-Offset": str(st["received"]), "Upload-Complete": "1" if st["complete"] else "0"})

    @app.put("/v1/sessions/{sid}/files/{rel:path}", dependencies=[Depends(auth)])
    async def file_put(sid: str, rel: str, request: Request):
        try:
            rel = safe_rel_path(rel)
        except ValueError as e:
            raise HTTPException(400, str(e))
        m = manifest(sid)
        meta = m["files"].get(rel)
        if meta is None:
            raise HTTPException(404, "file not in manifest")
        try:
            offset = int(request.headers.get("upload-offset", ""))
        except ValueError:
            raise HTTPException(400, "Upload-Offset header (bytes already stored) is required")
        d = sdir(sid)
        final, part = d / rel, d / (rel + ".part")
        lock = locks.setdefault((sid, rel), asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "another upload of this file is in progress")
        async with lock:
            if file_state(d, rel, meta)["complete"]:
                return Response(headers={"Upload-Offset": str(meta["size"]), "Upload-Complete": "1"})
            have = part.stat().st_size if part.exists() else 0
            if offset != have:
                return JSONResponse({"detail": "offset mismatch, continue from Upload-Offset"}, status_code=409,
                                    headers={"Upload-Offset": str(have)})
            part.parent.mkdir(parents=True, exist_ok=True)
            written = have
            with part.open("ab") as fh:
                async for chunk in request.stream():
                    if written + len(chunk) > meta["size"]:
                        fh.flush()
                        raise HTTPException(413, "more data than the manifest declared")
                    fh.write(chunk)
                    written += len(chunk)
            if written == meta["size"]:
                digest = await run_in_threadpool(_sha256_file, part)
                if digest != meta["sha256"]:
                    part.unlink(missing_ok=True)
                    return JSONResponse({"detail": "checksum mismatch, restart this file from offset 0"}, status_code=422,
                                        headers={"Upload-Offset": "0"})
                part.replace(final)
                return Response(headers={"Upload-Offset": str(written), "Upload-Complete": "1"})
            return Response(headers={"Upload-Offset": str(written), "Upload-Complete": "0"})

    @app.post("/v1/sessions/{sid}/finish", dependencies=[Depends(auth)])
    async def finish(sid: str, body: dict | None = None):
        st = session_status(sid)
        if st["files_complete"] < st["files_total"]:
            missing = [r for r, s in st["files"].items() if not s["complete"]][:20]
            raise HTTPException(409, {"detail": "upload incomplete", "missing": missing})
        rep = await run_in_threadpool(validate_session, sdir(sid))
        if not rep.ok:
            raise HTTPException(422, {"detail": "session is not valid", "errors": rep.errors, "warnings": rep.warnings})
        try:
            opts = {**defaults, **sanitize_options((body or {}).get("options"))}
        except ValueError as e:
            raise HTTPException(400, str(e))
        existing = next((j for j in reversed(jobs.list()) if j["session_id"] == sid and j["state"] in ("queued", "running")), None)
        if existing:
            return existing
        job = jobs.submit(sid, opts)
        job["warnings"] = rep.warnings
        return job

    # -- jobs and results -------------------------------------------------------------------------------------
    @app.get("/v1/jobs", dependencies=[Depends(auth)])
    def list_jobs():
        return [{k: j[k] for k in ("id", "session_id", "state", "stage", "progress", "message")} for j in jobs.list()[-50:]]

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
    def get_job(job_id: str):
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise HTTPException(400, "bad job id")
        j = jobs.get(job_id)
        if not j:
            raise HTTPException(404, "unknown job")
        return j

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    def cancel_job(job_id: str):
        if not re.fullmatch(r"[0-9a-f]{12}", job_id) or not jobs.cancel(job_id):
            raise HTTPException(404, "no cancellable job with that id")
        return {"cancelling": job_id}

    @app.get("/v1/sessions/{sid}/results", dependencies=[Depends(auth)])
    def list_results(sid: str):
        manifest(sid)
        r = sdir(sid) / "work" / "result"
        if not r.is_dir():
            return []
        return [{"name": p.name, "size": p.stat().st_size} for p in sorted(r.iterdir()) if RESULT_NAME_RE.match(p.name)]

    @app.get("/v1/sessions/{sid}/result/{fname}", dependencies=[Depends(auth)])
    def get_result(sid: str, fname: str):
        manifest(sid)
        if not RESULT_NAME_RE.match(fname):
            raise HTTPException(400, "bad file name")
        p = sdir(sid) / "work" / "result" / fname
        if not p.is_file():
            raise HTTPException(404, "no such result (is the job finished?)")
        return FileResponse(p)  # supports Range requests, so large downloads can resume

    return app
