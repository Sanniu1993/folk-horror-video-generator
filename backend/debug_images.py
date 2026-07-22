"""图片调试模式:只按正向描述出图,跳过故事/分镜/prompt 生成。

专门用来快速测试和调试画面问题(服饰、环境、道具、风格、约束)。
输入一句画面描述 + 可选美术设定,直接生成 N 张(不同随机种子的变体),
不产生任何故事情节。中文描述会自动翻译成英文。

    python backend/debug_images.py "深夜江边一个渔夫的侧影" 4
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

# 描述里"图N=角色"的角色关键词 -> IPAdapter 注入权重(人物强、环境弱)
_ROLE_WEIGHT = [
    (("人物", "角色", "主角", "脸", "人"), 0.85),
    (("环境", "背景", "场景", "地点", "地方"), 0.5),
    (("风格", "色调", "画风", "氛围"), 0.6),
    (("道具", "物品", "服饰", "衣服"), 0.65),
]
_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_REF_RE = re.compile(r"图\s*([0-9一二两三四五六七八九十])\s*[是:：=]?\s*([^\s，,。；;、]{0,6})")


def parse_ref_roles(text: str, n_refs: int):
    """从描述中解析'图N=角色',返回(清理后的场景文本, 每张图的权重列表)。"""
    weights = {}
    for m in _REF_RE.finditer(text or ""):
        num = m.group(1)
        idx = _CN.get(num, None) or (int(num) if num.isdigit() else None)
        if not idx:
            continue
        role = m.group(2)
        w = 0.7
        for kws, val in _ROLE_WEIGHT:
            if any(k in role for k in kws):
                w = val
                break
        weights[idx] = w
    clean = _REF_RE.sub("", text or "").strip(" ，,。；;、")
    ordered = [weights.get(i + 1, 0.7) for i in range(n_refs)]
    return clean, ordered

from comfy_client import ComfyClient, RunCancelled
from deepseek_client import DeepSeekClient
from logging_config import setup_logging
from pipeline import Cancelled, ROOT, _slugify, load_config
from prompt_generator import _style_suffix, to_english_fragment

logger = logging.getLogger(__name__)


def run_prompt_images(prompt, num_images=1, on_event=None, should_cancel=None,
                      art_positive="", art_negative="", references=None, style="realistic") -> Path:
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

    art = config.get("art_direction", {})
    # 参考图:先从描述解析"图N=角色"得到权重,并把这些标记从描述里剔除
    references = references or []
    scene_text, ref_weights = parse_ref_roles(prompt or "", len(references))
    refs = [{"path": p, "weight": ref_weights[i]} for i, p in enumerate(references)]
    if refs:
        logger.info("References: %s", [(Path(r["path"]).name, r["weight"]) for r in refs])

    base = to_english_fragment(ds, scene_text.strip())
    art_pos = to_english_fragment(ds, (art_positive or "").strip() or art.get("positive", ""))
    art_neg = to_english_fragment(ds, (art_negative or "").strip() or art.get("negative", ""))
    negative = config["negative_prompt"] + ((", " + art_neg) if art_neg.strip() else "")
    suffix = _style_suffix({}, style)  # 风格档:realistic/horror/none
    positive = ", ".join([base] + ([art_pos] if art_pos.strip() else []) + ([suffix] if suffix else []))

    n = max(1, int(num_images))
    logger.info("=== Debug images | n=%d | prompt=%r ===", n, base[:60])
    logger.info("Full positive: %s", positive[:300])

    run_dir = None
    try:
        check_cancel()
        run_dir = ROOT / config["output_dir"] / f"{datetime.now():%Y%m%d_%H%M%S}_{_slugify(base)}_debug"
        images_dir = run_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        comfy = ComfyClient(config)
        if not comfy.ping():
            raise RuntimeError(f"ComfyUI not reachable at {comfy.base}")

        emit("stage", {"step": 1, "total": 1, "label": "出图"})
        for i in range(n):
            check_cancel()
            seed = gen_cfg["base_seed"] + i
            logger.info("Rendering %d/%d (seed %d)", i + 1, n, seed)
            emit("frame_start", {"scene_id": i + 1, "index": i + 1, "total": n})
            dest = comfy.generate(
                positive=positive, negative=negative, seed=seed,
                width=gen_cfg["width"], height=gen_cfg["height"], gen_cfg=gen_cfg,
                filename_prefix=f"debug_{i:02d}", out_dir=images_dir,
                should_cancel=should_cancel, references=refs,
            )
            emit("frame_done", {"scene_id": i + 1, "index": i + 1, "total": n,
                                "file": f"{run_dir.name}/images/{dest.name}",
                                "meta": {"seed": seed}})
        # 记录用到的完整提示词,方便复盘
        (run_dir / "prompt.txt").write_text(
            f"positive:\n{positive}\n\nnegative:\n{negative}\n", encoding="utf-8")
        emit("done", {"run_dir": run_dir.name})
        logger.info("=== Done: %s ===", run_dir)
        return run_dir
    except (Cancelled, RunCancelled):
        logger.warning("=== Debug images cancelled ===")
        emit("cancelled", {"run_dir": run_dir.name if run_dir else None})
        return run_dir if run_dir else ROOT / config["output_dir"]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python backend/debug_images.py "画面描述" [张数=1]')
        raise SystemExit(1)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    run_prompt_images(sys.argv[1], n)
