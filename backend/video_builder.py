"""Turn still key frames into a "living stills" video — no GPU needed.

Procedural motion (Ken Burns slow zoom/pan) + crossfades + a light horror mood
overlay (vignette + film grain), assembled with a bundled ffmpeg. Not diffusion
image-to-video: runs fast on any Mac.

Output is 16:9 (YouTube long-video format) by default. Sizes/quality/voice are
all driven by the `video` block in config.yaml.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

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
    }


# Ken Burns movement presets, cycled per clip. Expressions use zoompan's 'on'
# (output frame index); 'IN' is replaced with the clip's frame count.
_PRESETS = [
    ("zoom_in",  "1.0+0.18*on/IN",  "iw/2-(iw/zoom/2)",        "ih/2-(ih/zoom/2)"),
    ("zoom_out", "1.18-0.18*on/IN", "iw/2-(iw/zoom/2)",        "ih/2-(ih/zoom/2)"),
    ("pan_right","1.12",            "(iw-iw/zoom)*on/IN",      "ih/2-(ih/zoom/2)"),
    ("pan_left", "1.12",            "(iw-iw/zoom)*(1-on/IN)",  "ih/2-(ih/zoom/2)"),
    ("pan_up",   "1.12",            "iw/2-(iw/zoom/2)",        "(ih-ih/zoom)*(1-on/IN)"),
]


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


def synth_narration(text: str, out_base: Path, tts_cfg: Optional[dict[str, Any]] = None):
    """Speak `text`; return (path, duration_seconds).

    首选 edge-tts(神经语音,自然);带重试。仅当重试全部失败、且未强制禁用兜底
    (`allow_say_fallback`)时,才降级到机械的 macOS `say`,并明确告警——因为半段
    神经声半段机械声会很突兀。"""
    cfg = tts_cfg or {}
    out_base.parent.mkdir(parents=True, exist_ok=True)
    if cfg.get("engine", TTS_ENGINE) == "edge":
        out = out_base.with_suffix(".mp3")
        try:
            _edge_tts(text, out, cfg.get("voice", TTS_VOICE),
                      cfg.get("rate", TTS_RATE), cfg.get("pitch", TTS_PITCH))
            return out, _duration(out)
        except Exception as exc:
            if not cfg.get("allow_say_fallback", True):
                # 宁可整段失败也不用机械声(神经声一致性优先)。
                raise RuntimeError(
                    f"edge-tts 合成失败且已禁用 say 兜底:{exc}。"
                    "请检查网络/代理是否能访问微软 edge-tts。") from exc
            logger.warning("edge-tts 最终失败(%s);降级到 macOS say(音质会明显变机械)", exc)
    out = out_base.with_suffix(".aiff")
    proc = subprocess.run(["say", "-v", cfg.get("say_voice", SAY_VOICE), text,
                           "-o", str(out)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"say failed: {proc.stderr[-300:]}")
    return out, _duration(out)


# --- subtitles -------------------------------------------------------------

_CJK_RE = __import__("re").compile(r"[一-鿿]")


def _wrap_cjk(text: str, max_chars: int) -> list[str]:
    """Wrap subtitle text. CJK text breaks per-char (with punctuation preference);
    non-CJK text (English, etc.) wraps on word boundaries."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    # 英文/纯拉丁按单词换行(允许 ~2x 字符宽,因为拉丁字符窄)
    if not _CJK_RE.search(text):
        limit = max(max_chars * 2, 20)
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

def _ken_burns_clip(img: Path, out: Path, dur: float, p: dict[str, Any], preset_idx: int) -> None:
    frames = max(1, int(dur * p["fps"]))
    name, z, x, y = _PRESETS[preset_idx % len(_PRESETS)]
    z, x, y = (e.replace("IN", str(frames)) for e in (z, x, y))
    upw, uph = p["w"] * 2, p["h"] * 2   # upscale so panning stays smooth
    vf = (f"scale={upw}:{uph},zoompan=z='{z}':x='{x}':y='{y}':"
          f"d={frames}:s={p['w']}x{p['h']}:fps={p['fps']}")
    if p["mood"] and p["grain"] > 0:
        vf += f",vignette=PI/5,noise=alls={p['grain']}:allf=t"
    vf += ",format=yuv420p"
    _run([FFMPEG, "-y", "-loop", "1", "-i", str(img), "-t", f"{dur}", "-r", str(p["fps"]),
          "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", str(p["crf"]),
          "-pix_fmt", "yuv420p", str(out)])
    logger.info("Ken Burns clip [%s] %.1fs -> %s", name, dur, out.name)


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
          "-c:v", "libx264", "-preset", "medium", "-crf", str(p["crf"]),
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
          "-c:v", "libx264", "-preset", "medium", "-crf", str(p["crf"]), "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(out_path)])
    _wipe(work)
    logger.info("Narrated video built: %s (%.1fs)", out_path, total)
    return out_path


# --- long narrated video (per-line SRT subtitles) --------------------------

def _fmt_ass(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_header(w: int, h: int, font_size: int, margin_v: int) -> str:
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
        f"Style: Default,Heiti SC,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
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

    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / "_long"
    work.mkdir(exist_ok=True)

    clips, all_audio, events, t, idx = [], [], [], 0.0, 0
    for i, seg in enumerate(segments):
        seg_dur = sum(l["dur"] for l in seg["lines"]) or 2.0
        clip = work / f"clip_{i:03d}.mp4"
        _ken_burns_clip(seg["image"], clip, seg_dur, p, i)
        clips.append(clip)
        for l in seg["lines"]:
            all_audio.append(l["audio"])
            text = "\\N".join(_wrap_cjk(l["text"], max_chars))
            events.append(f"Dialogue: 0,{_fmt_ass(t)},{_fmt_ass(t + l['dur'])},"
                          f"Default,,0,0,0,,{text}")
            idx += 1; t += l["dur"]

    concat = work / "clips.txt"
    concat.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    silent = work / "silent.mp4"
    _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(silent)])

    alist = work / "audio.txt"
    alist.write_text("".join(f"file '{a.resolve()}'\n" for a in all_audio), encoding="utf-8")
    audio = work / "audio.m4a"
    _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(alist),
          "-c:a", "aac", "-b:a", "160k", str(audio)])

    ass_file = work / "subs.ass"
    ass_file.write_text(_ass_header(p["w"], p["h"], font_size, margin_v) + "\n".join(events) + "\n",
                        encoding="utf-8")
    subs = f"ass={ass_file.as_posix()}:fontsdir=/System/Library/Fonts"
    _run([FFMPEG, "-y", "-i", str(silent), "-i", str(audio), "-vf", subs,
          "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium",
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
