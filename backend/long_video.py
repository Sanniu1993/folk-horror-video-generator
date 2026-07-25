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
from pipeline import Cancelled, ROOT, _dump, _slugify, load_config
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

# 各非中文语种的 TTS 音色(edge-tts 神经语音 + macOS say 离线兜底)。
# 中文(zh)沿用 config.yaml 里的默认音色,不在此表。
LANG_VOICES = {
    "en": {"voice": EN_VOICE_DEFAULT,      "say": EN_SAY_VOICE},
    "ja": {"voice": "ja-JP-KeitaNeural",   "say": "Kyoko"},   # 日语男声旁白
    "ko": {"voice": "ko-KR-InJoonNeural",  "say": "Yuna"},    # 韩语男声旁白
}


def _chinese_title(ds: DeepSeekClient, script: str) -> str:
    """让 DeepSeek 给剧本起一个简短中文标题(用于输出文件夹名,方便查找)。
    剧本可为任意语种,标题统一中文。失败返回空串(退回纯时间戳命名)。"""
    try:
        res = ds.chat_json(
            "你根据下面的剧本起一个简短的中文标题,用作文件名:6~12 个汉字、"
            "概括主题、不含标点/空格/引号。直接输出紧凑 JSON,前后不要有多余空白。"
            "格式:{\"title\": \"中文标题\"}",
            script[:2000],
            temperature=0.3,
            max_tokens=512,
        )
        return (res.get("title") or "").strip()[:20]
    except Exception as exc:
        logger.warning("中文标题生成失败(退回时间戳命名):%s", exc)
        return ""


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


# 语速档:fast = 现在的默认最快;slow = 比默认慢一档,适合 YouTube 长视频。
# 数值是 edge-tts 的 rate(相对基准语速)。config 里的 rate 作为 fast 档基准。
SPEED_RATES = {
    "fast": "+15%",
    "slow": "-5%",
}


def _resolve_video_cfg(base_vcfg: dict, orientation: str, language: str,
                       speed: str = "fast") -> tuple[dict, int, int]:
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

    tts = dict(vcfg.get("tts", {}))
    if language in LANG_VOICES:      # en / ja / ko:切换到对应语种音色
        tts["voice"] = LANG_VOICES[language]["voice"]
        tts["say_voice"] = LANG_VOICES[language]["say"]
        tts.setdefault("pitch", "-2Hz")
    # 中文(zh)沿用 config.yaml 里的默认音色,不覆盖。
    vcfg["language"] = language
    # 语速档覆盖 config 里的 rate(fast=默认最快 / slow=长视频稍慢)。
    tts["rate"] = SPEED_RATES.get(speed, SPEED_RATES["fast"])
    vcfg["tts"] = tts
    return vcfg, img_w, img_h


def run_long_video(script: str, num_images: int = 25, on_event=None, should_cancel=None,
                   art_positive="", art_negative="", style="horror",
                   orientation: str = "landscape", language: str = "zh",
                   speed: str = "fast") -> Path:
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
    vcfg, img_w, img_h = _resolve_video_cfg(vcfg_base, orientation, language, speed)
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
        # 先起一个中文标题拼进文件夹名(方便在 outputs 里辨认),失败则退回纯时间戳。
        slug = _slugify(_chinese_title(ds, script))
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{slug}_long" if slug and slug != "story" \
            else f"{datetime.now():%Y%m%d_%H%M%S}_long"
        emit("stage", {"step": 1, "total": 4, "label": "配音(逐句)"})
        run_dir = ROOT / config["output_dir"] / name
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

        # 中文剧本用 ZH 版(强制中式设定);其余语种(en/ja/ko)用通用版,
        # 让 LLM 从剧本本身推断人物/场景设定,图像 prompt 仍输出英文。
        non_zh = language != "zh"
        style_system = STYLE_SYSTEM_EN if non_zh else STYLE_SYSTEM_ZH
        prompt_system = (PROMPT_SYSTEM_EN if non_zh else PROMPT_SYSTEM_ZH).format(n=len(groups))
        user_header_char = "Characters (use verbatim when a character appears):" if non_zh \
            else "角色(出现时按此英文描述):"
        user_header_seg = "Segments (%d total):" if non_zh else "分段(共 %d 段):"

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
        print('Usage: python backend/long_video.py <script.txt> [num_images=25] [orientation=landscape] [language=zh] [speed=fast]')
        raise SystemExit(1)
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    n_img = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    orient = sys.argv[3] if len(sys.argv) > 3 else "landscape"
    lang = sys.argv[4] if len(sys.argv) > 4 else "zh"
    spd = sys.argv[5] if len(sys.argv) > 5 else "fast"
    run_long_video(text, n_img, orientation=orient, language=lang, speed=spd)
