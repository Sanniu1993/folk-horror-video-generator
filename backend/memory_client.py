"""视觉记忆库(folk-horror-memory)对接适配层 —— 全部耦合只存在于本文件。

设计原则(松耦合):
  * 只通过 HTTP 调用对方的 /api/retrieve,不 import 对方任何代码;
  * 默认关闭(config visual_memory.enabled=false),不配就完全是原行为;
  * fail-open:对方没启动/超时/报错/返回异常,一律记 warning 后返回空片段,
    绝不让主流程(出图)因此中断;
  * 对外只暴露 make_enricher():返回一个 scene->英文提示词片段 的纯函数,
    prompt_generator 拿到的只是个可调用 hook,并不知道记忆库的存在。

启用后,它把检索到的真实民俗素材的 visual_tags(英文、SDXL 友好)去重后
拼成一小段,注入到该分镜的提示词里,让画面带上正确的中国民俗恐怖质感。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from prompt_generator import has_state_words

logger = logging.getLogger(__name__)

# scene(dict) -> 追加到该分镜提示词的英文片段(可能为空字符串)
Enricher = Callable[[dict[str, Any]], str]
# scene(dict) -> IPAdapter 参考图列表 [{path, weight}](可能为空列表)
RefProvider = Callable[[dict[str, Any]], list[dict[str, Any]]]


def _noop(_scene: dict[str, Any]) -> str:
    return ""


def _noop_refs(_scene: dict[str, Any]) -> list[dict[str, Any]]:
    return []


def is_reachable(base_url: str, timeout: float = 2.0) -> bool:
    """开跑前探一次记忆库是否在线。

    记忆库改为默认开启后,服务没启动的情况会变常见。若不先探测,每个分镜都要白发两次
    检索请求、并各记一条 warning;探一次就能整轮跳过,日志也只留一条明白话。
    """
    try:
        resp = requests.get(f"{str(base_url).rstrip('/')}/health", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def _retrieve(base_url: str, query: str, top_k: int, timeout: float) -> list[dict[str, Any]]:
    """调 folk-horror-memory 的只读检索接口;任何失败一律 fail-open 返回 []。"""
    try:
        resp = requests.post(f"{base_url}/api/retrieve",
                             json={"query": query, "top_k": top_k}, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("memories", [])
    except Exception as exc:
        logger.warning("visual-memory retrieve skipped (%s): %s", type(exc).__name__, exc)
        return []


def _download_image(base_url: str, url_path: str, cache_dir: Path, timeout: float) -> Optional[Path]:
    """把记忆库返回的图片(/uploads/xxx.jpg)下载到本地缓存,返回本地 Path。

    ComfyUI 只能读本地文件,故需先落盘;按 url 做内容无关的稳定文件名,重复命中直接复用缓存。
    """
    if not url_path:
        return None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        stem = hashlib.md5(url_path.encode("utf-8")).hexdigest()[:16]
        suffix = Path(url_path).suffix or ".jpg"
        dest = cache_dir / f"mem_{stem}{suffix}"
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        resp = requests.get(f"{base_url}{url_path}", timeout=timeout)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest
    except Exception as exc:
        logger.warning("visual-memory ref download skipped (%s): %s", type(exc).__name__, exc)
        return None


# 检索 query 的长度上限。实测:把整段旁白(800+ 字)当 query,embedding 会被拉向
# "泛中式恐怖"的平均方向,Top-1 反而是弱相关素材(枯井女尸 0.5427);同一镜头改用
# 15 字关键词后 Top-1 变成正确的"镜中女鬼 0.6075"。故 query 必须短而准。
_MAX_QUERY_CHARS = 220


def _scene_queries(scene: dict[str, Any], context: str) -> list[str]:
    """本镜头要发起的检索(1~2 条,去重)。

    素材库是按**场景**标注的,一条把「主体 + 事件 + 环境」揉在一起的 query 会两头不靠:
    实测某镜 mem_query="小区楼下 人群围观 居民楼 白天 2020年 事故",正确的「密集高层
    住宅群」只有 0.4633(被阈值挡掉);去掉"人群围观/事故"只留环境后是 0.5756。
    所以额外发一条**纯环境** query(mem_env:地点+建筑+时段),两路各自过门槛后合并。
    """
    out: list[str] = []
    for key in ("mem_query", "mem_env"):
        q = _scene_query(scene, context, key)
        if q and q not in out:
            out.append(q)
    return out


def _scene_query(scene: dict[str, Any], context: str, key: str = "mem_query") -> str:
    """把分镜里最能表达"画面内容"的字段组合成检索 query。

    优先用规划阶段产出的 `mem_query`(主体+环境+年代的精炼关键词);没有时退回
    画面字段拼接,并截断到 _MAX_QUERY_CHARS。emotion/purpose 是叙事功能描述
    (如"建立悬念"),对视觉检索是噪声,不再参与。

    context 是运行级线索(如 '贵州 1990年代 山村'),来自美术设定,帮助命中地域/年代。
    """
    focus = str(scene.get(key) or "").strip()
    if focus:
        return " ".join(p for p in (str(context or "").strip(), focus) if p)[:_MAX_QUERY_CHARS]
    if key != "mem_query":
        return ""          # 没给 mem_env 就只发主 query,不用画面字段兜底(那更不准)
    parts = [context, scene.get("visual", ""), scene.get("scene_prompt", "")]
    return " ".join(str(p) for p in parts if p).strip()[:_MAX_QUERY_CHARS]


def _gather(base_url: str, queries: list[str], top_k: int, timeout: float,
            min_score: float, margin: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按多条 query 检索并各自过门槛,返回 (全部召回带 _query 标注, 去重后的可用命中)。

    两路结果按 url 去重(同一条素材两路都命中时只保留分高的那次)。
    """
    seen_all: list[dict[str, Any]] = []
    kept: dict[str, dict[str, Any]] = {}
    for qi, q in enumerate(queries):
        mems = _retrieve(base_url, q, top_k, timeout)
        for m in mems:
            m["_query"] = q
            m["_env_route"] = qi > 0      # 第二路(mem_env)只贡献背景质感,见 _collect_tags
        seen_all.extend(mems)
        for m in _select_hits(mems, min_score, margin):
            key = str(m.get("url") or m.get("title") or id(m))
            if key not in kept or float(m.get("score", 0)) > float(kept[key].get("score", 0)):
                kept[key] = m
    ordered = sorted(kept.values(), key=lambda m: -float(m.get("score", 0)))
    return seen_all, ordered


