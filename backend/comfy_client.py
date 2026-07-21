"""ComfyUI client — queues the SDXL workflow once per frame and saves images.

It loads the workflow JSON (API format), patches the prompt / seed / size /
filename nodes, submits it over the /prompt endpoint, waits for completion via
/history, then downloads the produced image through /view.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import requests

# Callable that returns True once the caller wants the run cancelled.
CancelCheck = Optional[Callable[[], bool]]


class RunCancelled(Exception):
    """Raised when a render is aborted because the job was cancelled."""


class ComfyClient:
    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config["comfyui"]
        self.base = f"http://{cfg['host']}:{cfg['port']}"
        self.nodes = cfg["nodes"]
        self.timeout = cfg.get("timeout_seconds", 300)
        self.client_id = uuid.uuid4().hex

        workflow_path = Path(__file__).parent / cfg["workflow"]
        with open(workflow_path, "r", encoding="utf-8") as fh:
            self.workflow_template: dict[str, Any] = json.load(fh)

    # -- public ------------------------------------------------------------

    def generate(
        self,
        positive: str,
        negative: str,
        seed: int,
        width: int,
        height: int,
        gen_cfg: dict[str, Any],
        filename_prefix: str,
        out_dir: Path,
        should_cancel: CancelCheck = None,
    ) -> Path:
        """Generate one image and return the local path it was saved to.

        If should_cancel() becomes true while waiting, the in-flight ComfyUI
        render is interrupted and RunCancelled is raised.
        """
        wf = self._build_workflow(
            positive, negative, seed, width, height, gen_cfg, filename_prefix
        )
        prompt_id = self._queue(wf)
        images = self._await_images(prompt_id, should_cancel)
        if not images:
            raise RuntimeError(f"ComfyUI produced no image for prompt {prompt_id}.")
        # We only expect one SaveImage output per frame.
        img = images[0]
        data = self._download(img["filename"], img["subfolder"], img["type"])
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{filename_prefix}.png"
        dest.write_bytes(data)
        return dest

    # -- internals ---------------------------------------------------------

    def _build_workflow(
        self,
        positive: str,
        negative: str,
        seed: int,
        width: int,
        height: int,
        gen_cfg: dict[str, Any],
        filename_prefix: str,
    ) -> dict[str, Any]:
        wf = copy.deepcopy(self.workflow_template)
        n = self.nodes
        wf[n["positive_prompt"]]["inputs"]["text"] = positive
        wf[n["negative_prompt"]]["inputs"]["text"] = negative

        k = wf[n["ksampler"]]["inputs"]
        k["seed"] = seed
        k["steps"] = gen_cfg["steps"]
        k["cfg"] = gen_cfg["cfg"]
        k["sampler_name"] = gen_cfg["sampler_name"]
        k["scheduler"] = gen_cfg["scheduler"]

        latent = wf[n["latent"]]["inputs"]
        latent["width"] = width
        latent["height"] = height

        wf[n["save_image"]]["inputs"]["filename_prefix"] = filename_prefix
        return wf

    def _queue(self, workflow: dict[str, Any]) -> str:
        resp = requests.post(
            f"{self.base}/prompt",
            json={"prompt": workflow, "client_id": self.client_id},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"ComfyUI /prompt failed {resp.status_code}: {resp.text[:400]}")
        return resp.json()["prompt_id"]

    def _await_images(
        self, prompt_id: str, should_cancel: CancelCheck = None
    ) -> list[dict[str, str]]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                self.interrupt()  # abort the render currently on the GPU
                raise RunCancelled(f"Render of prompt {prompt_id} cancelled.")
            resp = requests.get(f"{self.base}/history/{prompt_id}", timeout=30)
            if resp.status_code == 200:
                history = resp.json()
                if prompt_id in history:
                    return self._collect_images(history[prompt_id])
            time.sleep(1.0)
        raise TimeoutError(f"ComfyUI did not finish prompt {prompt_id} within {self.timeout}s.")

    def interrupt(self) -> None:
        """Tell ComfyUI to stop the currently executing prompt."""
        try:
            requests.post(f"{self.base}/interrupt", timeout=10)
        except requests.RequestException:
            pass

    @staticmethod
    def _collect_images(entry: dict[str, Any]) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []
        for node_output in entry.get("outputs", {}).values():
            for img in node_output.get("images", []):
                images.append(img)
        return images

    def _download(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        resp = requests.get(
            f"{self.base}/view",
            params={"filename": filename, "subfolder": subfolder, "type": folder_type},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content

    def ping(self) -> bool:
        """Quick reachability check so we fail early with a clear message."""
        try:
            return requests.get(f"{self.base}/system_stats", timeout=5).status_code == 200
        except requests.RequestException:
            return False
