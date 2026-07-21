"""Turn still key frames into a "living stills" video — no GPU needed.

Uses procedural motion (Ken Burns slow zoom/pan) + crossfades + a light horror
mood overlay (vignette + film grain), assembled with a bundled ffmpeg. This is
deliberately NOT diffusion image-to-video: it runs fast on any Mac.

Phase 1 (this module's core): images -> silent motion video.
Phase 2 (functions below): add narrator TTS audio + burned-in subtitles.
"""
from __future__ import annotations

import logging
import re
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
TTS_RATE = "-8%"                    # slightly slower = more ominous
TTS_PITCH = "-2Hz"
SAY_VOICE = "Tingting"              # macOS fallback voice
SUBTITLE_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"

# Ken Burns movement presets, cycled per clip for variety. Expressions use
# zoompan's 'on' (output frame index) and 'IN' (total frames) placeholders.
_PRESETS = [
    # name        zoom expr                         x expr                              y expr
    ("zoom_in",  "1.0+0.22*on/IN",                 "iw/2-(iw/zoom/2)",                 "ih/2-(ih/zoom/2)"),
    ("zoom_out", "1.22-0.22*on/IN",                "iw/2-(iw/zoom/2)",                 "ih/2-(ih/zoom/2)"),
    ("pan_right","1.15",                            "(iw-iw/zoom)*on/IN",               "ih/2-(ih/zoom/2)"),
    ("pan_left", "1.15",                            "(iw-iw/zoom)*(1-on/IN)",           "ih/2-(ih/zoom/2)"),
    ("pan_up",   "1.15",                            "iw/2-(iw/zoom/2)",                 "(ih-ih/zoom)*(1-on/IN)"),
]


def _run(cmd: list[str]) -> None:
    logger.debug("ffmpeg: %s", " ".join(cmd[:6]) + " ...")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.error("ffmpeg failed:\n%s", proc.stderr[-1500:])
        raise RuntimeError(f"ffmpeg exited {proc.returncode}")


def _ken_burns_clip(
    img: Path, out: Path, dur: float, fps: int, size: int, preset_idx: int, mood: bool
) -> None:
    frames = int(dur * fps)
    name, zexpr, xexpr, yexpr = _PRESETS[preset_idx % len(_PRESETS)]
    zexpr = zexpr.replace("IN", str(frames))
    xexpr = xexpr.replace("IN", str(frames))
    yexpr = yexpr.replace("IN", str(frames))
    # Upscale first so zoompan panning stays smooth (avoids pixel-step jitter).
    up = size * 4
    vf = (
        f"scale={up}:{up},"
        f"zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}':d={frames}:s={size}x{size}:fps={fps}"
    )
    if mood:
        vf += ",vignette=PI/4,noise=alls=7:allf=t"
    vf += ",format=yuv420p"
    _run([
        FFMPEG, "-y", "-loop", "1", "-i", str(img),
        "-t", f"{dur}", "-r", str(fps),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", str(out),
    ])
    logger.info("Ken Burns clip [%s] %.1fs -> %s", name, dur, out.name)