def _select_hits(mems: list[dict[str, Any]], min_score: float,
                 margin: float) -> list[dict[str, Any]]:
    """双重门槛筛命中:绝对阈值 + 相对 top-1 的差距。

    绝对阈值挡住"整体都不相关"的镜头(宁可不注入也别注错);相对门槛挡住"有一条很准、
    其余是陪跑"的情况——实测无关素材也能到 0.52,只靠绝对阈值会把陪跑项一起放进来。
    """
    if not mems:
        return []
    top = max(float(m.get("score", 0)) for m in mems)
    floor = max(min_score, top - margin) if margin > 0 else min_score
    return [m for m in mems if float(m.get("score", 0)) >= floor]


def _collect_tags(mems: list[dict[str, Any]], max_fragments: int,
                  max_per_hit: int, allow_state: bool = True,
                  env_max: int = 2) -> list[str]:
    """跨命中轮转收集 visual_tags(去重保序)。

    顺序取会让 Top-1 一条把 max_fragments 吃满(它自己就有 6-7 个 tag),等于整段
    注入由单条素材决定;轮转 + 每条上限让多条命中都能贡献,单条跑偏的影响被摊薄。

    allow_state=False 时丢掉带"破败状态"的标签(abandoned / dilapidated / ruined…)。
    民俗素材库里破败建筑占多数,检索一个正常的白天小区,近邻往往是「荒废筒子楼」之类,
    它们的标签会把画面直接拽成废墟 —— 实测某镜就是这么变成废弃楼的。镜头自己写了
    是废墟时(allow_state=True)则照常注入。
    """
    lists = []
    for m in mems:
        tags = [t for t in (str(x).strip() for x in (m.get("visual_tags") or [])) if t]
        if not allow_state:
            tags = [t for t in tags if not has_state_words(t)]
        lists.append((bool(m.get("_env_route")), tags[:max_per_hit]))
    seen: set[str] = set()
    out: list[str] = []
    env_used = 0
    for rank in range(max_per_hit):
        for is_env, lst in lists:
            if rank >= len(lst):
                continue
            # 环境路的标签全是建筑名词,给多了会把构图拽成"建筑照"、把人物挤出画面
            # (实测某镜本体写着"人群围观",注入 6 个建筑标签后人群消失,只剩楼)。
            # 它的职责是纠正建筑形态,给两个就够。
            if is_env and env_used >= env_max:
                continue
            t = lst[rank]
            if t.lower() in seen:
                continue
            seen.add(t.lower())
            out.append(t)
            if is_env:
                env_used += 1
            if len(out) >= max_fragments:
                return out
    return out


