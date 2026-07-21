"""Narration-driven long video: a full script (you provide it) -> ~10 min film.

Unlike short_video.py (which invents a story), this takes YOUR script verbatim
as the narration, splits it into lines, groups the lines under N key images, has
DeepSeek write one image prompt per group (consistent style + characters), then
renders the images and assembles a narrated video with per-line subtitles.

    python backend/long_video.py path/to/script.txt 25
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from comfy_client import ComfyClient, RunCancelled
from deepseek_client import DeepSeekClient
from logging_config import setup_logging
from pipeline import Cancelled, ROOT, _dump, _slugify, load_config
from prompt_generator import _style_suffix
from video_builder import build_long_video, synth_narration

logger = logging.getLogger(__name__)

STYLE_SYSTEM = """You are a horror art director for a CHINESE folk-horror narration video.
Output ONLY a JSON object, no prose.

From the full narration script, extract a consistent visual bible and the key
recurring characters/places so every shot matches.

Schema:
{
  "style_bible": {"visual_style": "...", "color": "...", "camera": "...", "lighting": "...", "texture": "..."},
  "characters": [{"name": "中文名/称呼", "description": "ENGLISH, start with ethnicity e.g. 'a Chinese girl', stable, redrawable"}]
}
Characters are Chinese/East Asian; settings are rural Chinese. 2-5 characters/places max.
"""

PROMPT_SYSTEM = """You are an SDXL prompt engineer for a CHINESE folk-horror video.
Output ONLY a JSON object, no prose.

You get a visual bible, character descriptions, and {n} ordered story segments.
For EACH segment write ONE English image prompt depicting that moment. Weave in
the exact character descriptions when a character appears (keep them consistent).
Do NOT add global style words (added automatically). Any person is Chinese/East Asian.

Schema: {{ "prompts": [{{"index": 1, "prompt": "..."}}] }}   // exactly {n} items, index 1..{n}
"""


def _split_lines(script: str) -> list[str]:
    lines = [ln.strip() for ln in script.splitlines()]
    return [ln for ln in lines if ln]


def _group_lines(lines: list[str], durs: list[float], n_images: int) -> list[list[int]]:
    """Split line indices into exactly n contiguous groups of ~equal total time."""
    n = min(n_images, len(lines))
    cum, s = [], 0.0
    for d in durs:
        s += d
        cum.append(s)
    total = s or 1.0
    groups: list[list[int]] = [[] for _ in range(n)]
    k = 0
    for i in range(len(lines)):
        groups[k].append(i)
        lines_left = len(lines) - i - 1
        groups_left = n - 1 - k
        # advance to the next group past a time boundary, but only if enough
        # lines remain to fill every remaining group (guarantees no empties).
        if k < n - 1 and cum[i] >= (k + 1) * total / n and lines_left > groups_left:
            k += 1
    return [g for g in groups if g]


def run_long_video(script: str, num_images: int = 25, on_event=None, should_cancel=None) -> Path:
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
    tts_cfg = vcfg.get("tts", {})
    img_w = vcfg.get("image_width", gen_cfg["width"])
    img_h = vcfg.get("image_height", gen_cfg["height"])

    lines = _split_lines(script)
    logger.info("=== Long video | %d lines | target %d images ===", len(lines), num_images)
    run_dir = None
    try:
        check_cancel()
        # 1) TTS every line (drives timing + subtitles).
        emit("stage", {"step": 1, "total": 4, "label": "配音(逐句)"})
        run_dir = ROOT / config["output_dir"] / f"{datetime.now():%Y%m%d_%H%M%S}_long"
        audio_dir = run_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        line_items = []
        for i, ln in enumerate(lines):
            check_cancel()
            path, dur = synth_narration(ln, audio_dir / f"l_{i:03d}", tts_cfg)
            line_items.append({"text": ln, "audio": path, "dur": dur})
            if (i + 1) % 20 == 0:
                logger.info("  TTS %d/%d lines", i + 1, len(lines))
        total_dur = sum(li["dur"] for li in line_items)
        logger.info("Narration total: %.1fs (%.1f min)", total_dur, total_dur / 60)

        # 2) Group lines under N images + get style bible / characters / prompts.
        groups = _group_lines(lines, [li["dur"] for li in line_items], num_images)
        emit("stage", {"step": 2, "total": 4, "label": "分镜规划"})
        bible = ds.chat_json(STYLE_SYSTEM, "剧本全文:\n" + script, temperature=0.5)
        suffix = _style_suffix(bible.get("style_bible", {}))
        chars = "\n".join(f'- {c["name"]}: {c["description"]}' for c in bible.get("characters", []))
        group_texts = ["".join(lines[i] for i in g) for g in groups]
        user = (
            f"角色(出现时按此英文描述):\n{chars}\n\n"
            + "分段(共 %d 段):\n" % len(groups)
            + "\n".join(f"[{i+1}] {t}" for i, t in enumerate(group_texts))
        )
        res = ds.chat_json(PROMPT_SYSTEM.format(n=len(groups)), user, temperature=0.4, max_tokens=8000)
        pmap = {p["index"]: p["prompt"] for p in res.get("prompts", [])}
        _dump(run_dir / "long_plan.json",
              {"style_bible": bible.get("style_bible"), "characters": bible.get("characters"),
               "groups": [{"index": i + 1, "text": t, "prompt": pmap.get(i + 1, "")}
                          for i, t in enumerate(group_texts)]})

        # 3) Render one image per group.
        emit("stage", {"step": 3, "total": 4, "label": "渲染分镜图"})
        comfy = ComfyClient(config)
        if not comfy.ping():
            raise RuntimeError(f"ComfyUI not reachable at {comfy.base}")
        images_dir = run_dir / "images"
        n = len(groups)
        segments = []
        for i, g in enumerate(groups):
            check_cancel()
            scene_prompt = pmap.get(i + 1) or group_texts[i]
            prompt = f"{scene_prompt}, {suffix}"
            logger.info("Rendering image %d/%d", i + 1, n)
            emit("frame_start", {"scene_id": i + 1, "index": i + 1, "total": n})
            dest = comfy.generate(
                positive=prompt, negative=config["negative_prompt"],
                seed=gen_cfg["base_seed"] + i,
                width=img_w, height=img_h,
                gen_cfg=gen_cfg, filename_prefix=f"img_{i:03d}",
                out_dir=images_dir, should_cancel=should_cancel,
            )
            emit("frame_done", {"scene_id": i + 1, "index": i + 1, "total": n,
                                "file": f"{run_dir.name}/images/{dest.name}"})
            segments.append({"image": dest, "lines": [line_items[j] for j in g]})

        # 4) Assemble the long narrated video.
        emit("stage", {"step": 4, "total": 4, "label": "合成长片(字幕+配音)"})
        out = run_dir / "video.mp4"
        build_long_video(segments, out, config.get("video", {}))
        emit("video", {"url": f"/outputs/{run_dir.name}/video.mp4"})
        emit("done", {"run_dir": run_dir.name})
        logger.info("=== Done: %s ===", out)
        return out
    except (Cancelled, RunCancelled):
        logger.warning("=== Long video cancelled ===")
        emit("cancelled", {"run_dir": run_dir.name if run_dir else None})
        return run_dir if run_dir else ROOT / config["output_dir"]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python backend/long_video.py <script.txt> [num_images=25]')
        raise SystemExit(1)
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    n_img = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    run_long_video(text, n_img)
