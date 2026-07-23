"""Narration-driven long video: a full script (you provide it) -> narrated film.

Takes YOUR script verbatim as the narration, splits it into lines, groups the
lines under N key images, has DeepSeek write one image prompt per group
(consistent style + characters), then renders the images and assembles a
narrated video with per-line subtitles.

支持两种画幅(orientation):
  - landscape: YouTube 长视频 (16:9, 1920x1080)
  - portrait:  YouTube Shorts (9:16, 1080x1920)

支持两种语言(language):
  - zh: 中文剧本 -> 中文旁白/字幕
  - en: 英文剧本 -> 英文旁白/字幕

    python backend/long_video.py path/to/script.txt 25
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from comfy_client import ComfyClient, RunCancelled
from deepseek_client import DeepSeekClient
from logging_config import setup_logging
from pipeline import Cancelled, ROOT, _dump, load_config
from prompt_generator import _style_suffix, art_note
from video_builder import build_long_video, synth_narration

logger = logging.getLogger(__name__)

# --- 中文剧本用的系统提示 ---------------------------------------------------
STYLE_SYSTEM_ZH = """You are a horror art director for a CHINESE folk-horror narration video.
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

# --- 英文剧本用的系统提示 ---------------------------------------------------
STYLE_SYSTEM_EN = """You are a horror art director for a narration horror video.
Output ONLY a JSON object, no prose.

From the full narration script, extract a consistent visual bible and the key
recurring characters/places so every shot matches.

Schema:
{
  "style_bible": {"visual_style": "...", "color": "...", "camera": "...", "lighting": "...", "texture": "..."},
  "characters": [{"name": "name/label", "description": "ENGLISH, describe appearance/ethnicity, stable, redrawable"}]
}
Infer characters and settings from the script itself. 2-5 characters/places max.
"""

PROMPT_SYSTEM_ZH = """You are an SDXL prompt engineer for a CHINESE folk-horror video.
Output ONLY a JSON object, no prose.

You get a visual bible, character descriptions, and {n} ordered story segments.
For EACH segment write ONE English image prompt depicting that moment. Weave in
the exact character descriptions when a character appears (keep them consistent).
Do NOT add global style words (added automatically). Any person is Chinese/East Asian.

Schema: {{ "prompts": [{{"index": 1, "prompt": "..."}}] }}   // exactly {n} items, index 1..{n}
"""

PROMPT_SYSTEM_EN = """You are an SDXL prompt engineer for a narration horror video.
Output ONLY a JSON object, no prose.

You get a visual bible, character descriptions, and {n} ordered story segments.
For EACH segment write ONE English image prompt depicting that moment. Weave in
the exact character descriptions when a character appears (keep them consistent).
Do NOT add global style words (added automatically).

Schema: {{ "prompts": [{{"index": 1, "prompt": "..."}}] }}   // exactly {n} items, index 1..{n}
"""


# 英文默认音色(edge-tts 神经语音):Christopher 低沉沉稳,叙事/恐怖旁白感强。
# 其他可选:en-US-GuyNeural / en-US-BrianNeural(温暖) / en-US-AndrewNeural。
EN_VOICE_DEFAULT = "en-US-ChristopherNeural"
EN_SAY_VOICE = "Samantha"   # macOS 离线兜底(几乎所有 Mac 都有);仅 edge-tts 彻底失败时才用


def _split_lines(script: str) -> list[str]:
    """按换行和中英文句末标点粗略拆句;既支持中文剧本(按行/句号)也支持英文段落。"""
    raw_lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    out: list[str] = []
    for ln in raw_lines:
        # 若单行很长,按句末标点(中英文)进一步拆
        if len(ln) > 60:
            parts = re.split(r'(?<=[。！？!?\.])\s+', ln)
            out.extend(p.strip() for p in parts if p.strip())
        else:
            out.append(ln)
    return out


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
        if k < n - 1 and cum[i] >= (k + 1) * total / n and lines_left > groups_left:
            k += 1
    return [g for g in groups if g]


def _resolve_video_cfg(base_vcfg: dict, orientation: str, language: str) -> tuple[dict, int, int]:
    """依据画幅返回:调整后的 video 配置、SDXL 出图宽、SDXL 出图高。
    横向: 1920x1080 输出 / 1344x768 出图 (~16:9)
    竖向: 1080x1920 输出 /  768x1344 出图 (~9:16, YouTube Shorts)
    英文剧本会覆盖 TTS 音色。
    """
    vcfg = dict(base_vcfg or {})
    if orientation == "portrait":
        vcfg["out_width"] = 1080
        vcfg["out_height"] = 1920
        img_w, img_h = 768, 1344
    else:
        vcfg["out_width"] = 1920
        vcfg["out_height"] = 1080
        img_w, img_h = 1344, 768

    if language == "en":
        tts = dict(vcfg.get("tts", {}))
        tts["voice"] = tts.get("voice_en", EN_VOICE_DEFAULT)
        tts["say_voice"] = tts.get("say_voice_en", EN_SAY_VOICE)
        tts.setdefault("rate", "+0%")
        tts.setdefault("pitch", "-2Hz")
        vcfg["tts"] = tts
        vcfg["language"] = "en"
    else:
        vcfg["language"] = "zh"
    return vcfg, img_w, img_h


