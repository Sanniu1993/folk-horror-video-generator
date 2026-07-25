"""出图提速基准:用给定参数渲染一张图,打印耗时。
用法: bench_render.py <label> <ckpt> <steps> <cfg> <sampler> <scheduler>
不传参则用 config.yaml 的 generation 段。图存到 outputs/_bench/。
"""
from __future__ import annotations
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import ROOT, load_config
from comfy_client import ComfyClient

PROMPT = ("a lone woman in traditional Hanfu standing in a misty Chinese courtyard at night, "
          "red lanterns, fog, cinematic horror, cold blue tone, 35mm film, highly detailed, film grain")
NEG = "lowres, bad anatomy, extra digits, worst quality, blurry, cartoon, anime, watermark, text"

def main():
    cfg = load_config()
    gen = dict(cfg["generation"])
    label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if len(sys.argv) > 2:
        gen["ckpt_name"] = sys.argv[2]
        gen["steps"] = int(sys.argv[3])
        gen["cfg"] = float(sys.argv[4])
        gen["sampler_name"] = sys.argv[5]
        gen["scheduler"] = sys.argv[6]
    out = ROOT / "outputs" / "_bench"
    out.mkdir(parents=True, exist_ok=True)
    comfy = ComfyClient(cfg)
    if not comfy.ping():
        print("ComfyUI 不可达"); raise SystemExit(1)
    print(f"[{label}] ckpt={gen['ckpt_name']} steps={gen['steps']} cfg={gen['cfg']} "
          f"{gen['sampler_name']}/{gen['scheduler']}")
    t0 = time.monotonic()
    dest = comfy.generate(positive=PROMPT, negative=NEG, seed=20260721,
                          width=768, height=1344, gen_cfg=gen,
                          filename_prefix=f"bench_{label}", out_dir=out)
    dt = time.monotonic() - t0
    print(f"[{label}] 耗时 {dt:.1f}s -> {dest}")

if __name__ == "__main__":
    main()
