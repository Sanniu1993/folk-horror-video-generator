"""Step 1 — Story Creation Agent.

Turns a one-line theme into a *structured* story (JSON), never prose. The
structure is what makes downstream image generation stable: characters,
location and a chapter-by-chapter arc are all first-class fields.

We also produce a Style Bible here (the single most valuable part of the demo)
so that every image later shares one visual language instead of looking like
ten different movies.
"""
from __future__ import annotations

from typing import Any

from deepseek_client import DeepSeekClient

SYSTEM = """You are a horror screenwriter and worldbuilding agent specializing in
CHINESE-SET horror. You output ONLY a JSON object, no prose, no markdown fences.

Design a coherent, cinematic horror story from the user's theme, set firmly in a
CHINESE cultural context:
- Characters are Chinese. Give them Chinese names (Chinese characters, e.g. 李梅).
- Locations are Chinese (e.g. a decaying old Chinese state hospital, tube-lit
  corridors, enamel signage, Chinese wall notices).
- Draw on Chinese horror atmosphere (folk beliefs, hungry ghosts, red/white
  funeral motifs) where it fits — tasteful, not stereotyped.

Return this exact schema:
{
  "title": "string (short, evocative)",
  "genre": "string, e.g. psychological horror",
  "logline": "one-sentence summary",
  "characters": [
    {
      "name": "Chinese name in Chinese characters, e.g. 李梅",
      "description": "ENGLISH physical description usable for image prompts. MUST begin with ethnicity, e.g. 'a Chinese woman' / 'an East Asian man'. Include age, hair, clothing, distinguishing features."
    }
  ],
  "location": {
    "main": "Chinese location name (Chinese allowed)",
    "style": "short ENGLISH visual descriptor, e.g. dark abandoned Chinese hospital"
  },
  "style_bible": {
    "visual_style": "e.g. cinematic horror movie",
    "color": "e.g. cold blue and gray",
    "camera": "e.g. 35mm film",
    "lighting": "e.g. low key",
    "texture": "e.g. dark realistic"
  },
  "story_arc": [
    {
      "chapter": 1,
      "title": "string",
      "event": "what concretely happens, 1-2 sentences"
    }
  ]
}

Rules:
- 1 to 3 characters, each with a description stable enough to redraw consistently.
- 6 to 8 chapters forming a clear rising-tension arc ending in a horror climax.
- style_bible must be internally consistent (a single mood, palette, era).
- Language split:
    * title, logline, chapter titles/events, location.main -> Chinese.
    * character.description, location.style, ALL style_bible fields -> ENGLISH
      (these feed the image model directly; SDXL understands English best).
- Every character.description MUST state Chinese / East Asian ethnicity so the
  rendered people look Chinese, not Western.
"""


def generate_story(client: DeepSeekClient, theme: str, temperature: float = 0.8) -> dict[str, Any]:
    user = f"Story theme: {theme}\n\nCreate the structured story now."
    story = client.chat_json(SYSTEM, user, temperature=temperature)
    _validate(story)
    return story


def _validate(story: dict[str, Any]) -> None:
    required = ["title", "characters", "location", "style_bible", "story_arc"]
    missing = [k for k in required if k not in story]
    if missing:
        raise ValueError(f"Story is missing required fields: {missing}")
    if not story["story_arc"]:
        raise ValueError("Story arc is empty.")