def make_enricher(cfg: Optional[dict[str, Any]], context: str = "") -> Enricher:
    """按 visual_memory 配置构造 enrich 函数;未启用/缺配置时返回一个恒空的 no-op。

    cfg 例:
        enabled: true
        base_url: http://127.0.0.1:8100
        top_k: 3
        timeout: 4
        min_score: 0.15
        max_fragments: 6
    """
    cfg = cfg or {}
    if not cfg.get("enabled"):
        return _noop

    base_url = str(cfg.get("base_url", "http://127.0.0.1:8100")).rstrip("/")
    top_k = int(cfg.get("top_k", 3))
    timeout = float(cfg.get("timeout", 4))
    min_score = float(cfg.get("min_score", 0.55))
    score_margin = float(cfg.get("score_margin", 0.04))
    max_fragments = int(cfg.get("max_fragments", 6))
    max_per_hit = int(cfg.get("max_tags_per_hit", 3))

    def enrich(scene: dict[str, Any]) -> str:
        queries = _scene_queries(scene, context)
        if not queries:
            return ""
        # 主 query + 纯环境 query 各自过门槛后合并,再跨命中轮转取 visual_tags。
        _, kept = _gather(base_url, queries, top_k, timeout, min_score, score_margin)
        allow_state = has_state_words(" ".join(queries) + " " + str(scene.get("scene_prompt", "")))
        frags = _collect_tags(kept, max_fragments, max_per_hit, allow_state)
        if frags:
            logger.info("visual-memory enriched scene %s with %d tag(s)",
                        scene.get("scene_id"), len(frags))
        return ", ".join(frags)

    return enrich


