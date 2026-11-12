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
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from comfy_client import ComfyClient, RunCancelled
from deepseek_client import DeepSeekClient
from logging_config import setup_logging
from memory_client import is_reachable, make_memory_agent
from pipeline import Cancelled, ROOT, _dump, _slugify, load_config
from prompt_generator import (CJK_COSTUME_GUARD, NO_PEOPLE_NEGATIVE, _style_suffix,
                              anachronism_negative, art_note, ensure_env_shots, env_quotas,
                              front_load_shot_size, has_people, world_tail)
from video_builder import build_long_video, synth_narration, synth_narration_batch

logger = logging.getLogger(__name__)

# --- 中文剧本用的系统提示 ---------------------------------------------------
STYLE_SYSTEM_ZH = """You are a horror art director for a CHINESE folk-horror narration video.
Output ONLY a JSON object, no prose.

From the full narration script, extract a consistent visual bible and the key
recurring characters/places so every shot matches.

Schema:
{
  "world": {"era": "...", "region": "...", "architecture": "...", "wardrobe": "..."},
  "anachronisms": ["..."],
  "characters": [{"name": "中文名/称呼", "description": "ENGLISH, start with ethnicity e.g. 'a Chinese girl', stable, redrawable"}]
}

world = 全片世界设定,从剧本推断,英文关键词(防止服饰/建筑画成现代西式):
  era 具体年代 "1990s" | region 地域+城乡 "rural Guizhou, southwest China"
  architecture 必须写建材 "mud-brick walls, grey tile roofs, wooden lattice windows"
  wardrobe 该年代穿什么,写材质/剪裁/颜色 "worn dark blue cotton work clothes, cloth shoes"
anachronisms = 该年代不该出现的东西,3-8 个英文词(作负向):["smartphone", "modern car", "LED light"]
character.description 必须写明符合该年代的服饰,不能只写 "modern clothes"。

**不要**输出全片统一的画风/色调设定 —— 影调由每一镜按剧情自己决定(见分镜提示)。
Characters are Chinese/East Asian; settings are Chinese. 2-5 characters/places max.
"""

# --- 英文剧本用的系统提示 ---------------------------------------------------
STYLE_SYSTEM_EN = """You are a horror art director for a narration horror video.
Output ONLY a JSON object, no prose.

From the full narration script, extract a consistent visual bible and the key
recurring characters/places so every shot matches.

Schema:
{
  "world": {"era": "...", "region": "...", "architecture": "...", "wardrobe": "..."},
  "anachronisms": ["..."],
  "characters": [{"name": "name/label", "description": "ENGLISH, describe appearance/ethnicity, stable, redrawable"}]
}

world = film-wide setting inferred from the script, English keywords (this is what stops
the model defaulting to modern Western clothing and buildings):
  era specific "1990s" | region place + urban/rural "rural Guizhou, southwest China"
  architecture NAME THE MATERIALS "mud-brick walls, grey tile roofs, wooden shutters"
  wardrobe what people then/there wore — fabric, cut, colour
anachronisms = 3-8 English keywords that must NOT appear for this era (used as negatives).
Every character.description MUST state era-correct clothing, never "modern clothes".

Do NOT output a film-wide look/palette — each shot decides its own (see the shot prompt
instructions). A single global grade makes daytime everyday scenes look like ruins.
Infer characters and settings from the script itself. 2-5 characters/places max.
"""

