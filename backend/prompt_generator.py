"""Step 3 — Image Prompt Generation Agent.

Converts each storyboard shot into an SDXL-ready prompt, then *deterministically*
appends the Style Bible so all frames share one visual language. Character
descriptions are injected verbatim so faces/clothing stay recognizable across
frames (V1 consistency; V2 upgrades this with IPAdapter reference images).
"""
from __future__ import annotations

import json
from typing import Any

from deepseek_client import DeepSeekClient

SYSTEM = """You are an SDXL prompt engineer for cinematic horror imagery.
You output ONLY a JSON object, no prose, no markdown fences.

For each input scene, write a vivid English image prompt describing ONLY the
scene content (subject, setting, action, framing, mood). Do NOT include global
style words like "cinematic", "35mm", color grading, or quality tags — those
are appended automatically later. Weave in the exact character descriptions
provided so recurring characters look consistent.

This is a CHINESE-set horror story. Every person must be explicitly described as
Chinese / East Asian, and every environment as a Chinese setting, so the image
model does not default to Western faces or places.

Return this schema:
{
  "prompts": [
    { "scene_id": 1, "prompt": "scene-only image prompt" }
  ]
}
"""


import re as _re

_CJK = _re.compile(r"[一-鿿]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK.search(s or ""))


def to_english_fragment(client, text: str) -> str:
    """If the text contains Chinese, translate it into a concise English,
    comma-separated prompt fragment (so it works in the SDXL layer). English or
    empty input is returned unchanged. One cheap DeepSeek call when needed."""
    text = (text or "").strip()
    if not text or not _has_cjk(text):
        return text
    try:
        res = client.chat_json(
            "你把用户给的画面/美术约束翻译成简洁的英文关键词短语(逗号分隔),"
            "供图像模型提示词使用。保留具体的年代/服饰/环境/道具信息。"
            '只输出 JSON:{"english": "..."}',
            text,
            temperature=0.2,
            max_tokens=512,
        )
        return (res.get("english") or text).strip()
    except Exception:
        return text  # fall back to the original if translation fails


def art_note(art_positive: str) -> str:
    """Instruction block injecting the user's art direction into a DeepSeek call."""
    art_positive = (art_positive or "").strip()
    if not art_positive:
        return ""
    return (
        "\n\n【美术设定 / ART DIRECTION — 必须严格遵守，融入每个角色描述、场景与提示词，"
        "年代/服饰/建筑/道具都要符合】\n" + art_positive
    )


# 可选风格档:控制拼到每张图末尾的风格尾巴。
#   horror  -> 用故事自动生成的画风 + 电影恐怖尾巴(默认,保持原行为)
#   realistic -> 纪实摄影,不加恐怖调,适合"80年代农民/护士"等通用/年代题材
#   none    -> 不加任何风格尾巴,纯 prompt(+ 美术设定)
STYLE_PROFILES = {
    "horror": None,  # None 表示走下面的 style_bible 逻辑
    "realistic": ("documentary photograph, vintage color film photo, natural lighting, "
                  "candid, realistic, highly detailed, sharp focus"),
    "none": "",
}


def _style_suffix(style: dict[str, Any], profile: str = "horror") -> str:
    """The fixed tail appended to every prompt. `profile` picks the style档:
    'horror' uses the story's Style Bible (default), others use a fixed suffix."""
    override = STYLE_PROFILES.get(profile, None)
    if override is not None:      # realistic / none / 自定义
        return override
    parts = [
        style.get("visual_style", "cinematic horror movie"),
        style.get("color", "cold blue and gray"),
        style.get("camera", "35mm film"),
        style.get("lighting", "low key lighting"),
        style.get("texture", "dark realistic"),
        "highly detailed, film grain, dramatic atmosphere, masterpiece",
    ]
    return ", ".join(p for p in parts if p)


def generate_prompts(
    client: DeepSeekClient,
    story: dict[str, Any],
    scenes: list[dict[str, Any]],
    temperature: float = 0.4,
    art_positive: str = "",
    style: str = "horror",
) -> list[dict[str, Any]]:
    characters = story.get("characters", [])
    char_block = "\n".join(f'- {c["name"]}: {c["description"]}' for c in characters)

    user = (
        "Characters (use these descriptions verbatim when a character appears):\n"
        f"{char_block or '(none)'}\n\n"
        "Scenes:\n"
        + json.dumps(scenes, ensure_ascii=False, indent=2)
        + "\n\nWrite one scene-only prompt per scene now."
        + art_note(art_positive)
    )
    result = client.chat_json(SYSTEM, user, temperature=temperature)

    raw = {p["scene_id"]: p["prompt"] for p in result.get("prompts", [])}
    suffix = _style_suffix(story.get("style_bible", {}), style)
    art = (art_positive or "").strip()

    final: list[dict[str, Any]] = []
    for scene in scenes:
        sid = scene["scene_id"]
        scene_prompt = raw.get(sid, scene.get("visual", ""))
        # SDXL fallback: append the art direction to the final prompt as a guarantee.
        parts = [scene_prompt] + ([art] if art else []) + [suffix]
        final.append(
            {
                "scene_id": sid,
                "prompt": ", ".join(parts),
                "scene_prompt": scene_prompt,
                "meta": {
                    "chapter": scene.get("chapter"),
                    "time": scene.get("time"),
                    "purpose": scene.get("purpose"),
                    "camera": scene.get("camera"),
                    "emotion": scene.get("emotion"),
                },
            }
        )
    return final
