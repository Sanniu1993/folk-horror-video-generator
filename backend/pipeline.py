"""End-to-end orchestrator: theme -> story.json -> scene.json -> 10 images.

This is the V1 success criterion: one theme in, a reproducible, style-consistent
set of key frames plus the JSON artifacts that produced them.

Run directly:
    python backend/pipeline.py "一个废弃医院里的午夜护士故事"
"""
from __future__ import annotations

import json
import logging
import sys
import time
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
from logging_config import setup_logging
from prompt_generator import _style_suffix, generate_prompts
from scene_planner import plan_scenes
from story_generator import generate_story

logger = logging.getLogger(__name__)

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
    num_frames: Optional[int] = None,
    character_consistency: bool = False,
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
    setup_logging(config)
    ds_cfg = config["deepseek"]
    gen_cfg = config["generation"]

    frames = int(num_frames) if num_frames else gen_cfg["num_key_frames"]
    logger.info(
        "=== Run start | theme=%r | render=%s | frames=%d | char_consistency=%s ===",
        theme, render_images, frames, character_consistency,
    )
    t0 = time.monotonic()
    ds = DeepSeekClient(ds_cfg)
    run_dir: Path | None = None

    try:
        check_cancel()

        # --- Step 1: structured story + style bible ----------------------
        logger.info("[1/4] Story creation for theme: %r", theme)
        emit("stage", {"step": 1, "total": 4, "label": "故事创作"})
        story = generate_story(ds, theme, temperature=ds_cfg.get("temperature", 0.8))

        run_dir = (
            ROOT
            / config["output_dir"]
            / f"{datetime.now():%Y%m%d_%H%M%S}_{_slugify(story['title'])}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        _dump(run_dir / "story.json", story)
        logger.info("      -> %s (%s) | run_dir=%s",
                    story["title"], story.get("genre", "horror"), run_dir.name)
        emit("run_dir", run_dir.name)
        emit("story", story)
        check_cancel()

        # --- Step 2: storyboard shot list --------------------------------
        logger.info("[2/4] Scene planning (storyboard)")
        emit("stage", {"step": 2, "total": 4, "label": "分镜规划"})
        scenes = plan_scenes(
            ds,
            story,
            num_frames=frames,
            temperature=ds_cfg.get("analysis_temperature", 0.4),
        )
        _dump(run_dir / "scenes.json", {"scenes": scenes})
        logger.info("      -> %d scenes", len(scenes))
        emit("scenes", scenes)
        check_cancel()

        # --- Step 3: image prompts (scene + fixed style bible) -----------
        logger.info("[3/4] Prompt generation")
        emit("stage", {"step": 3, "total": 4, "label": "Prompt 生成"})
        prompts = generate_prompts(
            ds, story, scenes, temperature=ds_cfg.get("analysis_temperature", 0.4)
        )
        _dump(run_dir / "prompts.json", {"prompts": prompts})
        emit("prompts", prompts)
        check_cancel()

        # --- Step 4: render key frames via ComfyUI -----------------------
        if not render_images:
            logger.info("[4/4] Skipped image rendering (render_images=False).")
            logger.info("Artifacts written to: %s (%.1fs total)", run_dir, time.monotonic() - t0)
            emit("done", {"run_dir": run_dir.name, "rendered": False})
            return run_dir

        logger.info("[4/4] Rendering %d key frames via ComfyUI", len(prompts))
        emit("stage", {"step": 4, "total": 4, "label": "渲染关键帧"})
        comfy = ComfyClient(config)
        if not comfy.ping():
            msg = (
                f"ComfyUI is not reachable at {comfy.base}. JSON artifacts are "
                "saved; start ComfyUI and rerun to render images."
            )
            logger.error(msg)
            emit("error", msg)
            return run_dir

        images_dir = run_dir / "images"

        # Optional: generate a reference portrait per character, then condition
        # every frame that features them via IPAdapter for face consistency.
        char_refs: dict[str, Path] = {}
        if character_consistency:
            if comfy.workflow_ipadapter is None:
                logger.warning("character_consistency requested but IPAdapter workflow "
                               "is not available; rendering without it.")
            else:
                char_refs = _generate_character_refs(
                    comfy, story, run_dir, gen_cfg, config, should_cancel, emit
                )
        # Which characters appear in each scene (for picking a reference).
        chars_by_scene = {sc["scene_id"]: sc.get("characters", []) for sc in scenes}

        total = len(prompts)
        rendered = 0
        for i, p in enumerate(prompts):
            check_cancel()  # stop before starting the next frame
            seed = gen_cfg["base_seed"] + p["scene_id"]
            prefix = f"frame_{p['scene_id']:02d}"
            # First character present in this scene that we have a reference for.
            ref = None
            for name in chars_by_scene.get(p["scene_id"], []):
                if name in char_refs:
                    ref = char_refs[name]
                    break
            logger.info("[4/4] frame %d/%d (%s, seed %d)%s", i + 1, total, prefix, seed,
                        f" [ref: {ref.stem}]" if ref else "")
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
                    reference_image=ref,
                )
                rendered += 1
                logger.info("      saved %s", dest.name)
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
                logger.exception("frame %s failed: %s", prefix, exc)
                emit("frame_error", {"scene_id": p["scene_id"], "error": str(exc)})

        logger.info("=== Done: %d/%d frames in %.1fs | %s ===",
                    rendered, total, time.monotonic() - t0, run_dir)
        emit("done", {"run_dir": run_dir.name, "rendered": True})

    except Cancelled:
        logger.warning("=== Run cancelled by user after %.1fs ===", time.monotonic() - t0)
        emit("cancelled", {"run_dir": run_dir.name if run_dir else None})
        return run_dir if run_dir else ROOT / config["output_dir"]
    return run_dir


def _generate_character_refs(
    comfy: ComfyClient,
    story: dict[str, Any],
    run_dir: Path,
    gen_cfg: dict[str, Any],
    config: dict[str, Any],
    should_cancel: CancelHook,
    emit: Callable[[str, Any], None],
) -> dict[str, Path]:
    """Render one clean portrait per character to use as an IPAdapter reference."""
    refs: dict[str, Path] = {}
    chars_dir = run_dir / "characters"
    suffix = _style_suffix(story.get("style_bible", {}))
    characters = story.get("characters", [])
    for idx, c in enumerate(characters):
        if should_cancel is not None and should_cancel():
            raise Cancelled()
        name = c["name"]
        prompt = (
            f"{c['description']}, character reference portrait, head and shoulders, "
            f"front view, neutral plain background, sharp focus, {suffix}"
        )
        logger.info("[ref] character %d/%d: %s", idx + 1, len(characters), name)
        emit("character_ref_start", {"name": name})
        try:
            dest = comfy.generate(
                positive=prompt,
                negative=config["negative_prompt"],
                seed=gen_cfg["base_seed"] + 900 + idx,
                width=gen_cfg["width"],
                height=gen_cfg["height"],
                gen_cfg=gen_cfg,
                filename_prefix=f"char_{idx:02d}",
                out_dir=chars_dir,
                should_cancel=should_cancel,
            )
            refs[name] = dest
            emit("character_ref_done", {"name": name, "file": f"{run_dir.name}/characters/{dest.name}"})
        except RunCancelled:
            raise Cancelled()
        except Exception as exc:
            logger.exception("character reference for %s failed: %s", name, exc)
    return refs


def _dump(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python backend/pipeline.py "your horror theme"')
        raise SystemExit(1)
    run(" ".join(sys.argv[1:]))
