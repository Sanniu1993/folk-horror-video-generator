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
import queue
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
# 人工确认闸口:页面上的「重出 / 确认」动作投进队列,渲染线程取出后执行。
# 用队列而不是直接在请求线程里渲染,是为了让所有 ComfyUI 调用都留在同一个线程,
# 不会出现两张图并发排队、也不会和取消逻辑打架。
_reviews: dict[str, queue.Queue] = {}
_lock = threading.Lock()


def _new_job(script: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _cancels[job_id] = threading.Event()
        _reviews[job_id] = queue.Queue()
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
            "memory": [],            # 每镜头 RAG 检索命中与贡献(调试用)
            "notice": None,            # 给页面的温和提示(如分镜粒度建议),不是错误
            "awaiting_review": False,  # 出图完毕、等待人工确认
            "review_shots": [],        # 评审面板要展示的每镜头信息(prompt / 改进要求 / 重出次数)
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
    speed: str,
    use_memory: bool,
    review: bool,
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
                item = {
                    "scene_id": payload["scene_id"],
                    "url": f"/outputs/{payload['file']}",
                    "meta": payload.get("meta", {}),
                }
                # 重出的图要替换原位,不能再往后追加(否则网格会多出一张)。
                for k, old in enumerate(job["images"]):
                    if old["scene_id"] == item["scene_id"]:
                        job["images"][k] = item
                        break
                else:
                    job["images"].append(item)
            elif event == "notice":
                job["notice"] = payload
            elif event == "memory_debug":
                job["memory"].append(payload)
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
    review_q = _reviews[job_id]

    def review_fn(shots: list[dict[str, Any]]) -> dict[str, Any]:
        """阻塞等待页面上的操作;期间把每镜头信息挂到 job 上供轮询展示。

        取消时立刻放行(返回 cancel),由 run_long_video 那边的 check_cancel 收尾。
        """
        with _lock:
            job = _jobs[job_id]
            job["awaiting_review"] = True
            job["review_shots"] = [
                {"scene_id": s["scene_id"], "prompt": s["prompt"],
                 "note": s.get("note", ""), "attempt": s.get("attempt", 0)}
                for s in shots
            ]
        try:
            while True:
                if cancel_event.is_set():
                    return {"type": "cancel"}
                try:
                    return review_q.get(timeout=0.5)
                except queue.Empty:
                    continue
        finally:
            with _lock:
                _jobs[job_id]["awaiting_review"] = False

    try:
        run_long_video(
            script,
            num_images=num_frames,
            on_event=on_event,
            should_cancel=cancel_event.is_set,
            review_fn=review_fn if review else None,
            art_positive=art_positive,
            art_negative=art_negative,
            style=style,
            orientation=orientation,
            language=language,
            speed=speed,
            use_memory=use_memory,
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
    style: str = "auto"        # auto(风格跟随剧情) / horror(全片统一暗调) / realistic / none
    orientation: str = "landscape"  # landscape (YouTube 16:9) | portrait (Shorts 9:16)
    language: str = "zh"       # zh (中文) | en (English) | ja (日本語) | ko (한국어)
    speed: str = "fast"        # fast (默认最快) | slow (适中,长视频)
    use_memory: bool = True    # 民俗视觉记忆库(folk-horror-memory)增强,默认开;
                               # 服务没起会自动跳过并在页面上给提示
    review: bool = False       # 出图后先停下来人工确认(可单张带改进要求重出)再合成视频


@app.post("/api/generate")
def generate(req: GenerateRequest) -> dict[str, str]:
    script = req.script.strip()
    if not script:
        raise HTTPException(status_code=400, detail="script 不能为空")
    frames = max(1, min(40, req.num_frames))
    if req.orientation not in ("landscape", "portrait"):
        raise HTTPException(status_code=400, detail="orientation 必须是 landscape 或 portrait")
    if req.language not in ("zh", "en", "ja", "ko"):
        raise HTTPException(status_code=400, detail="language 必须是 zh / en / ja / ko")
    if req.speed not in ("fast", "slow"):
        raise HTTPException(status_code=400, detail="speed 必须是 fast 或 slow")

    job_id = _new_job(script)
    logger.info("Job %s created (frames=%d, style=%s, orientation=%s, lang=%s, speed=%s, "
                "memory=%s, review=%s)",
                job_id, frames, req.style, req.orientation, req.language, req.speed,
                req.use_memory, req.review)
    threading.Thread(
        target=_run_job,
        args=(job_id, script, frames, req.art_positive, req.art_negative,
              req.style, req.orientation, req.language, req.speed, req.use_memory,
              req.review),
        daemon=True,
    ).start()
    return {"job_id": job_id}


class ReviewAction(BaseModel):
    index: int                 # 分镜序号(1-based)
    note: str = ""             # 改进要求(中文即可,会自动翻成英文再进提示词)
    prompt: str = ""           # 可选:直接覆盖该镜头的英文提示词(填了就忽略 note 的拼接)


def _push_review(job_id: str, action: dict[str, Any], label: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        q = _reviews.get(job_id)
        if job is None or q is None:
            raise HTTPException(status_code=404, detail="job not found")
        if not job.get("awaiting_review"):
            raise HTTPException(status_code=409, detail="当前不在人工确认阶段")
        job["stage"] = {**(job.get("stage") or {}), "label": label}
    q.put(action)


@app.post("/api/jobs/{job_id}/regenerate")
def regenerate_frame(job_id: str, req: ReviewAction) -> dict[str, str]:
    _push_review(job_id, {"type": "regenerate", "index": req.index,
                          "note": req.note.strip(), "prompt": req.prompt.strip()},
                 f"重出分镜 #{req.index}…")
    logger.info("Job %s: regenerate frame %d (note=%r)", job_id, req.index, req.note[:80])
    return {"status": "regenerating"}


@app.post("/api/jobs/{job_id}/approve")
def approve_frames(job_id: str) -> dict[str, str]:
    _push_review(job_id, {"type": "approve"}, "开始合成视频…")
    logger.info("Job %s: frames approved, composing video", job_id)
    return {"status": "approved"}


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