def make_memory_agent(cfg: Optional[dict[str, Any]], context: str = "",
                      cache_dir: Optional[Path] = None) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """调试友好版:一次检索,同时产出「注入提示词的片段 / IPAdapter 参考图 / 完整命中明细」。

    返回 run(scene) -> dict:
      {
        "enabled": bool,
        "query": "本镜头检索用的 query",
        "hits": [{title, score, used, visual_tags, culture_tags, story_tags, url}],  # 全部召回(含被过滤的)
        "injected_tags": ["最终注入的英文标签"],
        "fragment": "拼进提示词的那段(injected_tags 逗号连接)",
        "ref_images": [{title, score, weight, url, path}],  # 实际用作 IPAdapter 的参考图
      }
    关闭时返回 enabled=False 的空壳。任何失败 fail-open。
    """
    cfg = cfg or {}
    enabled = bool(cfg.get("enabled"))
    base_url = str(cfg.get("base_url", "http://127.0.0.1:8100")).rstrip("/")
    top_k = int(cfg.get("top_k", 3))
    timeout = float(cfg.get("timeout", 8))
    min_score = float(cfg.get("min_score", 0.55))
    score_margin = float(cfg.get("score_margin", 0.04))
    max_fragments = int(cfg.get("max_fragments", 6))
    max_per_hit = int(cfg.get("max_tags_per_hit", 3))
    use_refs = bool(cfg.get("use_reference_images"))
    ref_top_n = int(cfg.get("reference_top_n", 1))
    ref_weight = float(cfg.get("reference_weight", 0.6))
    cache = cache_dir or (Path(__file__).parent.parent / "outputs" / "_mem_cache")

    def run(scene: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"enabled": enabled, "query": "", "hits": [],
                               "injected_tags": [], "fragment": "", "ref_images": []}
        if not enabled:
            return out
        queries = _scene_queries(scene, context)
        out["query"] = " ▸ ".join(queries)
        if not queries:
            return out
        mems, kept = _gather(base_url, queries, top_k, timeout, min_score, score_margin)

        kept_ids = {id(m) for m in kept}
        for m in mems:   # hits 保留全部召回(含被过滤的),前端调试能看到为什么没用上
            out["hits"].append({
                "title": m.get("title"), "score": round(float(m.get("score", 0)), 4),
                "used": id(m) in kept_ids, "query": m.get("_query", ""),
                "visual_tags": m.get("visual_tags") or [],
                "culture_tags": m.get("culture_tags") or [],
                "story_tags": m.get("story_tags") or [],
                "url": (base_url + m["url"]) if m.get("url") else "",
            })
        allow_state = has_state_words(" ".join(queries) + " " + str(scene.get("scene_prompt", "")))
        frags = _collect_tags(kept, max_fragments, max_per_hit, allow_state)
        out["allow_state"] = allow_state
        out["injected_tags"] = frags
        out["fragment"] = ", ".join(frags)
        # 全被门槛过滤时也留一行日志:否则"这镜为什么没注入"只能靠翻前端调试面板。
        logger.info("visual-memory scene %s: %d/%d hit(s) passed (top=%.3f), %d tag(s)",
                    scene.get("scene_id"), len(kept), len(mems),
                    max((float(m.get("score", 0)) for m in mems), default=0.0), len(frags))

        if use_refs:
            for m in kept[:ref_top_n]:
                path = _download_image(base_url, m.get("url", ""), cache, timeout)
                if path is not None:
                    out["ref_images"].append({
                        "title": m.get("title"), "score": round(float(m.get("score", 0)), 4),
                        "weight": ref_weight, "path": str(path),
                        "url": (base_url + m["url"]) if m.get("url") else "",
                    })
        return out

    return run


def make_ref_provider(cfg: Optional[dict[str, Any]], context: str = "",
                      cache_dir: Optional[Path] = None) -> RefProvider:
    """V2:按 visual_memory 配置构造"参考图提供器"。

    返回 scene -> [{path, weight}]:把该分镜检索到的记忆图下载到本地,作为 IPAdapter
    参考图(构图/质感层面的强增强,比纯文字标签更狠)。未启用 use_reference_images、
    服务不可用或下载失败时返回 no-op / 空列表(fail-open)。
    """
    cfg = cfg or {}
    if not (cfg.get("enabled") and cfg.get("use_reference_images")):
        return _noop_refs

    base_url = str(cfg.get("base_url", "http://127.0.0.1:8100")).rstrip("/")
    top_k = int(cfg.get("top_k", 3))
    timeout = float(cfg.get("timeout", 8))
    min_score = float(cfg.get("min_score", 0.55))
    score_margin = float(cfg.get("score_margin", 0.04))
    ref_top_n = int(cfg.get("reference_top_n", 1))
    ref_weight = float(cfg.get("reference_weight", 0.6))
    cache = cache_dir or (Path(__file__).parent.parent / "outputs" / "_mem_cache")

    def provide(scene: dict[str, Any]) -> list[dict[str, Any]]:
        queries = _scene_queries(scene, context)
        if not queries:
            return []
        _, kept = _gather(base_url, queries, top_k, timeout, min_score, score_margin)
        mems = kept[:ref_top_n]
        refs: list[dict[str, Any]] = []
        for m in mems:
            path = _download_image(base_url, m.get("url", ""), cache, timeout)
            if path is not None:
                refs.append({"path": str(path), "weight": ref_weight,
                             "title": m.get("title"), "score": m.get("score")})
        if refs:
            logger.info("visual-memory scene %s -> %d reference image(s)",
                        scene.get("scene_id"), len(refs))
        return refs

    return provide
