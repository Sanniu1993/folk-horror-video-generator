"""FastAPI app: serves the web UI, runs generation jobs, streams progress.

Layout:
    GET  /                      -> the web UI (frontend/index.html)
    POST /api/generate          -> start a job, returns {job_id}
    GET  /api/jobs/{job_id}      -> job status + artifacts + image URLs (poll this)
    GET  /outputs/<run>/...      -> generated images / video (static)

Run:
    uvicorn main:app --reload --app-dir backend
Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from logging_config import setup_logging
from pipeline import ROOT, load_config
from long_video import run_long_video

setup_logging(load_config())
logger = logging.getLogger(__name__)

OUTPUTS_DIR = ROOT / "outputs"
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="AI Horror Story Visual Generator", version="0.3.0")

OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")


# --- in-memory job store ---------------------------------------------------
_jobs: dict[str, dict[str, Any]] = {}
_cancels: dict[str, threading.Event] = {}
_lock = threading.Lock()


def _new_job(script: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _cancels[job_id] = threading.Event()
        _jobs[job_id] = {
            "job_id": job_id,
            "script": script[:200],
            "status": "running",
            "stage": None,
            "run_dir": None,
            "story": None,
            "scenes": None,
            "prompts": None,
            "images": [],
            "video": None,
            "error": None,
        }
    return job_id


def _run_job(
    job_id: str,
    script: str,
    num_frames: int,
    art_positive: str,
    art_negative: str,
    style: str,
    orientation: str,
    language: str,
) -> None:
    def on_event(event: str, payload: Any) -> None:
        with _lock:
            job = _jobs[job_id]
            if event == "stage":
                job["stage"] = payload
            elif event == "run_dir":
                job["run_dir"] = payload
            elif event == "story":
                job["story"] = payload
            elif event == "scenes":
                job["scenes"] = payload
            elif event == "prompts":
                job["prompts"] = payload
            elif event == "frame_done":
                job["images"].append(
                    {
                        "scene_id": payload["scene_id"],
                        "url": f"/outputs/{payload['file']}",
                        "meta": payload.get("meta", {}),
                    }
                )
            elif event == "video":
                job["video"] = payload["url"]
            elif event == "done":
                job["status"] = "done"
            elif event == "cancelled":
                job["status"] = "cancelled"
            elif event == "error":
                job["status"] = "error"
                job["error"] = payload

    cancel_event = _cancels[job_id]
    try:
        run_long_video(
            script,
            num_images=num_frames,
            on_event=on_event,
            should_cancel=cancel_event.is_set,
            art_positive=art_positive,
            art_negative=art_negative,
            style=style,
            orientation=orientation,
            language=language,
        )
        with _lock:
            if _jobs[job_id]["status"] == "running":
                _jobs[job_id]["status"] = "done"
    except Exception as exc:
        with _lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)


class GenerateRequest(BaseModel):
    script: str                # 故事稿 / 播音稿(逐行)
    num_frames: int = 25       # 关键分镜图数量
    art_positive: str = ""
    art_negative: str = ""
    style: str = "horror"      # horror / realistic / none
    orientation: str = "landscape"  # landscape (YouTube 16:9) | portrait (Shorts 9:16)
    language: str = "zh"       # zh (中文) | en (English)


@app.post("/api/generate")
def generate(req: GenerateRequest) -> dict[str, str]:
    script = req.script.strip()
    if not script:
        raise HTTPException(status_code=400, detail="script 不能为空")
    frames = max(1, min(40, req.num_frames))
    if req.orientation not in ("landscape", "portrait"):
        raise HTTPException(status_code=400, detail="orientation 必须是 landscape 或 portrait")
    if req.language not in ("zh", "en"):
        raise HTTPException(status_code=400, detail="language 必须是 zh 或 en")

    job_id = _new_job(script)
    logger.info("Job %s created (frames=%d, style=%s, orientation=%s, lang=%s)",
                job_id, frames, req.style, req.orientation, req.language)
    threading.Thread(
        target=_run_job,
        args=(job_id, script, frames, req.art_positive, req.art_negative,
              req.style, req.orientation, req.language),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, str]:
    with _lock:
        event = _cancels.get(job_id)
        job = _jobs.get(job_id)
        if event is None or job is None:
            raise HTTPException(status_code=404, detail="job not found")
        event.set()
        if job["status"] == "running":
            job["stage"] = {**(job.get("stage") or {}), "label": "正在停止…"}
    logger.info("Job %s: cancel requested", job_id)
    return {"status": "cancelling"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return dict(job)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))
