"""批量生成:把 inputs/ 下的每份稿子生成一个长视频。

用法:
    .venv/bin/python backend/batch_generate.py

参数(此次任务的固定设置):英语 / 竖向(Shorts 9:16)/ 恐怖风格 / 每片 6 张分镜 / 快速语速。
逐个串行执行(共用一个 ComfyUI,不能并发出图)。每片进度打印到控制台,
产物路径 = outputs/<时间戳>_long/video.mp4。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from long_video import run_long_video  # noqa: E402

INPUTS = Path(__file__).resolve().parent.parent / "inputs"

SETTINGS = dict(
    num_images=6,
    orientation="portrait",   # 竖向 Shorts 9:16
    language="en",            # 英语
    style="horror",           # 恐怖(默认)
    speed="fast",             # 快速(默认)
)


def main() -> None:
    files = sorted(INPUTS.glob("story*.txt"))
    if not files:
        print(f"No story*.txt in {INPUTS}")
        raise SystemExit(1)

    print(f"共 {len(files)} 份稿件,设置:{SETTINGS}\n")
    results = []
    for i, f in enumerate(files, 1):
        script = f.read_text(encoding="utf-8")
        print(f"\n{'='*60}\n[{i}/{len(files)}] {f.name}\n{'='*60}")

        def on_event(event, payload, _name=f.name):
            if event == "stage":
                print(f"  · {payload.get('step')}/{payload.get('total')} {payload.get('label')}")
            elif event == "frame_done":
                print(f"    图 {payload.get('index')}/{payload.get('total')} ✓")
            elif event == "video":
                print(f"  ▶ 成片:{payload.get('url')}")
            elif event == "error":
                print(f"  ✗ 出错:{payload}")

        try:
            out = run_long_video(script, on_event=on_event, **SETTINGS)
            results.append((f.name, str(out)))
            print(f"[{i}/{len(files)}] {f.name} 完成 -> {out}")
        except Exception as exc:
            results.append((f.name, f"FAILED: {exc}"))
            print(f"[{i}/{len(files)}] {f.name} 失败:{exc}")

    print(f"\n\n{'='*60}\n批量完成,汇总:\n{'='*60}")
    for name, res in results:
        print(f"  {name:16s} -> {res}")


if __name__ == "__main__":
    main()