PROMPT_SYSTEM_ZH = """You are an SDXL prompt engineer for a CHINESE folk-horror video.
Output ONLY a JSON object, no prose.

You get a visual bible, character descriptions, and {n} ordered story segments.
For EACH segment write ONE English image prompt depicting that moment. Weave in
the exact character descriptions when a character appears (keep them consistent).
Keep EACH prompt to ONE concise line: comma-separated visual phrases, ≤45 words.
Do NOT write full sentences or long paragraphs (they overflow and break the output).
Any person is Chinese/East Asian.

每条 prompt **必须以景别开头**(close-up / medium close-up / medium shot / wide shot),
例:"close-up, a Chinese woman in her 30s, ..."。景别写在句尾等于没写 —— SDXL 的构图由
最前面的词决定,实测句尾写了 close-up 仍然出全身站姿、脸只占画面高度 1/10。
有人物的镜头一律用 close-up 或 medium close-up,除非这一镜就是要交代环境。
**这一镜的影调由你按剧情决定**(不再有全片统一的风格设定):每条 prompt 结尾带上
该镜的【时间/光源 + 色调】,3-5 个词即可。要贴合这一段的情节与 mood:
  白天的日常/交代段落 -> "overcast daylight, natural colours, documentary look"
  夜里的不安/悬疑     -> "night, single streetlamp, cold blue shadows"
  惊吓/高潮           -> "harsh flashlight beam, deep black shadows, high contrast"
  回忆/悲伤           -> "soft dim light, muted warm tones"
别把整片都写成阴森暗调 —— 白天就是白天,日常段落用自然光,恐怖感来自内容而非滤镜。
同一地点在不同镜头里的建筑/陈设要保持一致(世界设定已给出),变化的只是光线与情绪。
ONE image = ONE subject doing ONE action in ONE place. 一段旁白讲了好几件事时,只挑
最有画面感的那一个瞬间来画,其余不要塞进同一张图(堆四五个事件会让画面糊成一团)。
人物出现时用中景或近景(medium shot / close-up),别老是全身远景 —— 远景里脸只占几十
像素,画不出五官。
{env_block}Also classify each segment's emotional beat as ONE "mood" from EXACTLY this set:
  calm | reveal | eerie | tension | sad | shock
(calm=setup/neutral, reveal=explaining/showing, eerie=unsettling, tension=dread building,
 sad=grief/memory, shock=scare/climax). Use shock sparingly, only for real jump/peak moments.
Also give "mem_query": 8-16 个中文关键词,用空格分隔,只写【主体 + 环境 + 年代/地域】,
用来去民俗素材库检索这一镜的参考(例:"夜晚楼道 女鬼 长发白裙 90年代 居民楼")。
不要写剧情、情绪或镜头术语,只写画面里看得见的东西。
用通用民俗词汇,不要用方言/地方别称——素材库是按通用词标注的,别称会检索不到
(例:"端公"要写成"道士 做法事";"仙娘"写成"神婆")。
再给 "mem_env":4-8 个中文词,**只写【地点 + 建筑 + 时段】,绝不写人物和事件**
(例:"小区 高层住宅楼 楼下空地 白天")。素材库按场景标注,人和事件混进去会把检索
拉偏——实测某镜混入"人群围观 事故"后,正确的高层住宅素材从 0.58 掉到 0.46 而漏检。

Schema: {{ "prompts": [{{"index": 1, "prompt": "...", "mood": "eerie", "mem_query": "...", "mem_env": "..."}}] }}   // exactly {n} items, index 1..{n}
"""

PROMPT_SYSTEM_EN = """You are an SDXL prompt engineer for a narration horror video.
Output ONLY a JSON object, no prose.

You get a visual bible, character descriptions, and {n} ordered story segments.
For EACH segment write ONE English image prompt depicting that moment. Weave in
the exact character descriptions when a character appears (keep them consistent).
Keep EACH prompt to ONE concise line: comma-separated visual phrases, ≤45 words.
Do NOT write full sentences or long paragraphs (they overflow and break the output).

Each prompt MUST START with the shot size (close-up / medium close-up / medium shot /
wide shot), e.g. "close-up, a Chinese woman in her 30s, ...". A shot size at the END is
ignored — SDXL takes composition from the FIRST tokens; a trailing "close-up" still
produced a full-body figure whose face was 1/10 of the frame height. Use close-up or
medium close-up whenever a person matters in the shot.
**YOU choose the look of each shot** (there is no film-wide style setting any more):
end each prompt with that shot's [time of day / light source + palette], 3-5 words,
matching the beat and its mood:
  everyday daytime exposition -> "overcast daylight, natural colours, documentary look"
  night unease                -> "night, single streetlamp, cold blue shadows"
  scare / climax              -> "harsh flashlight beam, deep black shadows, high contrast"
  memory / grief              -> "soft dim light, muted warm tones"
Do NOT make every shot gloomy — daytime is daytime; the dread comes from content, not a
filter. Keep architecture/props of a recurring place consistent (see WORLD); only the
light and the mood change.
ONE image = ONE subject doing ONE action in ONE place. When a segment covers several
events, pick the single most visual moment; do not cram the rest into the same image.
Frame people as medium shot or close-up, not full-body wide shots — in a wide shot the
face gets only a few dozen pixels and cannot be rendered.
{env_block}Also classify each segment's emotional beat as ONE "mood" from EXACTLY this set:
  calm | reveal | eerie | tension | sad | shock
(calm=setup/neutral, reveal=explaining/showing, eerie=unsettling, tension=dread building,
 sad=grief/memory, shock=scare/climax). Use shock sparingly, only for real jump/peak moments.
Also give "mem_query": 8-16 words, space separated, ONLY [subject + setting + era/region],
used to search a visual reference library for this shot
(e.g. "night stairwell female ghost long black hair 1990s apartment block").
No plot, no emotion, no camera jargon — only what is visible in the frame.
Also give "mem_env": 4-8 words, ONLY [place + building + time of day], never people or
events (e.g. "residential compound high-rise apartment block daytime"). The library is
indexed by scene; mixing people/events in drags the query away from the right material.

Schema: {{ "prompts": [{{"index": 1, "prompt": "...", "mood": "eerie", "mem_query": "...", "mem_env": "..."}}] }}   // exactly {n} items, index 1..{n}
"""


