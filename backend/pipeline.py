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
from memory_client import make_enricher, make_ref_provider
from prompt_generator import (CJK_COSTUME_GUARD, _style_suffix, anachronism_negative,
                              generate_prompts, to_english_fragment)
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
    art_positive: str = "",
    art_negative: str = "",
    style: str = "horror",
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

    art = config.get("art_direction", {})
    art_pos = (art_positive or "").strip() or art.get("positive", "")
    art_neg = (art_negative or "").strip() or art.get("negative", "")

    frames = int(num_frames) if num_frames else gen_cfg["num_key_frames"]
    logger.info(
        "=== Run start | theme=%r | render=%s | frames=%d | char_consistency=%s ===",
        theme, render_images, frames, character_consistency,
    )
    t0 = time.monotonic()
    ds = DeepSeekClient(ds_cfg)
    # Chinese art-direction input is auto-translated to English so it works in
    # both the DeepSeek planning and the SDXL prompt/negative layers.
    art_pos = to_english_fragment(ds, art_pos)
    art_neg = to_english_fragment(ds, art_neg)
    # 文本模式的故事按设计就是中式背景,民族服饰纠偏无条件加(见 CJK_COSTUME_GUARD 注释)。
    negative = (config["negative_prompt"] + ", " + CJK_COSTUME_GUARD
                + ((", " + art_neg) if art_neg.strip() else ""))
    run_dir: Path | None = None

    try:
        check_cancel()

        # --- Step 1: structured story + style bible ----------------------
        logger.info("[1/4] Story creation for theme: %r", theme)
        emit("stage", {"step": 1, "total": 4, "label": "故事创作"})
        story = generate_story(ds, theme, temperature=ds_cfg.get("temperature", 0.8),
                               art_positive=art_pos)

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
            art_positive=art_pos,
        )
        _dump(run_dir / "scenes.json", {"scenes": scenes})
        logger.info("      -> %d scenes", len(scenes))
        emit("scenes", scenes)
        check_cancel()

        # --- Step 3: image prompts (scene + fixed style bible) -----------
        logger.info("[3/4] Prompt generation")
        emit("stage", {"step": 3, "total": 4, "label": "Prompt 生成"})
        # 可选:接入视觉记忆库,给每个分镜注入检索到的民俗视觉细节。
        # 未配置或 enabled=false 时 make_enricher 返回 no-op,行为与原来一致(松耦合)。
        vm_cfg = config.get("visual_memory", {})
        mem_context = " ".join(
            str(x) for x in (art_pos, story.get("setting", ""), story.get("era", "")) if x
        )
        enrich_fn = make_enricher(vm_cfg, context=mem_context)
        prompts = generate_prompts(
            ds, story, scenes, temperature=ds_cfg.get("analysis_temperature", 0.4),
            art_positive=art_pos, style=style, enrich_fn=enrich_fn,
        )
        if vm_cfg.get("enabled"):
            hits = sum(1 for p in prompts if p.get("memory_fragment"))
            logger.info("      visual-memory enrichment on: %d/%d scenes enriched",
                        hits, len(prompts))
        # 该年代不该出现的东西进负向(story.world 同源);story 没给就保持原负向。
        anachro = anachronism_negative(story.get("anachronisms"))
        if anachro:
            negative = negative + ", " + anachro
            logger.info("      年代负向: %s", anachro)
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
                    comfy, story, run_dir, gen_cfg, config, should_cancel, emit, style
                )
        # Which characters appear in each scene (for picking a reference).
        chars_by_scene = {sc["scene_id"]: sc.get("characters", []) for sc in scenes}

        # V2 可选:把记忆库检索到的图当 IPAdapter 参考图。按 scene 预取(内容更全),
        # 未启用 use_reference_images 时 provider 是 no-op,scene_refs 全空(松耦合、fail-open)。
        ref_provider = make_ref_provider(vm_cfg, context=mem_context)
        scene_refs = {sc["scene_id"]: ref_provider(sc) for sc in scenes}
        if vm_cfg.get("use_reference_images"):
            n_ref = sum(1 for v in scene_refs.values() if v)
            logger.info("      visual-memory reference images on: %d/%d scenes have refs",
                        n_ref, len(scenes))

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
            mem_refs = scene_refs.get(p["scene_id"]) or None
            try:
                dest = comfy.generate(
                    positive=p["prompt"],
                    negative=negative,
                    seed=seed,
                    width=gen_cfg["width"],
                    height=gen_cfg["height"],
                    gen_cfg=gen_cfg,
                    filename_prefix=prefix,
                    out_dir=images_dir,
                    should_cancel=should_cancel,  # interrupt mid-render if asked
                    reference_image=ref,           # 角色一致性参考(V1 特性)
                    references=mem_refs,            # V2:记忆库参考图(存在则优先走多图 IPAdapter 链)
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
    style: str = "horror",
) -> dict[str, Path]:
    """Render one clean portrait per character to use as an IPAdapter reference."""
    refs: dict[str, Path] = {}
    chars_dir = run_dir / "characters"
    suffix = _style_suffix(story.get("style_bible", {}), style,
                           auto_tail=config.get("base_style_tail"))
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
