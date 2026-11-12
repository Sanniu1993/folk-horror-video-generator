"""Step 3 — Image Prompt Generation Agent.

Converts each storyboard shot into an SDXL-ready prompt, then *deterministically*
appends the Style Bible so all frames share one visual language. Character
descriptions are injected verbatim so faces/clothing stay recognizable across
frames (V1 consistency; V2 upgrades this with IPAdapter reference images).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

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


# 世界设定(年代/地域/服饰/建筑)。这四项是"服饰、建筑不匹配"的直接抓手:
# 不写死年代和建材,SDXL 会按训练分布默认画现代西式;而 config 的 art_direction
# 默认是空的,等于全片没有任何年代约束。故让规划阶段从剧本自动推一份出来,
# 再确定性地拼进每一张图(用户手填的 art_direction 仍然叠加在后面、优先级更高)。
# 各字段的词数预算。architecture / wardrobe 给得宽,它们正是"建筑、服饰画错"的抓手,
# 裁太狠会把 "wooden lattice windows"、"cloth shoes" 这类决定性细节丢掉;
# era / region 本来就是短语,给窄的即可。合计 ~26 词,不至于压过镜头本体。
_WORLD_FIELDS = {"era": 4, "region": 6, "architecture": 9, "wardrobe": 9}

# 中式设定的"民族服饰纠偏"。SDXL 一见 "Chinese old man / folk exorcist / priest" 这类词,
# 就会按训练分布画成和服式交领束腰长袍——实测某镜提示词明明写的是 "dark cotton jacket
# and cloth shoes",出来的却是一身黑色和服。提示词写对了也压不住,只能从负向堵。
# 只列日/韩特有的服饰词:不 negate 泛指的 "robe",免得真要画道袍/寿衣时被一起压掉。
# 【2026-08-05 补帽子词】原来这份只管"衣服",实测有漏:同一条 prompt 带上 guard 后袍子
# 从交领改对成圆领中式蓝袍了,**帽子仍然是韩式笠帽**(宽平檐 + 圆顶 + 红缨帽带),
# 因为 guard 里一个帽子名都没有。补上 gat / jeonmo / Korean hat 之后出的是清代瓜皮帽
# 加红顶缨,对了。
# 仍然守住"只列日韩特有词"这条:**不能** negate "conical hat / straw hat" —— 斗笠是
# 中国农民、挑夫、赶尸匠的正当装束,压掉就画不出来了。
CJK_COSTUME_GUARD = ("kimono, yukata, hakama, obi sash, samurai, geisha, "
                     "Japanese clothing, jingasa, "
                     "hanbok, Korean clothing, gat, jeonmo, Korean hat")


def world_tail(world: Optional[dict[str, Any]]) -> str:
    """把 world 的四个字段压成一小段英文关键词;缺字段自动跳过。"""
    if not world:
        return ""
    parts = [_trim_style_field(world.get(f), budget) for f, budget in _WORLD_FIELDS.items()]
    return ", ".join(p for p in parts if p)


def anachronism_negative(items: Optional[list[Any]], limit: int = 10) -> str:
    """把"该年代不该出现的东西"拼成负向片段(英文关键词,去重保序)。"""
    seen: set[str] = set()
    out: list[str] = []
    for it in (items or []):
        t = str(it).strip().strip(",")
        if t and t.lower() not in seen and not _has_cjk(t):
            seen.add(t.lower())
            out.append(t)
        if len(out) >= limit:
            break
    return ", ".join(out)


# 景别词。SDXL 的构图由**提示词最前面**的 token 决定,写在末尾几乎没有权重 ——
# 实测某镜提示词末尾明确写了 "close-up",出来仍是全身站姿、脸只占画面高度 1/10。
# 所以把景别从句尾拎到句首,并稍加权重。ComfyUI 的 CLIPTextEncode 支持 (词:权重)。
_SHOT_SIZES = (
    "extreme close-up", "extreme closeup", "medium close-up", "medium closeup",
    "close-up portrait", "close-up", "closeup", "head and shoulders",
    "medium wide shot", "medium shot", "waist-up", "half body",
    "wide shot", "full body shot", "full-body shot", "long shot", "establishing shot",
)


def front_load_shot_size(prompt: str, weight: float = 1.25) -> str:
    """把景别词从提示词里挪到最前面并加权;没写景别就原样返回。"""
    parts = [p.strip() for p in str(prompt or "").split(",")]
    for i, part in enumerate(parts):
        low = part.lower()
        for size in _SHOT_SIZES:          # 长的在前,优先匹配 "medium close-up" 而非 "close-up"
            if low == size or low.startswith(size + " ") or low.endswith(" " + size):
                rest = [p for j, p in enumerate(parts) if j != i and p]
                return ", ".join([f"({size}:{weight:g})"] + rest)
    return prompt


# ---- 空镜(纯环境镜)------------------------------------------------------
# 实测统计最近 8 轮共 125 个分镜:94% 的画面里有人,真正的纯环境空镜只剩 3%。
# 原因不在模型,在提示词:上面那几条景别规则只管"有人时怎么拍"(一律近景),从没规定
# "多少镜可以没人";而每镜又和一段讲人的旁白一一对应 —— LLM 收到的净信号就是
# "人物 + 近景"。空镜承担交代地点、呼吸与转场,全片每镜都怼着脸会让观众疲劳,环境也
# 始终没交代清楚,而且实拍环境素材完全无处可用。
# 治法分两层:规划阶段下硬配额(见 long_video.PROMPT_SYSTEM_*),出图前再做确定性兜底。

# 判断"这条 prompt 里有人"。**宁可多判不可漏判**:多判(没人的镜被当成有人)只是少加
# 一条负向,无害;漏判会给真要画人的镜头加上 no-person 负向,直接和正向打架。
_PEOPLE_WORDS = """
man|men|woman|women|male|female|boy|girl|child|children|kid|baby|infant|toddler|
youth|youths|teenager|teenagers|adult|adults|elderly|old man|old woman|lady|gentleman|
person|people|human|figure|figures|silhouette|silhouettes|crowd|couple|family|
resident|residents|neighbour|neighbours|neighbor|neighbors|tenant|tenants|
villager|villagers|farmer|worker|workers|soldier|policeman|officer|cop|guard|driver|
passenger|nurse|doctor|patient|teacher|student|schoolgirl|schoolboy|servant|maid|
monk|priest|priestess|taoist|shaman|exorcist|witch|nun|beggar|stranger|visitor|guest|
bride|groom|widow|mourner|mourners|
mother|father|son|daughter|husband|wife|brother|sister|uncle|aunt|cousin|twins|
grandmother|grandfather|grandma|grandpa|
ghost|apparition|corpse|body|bodies|skeleton|skull|
face|faces|portrait|head|hair|eye|eyes|hand|hands|arm|arms|leg|legs|foot|feet|
shoulder|shoulders|finger|fingers|mouth|teeth|skin|pregnant
"""
_PEOPLE_RE = _re.compile(
    r"\b(" + "|".join(w for w in (x.strip() for x in _PEOPLE_WORDS.split("|")) if w) + r")\b",
    _re.I)

# 扩散模型不理解正向里的否定("no people"反而会把人召唤出来),要压人只能走负向。
NO_PEOPLE_NEGATIVE = ("person, people, human, man, woman, child, face, portrait, "
                      "hands, figure, silhouette, crowd, body")


def _strip_shot_sizes(text: str) -> str:
    """先摘掉景别词再判有没有人 —— 否则 "head and shoulders"、"full body shot"、
    "medium shot" 这些景别本身就带 head/body/medium,每条 prompt 都会被判成有人。"""
    t = str(text or "")
    for size in _SHOT_SIZES:
        t = _re.sub(_re.escape(size), " ", t, flags=_re.I)
    return t


def has_people(prompt: str) -> bool:
    """这条 prompt 的画面里是否有人(或人体部位)。"""
    return bool(_PEOPLE_RE.search(_strip_shot_sizes(prompt)))


def env_quotas(total: int, batch: int, ratio: float = 0.25) -> list[int]:
    """把全片空镜配额按批分摊,返回每批要求的空镜数。

    分镜提示词是分批要的(见 long_video.PROMPT_BATCH),配额只能写在每批的 system 里。
    但不能让每批各自 round(自己的镜数 × ratio):末批常常只剩 1-2 镜,那样会要求它
    50% 都是空镜。所以先按全片算总数,再轮转分摊,单批上限为批内镜数的一半。
    """
    sizes = [min(batch, total - s) for s in range(0, total, batch)]
    if not sizes:
        return []
    caps = [max(1, s // 2) for s in sizes]
    quotas = [0] * len(sizes)
    want = round(total * ratio)
    k = 0
    while want > 0 and any(q < c for q, c in zip(quotas, caps)):
        if quotas[k] < caps[k]:
            quotas[k] += 1
            want -= 1
        k = (k + 1) % len(sizes)
    return quotas


# 空镜候选的 mood 白名单。shock 是惊吓/高潮那一击,改成空镜等于把全片最重要的画面扔了。
_ENV_SAFE_MOODS = ("calm", "reveal", "sad", "eerie", "tension")
_ENV_MOOD_ORDER = {"calm": 0, "reveal": 1, "sad": 2, "eerie": 3, "tension": 4}

_LIGHT_RE = _re.compile(r"light|lit|lamp|dusk|dawn|night|daylight|daytime|sun|moon|"
                        r"shadow|tone|colour|color|palette|overcast|fog|contrast|documentary",
                        _re.I)


def _light_tail(prompt: str) -> str:
    """取 prompt 结尾那段【时间/光源 + 色调】(规划阶段要求写在结尾)。

    改写成空镜时沿用它:同一段的影调不该因为换成空镜就变，否则剪在一起会跳。
    """
    parts = [p.strip() for p in str(prompt or "").split(",") if p.strip()]
    if parts and _LIGHT_RE.search(parts[-1]) and not has_people(parts[-1]):
        return parts[-1]
    return ""


def ensure_env_shots(prompts: list[dict[str, Any]], ratio: float = 0.25,
                     translate: Optional[Callable[[str], str]] = None) -> list[int]:
    """确定性兜底:空镜不够配额,就把最合适的几镜改写成纯环境镜。返回被改写的 index。

    规划阶段的配额靠 LLM 自觉,而实测那 4 条"人物用近景"的规则说明**光靠劝没用**;
    所以这里像 front_load_shot_size 一样做一次不依赖模型的确认。

    改写材料用这一镜自己的 `mem_env`(地点 + 建筑 + 时段)—— 它本来就是为环境检索准备
    的、一直在产出,只是从没变成过一张属于自己的图。中文 mem_env 交给 translate 翻成英文
    (通常只要 0~2 次调用);没给 translate 或翻译失败就跳过这镜,不硬塞中文进 SDXL。

    不做的事:不动 mood=shock 的镜;不让两个空镜挨着(观感是"卡住了")。
    """
    items = sorted((p for p in prompts if p.get("index") is not None),
                   key=lambda p: int(p["index"]))
    if not items:
        return []
    want = round(len(items) * ratio)
    env_idx = {int(p["index"]) for p in items if not has_people(p.get("prompt", ""))}
    need = want - len(env_idx)
    if need <= 0:
        return []

    cands = [p for p in items
             if int(p["index"]) not in env_idx
             and p.get("mood") in _ENV_SAFE_MOODS
             and str(p.get("mem_env") or "").strip()]
    # 越"静"的段落越适合让位给空镜;同 mood 时 mem_env 写得越具体的优先(词多=信息全)。
    cands.sort(key=lambda p: (_ENV_MOOD_ORDER.get(p.get("mood"), 9),
                              -len(str(p["mem_env"]).split())))
    changed: list[int] = []
    picked_env: list[set[str]] = []
    for p in cands:
        if need <= 0:
            break
        i = int(p["index"])
        if (i - 1) in env_idx or (i + 1) in env_idx:
            continue
        # 同一部片里两张几乎一样的空镜(实测"农村 土房 室内 白天"和"农村 土坯房 堂屋 白天"
        # 会同时被选中),观众一眼看出是在凑镜头。词集重合过半就跳过,换下一个候选。
        words = set(str(p["mem_env"]).split())
        if any(len(words & w) * 2 > len(words | w) for w in picked_env):
            continue
        env_en = str(p["mem_env"]).strip()
        if _has_cjk(env_en):
            env_en = (translate(env_en) or "").strip() if translate else ""
            if not env_en or _has_cjk(env_en):
                continue
        tail = _light_tail(p.get("prompt", ""))
        p["prompt"] = ", ".join(x for x in ("wide shot", env_en, tail) if x)
        # 画面换成纯环境,检索也得跟着换:否则会把人物素材的标签(长发白裙…)注进空房间。
        p["mem_query"] = str(p["mem_env"]).strip()
        picked_env.append(words)
        env_idx.add(i)
        changed.append(i)
        need -= 1
    return changed


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
# style=auto(默认)时拼在每张图末尾的东西。风格改为「跟随剧情」后,全局层面只保留
# **与情绪无关的介质/画质**词:光线、色调、景别这些由每一镜自己根据剧情决定,
# 全片统一一个调子会让白天的日常镜头也套上恐怖片的暗调(实测把小区门口围观人群
# 画成了废墟)。config 的 base_style_tail 可覆盖,留空则一个字都不加。
AUTO_STYLE_TAIL = "photorealistic, film grain, highly detailed"

STYLE_PROFILES = {
    "auto": AUTO_STYLE_TAIL,   # 默认:风格跟随剧情,全局只留介质词
    "horror": None,  # None 表示走下面的 style_bible 逻辑
    "realistic": ("documentary photograph, vintage color film photo, natural lighting, "
                  "candid, realistic, highly detailed, sharp focus"),
    "none": "",
}


# 风格字段瘦身:每个字段只保留第一个分句、且不超过这么多词。
# 为什么:LLM 常把 style bible 写成整句甚至带分号的排比(如 camera 写成
# "handheld, low-angle shots, claustrophobic framing in stairwells; slow zooms
# into dark doorways; use of long takes to build dread"),拼出来的风格尾巴 96 词,
# 比镜头本体(63 词)还长——CLIP 按 77 token 分块,主体权重被严重稀释,
# 且 "use of long takes to build dread" 这类叙事描述对 SDXL 本就是噪声。
# 这里做确定性裁剪,历史 story.json / style_bible 也一并受益。
_MAX_WORDS_PER_STYLE_FIELD = 8
_STYLE_TAIL_STOPWORDS = {"and", "with", "of", "in", "on", "at", "the", "a", "an", "to", "for"}

# 风格尾巴里不该出现的「场景状态词」。style_bible 是**全片**通用的摄影处理,会拼到
# 每一张图上;而 rusted / grime / derelict 这类词描述的是"某个地点破败成什么样",
# 一旦进了尾巴,连白天小区门口围观人群这种正常场景也会被画成废墟。
# 实测:某镜本体写的是 "Summer afternoon, residential compound, crowd gathered",
# 尾巴里带着 "rusted metal, damp grime, brick dust, black voids",出图成了废弃楼。
# 该镜真的是废墟时,由分镜提示词自己写(那才是分镜级信息),不靠全局尾巴。
_STATE_WORDS = {
    "rusted", "rust", "rusty", "grime", "grimy", "derelict", "abandoned", "ruined",
    "ruins", "decayed", "decaying", "decay", "crumbling", "dilapidated", "peeling",
    "moldy", "mould", "mold", "mildew", "damp", "dust", "debris", "rubble", "broken",
    "cracked", "collapsed", "overgrown", "weathered", "stained", "filthy", "dirty",
    "condemned", "deserted",
}


_STATE_WORDS_CJK = ("废弃", "荒废", "破败", "废墟", "破旧", "斑驳", "残破", "坍塌",
                    "年久失修", "荒宅", "危楼")


def has_state_words(text: str) -> bool:
    """这段文字是否本来就在描述"破败/废弃"。

    用来判断某个镜头该不该接受带破败状态的素材标签:镜头自己说了是废墟,那注入
    "abandoned / dilapidated" 是对的;镜头是白天正常小区,这些词就得挡掉。
    """
    t = str(text or "")
    if any(w in t for w in _STATE_WORDS_CJK):
        return True
    return any(w.strip(".,;").lower() in _STATE_WORDS for w in t.split())


def _drop_state_words(text: str) -> str:
    """删掉描述"破败程度"的词,只留摄影/色调/质地本身。整条短语都是状态词就整条丢。"""
    kept = []
    for phrase in (p.strip() for p in text.split(",")):
        words = [w for w in phrase.split() if w.strip(".,;").lower() not in _STATE_WORDS]
        if words:
            kept.append(" ".join(words))
    return ", ".join(kept)


def _trim_style_field(text: Any, max_words: int = _MAX_WORDS_PER_STYLE_FIELD) -> str:
    """取第一个分句,再按逗号短语边界收到词数预算内。

    按词数硬切会留下半截短语(如 "rusted metal doors, bricked-up"),所以以逗号
    短语为最小单位累加;首个短语始终保留(哪怕它自己就超预算),避免整段被清空。
    """
    head = _drop_state_words(_re.split(r"[;；.。]", str(text or ""), maxsplit=1)[0])
    kept: list[str] = []
    used = 0
    for phrase in (p.strip() for p in head.split(",")):
        if not phrase:
            continue
        n = len(phrase.split())
        if kept and used + n > max_words:
            break
        if not kept and n > max_words:
            phrase = " ".join(phrase.split()[:max_words])   # 整段没逗号时仍按词数收
            n = max_words
        kept.append(phrase)
        used += n
    out = ", ".join(kept).strip().strip(",")
    words = out.split()
    # 上面按词数硬收时可能留下悬空连词;只在还剩内容时才去掉,别把整个字段清空。
    while len(words) > 1 and words[-1].strip(",").lower() in _STYLE_TAIL_STOPWORDS:
        words.pop()
    return " ".join(words).strip().strip(",")


def _style_suffix(style: dict[str, Any], profile: str = "auto",
                  auto_tail: Optional[str] = None) -> str:
    """The fixed tail appended to every prompt. `profile` picks the style档:
    'auto'(默认)= 风格跟随剧情,全局只留介质词;'horror' = 全片套用 Style Bible;
    'realistic'/'none' = 固定尾巴。auto_tail 可由 config.base_style_tail 覆盖。"""
    if profile == "auto":
        return AUTO_STYLE_TAIL if auto_tail is None else str(auto_tail).strip()
    override = STYLE_PROFILES.get(profile, None)
    if override is not None:      # realistic / none / 自定义
        return override
    parts = [
        _trim_style_field(style.get("visual_style", "cinematic horror movie")),
        _trim_style_field(style.get("color", "cold blue and gray")),
        _trim_style_field(style.get("camera", "35mm film")),
        _trim_style_field(style.get("lighting", "low key lighting")),
        _trim_style_field(style.get("texture", "dark realistic")),
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
    enrich_fn: Optional[Callable[[dict[str, Any]], str]] = None,
) -> list[dict[str, Any]]:
    # enrich_fn:可选的依赖注入 hook,输入 scene 返回一段追加到该分镜提示词的英文片段
    #（如从视觉记忆库检索到的民俗细节)。为 None 时行为与原来完全一致——本模块不关心
    # 片段来自哪里,因此不与任何具体记忆库耦合。
    characters = story.get("characters", [])
    char_block = "\n".join(f'- {c["name"]}: {c["description"]}' for c in characters)
    world = world_tail(story.get("world"))

    user = (
        (f"World (every shot must match this era/region/architecture/wardrobe):\n{world}\n\n"
         if world else "")
        + "Characters (use these descriptions verbatim when a character appears):\n"
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
        # 可选:从注入的 hook 取额外视觉片段(记忆库检索到的民俗细节)。hook 自己 fail-open,
        # 这里再包一层保险,任何异常都不影响出图。
        extra = ""
        if enrich_fn is not None:
            try:
                extra = (enrich_fn({**scene, "scene_prompt": scene_prompt}) or "").strip()
            except Exception:
                extra = ""
        # SDXL fallback: append world setting + art direction to the final prompt as a guarantee.
        parts = ([scene_prompt] + ([extra] if extra else []) + ([world] if world else [])
                 + ([art] if art else []) + [suffix])
        final.append(
            {
                "scene_id": sid,
                "prompt": ", ".join(parts),
                "scene_prompt": scene_prompt,
                "memory_fragment": extra,
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