# 空镜(纯环境镜)配额块。单独拎出来是因为 env_shot_ratio=0 时要整段不发 —— 否则会给
# 模型一句"本批必须有 0 镜是空镜"外加一整段怎么挑空镜的说明,自相矛盾还占上下文。
# 先用 (n, n_env) 渲染好再塞进 PROMPT_SYSTEM_* 的 {env_block},里面的花括号不会被二次解析。
ENV_QUOTA_ZH = """
【空镜配额 —— 硬指标,不是建议】
本批 {n} 镜里必须有 **{n_env} 镜是纯环境空镜**:画面里没有任何人,也没有脸/手/背影等
人体部位,prompt 以 wide shot 或 establishing shot 开头。
空镜不是浪费镜位 —— 它交代地点、承担呼吸与转场。上面那几条"人物用近景"的规则只管
有人时怎么拍,不代表每一镜都得有人;实测不加这条配额时 94% 的镜头里都有人,整片一直
怼着脸,观众会疲劳,地点也从头到尾没交代清楚。
挑哪几段做空镜:开场第一镜、地点或时间发生跳跃的那一段、高潮前的那一拍、结尾,以及
旁白只在交代环境/背景/传闻的段落。**绝不要**把有关键动作、惊吓、对话的段落做成空镜。
空镜画什么:就画这一段所在的地点本身 —— 建筑、门窗、陈设、天气、光线,以及人留下的
痕迹(空椅、燃尽的香、翻倒的碗、半开的门、地上的水渍)。把这一镜的 mem_env 那几个词
展开成一个具体画面,一个人都不要出现。空镜同样在结尾带上【时间/光源 + 色调】。
空镜的 mem_query 也只写环境词(与 mem_env 基本一致),不要写人物 —— 否则会把人物素材
的标签检索回来注进一间空房间。
"""

ENV_QUOTA_EN = """
[EMPTY-SHOT QUOTA — a hard requirement, not a suggestion]
Exactly {n_env} of this batch's {n} shots MUST be pure environment shots: NO people in
frame at all, and no body parts either (no face, hands, back of a head, silhouette).
Start those prompts with "wide shot" or "establishing shot".
Empty shots are not wasted frames — they establish place and carry the cuts. The
"frame people tight" rules above govern how to shoot people, not how often people must
appear; without this quota 94% of shots came back with a person in them, so the film
never stops staring at faces and never shows where it takes place.
Pick them from: the opening shot, any segment where the place or time jumps, the beat
just before the climax, the ending, and segments whose narration only sets up
environment/background/rumour. NEVER turn a segment with key action, a scare, or
dialogue into an empty shot.
What to draw: the place itself — building, doors and windows, furnishings, weather,
light, and the traces people left behind (an empty chair, burnt-out incense, an
overturned bowl, a half-open door, a wet patch on the floor). Expand that shot's
mem_env keywords into one concrete frame, with nobody in it. Empty shots still end with
their [time of day / light source + palette].
For an empty shot, mem_query must also be environment-only (essentially the same as
mem_env) — otherwise it retrieves character material and injects those tags into an
empty room.
"""


# 英文默认音色(edge-tts 神经语音):Christopher 低沉沉稳,叙事/恐怖旁白感强。
# 其他可选:en-US-GuyNeural / en-US-BrianNeural(温暖) / en-US-AndrewNeural。
# 分镜图数上限:与 main.py 对请求参数的限制一致(自动补足时也不会越过它)。
MAX_IMAGES = 40

# 每次向 DeepSeek 要多少条分镜提示词。每条要出 prompt/mood/mem_query/mem_env 四个字段,
# 一次要太多必被 max_tokens 截断(实测 5 条 = 5.7k 字符,16384 仍不够)。
PROMPT_BATCH = 6

EN_VOICE_DEFAULT = "en-US-ChristopherNeural"
EN_SAY_VOICE = "Samantha"   # macOS 离线兜底(几乎所有 Mac 都有);仅 edge-tts 彻底失败时才用

# 各非中文语种的 TTS 音色(edge-tts 神经语音 + macOS say 离线兜底)。
# 中文(zh)沿用 config.yaml 里的默认音色,不在此表。
LANG_VOICES = {
    # pitch 可选:某语种要更低沉就加 pitch;缺省用 -2Hz。英文压到 -12Hz(低沉男声)。
    "en": {"voice": EN_VOICE_DEFAULT,      "say": EN_SAY_VOICE, "pitch": "-12Hz"},
    "ja": {"voice": "ja-JP-KeitaNeural",   "say": "Kyoko"},   # 日语男声旁白
    "ko": {"voice": "ko-KR-InJoonNeural",  "say": "Yuna"},    # 韩语男声旁白
}