def run_long_video(script: str, num_images: int = 25, on_event=None, should_cancel=None,
                   art_positive="", art_negative="", style="horror",
                   orientation: str = "landscape", language: str = "zh") -> Path:
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
    vcfg_base = config.get("video", {})
    vcfg, img_w, img_h = _resolve_video_cfg(vcfg_base, orientation, language)
    tts_cfg = vcfg.get("tts", {})

    from prompt_generator import to_english_fragment
    art = config.get("art_direction", {})
    art_pos = to_english_fragment(ds, (art_positive or "").strip() or art.get("positive", ""))
    art_neg = to_english_fragment(ds, (art_negative or "").strip() or art.get("negative", ""))
    negative = config["negative_prompt"] + ((", " + art_neg) if art_neg.strip() else "")

    lines = _split_lines(script)
    logger.info("=== Long video | lang=%s | %s | %d lines | target %d images ===",
                language, orientation, len(lines), num_images)
    run_dir = None
    try:
        check_cancel()
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

        groups = _group_lines(lines, [li["dur"] for li in line_items], num_images)
        emit("stage", {"step": 2, "total": 4, "label": "分镜规划"})

        style_system = STYLE_SYSTEM_EN if language == "en" else STYLE_SYSTEM_ZH
        prompt_system = (PROMPT_SYSTEM_EN if language == "en" else PROMPT_SYSTEM_ZH).format(n=len(groups))
        user_header_char = "Characters (use verbatim when a character appears):" if language == "en" \
            else "角色(出现时按此英文描述):"
        user_header_seg = "Segments (%d total):" if language == "en" else "分段(共 %d 段):"

        bible = ds.chat_json(style_system, "SCRIPT:\n" + script + art_note(art_pos), temperature=0.5)
        suffix = _style_suffix(bible.get("style_bible", {}), style)
        chars = "\n".join(f'- {c["name"]}: {c["description"]}' for c in bible.get("characters", []))
        group_texts = [" ".join(lines[i] for i in g) for g in groups]
        user = (
            f"{user_header_char}\n{chars}\n\n"
            + (user_header_seg % len(groups)) + "\n"
            + "\n".join(f"[{i+1}] {t}" for i, t in enumerate(group_texts))
            + art_note(art_pos)
        )
        res = ds.chat_json(prompt_system, user, temperature=0.4, max_tokens=8000)
        pmap = {p["index"]: p["prompt"] for p in res.get("prompts", [])}
        _dump(run_dir / "long_plan.json",
              {"style_bible": bible.get("style_bible"), "characters": bible.get("characters"),
               "orientation": orientation, "language": language,
               "groups": [{"index": i + 1, "text": t, "prompt": pmap.get(i + 1, "")}
                          for i, t in enumerate(group_texts)]})

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
            prompt = ", ".join([scene_prompt] + ([art_pos] if art_pos else []) + [suffix])
            logger.info("Rendering image %d/%d", i + 1, n)
            emit("frame_start", {"scene_id": i + 1, "index": i + 1, "total": n})
            dest = comfy.generate(
                positive=prompt, negative=negative,
                seed=gen_cfg["base_seed"] + i,
                width=img_w, height=img_h,
                gen_cfg=gen_cfg, filename_prefix=f"img_{i:03d}",
                out_dir=images_dir, should_cancel=should_cancel,
            )
            emit("frame_done", {"scene_id": i + 1, "index": i + 1, "total": n,
                                "file": f"{run_dir.name}/images/{dest.name}"})
            segments.append({"image": dest, "lines": [line_items[j] for j in g]})

        emit("stage", {"step": 4, "total": 4, "label": "合成长片(字幕+配音)"})
        out = run_dir / "video.mp4"
        build_long_video(segments, out, vcfg)
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
        print('Usage: python backend/long_video.py <script.txt> [num_images=25] [orientation=landscape] [language=zh]')
        raise SystemExit(1)
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    n_img = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    orient = sys.argv[3] if len(sys.argv) > 3 else "landscape"
    lang = sys.argv[4] if len(sys.argv) > 4 else "zh"
    run_long_video(text, n_img, orientation=orient, language=lang)
