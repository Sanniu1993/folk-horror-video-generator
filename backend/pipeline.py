"""End-to-end orchestrator: theme -> story.json -> scene.json -> 10 images.

This is the V1 success criterion: one theme in, a reproducible, style-consistent
set of key frames plus the JSON artifacts that produced them.

Run directly:
    python backend/pipeline.py "一个废弃医院里的午夜护士故事"
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# on_event(event: str, payload: Any) — lets a caller (e.g. the web UI) observe
# progress without coupling to the pipeline internals.
EventHook = Optional[Callable[[str, Any], None]]
# should_cancel() -> True once the caller wants the run stopped.
CancelHook = Optional[Callable[[], bool]]


class Cancelled(Exception):
    """Raised internally to unwind the pipeline when cancellation is requested."""

import yaml

from comfy_client import ComfyClient, RunCancelled
from deepseek_client import DeepSeekClient
from prompt_generator import generate_prompts
from scene_planner import plan_scenes
from story_generator import generate_story

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _slugify(title: str) -> str:
    keep = [c if c.isalnum() else "_" for c in title.lower()]
    return "".join(keep).strip("_")[:40] or "story"


def run(
    theme: str,
    render_images: bool = True,
    on_event: EventHook = None,
    should_cancel: CancelHook = None,
) -> Path:
    def emit(event: str, payload: Any) -> None:
        if on_event is not None:
            try:
                on_event(event, payload)
            except Exception:  # a broken observer must never break the pipeline
                pass

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise Cancelled()

    config = load_config()
    ds_cfg = config["deepseek"]
    gen_cfg = config["generation"]

    ds = DeepSeekClient(ds_cfg)
    run_dir: Path | None = None

    try:
        check_cancel()

        # --- Step 1: structured story + style bible ----------------------
        print(f"[1/4] Story creation for theme: {theme!r}")
        emit("stage", {"step": 1, "total": 4, "label": "故事创作"})
        story = generate_story(ds, theme, temperature=ds_cfg.get("temperature", 0.8))

        run_dir = (
            ROOT
            / config["output_dir"]
            / f"{datetime.now():%Y%m%d_%H%M%S}_{_slugify(story['title'])}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        _dump(run_dir / "story.json", story)
        print(f"      -> {story['title']} ({story.get('genre', 'horror')})")
        emit("run_dir", run_dir.name)
        emit("story", story)
        check_cancel()

        # --- Step 2: storyboard shot list --------------------------------
        print("[2/4] Scene planning (storyboard)")
        emit("stage", {"step": 2, "total": 4, "label": "分镜规划"})
        scenes = plan_scenes(
            ds,
            story,
            num_frames=gen_cfg["num_key_frames"],
            temperature=ds_cfg.get("analysis_temperature", 0.4),
        )
        _dump(run_dir / "scenes.json", {"scenes": scenes})
        print(f"      -> {len(scenes)} scenes")
        emit("scenes", scenes)
        check_cancel()

        # --- Step 3: image prompts (scene + fixed style bible) -----------
        print("[3/4] Prompt generation")
        emit("stage", {"step": 3, "total": 4, "label": "Prompt 生成"})
        prompts = generate_prompts(
            ds, story, scenes, temperature=ds_cfg.get("analysis_temperature", 0.4)
        )
        _dump(run_dir / "prompts.json", {"prompts": prompts})
        emit("prompts", prompts)
        check_cancel()

        # --- Step 4: render key frames via ComfyUI -----------------------
        if not render_images:
            print("[4/4] Skipped image rendering (render_images=False).")
            print(f"\nArtifacts written to: {run_dir}")
            emit("done", {"run_dir": run_dir.name, "rendered": False})
            return run_dir

        print("[4/4] Rendering key frames via ComfyUI")
        emit("stage", {"step": 4, "total": 4, "label": "渲染关键帧"})
        comfy = ComfyClient(config)
        if not comfy.ping():
            msg = (
                f"ComfyUI is not reachable at {comfy.base}. JSON artifacts are "
                "saved; start ComfyUI and rerun to render images."
            )
            print(f"      ! {msg}")
            emit("error", msg)
            return run_dir

        images_dir = run_dir / "images"
        total = len(prompts)
        for i, p in enumerate(prompts):
            check_cancel()  # stop before starting the next frame
            seed = gen_cfg["base_seed"] + p["scene_id"]
            prefix = f"frame_{p['scene_id']:02d}"
            print(f"      [{i + 1}/{total}] {prefix} (seed {seed})")
            emit("frame_start", {"scene_id": p["scene_id"], "index": i + 1, "total": total})
            try:
                dest = comfy.generate(
                    positive=p["prompt"],
                    negative=config["negative_prompt"],
                    seed=seed,
                    width=gen_cfg["width"],
                    height=gen_cfg["height"],
                    gen_cfg=gen_cfg,
                    filename_prefix=prefix,
                    out_dir=images_dir,
                    should_cancel=should_cancel,  # interrupt mid-render if asked
                )
                print(f"          saved {dest.name}")
                emit(
                    "frame_done",
                    {
                        "scene_id": p["scene_id"],
                        "index": i + 1,
                        "total": total,
                        "file": f"{run_dir.name}/images/{dest.name}",
                        "meta": p.get("meta", {}),
                    },
                )
            except RunCancelled:
                raise Cancelled()  # unwind to the single cancel handler below
            except Exception as exc:  # one bad frame shouldn't kill the run
                print(f"          ! failed: {exc}")
                emit("frame_error", {"scene_id": p["scene_id"], "error": str(exc)})

        print(f"\nDone. Artifacts + images in: {run_dir}")
        emit("done", {"run_dir": run_dir.name, "rendered": True})

    except Cancelled:
        print("\n[cancelled] Run stopped by user.")
        emit("cancelled", {"run_dir": run_dir.name if run_dir else None})
        return run_dir if run_dir else ROOT / config["output_dir"]
    return run_dir


def _dump(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python backend/pipeline.py "your horror theme"')
        raise SystemExit(1)
    run(" ".join(sys.argv[1:]))