def _salvage_prompts(raw: str) -> list[dict]:
    """从被截断的 JSON 原文里,用正则抢救出已完整的 {index, prompt[, mood]} 条目。

    截断通常发生在最后一条 prompt 的字符串中间;前面已闭合的对象仍可提取。
    """
    import json as _json
    items: list[dict] = []
    # 匹配每个完整的对象:含 index 和 prompt(mood 可选,顺序不限)
    for m in re.finditer(r'\{[^{}]*?"index"\s*:\s*(\d+)[^{}]*?\}', raw, re.S):
        blob = m.group(0)
        try:
            obj = _json.loads(blob)
        except Exception:
            # 手动抠字段(prompt 里可能有转义)
            pm = re.search(r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', blob)
            if not pm:
                continue
            obj = {"index": int(m.group(1)),
                   "prompt": pm.group(1).encode().decode("unicode_escape", "ignore")}
            mo = re.search(r'"mood"\s*:\s*"(\w+)"', blob)
            if mo:
                obj["mood"] = mo.group(1)
            qm = re.search(r'"mem_query"\s*:\s*"((?:[^"\\]|\\.)*)"', blob)
            if qm:
                obj["mem_query"] = qm.group(1).encode().decode("unicode_escape", "ignore")
            em = re.search(r'"mem_env"\s*:\s*"((?:[^"\\]|\\.)*)"', blob)
            if em:
                obj["mem_env"] = em.group(1).encode().decode("unicode_escape", "ignore")
        items.append(obj)
    return items


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
        # 何时切到下一组:
        #   must  —— 剩下的句子刚好只够填满剩下的组,再不切就有空组(纯按时间切会漏掉
        #            这种情况:比如首句很短、永远跨不过时间阈值,结果所有句子挤进第一组,
        #            3 句要 3 张最后只出 1 张图)。
        #   time  —— 累计时长跨过了本组应占的比例,且剩余句子够填满剩余的组。
        must_split = lines_left <= groups_left
        time_split = cum[i] >= (k + 1) * total / n and lines_left >= groups_left
        if k < n - 1 and (must_split or time_split):
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
    横向: 1920x1080 输出 / 1152x640 出图 (~16:9)
    竖向: 1080x1920 输出 /  640x1152 出图 (~9:16, YouTube Shorts)
    出图分辨率从 768/1344 降到 640/1152(约 0.71× 像素):Mac MPS 下再快约 1.5×,
    经 Ken Burns 2× 放大到 1080p 输出后画质肉眼无差,SDXL 也不至于低分辨率翻车。
    英文剧本会覆盖 TTS 音色。
    """
    vcfg = dict(base_vcfg or {})
    if orientation == "portrait":
        vcfg["out_width"] = 1080
        vcfg["out_height"] = 1920
        img_w, img_h = 640, 1152
    else:
        vcfg["out_width"] = 1920
        vcfg["out_height"] = 1080
        img_w, img_h = 1152, 640

    tts = dict(vcfg.get("tts", {}))
    if language in LANG_VOICES:      # en / ja / ko:切换到对应语种音色
        tts["voice"] = LANG_VOICES[language]["voice"]
        tts["say_voice"] = LANG_VOICES[language]["say"]
        # 该语种指定了 pitch 就用它(如英文 -12Hz 低沉),否则缺省 -2Hz。
        tts["pitch"] = LANG_VOICES[language].get("pitch", "-2Hz")
    # 中文(zh)沿用 config.yaml 里的默认音色,不覆盖。
    vcfg["language"] = language
    # 语速档覆盖 config 里的 rate(fast=默认最快 / slow=长视频稍慢)。
    tts["rate"] = SPEED_RATES.get(speed, SPEED_RATES["fast"])
    vcfg["tts"] = tts
    return vcfg, img_w, img_h


def run_long_video(script: str, num_images: int = 25, on_event=None, should_cancel=None,
                   art_positive="", art_negative="", style="horror",
                   orientation: str = "landscape", language: str = "zh",
                   speed: str = "fast", use_memory=None, review_fn=None) -> Path:
    # use_memory: True/False 显式开关民俗视觉记忆库(覆盖 config.visual_memory.enabled);
    # None 表示沿用 config 里的默认(松耦合:不传就照旧)。
    # review_fn: 可选的人工确认闸口。出完图后调用它(阻塞)拿用户动作:
    #   {"type": "regenerate", "index": 1-based, "note": "改进要求", "prompt": "可选,直接覆盖英文提示词"}
    #   {"type": "approve"} / {"type": "cancel"} -> 放行去合成视频。
    # 不传就完全不进这段,命令行调用与旧行为一致。
    total_steps = 5 if review_fn is not None else 4
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
    # 中文剧本才加民族服饰纠偏(日/韩语剧本要的正是那些服饰,不能压)。
    if language == "zh":
        negative += ", " + CJK_COSTUME_GUARD

    # 民俗视觉记忆库(folk-horror-memory)接入 —— 松耦合、可开关、fail-open。
    # use_memory 显式覆盖 config.enabled;enricher/ref_provider 在关闭时是 no-op,零开销。
    vm_cfg = dict(config.get("visual_memory", {}))
    if use_memory is not None:
        vm_cfg["enabled"] = bool(use_memory)
    mem_context = art_pos
    # 记忆库默认开启,所以先探一次在线状态:没起就整轮关掉,避免每个分镜白发两次请求、
    # 刷一屏 warning。探测失败只是跳过增强,绝不影响出图(fail-open)。
    if vm_cfg.get("enabled"):
        if is_reachable(vm_cfg.get("base_url", "http://127.0.0.1:8100")):
            logger.info("visual-memory ON (base_url=%s, reference_images=%s)",
                        vm_cfg.get("base_url"), bool(vm_cfg.get("use_reference_images")))
        else:
            vm_cfg["enabled"] = False
            msg = (f"民俗视觉记忆库没连上({vm_cfg.get('base_url')}),本次跳过素材增强。"
                   f"要用的话先启动 folk-horror-memory(./run.sh),不影响这次出图。")
            logger.warning("%s", msg)
            emit("notice", msg)
    # 合并版:一次检索同时给 注入片段 / 参考图 / 命中明细(供前端 RAG 调试展开)。
    mem_agent = make_memory_agent(vm_cfg, context=mem_context)

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
        emit("stage", {"step": 1, "total": total_steps, "label": "配音(逐句)"})
        run_dir = ROOT / config["output_dir"] / name
        audio_dir = run_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        # 并发合成:单句 = 网络往返 + 两次 ffmpeg 子进程,本地几乎不吃 CPU。
        # 实测 416 句串行 18.1 分钟(占整条流水线 26%),这段时间纯粹在等网络。
        workers = int(tts_cfg.get("workers", 8) or 1)
        logger.info("配音开始:%d 句,并发 %d 路", len(lines), workers)
        t_tts = time.monotonic()
        synth = synth_narration_batch(
            [(ln, audio_dir / f"l_{i:03d}") for i, ln in enumerate(lines)],
            tts_cfg, workers=workers, should_cancel=should_cancel,
            on_progress=lambda d, t: logger.info("  TTS %d/%d lines", d, t))
        check_cancel()          # 取消时 batch 里未完成的会返回 None,在这里统一收尾
        missing = [i for i, r in enumerate(synth) if r is None]
        if missing:
            raise RuntimeError(f"有 {len(missing)} 句旁白没能合成(首个:第 {missing[0]+1} 句)")
        line_items = [{"text": ln, "audio": synth[i][0], "dur": synth[i][1]}
                      for i, ln in enumerate(lines)]
        logger.info("配音完成:%d 句用时 %.0fs(平均 %.2fs/句)",
                    len(lines), time.monotonic() - t_tts,
                    (time.monotonic() - t_tts) / max(1, len(lines)))
        total_dur = sum(li["dur"] for li in line_items)
        logger.info("Narration total: %.1fs (%.1f min)", total_dur, total_dur / 60)

        # 分镜粒度:一张图覆盖太长的旁白有两个坏处 —— ① LLM 只能把 4-5 个事件塞进一张
        # 图,SDXL 画复合场景必糊;② 运镜幅度是按镜头时长归一化的,摊到几十秒就慢到看不
        # 出在动(实测 80s/张 = 0.25%/秒,而竖屏短片 5.8s/张 = 3.4%/秒)。
        # 所以按"每张图最多覆盖多少秒旁白"自动补足图数。设 0 关闭,完全听用户选的数。
        max_sec_per_img = float(vcfg.get("max_seconds_per_image", 0) or 0)
        if max_sec_per_img > 0 and total_dur > 0:
            need = min(MAX_IMAGES, math.ceil(total_dur / max_sec_per_img))
            if need > num_images:
                if vcfg.get("auto_adjust_frames"):
                    logger.info("分镜数自动提高:%d -> %d 张(旁白 %.0fs,目标每张 ≤%.0fs)",
                                num_images, need, total_dur, max_sec_per_img)
                    num_images = need
                else:
                    # 默认只提示不改:用户选了几张就是几张,不能让出图数量和耗时在
                    # 他不知情的情况下翻几倍。提示发到页面上,别只躺在日志里。
                    msg = (f"旁白共 {total_dur:.0f} 秒 ÷ {num_images} 张 = 每张约 "
                           f"{total_dur / num_images:.0f} 秒。超过 {max_sec_per_img:.0f} 秒后,"
                           f"一张图要覆盖的情节偏多、运镜也会偏慢(长镜头会自动拆成多段缓解)。"
                           f"想更贴合剧情可把分镜数调到 {need} 张左右。")
                    logger.info("分镜粒度提示:%s", msg)
                    emit("notice", msg)

        groups = _group_lines(lines, [li["dur"] for li in line_items], num_images)
        emit("stage", {"step": 2, "total": total_steps, "label": "分镜规划"})

        # 中文剧本用 ZH 版(强制中式设定);其余语种(en/ja/ko)用通用版,
        # 让 LLM 从剧本本身推断人物/场景设定,图像 prompt 仍输出英文。
        non_zh = language != "zh"
        style_system = STYLE_SYSTEM_EN if non_zh else STYLE_SYSTEM_ZH
        user_header_char = "Characters (use verbatim when a character appears):" if non_zh \
            else "角色(出现时按此英文描述):"
        user_header_seg = "Segments (%d total):" if non_zh else "分段(共 %d 段):"

        # max_tokens 给足:风格+世界设定 schema 变大后,推理模型光"思考"就可能吃掉 4096
        # 默认额度,导致返回空内容(实测 finish_reason=length、reasoning_tokens=4096)。
        # 给到 16384:推理模型光"思考"就可能吃掉 8192 而返回空内容,触发一次白跑的重试。
        # max_tokens 是上限不是消耗,给高不额外花钱,但能省掉那次重试。
        bible = ds.chat_json(style_system, "SCRIPT:\n" + script + art_note(art_pos),
                             temperature=0.5, max_tokens=16384)
        # style=auto(默认):全局只留介质词,影调由每镜自带;horror/realistic 仍是固定尾巴。
        suffix = _style_suffix(bible.get("style_bible", {}), style,
                               auto_tail=config.get("base_style_tail"))
        # 世界设定:年代/地域/建筑/服饰。既喂给分镜 LLM(融进每条 prompt),也确定性地
        # 拼进最终 SDXL 提示词兜底;该年代不该有的东西进负向。用户手填的美术设定优先级更高
        # (art_pos 拼在 world 之后),留空时这份自动推导的设定就是唯一的年代约束。
        world = bible.get("world") or {}
        world_tail_str = world_tail(world)
        anachro = anachronism_negative(bible.get("anachronisms"))
        if anachro:
            negative = negative + ", " + anachro
        if world_tail_str:
            logger.info("世界设定: %s", world_tail_str)
            if anachro:
                logger.info("年代负向: %s", anachro)
        chars = "\n".join(f'- {c["name"]}: {c["description"]}' for c in bible.get("characters", []))
        group_texts = [" ".join(lines[i] for i in g) for g in groups]
        world_header = ("WORLD (every shot must match this era/region/architecture/wardrobe):"
                        if non_zh else "世界设定(每一镜的年代/地域/建筑/服饰都必须符合):")
        # **分批**要提示词:每镜要输出 prompt + mood + mem_query + mem_env,一次性要 25-40 条
        # 必然撑爆 max_tokens(实测 5 条就到 5.7k 字符、16384 仍被截断),被截掉的镜头只能
        # 退回拿中文旁白当提示词。分批后单次输出可控,且每批上下文更短、质量更稳。
        # 段落在批内重新编号 1..m,合并时加回偏移量 —— 让模型数小数字不容易错位。
        # 各批之间互相独立,并行发出去 —— 推理模型单批约 90 秒,20 镜 4 批串行要 6 分钟,
        # 并行后约等于最慢的那一批。
        # 空镜配额按全片算好再分摊到各批(末批只剩 1-2 镜时不能让它自己 round,
        # 否则会要求它一半都是空镜)。详见 prompt_generator.env_quotas。
        env_ratio = float(config.get("env_shot_ratio", 0.25))
        quotas = env_quotas(len(group_texts), PROMPT_BATCH, env_ratio)

        def _one_batch(start: int) -> list[dict]:
            chunk = group_texts[start:start + PROMPT_BATCH]
            chunk_user = (
                (f"{world_header}\n{world_tail_str}\n\n" if world_tail_str else "")
                + f"{user_header_char}\n{chars}\n\n"
                + (user_header_seg % len(chunk)) + "\n"
                + "\n".join(f"[{j+1}] {t}" for j, t in enumerate(chunk))
                + art_note(art_pos)
            )
            n_env = quotas[start // PROMPT_BATCH]
            env_block = ((ENV_QUOTA_EN if non_zh else ENV_QUOTA_ZH)
                         .format(n=len(chunk), n_env=n_env) if n_env else "")
            chunk_system = (PROMPT_SYSTEM_EN if non_zh else PROMPT_SYSTEM_ZH).format(
                n=len(chunk), env_block=env_block)
            try:
                res = ds.chat_json(chunk_system, chunk_user, temperature=0.4, max_tokens=12288)
                got = res.get("prompts", [])
            except Exception as exc:
                # 仍被截断:从原文抢救本批已完成的条目,缺的后面回退用原文旁白。
                got = _salvage_prompts(getattr(exc, "raw_content", "") or str(exc))
                logger.warning("分镜 prompt 第 %d 批被截断,抢救到 %d/%d 条",
                               start // PROMPT_BATCH + 1, len(got), len(chunk))
            out: list[dict] = []
            for p in got:                      # 批内 1..m -> 全局序号
                if p.get("index") is not None:
                    p["index"] = int(p["index"]) + start
                    out.append(p)
            return out

        starts = list(range(0, len(group_texts), PROMPT_BATCH))
        check_cancel()
        t_plan = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(4, len(starts))) as pool:
            batches = list(pool.map(_one_batch, starts))
        prompts = [p for b in batches for p in b]
        if not prompts:
            raise RuntimeError("分镜提示词一条都没拿到(DeepSeek 全部批次失败)")
        logger.info("分镜提示词:%d/%d 条(%d 批并行,用时 %.0fs)", len(prompts), len(group_texts),
                    len(starts), time.monotonic() - t_plan)
        # 空镜兜底(确定性):配额写在 system 里靠 LLM 自觉,这里再确认一次 —— 不够就按
        # mem_env 把最静的几镜确定性改写成纯环境镜。理由见 prompt_generator.ensure_env_shots。
        # 必须在 pmap/qmap 之前做:它会同时改写 prompt 和 mem_query。
        rewritten = ensure_env_shots(prompts, env_ratio,
                                     lambda t: to_english_fragment(ds, t))
        env_n = sum(1 for p in prompts if not has_people(p.get("prompt", "")))
        logger.info("空镜 %d/%d(%.0f%%,配额 %.0f%%)%s", env_n, len(prompts),
                    100 * env_n / max(len(prompts), 1), 100 * env_ratio,
                    f";确定性改写分镜 {rewritten}" if rewritten else "")
        if rewritten:
            emit("notice", f"空镜不足配额,已把分镜 {', '.join(map(str, rewritten))} "
                           f"按其环境关键词改写为纯环境空镜。")

        pmap = {p["index"]: p.get("prompt", "") for p in prompts if p.get("index") is not None}
        _VALID_MOODS = {"calm", "reveal", "eerie", "tension", "sad", "shock"}
        # 每段情绪(剧情驱动特效);非法/缺失回退 eerie(恐怖片安全默认)。
        mmap = {p["index"]: (p.get("mood") if p.get("mood") in _VALID_MOODS else "eerie")
                for p in prompts if p.get("index") is not None}
        # 记忆库检索用的精炼关键词(主体+环境+年代);缺失时 memory_client 自己回退。
        qmap = {p["index"]: str(p.get("mem_query") or "").strip()
                for p in prompts if p.get("index") is not None}
        # 纯环境检索词(地点+建筑+时段),与 mem_query 各发一路,详见 memory_client._scene_queries
        emap = {p["index"]: str(p.get("mem_env") or "").strip()
                for p in prompts if p.get("index") is not None}
        _dump(run_dir / "long_plan.json",
              {"world": world,
               "anachronisms": bible.get("anachronisms"),
               "characters": bible.get("characters"),
               "orientation": orientation, "language": language,
               "groups": [{"index": i + 1, "text": t, "prompt": pmap.get(i + 1, ""),
                           "mood": mmap.get(i + 1, "eerie"), "mem_query": qmap.get(i + 1, ""),
                           "mem_env": emap.get(i + 1, "")}
                          for i, t in enumerate(group_texts)]})

        emit("stage", {"step": 3, "total": total_steps, "label": "渲染分镜图"})
        comfy = ComfyClient(config)
        if not comfy.ping():
            raise RuntimeError(f"ComfyUI not reachable at {comfy.base}")
        images_dir = run_dir / "images"
        n = len(groups)
        segments = []
        # 评审阶段要按序号重出单张,所以把每镜头的渲染参数留存下来。
        shots_meta: list[dict[str, Any]] = []
        for i, g in enumerate(groups):
            check_cancel()
            scene_prompt = pmap.get(i + 1) or group_texts[i]
            # 记忆库增强:优先用规划阶段给的精炼 mem_query 检索(整段旁白当 query 会把
            # embedding 拉向"泛中式恐怖",召回弱相关素材);缺失时回退英文分镜 prompt。
            # mem_agent 在记忆库关闭时是 no-op(空片段 / 空参考图),不影响原行为。
            mem_scene = {"scene_id": i + 1, "mem_query": qmap.get(i + 1, ""),
                         "mem_env": emap.get(i + 1, ""),
                         "visual": scene_prompt, "scene_prompt": scene_prompt}
            md = mem_agent(mem_scene)
            extra = (md.get("fragment") or "").strip()
            mem_refs = [{"path": r["path"], "weight": r["weight"]}
                        for r in md.get("ref_images", [])] or None
            # 顺序:镜头本体 → 记忆细节 → 世界设定(年代/建筑/服饰兜底)→ 用户美术设定 → 风格尾巴
            # 兜底:LLM 若把景别写在句尾,确定性地拎到最前面并加权(见 front_load_shot_size)。
            scene_prompt = front_load_shot_size(scene_prompt)
            prompt = ", ".join(
                [scene_prompt] + ([extra] if extra else [])
                + ([world_tail_str] if world_tail_str else [])
                + ([art_pos] if art_pos else []) + [suffix])
            # 纯环境空镜:靠正向写 "no people" 是无效的(扩散模型不理解否定,反而会把人
            # 召唤出来),要真画空必须把人体词放进负向。按镜头本体判断,不看记忆库注入的
            # 标签 —— 本体写的才是这一镜的意图。
            is_env = not has_people(scene_prompt)
            shot_negative = (negative + ", " + NO_PEOPLE_NEGATIVE) if is_env else negative
            logger.info("Rendering image %d/%d%s%s%s", i + 1, n,
                        " [env]" if is_env else "",
                        " [+mem tags]" if extra else "",
                        f" [+{len(mem_refs)} ref]" if mem_refs else "")
            # RAG 调试:把本镜头的检索命中与贡献发给前端(展开显示)。
            if md.get("enabled"):
                emit("memory_debug", {
                    "scene_id": i + 1, "index": i + 1,
                    "query": md.get("query", ""),
                    "hits": md.get("hits", []),
                    "injected_tags": md.get("injected_tags", []),
                    "fragment": extra,
                    "ref_images": [{"title": r.get("title"), "score": r.get("score"),
                                    "weight": r.get("weight"), "url": r.get("url")}
                                   for r in md.get("ref_images", [])],
                    "final_prompt": prompt,
                })
            emit("frame_start", {"scene_id": i + 1, "index": i + 1, "total": n})
            dest = comfy.generate(
                positive=prompt, negative=shot_negative,
                seed=gen_cfg["base_seed"] + i,
                width=img_w, height=img_h,
                gen_cfg=gen_cfg, filename_prefix=f"img_{i:03d}",
                out_dir=images_dir, should_cancel=should_cancel,
                references=mem_refs,
            )
            emit("frame_done", {"scene_id": i + 1, "index": i + 1, "total": n,
                                "file": f"{run_dir.name}/images/{dest.name}",
                                "prompt": prompt})
            segments.append({"image": dest, "lines": [line_items[j] for j in g],
                             "effect": mmap.get(i + 1, "eerie")})
            shots_meta.append({"scene_id": i + 1, "scene_prompt": scene_prompt,
                               "memory_fragment": extra, "refs": mem_refs,
                               "prompt": prompt, "note": "", "attempt": 0,
                               "file": f"{run_dir.name}/images/{dest.name}"})

        # --- 可选:人工确认闸口 ---------------------------------------------
        # 出完图先停下来给人看,可以带"改进要求"单张重出,确认无误再合成视频。
        # review_fn 由调用方(web 层)提供:阻塞等待用户操作,返回一个动作 dict。
        # 不传 review_fn 时这段完全不执行,命令行/旧调用方行为不变。
        if review_fn is not None:
            emit("stage", {"step": 4, "total": total_steps, "label": "人工确认分镜图"})
            while True:
                check_cancel()
                action = review_fn(shots_meta) or {"type": "approve"}
                kind = action.get("type")
                if kind != "regenerate":
                    break                      # approve / cancel / 未知动作都视为放行
                idx = int(action.get("index", 0))
                if not 1 <= idx <= n:
                    logger.warning("重出请求的分镜序号越界:%s", idx)
                    continue
                meta = shots_meta[idx - 1]
                meta["attempt"] += 1
                custom = (action.get("prompt") or "").strip()
                note = (action.get("note") or "").strip()
                if custom:
                    new_prompt = custom          # 用户直接改了英文 prompt,原样用
                    meta["note"] = note
                else:
                    # 改进要求(中文也行)翻成英文,插在镜头本体之后、其余修饰之前,
                    # 靠前的位置权重更高,才压得住原来的画面。
                    meta["note"] = note
                    note_en = to_english_fragment(ds, note) if note else ""
                    new_prompt = ", ".join(
                        [front_load_shot_size(meta["scene_prompt"])]
                        + ([note_en] if note_en else [])
                        + ([meta["memory_fragment"]] if meta["memory_fragment"] else [])
                        + ([world_tail_str] if world_tail_str else [])
                        + ([art_pos] if art_pos else []) + [suffix])
                logger.info("重出分镜 %d(第 %d 次)%s", idx, meta["attempt"],
                            f",改进要求:{note}" if note else "(仅换种子)")
                emit("stage", {"step": 4, "total": total_steps,
                               "label": f"重出分镜 #{idx}(第 {meta['attempt']} 次)"})
                emit("frame_start", {"scene_id": idx, "index": idx, "total": n})
                # 重出也要沿用这一镜的负向:空镜若丢了 no-person 负向,重出一次人就回来了。
                # 用户手改了 prompt 时按新 prompt 判断(他可能就是要把人加回来)。
                re_negative = negative
                if not has_people(custom or meta["scene_prompt"]):
                    re_negative = negative + ", " + NO_PEOPLE_NEGATIVE
                # 换种子,否则同样的 prompt 会画出几乎一样的图;文件名带版本号避免浏览器缓存。
                dest = comfy.generate(
                    positive=new_prompt, negative=re_negative,
                    seed=gen_cfg["base_seed"] + (idx - 1) + meta["attempt"] * 1000,
                    width=img_w, height=img_h,
                    gen_cfg=gen_cfg,
                    filename_prefix=f"img_{idx - 1:03d}_v{meta['attempt']}",
                    out_dir=images_dir, should_cancel=should_cancel,
                    references=meta["refs"],
                )
                meta["prompt"] = new_prompt
                meta["file"] = f"{run_dir.name}/images/{dest.name}"
                segments[idx - 1]["image"] = dest
                emit("frame_done", {"scene_id": idx, "index": idx, "total": n,
                                    "file": meta["file"], "prompt": new_prompt,
                                    "replace": True})
                emit("stage", {"step": 4, "total": total_steps, "label": "人工确认分镜图"})
            # 评审期间点了停止:review_fn 会返回 cancel 让上面的循环退出,
            # 这里必须再查一次,否则会带着"取消"状态继续去合成视频。
            check_cancel()

        emit("stage", {"step": total_steps, "total": total_steps, "label": "合成长片(字幕+配音)"})
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
