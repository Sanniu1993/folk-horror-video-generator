"""Ultra-short narrated horror clip: theme -> N frames -> Ken Burns video + 讲述者配音 + 字幕.

A compact companion to pipeline.py, aimed at quick tests: one very short story,
a handful of scenes, each with a one-line narrator script that is both spoken
and shown as a subtitle.

    python backend/short_video.py "一个人走夜路看到前面有个红色人影，于是落荒而逃" 3
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from comfy_client import ComfyClient, RunCancelled
from deepseek_client import DeepSeekClient
from logging_config import setup_logging
from pipeline import Cancelled, ROOT, _dump, _slugify, load_config
from prompt_generator import _style_suffix
from video_builder import build_narrated_video

logger = logging.getLogger(__name__)

SYSTEM = """You are a horror micro-film director + narrator, working in a CHINESE setting.
Output ONLY a JSON object, no prose, no markdown fences.

Given a one-line theme, design an ULTRA-SHORT story told in exactly {n} shots.
A single narrator ("讲述者") tells it — calm, ominous, third person.

Return this schema:
{{
  "title": "短标题(中文)",
  "style_bible": {{
    "visual_style": "e.g. cinematic horror",
    "color": "e.g. cold blue with a red accent",
    "camera": "e.g. 35mm film",
    "lighting": "e.g. low key, near darkness",
    "texture": "e.g. grainy, realistic"
  }},
  "scenes": [
    {{
      "prompt": "ENGLISH image prompt for this shot; any person is Chinese/East Asian, setting is Chinese; concrete and self-contained (no global style words — added later)",
      "narration": "一句极短的中文旁白(讲述者口吻),≤18字"
    }}
  ]
}}

Rules:
- Exactly {n} scenes; together they form a tiny arc (setup -> shock -> escape/sting).
- narration total across all scenes should read as 1-2 flowing sentences.
- Keep it genuinely short and punchy; this is a ~15 second clip.
"""


def generate_short_story(client: DeepSeekClient, theme: str, num_scenes: int) -> dict[str, Any]:
    system = SYSTEM.format(n=num_scenes)
    story = client.chat_json(system, f"主题:{theme}\n\n现在生成这个极短故事。", temperature=0.7)
    scenes = story.get("scenes", [])[:num_scenes]
    if not scenes:
        raise ValueError("Short story has no scenes.")
    story["scenes"] = scenes
    return story


def run_short_video(theme, num_scenes=3, on_event=None, should_cancel=None) -> Path:
    def emit(event, payload):
        if on_event:
            try:
                on_event(event, payload)
            except Exception:
                pass

    def check_cancel():
        if should_cancel is not None and should_cancel():
            raise Cancelled()

    config = load_config()
    setup_logging(config)
    ds = DeepSeekClient(config["deepseek"])
    gen_cfg = config["generation"]
    vcfg = config.get("video", {})
    img_w = vcfg.get("image_width", gen_cfg["width"])
    img_h = vcfg.get("image_height", gen_cfg["height"])

    logger.info("=== Short video | theme=%r | scenes=%d ===", theme, num_scenes)
    run_dir = None
    try:
        check_cancel()
        emit("stage", {"step": 1, "total": 3, "label": "故事创作"})
        story = generate_short_story(ds, theme, num_scenes)
        suffix = _style_suffix(story.get("style_bible", {}))

        run_dir = ROOT / config["output_dir"] / f"{datetime.now():%Y%m%d_%H%M%S}_{_slugify(story['title'])}_short"
        (run_dir / "images").mkdir(parents=True, exist_ok=True)
        _dump(run_dir / "short_story.json", story)
        logger.info("Story: %s (%d scenes)", story["title"], len(story["scenes"]))
        emit("story", {
            "title": story["title"],
            "logline": " ".join(sc.get("narration", "") for sc in story["scenes"]),
            "style_bible": story.get("style_bible", {}),
        })

        comfy = ComfyClient(config)
        if not comfy.ping():
            raise RuntimeError(f"ComfyUI not reachable at {comfy.base}")

        emit("stage", {"step": 2, "total": 3, "label": "渲染镜头"})
        images, lines = [], []
        total = len(story["scenes"])
        for i, sc in enumerate(story["scenes"]):
            check_cancel()
            prompt = f"{sc['prompt']}, {suffix}"
            logger.info("Rendering shot %d/%d", i + 1, total)
            emit("frame_start", {"scene_id": i + 1, "index": i + 1, "total": total})
            dest = comfy.generate(
                positive=prompt,
                negative=config["negative_prompt"],
                seed=gen_cfg["base_seed"] + i,
                width=img_w,
                height=img_h,
                gen_cfg=gen_cfg,
                filename_prefix=f"shot_{i:02d}",
                out_dir=run_dir / "images",
                should_cancel=should_cancel,
            )
            images.append(dest)
            lines.append(sc["narration"])
            emit("frame_done", {
                "scene_id": i + 1, "index": i + 1, "total": total,
                "file": f"{run_dir.name}/images/{dest.name}",
                "meta": {"narration": sc["narration"]},
            })

        emit("stage", {"step": 3, "total": 3, "label": "合成视频(配音+字幕)"})
        out = run_dir / "video.mp4"
        build_narrated_video(images, lines, out, config.get("video", {}))
        logger.info("=== Done: %s ===", out)
        emit("video", {"url": f"/outputs/{run_dir.name}/video.mp4"})
        emit("done", {"run_dir": run_dir.name})
        return out
    except (Cancelled, RunCancelled):
        logger.warning("=== Short video cancelled ===")
        emit("cancelled", {"run_dir": run_dir.name if run_dir else None})
        return run_dir if run_dir else ROOT / config["output_dir"]


def run_short_video_cli(theme: str, num_scenes: int = 3) -> Path:
    return run_short_video(theme, num_scenes)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python backend/short_video.py "主题" [镜头数=3]')
        raise SystemExit(1)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    run_short_video(sys.argv[1], n)
