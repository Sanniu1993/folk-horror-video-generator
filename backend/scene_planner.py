"""Step 2 — Scene Planner Agent (the important middle layer).

We never hand a raw story straight to the image model. Instead this agent acts
like a film's storyboard department: it breaks the arc into discrete *shots*,
each with a camera, emotion and lighting cue. That shot list is what gives the
final 10 frames a directed, cinematic feel instead of ten unrelated pictures.
"""
from __future__ import annotations

import json
from typing import Any

from deepseek_client import DeepSeekClient

SYSTEM = """You are a film storyboard / cinematography agent.
You output ONLY a JSON object, no prose, no markdown fences.

Given a structured horror story, produce a shot list of exactly {n} key frames
that together tell the story as a {duration}-minute short film. Spread the
shots across the whole arc (establish -> escalate -> climax -> aftermath).

Return this schema:
{{
  "scenes": [
    {{
      "scene_id": 1,
      "chapter": 1,
      "time": "00:00",
      "purpose": "narrative/emotional job of this shot",
      "characters": ["names present, or empty list"],
      "visual": "concrete description of what is on screen (English)",
      "camera": "e.g. wide shot / close-up / over-the-shoulder / dutch angle",
      "emotion": "e.g. dread, fear, unease",
      "lighting": "e.g. cold blue moonlight, single flickering bulb"
    }}
  ]
}}

Rules:
- Exactly {n} scenes, scene_id from 1..{n}, time codes ascending across {duration} minutes.
- The first shot establishes the world; the peak shot is the horror climax.
- "visual" must be self-contained and shot-specific (no "he enters" — describe the frame).
- Only reference characters that exist in the story; use their exact names.
- This is a CHINESE-set story: write "visual" in English, but any person is
  Chinese / East Asian and any environment is a Chinese setting.
"""


def plan_scenes(
    client: DeepSeekClient,
    story: dict[str, Any],
    num_frames: int = 10,
    duration_minutes: int = 10,
    temperature: float = 0.4,
    art_positive: str = "",
) -> list[dict[str, Any]]:
    from prompt_generator import art_note
    system = SYSTEM.format(n=num_frames, duration=duration_minutes)
    user = (
        "Structured story:\n"
        + json.dumps(story, ensure_ascii=False, indent=2)
        + f"\n\nProduce exactly {num_frames} key-frame scenes now."
        + art_note(art_positive)
    )
    result = client.chat_json(system, user, temperature=temperature)
    scenes = result.get("scenes", [])
    if len(scenes) != num_frames:
        # Trim or pad-by-warning rather than hard-fail; the pipeline can still run.
        print(
            f"[scene_planner] warning: expected {num_frames} scenes, got {len(scenes)}."
        )
        scenes = scenes[:num_frames]
    return scenes
