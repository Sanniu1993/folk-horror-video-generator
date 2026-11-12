"""Turn still key frames into a "living stills" video — no GPU needed.

Procedural motion (Ken Burns slow zoom/pan) + crossfades + a light horror mood
overlay (vignette + film grain), assembled with a bundled ffmpeg. Not diffusion
image-to-video: runs fast on any Mac.

Output is 16:9 (YouTube long-video format) by default. Sizes/quality/voice are
all driven by the `video` block in config.yaml.
"""
from __future__ import annotations

import logging
import math
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import imageio_ffmpeg

logger = logging.getLogger(__name__)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# Narrator TTS. Default engine is edge-tts (Microsoft neural voices — far more
# natural than macOS `say`), with `say` as an offline fallback.
TTS_ENGINE = "edge"
TTS_VOICE = "zh-CN-YunjianNeural"   # deep male novel/narration voice
TTS_RATE = "+0%"                    # narration speed (see config video.tts.rate)
TTS_PITCH = "-2Hz"
SAY_VOICE = "Tingting"
SUBTITLE_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"

# Output / quality defaults (overridable via config `video`).
DEF_W, DEF_H = 1920, 1080           # 16:9
DEF_FPS = 24
DEF_CRF = 28                        # higher = smaller file
DEF_GRAIN = 3                       # film-grain strength (0 = off); big filesize driver
DEF_MAX_SHOT_SECONDS = 14.0         # 单个运镜镜头最长秒数(超过就拆子镜头;0 = 不拆)
DEF_MOTION_REF = 6.0                # 运镜速度基准时长(以此时长的幅度为准按比例放大)


