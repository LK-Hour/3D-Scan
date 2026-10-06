"""Single-worker job queue persisted on disk (jobs survive a server restart; only one GPU job runs at a time)."""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path

from .colmap_runner import Cancelled
from .pipeline import Pipeline, PipelineConfig

STATES = ("queued", "running", "done", "failed", "cancelled")


class JobStore:
    def __init__(self, root: Path, sessions_root: Path):
        self.dir = root / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sessions_root = sessions_root
        self.q: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._cancel: dict[str, threading.Event] = {}
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        # anything left "running" from a previous process was interrupted: re-queue it (stages are cached)
        for p in sorted(self.dir.glob("*.json")):
            j = json.loads(p.read_text())
            if j["state"] in ("queued", "running"):
                j["state"] = "queued"
                self._save(j)
                self.q.put(j["id"])

    # -- persistence ------------------------------------------------------------------------------------------
    def _path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.json"

    def _save(self, job: dict) -> None:
        tmp = self._path(job["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=1))
        tmp.replace(self._path(job["id"]))

    def get(self, job_id: str) -> dict | None:
        p = self._path(job_id)
        return json.loads(p.read_text()) if p.exists() else None

    def list(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.dir.glob("*.json"))]

    def _update(self, job_id: str, **kw) -> dict:
        with self._lock:
            j = self.get(job_id)
            j.update(kw)
            j["updated"] = time.time()
            self._save(j)
            return j

    # -- api --------------------------------------------------------------------------------------------------
    def submit(self, session_id: str, options: dict | None = None) -> dict:
        job = {"id": uuid.uuid4().hex[:12], "session_id": session_id, "state": "queued", "stage": "queued",
               "progress": 0.0, "message": "waiting for the GPU", "options": options or {}, "error": None,
               "report": None, "created": time.time(), "updated": time.time()}
        self._save(job)
        self.q.put(job["id"])
        return job

    def cancel(self, job_id: str) -> bool:
        j = self.get(job_id)
        if not j or j["state"] in ("done", "failed", "cancelled"):
            return False
        if j["state"] == "queued":
            self._update(job_id, state="cancelled", message="cancelled before start")
            return True
        ev = self._cancel.get(job_id)
        if ev:
            ev.set()
        return True

    # -- worker -----------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, name="scanpc-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self.q.put("")

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self.q.get()
            if not job_id or self._stop.is_set():
                continue
            j = self.get(job_id)
            if not j or j["state"] != "queued":
                continue
            self._run(j)

    def _run(self, job: dict) -> None:
        jid = job["id"]
        cancel = threading.Event()
        self._cancel[jid] = cancel
        self._update(jid, state="running", stage="starting", message="starting")

        def progress(stage: str, frac: float, msg: str) -> None:
            self._update(jid, stage=stage, progress=round(frac, 4), message=msg)

        try:
            cfg = PipelineConfig.from_dict(job["options"])
            report = Pipeline(self.sessions_root / job["session_id"], cfg, progress, cancel).run()
            self._update(jid, state="done", stage="done", progress=1.0, message="finished", report=report)
        except Cancelled:
            self._update(jid, state="cancelled", message="cancelled")
        except Exception as e:  # report every failure to the app, with the cause
            (self.sessions_root / job["session_id"] / "work").mkdir(parents=True, exist_ok=True)
            (self.sessions_root / job["session_id"] / "work" / "error.txt").write_text(traceback.format_exc())
            self._update(jid, state="failed", message=str(e).splitlines()[0][:300] if str(e) else type(e).__name__,
                         error=str(e)[:2000])
        finally:
            self._cancel.pop(jid, None)