def build_video(
    images: list[Path],
    out_path: Path,
    video_cfg: Optional[dict[str, Any]] = None,
) -> Path:
    """Make a silent motion video from still images with crossfades."""
    cfg = video_cfg or {}
    dur = float(cfg.get("clip_seconds", 4.0))
    fps = int(cfg.get("fps", 30))
    size = int(cfg.get("size", 1024))
    xfade = float(cfg.get("crossfade_seconds", 0.8))
    mood = bool(cfg.get("mood_overlay", True))

    if not images:
        raise ValueError("No images to build a video from.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / "_clips"
    tmp.mkdir(exist_ok=True)

    # 1) One Ken Burns clip per image.
    clips: list[Path] = []
    for i, img in enumerate(images):
        clip = tmp / f"clip_{i:02d}.mp4"
        _ken_burns_clip(img, clip, dur, fps, size, i, mood)
        clips.append(clip)

    # 2) Single clip -> just copy; otherwise crossfade-chain with xfade.
    if len(clips) == 1:
        out_path.write_bytes(clips[0].read_bytes())
        _cleanup(tmp)
        logger.info("Video built (1 clip): %s", out_path)
        return out_path

    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    steps = []
    prev = "[0:v]"
    for i in range(1, len(clips)):
        offset = i * (dur - xfade)
        label = f"[x{i}]"
        steps.append(
            f"{prev}[{i}:v]xfade=transition=fade:duration={xfade}:offset={offset:.3f}{label}"
        )
        prev = label
    filter_complex = ";".join(steps)
    _run([
        FFMPEG, "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", prev,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", str(out_path),
    ])
    _cleanup(tmp)
    total = len(clips) * dur - (len(clips) - 1) * xfade
    logger.info("Video built: %s (%d clips, ~%.1fs)", out_path, len(clips), total)
    return out_path


def _duration(path: Path) -> float:
    """Read a media file's duration by parsing ffmpeg's stderr."""
    proc = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", proc.stderr)
    if not m:
        return 0.0
    h, mm, s = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(s)


def _edge_tts(text: str, out_mp3: Path, voice: str, rate: str, pitch: str) -> None:
    import asyncio

    import edge_tts

    async def _go() -> None:
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        await comm.save(str(out_mp3))

    asyncio.run(_go())


def synth_narration(text: str, out_base: Path, tts_cfg: Optional[dict[str, Any]] = None):
    """Speak `text` to an audio file; return (path, duration_seconds).

    Uses edge-tts neural voices by default; falls back to macOS `say` if edge
    fails (e.g. no internet). `out_base` is a path WITHOUT extension.
    """
    cfg = tts_cfg or {}
    out_base.parent.mkdir(parents=True, exist_ok=True)
    engine = cfg.get("engine", TTS_ENGINE)

    if engine == "edge":
        out = out_base.with_suffix(".mp3")
        try:
            _edge_tts(
                text, out,
                cfg.get("voice", TTS_VOICE),
                cfg.get("rate", TTS_RATE),
                cfg.get("pitch", TTS_PITCH),
            )
            dur = _duration(out)
            logger.info("Narration [edge %s] (%.1fs): %s",
                        cfg.get("voice", TTS_VOICE), dur, text[:40])
            return out, dur
        except Exception as exc:  # offline / network / voice error -> fall back
            logger.warning("edge-tts failed (%s); falling back to macOS say", exc)

    out = out_base.with_suffix(".aiff")
    proc = subprocess.run(["say", "-v", cfg.get("say_voice", SAY_VOICE), text,
                           "-o", str(out)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"say failed: {proc.stderr[-300:]}")
    dur = _duration(out)
    logger.info("Narration [say] (%.1fs): %s", dur, text[:40])
    return out, dur


def _render_subtitle_png(text: str, png: Path, width: int, height: int) -> None:
    """Render a subtitle line to a transparent PNG (bottom-centred, outlined)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_size = max(28, width // 22)
    try:
        font = ImageFont.truetype(SUBTITLE_FONT, font_size)
    except OSError:
        font = ImageFont.load_default()

    # Wrap by character count that fits ~86% of the width.
    max_w = int(width * 0.86)
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur); cur = ch
    if cur:
        lines.append(cur)

    line_h = font_size + 12
    total_h = line_h * len(lines)
    y0 = int(height * 0.86) - total_h
    for i, line in enumerate(lines):
        w = draw.textlength(line, font=font)
        x = (width - w) / 2
        y = y0 + i * line_h
        # Outline for legibility over any background.
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=max(2, font_size // 14), stroke_fill=(0, 0, 0, 230))
    img.save(png)


def build_narrated_video(
    images: list[Path],
    lines: list[str],
    out_path: Path,
    video_cfg: Optional[dict[str, Any]] = None,
) -> Path:
    """Ken Burns video + narrator voiceover + burned-in subtitles, synced per scene.

    lines[i] is spoken (TTS) and shown as a subtitle while image[i] is on screen.
    """
    cfg = video_cfg or {}
    fps = int(cfg.get("fps", 30))
    size = int(cfg.get("size", 1024))
    xfade = float(cfg.get("crossfade_seconds", 0.6))
    if len(images) != len(lines):
        raise ValueError("images and lines must be the same length.")

    work = out_path.parent / "_narr"
    work.mkdir(parents=True, exist_ok=True)

    # 1) TTS each line; clip length is driven by the longest line (uniform clips).
    tts_cfg = cfg.get("tts", {})
    audios, durs = [], []
    for i, line in enumerate(lines):
        a, dur = synth_narration(line, work / f"a_{i:02d}", tts_cfg)
        durs.append(dur)
        audios.append(a)
    clip_dur = min(6.0, max(3.0, max(durs) + 0.9))

    # 2) Silent Ken Burns video with uniform clip duration.
    silent = work / "silent.mp4"
    build_video(images, silent, {**cfg, "clip_seconds": clip_dur, "crossfade_seconds": xfade})

    starts = [i * (clip_dur - xfade) for i in range(len(images))]
    total = len(images) * clip_dur - (len(images) - 1) * xfade

    # 3) Mixed narration track: each line delayed to its clip's start.
    audio_mix = work / "audio.m4a"
    a_inputs, a_filters, a_labels = [], [], []
    for i, a in enumerate(audios):
        a_inputs += ["-i", str(a)]
        delay_ms = int(starts[i] * 1000)
        a_filters.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[d{i}]")
        a_labels.append(f"[d{i}]")
    # apad pads with trailing silence so the audio matches the full video length
    # (otherwise -shortest would clip the last shot's visual tail).
    a_filters.append(f"{''.join(a_labels)}amix=inputs={len(audios)}:normalize=0,apad[a]")
    _run([FFMPEG, "-y", *a_inputs, "-filter_complex", ";".join(a_filters),
          "-map", "[a]", "-t", f"{total:.3f}", "-c:a", "aac", "-b:a", "192k", str(audio_mix)])

    # 4) Subtitle PNGs, one per scene.
    subs = []
    for i, line in enumerate(lines):
        p = work / f"sub_{i:02d}.png"
        _render_subtitle_png(line, p, size, size)
        subs.append(p)

    # 5) Overlay each subtitle during its clip, then add the audio track.
    ov_inputs, ov_steps, prev = [], [], "[0:v]"
    for i, p in enumerate(subs):
        ov_inputs += ["-i", str(p)]
        s = starts[i] + 0.15
        e = starts[i] + clip_dur - 0.15
        label = f"[o{i}]"
        ov_steps.append(
            f"{prev}[{i + 1}:v]overlay=0:0:enable='between(t,{s:.2f},{e:.2f})'{label}"
        )
        prev = label
    audio_idx = 1 + len(subs)
    _run([
        FFMPEG, "-y", "-i", str(silent), *ov_inputs, "-i", str(audio_mix),
        "-filter_complex", ";".join(ov_steps),
        "-map", prev, "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(out_path),
    ])
    for f in work.glob("*"):
        f.unlink(missing_ok=True)
    try:
        work.rmdir()
    except OSError:
        pass
    logger.info("Narrated video built: %s (%.1fs)", out_path, total)
    return out_path


def _cleanup(tmp: Path) -> None:
    for f in tmp.glob("clip_*.mp4"):
        f.unlink(missing_ok=True)
    try:
        tmp.rmdir()
    except OSError:
        pass


if __name__ == "__main__":  # quick manual test: python video_builder.py img1 img2 ...
    import sys
    logging.basicConfig(level=logging.INFO)
    imgs = [Path(p) for p in sys.argv[1:-1]]
    build_video(imgs, Path(sys.argv[-1]))