def _v(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve output video params from a config `video` block."""
    return {
        "w": int(cfg.get("out_width", DEF_W)),
        "h": int(cfg.get("out_height", DEF_H)),
        "fps": int(cfg.get("fps", DEF_FPS)),
        "crf": int(cfg.get("crf", DEF_CRF)),
        "grain": int(cfg.get("grain", DEF_GRAIN)),
        "mood": bool(cfg.get("mood_overlay", True)),
        "xfade": float(cfg.get("crossfade_seconds", 0.6)),
        "clip": float(cfg.get("clip_seconds", 4.0)),
        "color_grade": bool(cfg.get("color_grade", True)),
        "transition_seconds": float(cfg.get("transition_seconds", 0.5)),
        "plot_effects": bool(cfg.get("plot_effects", False)),
        "xfade_max_clips": int(cfg.get("xfade_max_clips", 60)),
        # x264 预设:成片要经过 每镜头片段 -> 转场链 -> 烧字幕 共三轮编码,
        # medium -> faster 单轮快约 1.5-2 倍,同 crf 下文件略大、肉眼画质基本无差。
        "x264_preset": str(cfg.get("x264_preset", "faster")),
        # 运镜:幅度随镜头时长放大(见 _zoom_expr),长镜头拆分(见 _split_shots)。
        "max_shot_seconds": float(cfg.get("max_shot_seconds", DEF_MAX_SHOT_SECONDS)),
        "motion_ref_seconds": float(cfg.get("motion_ref_seconds", DEF_MOTION_REF)),
        "motion_max_zoom": float(cfg.get("motion_max_zoom", 0.45)),
        "motion_max_pan": float(cfg.get("motion_max_pan", 0.30)),
    }


# Ken Burns movement presets, picked per clip. Expressions use zoompan's 'on'
# (output frame index); 'IN' is replaced with the clip's frame count.
# 含纯推拉、纯平移、以及"边推边移"的组合运镜,增加镜头语言的多样性。
#
# 幅度(amp)不再写死在表达式里,而是按镜头时长动态放大 —— 因为 zoompan 的 on/IN 是
# "当前帧/总帧数",总幅度固定就意味着**速度 = 幅度 / 时长**。实测:竖屏短片每张图
# 5.8s、推近 20% = 3.4%/秒(一眼看得出在动);横屏长片每张图 40-80s、同样推近 20%
# = 0.5%/秒甚至 0.25%/秒,肉眼等同静止图。
#   name, kind(zin=推近 / zout=拉远 / pan=定焦平移), 基准幅度, x 表达式, y 表达式
_PRESETS = [
    ("zoom_in",      "zin",  0.20, "iw/2-(iw/zoom/2)",        "ih/2-(ih/zoom/2)"),
    ("zoom_out",     "zout", 0.20, "iw/2-(iw/zoom/2)",        "ih/2-(ih/zoom/2)"),
    ("pan_right",    "pan",  0.14, "(iw-iw/zoom)*on/IN",      "ih/2-(ih/zoom/2)"),
    ("pan_left",     "pan",  0.14, "(iw-iw/zoom)*(1-on/IN)",  "ih/2-(ih/zoom/2)"),
    ("pan_up",       "pan",  0.14, "iw/2-(iw/zoom/2)",        "(ih-ih/zoom)*(1-on/IN)"),
    ("pan_down",     "pan",  0.14, "iw/2-(iw/zoom/2)",        "(ih-ih/zoom)*on/IN"),
    ("zin_right",    "zin",  0.18, "(iw-iw/zoom)*on/IN",      "ih/2-(ih/zoom/2)"),
    ("zin_left",     "zin",  0.18, "(iw-iw/zoom)*(1-on/IN)",  "ih/2-(ih/zoom/2)"),
    ("zin_downright","zin",  0.16, "(iw-iw/zoom)*on/IN",      "(ih-ih/zoom)*on/IN"),
]


def _motion_amp(base: float, kind: str, dur: float, p: dict[str, Any]) -> float:
    """按镜头时长放大运镜幅度,让「速度」而不是「幅度」保持恒定。

    以 motion_ref_seconds(默认 6s,即竖屏短片的观感)为基准:更长的镜头按比例放大
    幅度,并设上限防止推得太狠(推拉 0.45 / 平移 0.30)。短于基准的镜头保持原幅度,
    所以 Shorts 那边的观感与改动前逐帧一致。
    """
    ref = p.get("motion_ref_seconds") or DEF_MOTION_REF
    cap = p["motion_max_pan"] if kind == "pan" else p["motion_max_zoom"]
    return min(cap, base * max(1.0, dur / ref))


def _zoom_expr(kind: str, amp: float) -> str:
    """按运镜类型生成 zoompan 的 z 表达式。"""
    if kind == "zin":
        return f"1.0+{amp:.3f}*on/IN"
    if kind == "zout":
        return f"{1.0 + amp:.3f}-{amp:.3f}*on/IN"
    return f"{1.0 + amp:.3f}"          # pan:定焦,靠 x/y 位移


def _split_shots(segments: list[dict[str, Any]], seg_durs: list[float],
                 max_shot_seconds: float) -> list[dict[str, Any]]:
    """把停留过久的镜头拆成多个子镜头(同一张图,各跑一次完整运镜)。

    单张图停留 40-80 秒时,就算把幅度顶到上限也仍然"看不出在动",而且几十秒里一次
    转场都没有。拆成若干 ≤max_shot_seconds 的子镜头后,每段各自完成一次运镜、段间还
    多出转场。**子镜头时长之和等于原镜头时长**,所以音频/字幕的时间轴完全不受影响。

    被拆开的镜头交替用 zoom_in / zoom_out:两者时长相同 => 幅度相同,前一段推到 1+amp
    结束、后一段正好从 1+amp 拉回,接缝处缩放**连续**,不会在转场时"跳回广角"。
    没被拆的镜头仍走全局轮换,保留运镜多样性。

    effect(剧情特效)只给该镜头的第一个子镜头,否则同一种特效会连着重复播。
    """
    shots: list[dict[str, Any]] = []
    for i, (seg, dur) in enumerate(zip(segments, seg_durs)):
        k = 1 if max_shot_seconds <= 0 else max(1, math.ceil(dur / max_shot_seconds))
        for j in range(k):
            shots.append({"image": seg["image"], "dur": dur / k,
                          "effect": seg.get("effect") if j == 0 else None,
                          "preset": None if k == 1 else ("zoom_in" if j % 2 == 0 else "zoom_out"),
                          "segment": i})
    return shots


def _run(cmd: list[str]) -> None:
    logger.debug("ffmpeg: %s ...", " ".join(cmd[:6]))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.error("ffmpeg failed:\n%s", proc.stderr[-1500:])
        raise RuntimeError(f"ffmpeg exited {proc.returncode}")


def _duration(path: Path) -> float:
    import re
    proc = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", proc.stderr)
    if not m:
        return 0.0
    h, mm, s = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(s)


# --- narration -------------------------------------------------------------

# edge-tts 走微软服务器,偶发网络抖动/限流。默认多重试几次,避免一次失败
# 就把整段旁白降级到机械的 macOS `say`(音质断崖式下降)。
TTS_RETRIES = 4
TTS_RETRY_WAIT = 1.5   # 秒,每次翻倍退避


def _edge_tts(text: str, out_mp3: Path, voice: str, rate: str, pitch: str) -> None:
    import asyncio
    import time

    import edge_tts

    async def _go() -> None:
        await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(out_mp3))

    last_exc: Exception | None = None
    for attempt in range(1, TTS_RETRIES + 1):
        try:
            asyncio.run(_go())
            # 空文件视为失败(edge 偶尔"成功"却写出 0 字节)。
            if out_mp3.exists() and out_mp3.stat().st_size > 0:
                return
            raise RuntimeError("edge-tts wrote an empty file")
        except Exception as exc:
            last_exc = exc
            if attempt < TTS_RETRIES:
                wait = TTS_RETRY_WAIT * (2 ** (attempt - 1))
                logger.warning("edge-tts attempt %d/%d failed (%s); retry in %.1fs",
                               attempt, TTS_RETRIES, exc, wait)
                time.sleep(wait)
    raise RuntimeError(f"edge-tts failed after {TTS_RETRIES} attempts: {last_exc}")


def _trim_silence(src: Path, pad: float, threshold_db: int = 35) -> None:
    """就地压缩音频里所有静音(首尾 + 句内逗号/句号处),每处保留 `pad` 秒。

    句与句拼接、以及 TTS 在句尾/逗号处自带的停顿会累积成过长的间隔。用一次
    ffmpeg silenceremove:
      - start_periods=1  裁掉开头静音
      - stop_periods=-1  把之后每一段 ≥stop_duration 的静音收紧到 `pad` 秒
        (含句内停顿与结尾静音)
    threshold 用 -35dB 以抓住 TTS 句尾的低音量拖尾。失败则保留原文件。"""
    if pad < 0:
        pad = 0.0
    tmp = src.with_name(src.stem + "_trim" + src.suffix)
    af = (f"silenceremove=start_periods=1:start_silence={pad}:"
          f"start_threshold=-{threshold_db}dB:"
          f"stop_periods=-1:stop_silence={pad}:stop_duration=0.08:"
          f"stop_threshold=-{threshold_db}dB:detection=peak")
    proc = subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-af", af, str(tmp)],
        capture_output=True, text=True,
    )
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(src)
    else:
        logger.warning("silence trim skipped for %s: %s", src.name, proc.stderr[-200:])
        tmp.unlink(missing_ok=True)


def _resolve_bgm(bgm_cfg: dict[str, Any]) -> Optional[Path]:
    """从 bgm 配置解析出一个音乐文件:path 可为单文件或目录(目录则随机挑一首)。"""
    import random
    raw = str(bgm_cfg.get("path", "")).strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / raw
    if p.is_file():
        return p
    if p.is_dir():
        pool = [f for f in p.iterdir()
                if f.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac")]
        return random.choice(pool) if pool else None
    return None


def _mix_bgm(narration: Path, bgm: Path, out: Path, total: float,
             bgm_cfg: dict[str, Any]) -> None:
    """把 BGM 垫到旁白下面:循环铺满、压低音量、首尾淡入淡出、可选旁白闪避(ducking)。

    narration 为主轨(保持原响度),bgm 为背景。输出混音音轨。
    """
    vol = float(bgm_cfg.get("volume", 0.18))          # BGM 相对音量(旁白=1.0)
    fade = float(bgm_cfg.get("fade", 2.0))
    duck = bool(bgm_cfg.get("duck", True))
    fo = max(0.0, total - fade)
    # BGM:调音量 + 首尾淡入淡出
    steps = [f"[1:a]volume={vol:.3f},afade=t=in:st=0:d={fade:.2f},"
             f"afade=t=out:st={fo:.2f}:d={fade:.2f}[bg]"]
    bg_label = "[bg]"
    if duck:
        # 旁白一响就压低 BGM(sidechaincompress 以旁白为触发)
        steps.append("[bg][0:a]sidechaincompress=threshold=0.02:ratio=8:"
                     "attack=20:release=350[bgd]")
        bg_label = "[bgd]"
    # 混音:normalize=0 保持旁白响度不被拉低;duration=first 以旁白长度收尾
    steps.append(f"[0:a]{bg_label}amix=inputs=2:duration=first:normalize=0[mix]")
    _run([FFMPEG, "-y", "-i", str(narration), "-stream_loop", "-1", "-i", str(bgm),
          "-filter_complex", ";".join(steps), "-map", "[mix]",
          "-t", f"{total:.3f}", "-c:a", "aac", "-b:a", "192k", str(out)])
    logger.info("BGM mixed [%s] vol=%.2f duck=%s -> %s", bgm.name, vol, duck, out.name)


def synth_narration(text: str, out_base: Path, tts_cfg: Optional[dict[str, Any]] = None):
    """Speak `text`; return (path, duration_seconds).

    首选 edge-tts(神经语音,自然);带重试。仅当重试全部失败、且未强制禁用兜底
    (`allow_say_fallback`)时,才降级到机械的 macOS `say`,并明确告警——因为半段
    神经声半段机械声会很突兀。

    默认裁掉每句首尾静音(`trim_silence`,两端各留 `gap_seconds` 秒),避免句间
    停顿过长;时长在裁剪后重新测量,字幕时序自动对齐。"""
    cfg = tts_cfg or {}
    out_base.parent.mkdir(parents=True, exist_ok=True)
    out: Optional[Path] = None
    if cfg.get("engine", TTS_ENGINE) == "edge":
        out = out_base.with_suffix(".mp3")
        try:
            _edge_tts(text, out, cfg.get("voice", TTS_VOICE),
                      cfg.get("rate", TTS_RATE), cfg.get("pitch", TTS_PITCH))
        except Exception as exc:
            out = None
            if not cfg.get("allow_say_fallback", True):
                # 宁可整段失败也不用机械声(神经声一致性优先)。
                raise RuntimeError(
                    f"edge-tts 合成失败且已禁用 say 兜底:{exc}。"
                    "请检查网络/代理是否能访问微软 edge-tts。") from exc
            logger.warning("edge-tts 最终失败(%s);降级到 macOS say(音质会明显变机械)", exc)
    if out is None:
        out = out_base.with_suffix(".aiff")
        proc = subprocess.run(["say", "-v", cfg.get("say_voice", SAY_VOICE), text,
                               "-o", str(out)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"say failed: {proc.stderr[-300:]}")

    if cfg.get("trim_silence", True):
        _trim_silence(out, float(cfg.get("gap_seconds", 0.06)),
                      int(cfg.get("trim_threshold_db", 35)))
    return out, _duration(out)


def synth_narration_batch(items: list[tuple[str, Path]], tts_cfg: Optional[dict[str, Any]] = None,
                          workers: int = 8, on_progress: Optional[Callable[[int, int], None]] = None,
                          should_cancel: Optional[Callable[[], bool]] = None,
                          ) -> list[Optional[tuple[Path, float]]]:
    """并发合成多句旁白。items = [(文本, 输出基名)],返回等长的 [(path, dur)]。

    为什么要并发:单句 ≈ 网络往返(edge-tts)+ 两次 ffmpeg 子进程,本地 CPU 几乎闲着。
    实测 416 句串行花了 18.1 分钟(占整条流水线的 26%),而这段时间 GPU/CPU 全在等网络。
    并发 8 路后只受网络与微软端限速约束。

    被取消时后续任务直接跳过并返回 None(调用方自己 check_cancel 收尾),
    不跨模块抛异常。任一句真正失败仍照旧抛出 —— 半段没声音比慢更糟。
    """
    total = len(items)
    results: list[Optional[tuple[Path, float]]] = [None] * total
    if total == 0:
        return results
    done = 0
    lock = threading.Lock()

    def work(idx: int) -> None:
        nonlocal done
        if should_cancel is not None and should_cancel():
            return
        results[idx] = synth_narration(items[idx][0], items[idx][1], tts_cfg)
        with lock:
            done += 1
            if on_progress is not None and (done % 20 == 0 or done == total):
                on_progress(done, total)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, i) for i in range(total)]
        for fut in futures:
            fut.result()      # 有异常在这里原样抛出
    return results


# --- subtitles -------------------------------------------------------------

_re = __import__("re")
# 中文汉字 + 日文假名(平假名/片假名):全角、无空格 → 逐字换行。
_CJK_JP_RE = _re.compile(r"[一-鿿぀-ゟ゠-ヿ]")
# 韩文谚文:全角但词间有空格 → 按单词换行,但按全角宽度限制。
_HANGUL_RE = _re.compile(r"[가-힣]")


def _wrap_words(text: str, limit: int) -> list[str]:
    """按空格分词换行,每行不超过 limit 个字符。"""
    lines, cur = [], ""
    for word in text.split():
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= limit:
            cur += " " + word
        else:
            lines.append(cur); cur = word
    if cur:
        lines.append(cur)
    return lines


def _wrap_cjk(text: str, max_chars: int) -> list[str]:
    """按语言换行字幕:
    - 中文/日文(全角无空格):逐字换行,优先在标点断行;
    - 韩文(全角有空格):按词换行,行宽用全角上限 max_chars;
    - 拉丁语系(英文等,窄字符有空格):按词换行,行宽放宽到 ~2x。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    # 韩文:全角但有空格,按词换行、全角宽度
    if _HANGUL_RE.search(text) and not _CJK_JP_RE.search(text):
        return _wrap_words(text, max(max_chars, 6))
    # 拉丁语系:窄字符,按词换行、放宽到 ~2x
    if not _CJK_JP_RE.search(text):
        return _wrap_words(text, max(max_chars * 2, 20))
    # 中文/日文:逐字换行
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars:
            lines.append(cur); cur = ""
        elif ch in "，。！？、；：…" and len(cur) >= max_chars * 0.6:
            lines.append(cur); cur = ""
    if cur:
        lines.append(cur)
    return lines


def _render_subtitle_png(text: str, png: Path, width: int, height: int) -> None:
    """Render a subtitle line to a transparent PNG (bottom-centred, outlined)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_size = max(30, height // 20)
    try:
        font = ImageFont.truetype(SUBTITLE_FONT, font_size)
    except OSError:
        font = ImageFont.load_default()

    max_chars = max(8, int(width * 0.9 / font_size))
    lines = _wrap_cjk(text, max_chars)
    line_h = font_size + 12
    y0 = int(height * 0.88) - line_h * len(lines)
    for i, line in enumerate(lines):
        w = draw.textlength(line, font=font)
        draw.text(((width - w) / 2, y0 + i * line_h), line, font=font,
                  fill=(255, 255, 255, 255), stroke_width=max(2, font_size // 14),
                  stroke_fill=(0, 0, 0, 230))
    img.save(png)


# --- Ken Burns clips -------------------------------------------------------

# 剧情情绪 -> (运镜预设名, 附加滤镜, 颗粒增量)。按每段旁白的情绪给不同镜头语言:
#   calm    平静/铺垫  -> 缓慢推近
#   reveal  揭示/交代  -> 向右边推边移
#   eerie   诡异/不安  -> 轻微色差(红蓝错位)+ 更多颗粒
#   tension 紧张/压迫  -> 斜向推近 + 去饱和 + 更重暗角
#   sad     悲伤/追忆  -> 缓慢平移 + 冷调去饱和
#   shock   惊吓/高潮  -> 推近 + 强色差 + 开场闪白(见下)
_EFFECTS = {
    "calm":    ("zoom_in",      "",                              0),
    "reveal":  ("zin_right",    "",                              0),
    "eerie":   ("zin_left",     "rgbashift=rh=3:bh=-3",          2),
    "tension": ("zin_downright","eq=saturation=0.85,vignette=PI/4", 1),
    "sad":     ("pan_left",     "eq=saturation=0.80,hue=h=-6",   0),
    "shock":   ("zoom_in",      "rgbashift=rh=6:bh=-6",          3),
}
_PRESET_BY_NAME = {pr[0]: pr for pr in _PRESETS}


def _ken_burns_clip(img: Path, out: Path, dur: float, p: dict[str, Any], preset_idx: int,
                    effect: Optional[str] = None, preset_name: Optional[str] = None) -> None:
    frames = max(1, int(dur * p["fps"]))
    extra_vf, grain_add = "", 0
    if effect and effect in _EFFECTS:
        pname, extra_vf, grain_add = _EFFECTS[effect]
        preset = _PRESET_BY_NAME.get(pname, _PRESETS[(preset_idx * 4) % len(_PRESETS)])
    elif preset_name and preset_name in _PRESET_BY_NAME:
        preset = _PRESET_BY_NAME[preset_name]   # 拆开的子镜头:指定推近/拉远保证接缝连续
    else:
        # 无剧情标注:用与预设数互质的步长挑选,避免连续镜头运镜雷同。
        preset = _PRESETS[(preset_idx * 4) % len(_PRESETS)]
    name, kind, base_amp, x, y = preset
    amp = _motion_amp(base_amp, kind, dur, p)
    z = _zoom_expr(kind, amp)
    z, x, y = (e.replace("IN", str(frames)) for e in (z, x, y))
    upw, uph = p["w"] * 2, p["h"] * 2   # upscale so panning stays smooth
    vf = (f"scale={upw}:{uph},zoompan=z='{z}':x='{x}':y='{y}':"
          f"d={frames}:s={p['w']}x{p['h']}:fps={p['fps']}")
    # 电影感调色(轻微加对比/饱和/压 gamma),让画面更"成片"。
    if p.get("color_grade"):
        vf += ",eq=contrast=1.06:saturation=1.08:gamma=0.97"
    if p["mood"] and p["grain"] > 0:
        vf += f",vignette=PI/5,noise=alls={p['grain'] + grain_add}:allf=t"
    if extra_vf:                       # 情绪专属特效(色差/去饱和/冷调等)
        vf += "," + extra_vf
    # 转场:镜头首尾各做一次极短的淡入淡出(dip),硬切拼接下也有柔和过渡,
    # 且不改变时长(不与相邻片段重叠),不破坏音画/字幕同步。
    # shock(惊吓)镜头开场改为"闪白",更有冲击力。
    td = float(p.get("transition_seconds", 0.0))
    if td > 0 and dur > 2.2 * td:
        in_color = "white" if effect == "shock" else "black"
        vf += (f",fade=t=in:st=0:d={td:.2f}:color={in_color}"
               f",fade=t=out:st={dur - td:.3f}:d={td:.2f}")
    vf += ",format=yuv420p"
    _run([FFMPEG, "-y", "-loop", "1", "-i", str(img), "-t", f"{dur}", "-r", str(p["fps"]),
          "-vf", vf, "-c:v", "libx264", "-preset", p["x264_preset"], "-crf", str(p["crf"]),
          "-pix_fmt", "yuv420p", str(out)])
    logger.info("Ken Burns clip [%s%s] %.1fs amp=%.2f (%.2f%%/s) -> %s",
                name, f"/{effect}" if effect else "", dur, amp,
                100 * amp / max(dur, 0.001), out.name)


# 多样切换转场(xfade 类型),按镜头顺序轮换,给成片更丰富的转场语言。
# 都是 ffmpeg xfade 内置类型,兼顾电影感与恐怖氛围。
_TRANSITIONS = [
    "fade", "fadeblack", "dissolve", "smoothleft", "smoothright",
    "slideup", "wiperight", "circleopen", "radial", "fadewhite",
]


def _xfade_chain(clips: list[Path], vis_durs: list[float], D: float,
                 out: Path, p: dict[str, Any]) -> None:
    """用 xfade 把片段交叠拼接,轮换多种转场。每个片段已各自延长 D 秒,
    偏移(offset)按累计可见时长对齐,保证成片时间轴与音频/字幕同步。"""
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    steps, prev, off = [], "[0:v]", 0.0
    for k in range(1, len(clips)):
        off += vis_durs[k - 1]
        tr = _TRANSITIONS[(k - 1) % len(_TRANSITIONS)]
        lbl = f"[x{k}]"
        steps.append(f"{prev}[{k}:v]xfade=transition={tr}:duration={D:.3f}:"
                     f"offset={off:.3f}{lbl}")
        prev = lbl
    _run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(steps), "-map", prev,
          "-c:v", "libx264", "-preset", p["x264_preset"], "-crf", str(p["crf"]),
          "-pix_fmt", "yuv420p", str(out)])


def build_video(images: list[Path], out_path: Path,
                video_cfg: Optional[dict[str, Any]] = None,
                durations: Optional[list[float]] = None) -> Path:
    """Silent motion video with crossfades. Optional per-image durations."""
    p = _v(video_cfg or {})
    if not images:
        raise ValueError("No images to build a video from.")
    durs = durations or [p["clip"]] * len(images)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / "_clips"
    tmp.mkdir(exist_ok=True)

    clips = []
    for i, img in enumerate(images):
        clip = tmp / f"clip_{i:03d}.mp4"
        _ken_burns_clip(img, clip, durs[i], p, i)
        clips.append(clip)

    if len(clips) == 1:
        out_path.write_bytes(clips[0].read_bytes())
        _cleanup(tmp)
        return out_path

    inputs, steps, prev, start = [], [], "[0:v]", 0.0
    for c in clips:
        inputs += ["-i", str(c)]
    for i in range(1, len(clips)):
        start += durs[i - 1] - p["xfade"]
        label = f"[x{i}]"
        steps.append(f"{prev}[{i}:v]xfade=transition=fade:duration={p['xfade']}:"
                     f"offset={start:.3f}{label}")
        prev = label
    _run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(steps), "-map", prev,
          "-c:v", "libx264", "-preset", p["x264_preset"], "-crf", str(p["crf"]),
          "-pix_fmt", "yuv420p", str(out_path)])
    _cleanup(tmp)
    return out_path


# --- short narrated clip (per-scene subtitle PNG overlay) ------------------

def build_narrated_video(images: list[Path], lines: list[str], out_path: Path,
                         video_cfg: Optional[dict[str, Any]] = None) -> Path:
    p = _v(video_cfg or {})
    cfg = video_cfg or {}
    if len(images) != len(lines):
        raise ValueError("images and lines must be the same length.")
    work = out_path.parent / "_narr"
    work.mkdir(parents=True, exist_ok=True)

    tts_cfg = cfg.get("tts", {})
    audios, durs = [], []
    for i, line in enumerate(lines):
        a, dur = synth_narration(line, work / f"a_{i:02d}", tts_cfg)
        durs.append(dur); audios.append(a)
    clip_dur = min(6.0, max(3.0, max(durs) + 0.9))

    silent = work / "silent.mp4"
    build_video(images, silent, {**cfg, "clip_seconds": clip_dur})

    starts = [i * (clip_dur - p["xfade"]) for i in range(len(images))]
    total = len(images) * clip_dur - (len(images) - 1) * p["xfade"]

    audio_mix = work / "audio.m4a"
    a_inputs, a_filters, a_labels = [], [], []
    for i, a in enumerate(audios):
        a_inputs += ["-i", str(a)]
        d = int(starts[i] * 1000)
        a_filters.append(f"[{i}:a]adelay={d}|{d}[d{i}]"); a_labels.append(f"[d{i}]")
    a_filters.append(f"{''.join(a_labels)}amix=inputs={len(audios)}:normalize=0,apad[a]")
    _run([FFMPEG, "-y", *a_inputs, "-filter_complex", ";".join(a_filters), "-map", "[a]",
          "-t", f"{total:.3f}", "-c:a", "aac", "-b:a", "192k", str(audio_mix)])

    subs = []
    for i, line in enumerate(lines):
        sp = work / f"sub_{i:02d}.png"
        _render_subtitle_png(line, sp, p["w"], p["h"]); subs.append(sp)

    ov_inputs, ov_steps, prev = [], [], "[0:v]"
    for i, sp in enumerate(subs):
        ov_inputs += ["-i", str(sp)]
        s, e = starts[i] + 0.15, starts[i] + clip_dur - 0.15
        label = f"[o{i}]"
        ov_steps.append(f"{prev}[{i + 1}:v]overlay=0:0:enable='between(t,{s:.2f},{e:.2f})'{label}")
        prev = label
    _run([FFMPEG, "-y", "-i", str(silent), *ov_inputs, "-i", str(audio_mix),
          "-filter_complex", ";".join(ov_steps), "-map", prev, "-map", f"{1 + len(subs)}:a",
          "-c:v", "libx264", "-preset", p["x264_preset"], "-crf", str(p["crf"]), "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(out_path)])
    _wipe(work)
    logger.info("Narrated video built: %s (%.1fs)", out_path, total)
    return out_path


# --- long narrated video (per-line SRT subtitles) --------------------------

def _subtitle_cues(text: str, max_chars: int, max_lines: int = 2) -> list[str]:
    """把一句(可能很长的)旁白拆成若干"字幕屏",每屏最多 `max_lines` 行。

    欧美观看习惯是一次只显示 1–2 行、随语音滚动;整句一次铺 5–6 行不符合习惯。
    先按语言换行(`_wrap_cjk`:中文逐字、英文按词),再每 `max_lines` 行打包成一屏。
    返回每屏文本(行内用 ASS 的 \\N 连接)。"""
    disp_lines = _wrap_cjk(text, max_chars)
    cues = []
    for i in range(0, len(disp_lines), max_lines):
        cues.append("\\N".join(disp_lines[i:i + max_lines]))
    return cues or [text.strip()]


def _fmt_ass(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


# 按语言选字幕字体(family 名给 libass;都在 /System/Library/Fonts):
#   zh -> Heiti SC(中文);ja -> Hiragino Sans GB(含假名+汉字);
#   ko -> Apple SD Gothic Neo(谚文);en/其他 -> Heiti SC 也含拉丁字形。
SUBTITLE_FONTS = {
    "zh": "Heiti SC",
    "en": "Heiti SC",
    "ja": "Hiragino Sans GB",
    "ko": "Apple SD Gothic Neo",
}


def _ass_header(w: int, h: int, font_size: int, margin_v: int,
                fontname: str = "Heiti SC") -> str:
    # PlayResX/Y pinned to the video size so FontSize is in real pixels
    # (SRT would default to a 288-tall canvas and blow the font up ~3.7x).
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{fontname},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H64000000,1,0,0,0,100,100,0,0,1,3,0,2,{int(w*0.06)},{int(w*0.06)},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def build_long_video(segments: list[dict[str, Any]], out_path: Path,
                     video_cfg: Optional[dict[str, Any]] = None) -> Path:
    """Long narrated video: each image shown for its narration group; per-line
    subtitles (ASS, auto-wrapped); hard cuts keep audio/subtitle/video in sync."""
    p = _v(video_cfg or {})
    cfg = video_cfg or {}
    font_size = int(cfg.get("subtitle_font_size", max(28, p["h"] // 24)))
    margin_v = int(cfg.get("subtitle_margin_v", int(p["h"] * 0.07)))
    max_chars = max(8, int(p["w"] * 0.86 / font_size))
    max_sub_lines = int(cfg.get("subtitle_max_lines", 2))   # 每屏最多几行(欧美习惯 1–2)
    sub_font = SUBTITLE_FONTS.get(cfg.get("language", "zh"), "Heiti SC")  # 按语言选字体

    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / "_long"
    work.mkdir(exist_ok=True)

    # --- 先算每镜头时长 + 逐句字幕事件(与拼接方式无关)---
    seg_durs, all_audio, events, t, idx = [], [], [], 0.0, 0
    for seg in segments:
        seg_durs.append(sum(l["dur"] for l in seg["lines"]) or 2.0)
        for l in seg["lines"]:
            all_audio.append(l["audio"])
            # 一句旁白拆成多屏(每屏 ≤max_sub_lines 行),按字符数比例分摊该句时长,
            # 让字幕随语音分段滚动而不是整句一次铺满。音画总时长不变,保持同步。
            cues = _subtitle_cues(l["text"], max_chars, max_sub_lines)
            weights = [max(1, len(c.replace("\\N", ""))) for c in cues]
            wsum = sum(weights) or 1
            ct = t
            for c, w in zip(cues, weights):
                cdur = l["dur"] * w / wsum
                events.append(f"Dialogue: 0,{_fmt_ass(ct)},{_fmt_ass(ct + cdur)},"
                              f"Default,,0,0,0,,{c}")
                ct += cdur
            idx += 1; t += l["dur"]

    # --- 拆子镜头 + 渲染运镜片段 ---
    # 停留过久的镜头拆成多段(同图、各跑一次完整运镜),子镜头时长之和 = 原镜头时长,
    # 因此上面算好的字幕时间轴与音频拼接完全不用改。
    shots = _split_shots(segments, seg_durs, p["max_shot_seconds"])
    shot_durs = [s["dur"] for s in shots]
    if len(shots) > len(segments):
        logger.info("长镜头拆分:%d 张图 -> %d 个运镜镜头(单镜头上限 %.0fs)",
                    len(segments), len(shots), p["max_shot_seconds"])
    n = len(shots)
    D = float(p.get("transition_seconds", 0.0))
    # xfade 多样转场:镜头间交叠,需把每片段延长 D、按累计时长对齐(保证音画同步)。
    # 片段数过多时 xfade 滤镜图过大,回退到 concat 硬切(仍带片内 dip)。
    use_xfade = D > 0 and 2 <= n <= int(p.get("xfade_max_clips", 60))
    p_clip = {**p, "transition_seconds": 0.0} if use_xfade else p  # xfade 模式关掉片内 dip
    clips = []
    for i, shot in enumerate(shots):
        clip = work / f"clip_{i:03d}.mp4"
        eff = shot.get("effect") if p.get("plot_effects", False) else None
        render_dur = shot_durs[i] + (D if use_xfade else 0.0)  # xfade 需要延长供交叠
        _ken_burns_clip(shot["image"], clip, render_dur, p_clip, i, effect=eff,
                        preset_name=shot.get("preset"))
        clips.append(clip)

    silent = work / "silent.mp4"
    if use_xfade:
        _xfade_chain(clips, shot_durs, D, silent, p)
    else:
        concat = work / "clips.txt"
        concat.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
        _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(silent)])

    alist = work / "audio.txt"
    alist.write_text("".join(f"file '{a.resolve()}'\n" for a in all_audio), encoding="utf-8")
    audio = work / "audio.m4a"
    _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(alist),
          "-c:a", "aac", "-b:a", "160k", str(audio)])

    # BGM:启用且找到音乐文件时,把旁白与背景乐混音(失败则跳过,不影响成片)。
    bgm_cfg = cfg.get("bgm", {}) or {}
    if bgm_cfg.get("enabled"):
        bgm_file = _resolve_bgm(bgm_cfg)
        if bgm_file is not None:
            try:
                mixed = work / "audio_bgm.m4a"
                _mix_bgm(audio, bgm_file, mixed, t, bgm_cfg)
                audio = mixed
            except Exception as exc:
                logger.warning("BGM 混音失败,用纯旁白继续:%s", exc)
        else:
            logger.warning("BGM 已启用但未找到音乐文件(path=%s)", bgm_cfg.get("path"))

    ass_file = work / "subs.ass"
    ass_file.write_text(_ass_header(p["w"], p["h"], font_size, margin_v, sub_font) + "\n".join(events) + "\n",
                        encoding="utf-8")
    subs = f"ass={ass_file.as_posix()}:fontsdir=/System/Library/Fonts"
    _run([FFMPEG, "-y", "-i", str(silent), "-i", str(audio), "-vf", subs,
          "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", p["x264_preset"],
          "-crf", str(p["crf"]), "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out_path)])
    _wipe(work)
    logger.info("Long video built: %s (%.1fs, %d images, %d subtitle lines)",
                out_path, t, len(segments), idx)
    return out_path


def _cleanup(tmp: Path) -> None:
    for f in tmp.glob("clip_*.mp4"):
        f.unlink(missing_ok=True)
    try:
        tmp.rmdir()
    except OSError:
        pass


def _wipe(d: Path) -> None:
    for f in d.glob("*"):
        f.unlink(missing_ok=True)
    try:
        d.rmdir()
    except OSError:
        pass


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    build_video([Path(x) for x in sys.argv[1:-1]], Path(sys.argv[-1]))
