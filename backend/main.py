"""FastAPI app: serves the web UI, runs generation jobs, streams progress.

Layout of what it serves:
    GET  /                      -> the web UI (frontend/index.html)
    POST /api/generate          -> start a job, returns {job_id}
    GET  /api/jobs/{job_id}      -> job status + artifacts + image URLs (poll this)
    GET  /outputs/<run>/...      -> generated images (static)

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
from pipeline import ROOT, load_config, run
from short_video import run_short_video
from long_video import run_long_video

setup_logging(load_config())
logger = logging.getLogger(__name__)

OUTPUTS_DIR = ROOT / "outputs"
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="AI Horror Story Visual Generator", version="0.2.0")

# Generated images are served straight from the outputs directory.
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")


# --- in-memory job store ---------------------------------------------------
# Fine for a single-user local demo. A real deployment would use a queue + DB.
_jobs: dict[str, dict[str, Any]] = {}
# Cancel flags live outside the job dict so the dict stays JSON-serializable.
_cancels: dict[str, threading.Event] = {}
_lock = threading.Lock()


def _new_job(theme: str, render_images: bool) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _cancels[job_id] = threading.Event()
        _jobs[job_id] = {
            "job_id": job_id,
            "theme": theme,
            "status": "running",     # running | done | error | cancelled
            "stage": None,           # {step,total,label}
            "run_dir": None,
            "story": None,
            "scenes": None,
            "prompts": None,
            "images": [],            # [{scene_id,file,meta}]
            "characters": [],        # [{name,url}] reference portraits
            "video": None,           # url of the finished narrated clip
            "error": None,
        }
    return job_id


def _run_job(
    job_id: str,
    theme: str,
    render_images: bool,
    num_frames: int,
    character_consistency: bool,
    mode: str = "frames",
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
            elif event == "character_ref_done":
                job["characters"].append(
                    {"name": payload["name"], "url": f"/outputs/{payload['file']}"}
                )
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
        if mode == "long":
            run_long_video(
                theme,  # in long mode the "theme" field carries the full script
                num_images=num_frames,
                on_event=on_event,
                should_cancel=cancel_event.is_set,
            )
        elif mode == "video":
            run_short_video(
                theme,
                num_scenes=num_frames,
                on_event=on_event,
                should_cancel=cancel_event.is_set,
            )
        else:
            run(
                theme,
                render_images=render_images,
                on_event=on_event,
                should_cancel=cancel_event.is_set,
                num_frames=num_frames,
                character_consistency=character_consistency,
            )
        with _lock:
            if _jobs[job_id]["status"] == "running":
                _jobs[job_id]["status"] = "done"
    except Exception as exc:  # surface setup errors (missing key, etc.) to the UI
        with _lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)


class GenerateRequest(BaseModel):
    theme: str
    render_images: bool = True
    num_frames: int = 10
    character_consistency: bool = False
    mode: str = "frames"       # "frames" (storyboard) | "video" (short clip) | "long" (script-driven)


@app.post("/api/generate")
def generate(req: GenerateRequest) -> dict[str, str]:
    theme = req.theme.strip()
    if not theme:
        raise HTTPException(status_code=400, detail="theme 不能为空")
    cap = 40 if req.mode == "long" else 10   # long videos allow many more images
    frames = max(1, min(cap, req.num_frames))
    job_id = _new_job(theme, req.render_images)
    logger.info("Job %s created (mode=%s, render=%s, frames=%d, char_consistency=%s): %r",
                job_id, req.mode, req.render_images, frames, req.character_consistency, theme)
    threading.Thread(
        target=_run_job,
        args=(job_id, theme, req.render_images, frames, req.character_consistency, req.mode),
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
        event.set()  # pipeline checks this between/within stages
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
        return dict(job)  # shallow copy for the response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))
