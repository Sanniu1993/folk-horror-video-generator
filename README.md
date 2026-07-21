# AI Horror Story Visual Generator

Input one line of horror theme → automatically produce the **key visual assets**
for a ~10-minute video: a structured story, a storyboard, style-consistent image
prompts, and 10 rendered key frames.

```
用户输入 (theme)
      ↓
   DeepSeek
      ↓
 故事创作 Agent   (story_generator.py)   → story.json + Style Bible
      ↓
 故事分析/分镜 Agent (scene_planner.py)  → scenes.json  (film storyboard)
      ↓
 图片Prompt生成 Agent (prompt_generator.py) → prompts.json (scene + style bible)
      ↓
   ComfyUI (comfy_client.py) + SDXL
      ↓
  10 张关键剧情图片  →  outputs/<run>/images/
```

## Why the middle layers matter

- **No prose, only JSON.** The story is emitted as structured data so downstream
  image generation has stable characters, location and arc to draw from.
- **Scene Planner ≈ storyboard.** We never feed the raw story to the image model.
  Each shot gets a camera, emotion and lighting cue — that's what makes the 10
  frames feel like one directed film.
- **Style Bible = consistency.** After the story is generated we lock a single
  `visual_style / color / camera / lighting / texture`, then append it to *every*
  prompt automatically. Ten frames, one movie.

## Project layout

```
ai-horror-generator/
├── backend/
│   ├── deepseek_client.py     # OpenAI-compatible DeepSeek wrapper (JSON mode)
│   ├── story_generator.py     # Step 1: theme -> structured story + style bible
│   ├── scene_planner.py       # Step 2: story -> storyboard shot list
│   ├── prompt_generator.py    # Step 3: shots -> SDXL prompts (+ style suffix)
│   ├── comfy_client.py        # Step 4: queue workflow, poll, save images
│   ├── pipeline.py            # orchestrator (run this)
│   ├── main.py                # optional FastAPI interface
│   └── workflows/
│       └── sdxl_horror.json   # ComfyUI API-format workflow (text2img)
├── outputs/                   # one timestamped folder per run
├── config.yaml
└── requirements.txt
```

## Setup

```bash
cd ai-horror-generator
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure your DeepSeek key in a project-local .env file:
cp .env.example .env
# then edit .env and set DEEPSEEK_API_KEY=sk-...
```

`.env` is git-ignored and loaded automatically at startup (no need to `export`).
An OS environment variable, if set, still takes precedence over the file.

You also need a running **ComfyUI** with an SDXL checkpoint. Edit `config.yaml`:

- `comfyui.host` / `comfyui.port` — where ComfyUI listens (default `127.0.0.1:8188`).
- The checkpoint name in `backend/workflows/sdxl_horror.json`
  (`ckpt_name`, default `sd_xl_base_1.0.safetensors`) must match a model you have.

## Run

```bash
# Full run (story + storyboard + prompts + rendered images)
python backend/pipeline.py "一个废弃医院里的午夜护士故事"
```

If ComfyUI is not reachable, the run still writes `story.json`, `scenes.json`
and `prompts.json` — you can start ComfyUI and render later.

### Text-only (no image rendering)

```python
from backend.pipeline import run
run("一个废弃医院里的午夜护士故事", render_images=False)
```

### As an HTTP service (optional)

```bash
uvicorn main:app --reload --app-dir backend
curl -X POST localhost:8000/generate \
  -H 'content-type: application/json' \
  -d '{"theme":"一个废弃医院里的午夜护士故事"}'
```

## Reproducibility (V1 success criterion)

Success is **not** one pretty picture — it's that a second run of the same theme
produces comparable, style-consistent output. We support this with:

- A fixed `base_seed` in `config.yaml` (each frame = `base_seed + scene_id`).
- The Style Bible appended identically to every prompt.
- Low temperature for the analysis/prompt agents.

## Model choice

- **V1 uses SDXL** — mature ecosystem (LoRA / ControlNet / IPAdapter) and the best
  target for *learning ComfyUI nodes, control and consistency*.
- **Flux** (`Flux.1 dev/schnell`) is the higher-fidelity upgrade path once the
  pipeline works end to end.

## Roadmap

- **V2** — character consistency via IPAdapter / Reference-Only (generate a
  character sheet first, then condition each frame on it).
- **V3** — image → video (Wan / AnimateDiff).
- **V4** — full agent chain: story → storyboard → image → video → audio →
  subtitles → publish.
