"""ComfyUI client — queues the SDXL workflow once per frame and saves images.

It loads the workflow JSON (API format), patches the prompt / seed / size /
filename nodes, submits it over the /prompt endpoint, waits for completion via
/history, then downloads the produced image through /view.
"""
from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import requests

logger = logging.getLogger(__name__)

# Callable that returns True once the caller wants the run cancelled.
CancelCheck = Optional[Callable[[], bool]]

# 画面里有没有人。人脸是"糊"的重灾区(视频模式 1152x640 的中远景里,脸只占几十像素,
# SDXL 在这个尺度下画不出五官),所以只给有人的镜头做二次采样,环境空镜按原速出图。
#
# 【2026-08-05 统一到 prompt_generator.has_people】原来这里有一份独立词表,和空镜配额
# 那边新加的检测器会互相打架,两个方向都错:
#   * 词表里有 "medium" 和 "body",而不摘景别 —— 于是 "medium shot, inside a greenhouse"、
#     "medium close-up, rusted nails..." 这类纯环境镜全被判成有人,白跑一次 FaceDetailer
#     (每张约 30s)。以前 94% 的镜头本来就有人,这个 bug 一直没显形;空镜配额上线后
#     每轮都会白烧几张的时间。
#   * 反过来又漏了 youths / residents / hands 这类词。
# 现在只保留一个实现:那边宁可多判不可漏判(漏判会给要画人的镜头加 no-person 负向),
# 这个取向对修脸门控同样成立 —— 多判只是多花一次修脸,漏判是脸糊。
from prompt_generator import has_people as has_person  # noqa: E402  (同一份实现,别再分叉)


class RunCancelled(Exception):
    """Raised when a render is aborted because the job was cancelled."""


class ComfyClient:
    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config["comfyui"]
        self.base = f"http://{cfg['host']}:{cfg['port']}"
        self.nodes = cfg["nodes"]
        self.nodes_ipadapter = cfg.get("nodes_ipadapter", {})
        self.ipadapter_weight = cfg.get("ipadapter_weight", 0.75)
        self.ckpt_name = config.get("generation", {}).get("ckpt_name")  # single source
        self.timeout = cfg.get("timeout_seconds", 300)
        self.client_id = uuid.uuid4().hex

        here = Path(__file__).parent
        with open(here / cfg["workflow"], "r", encoding="utf-8") as fh:
            self.workflow_template: dict[str, Any] = json.load(fh)
        # IPAdapter workflow is optional — only loaded if the file exists.
        self.workflow_ipadapter: dict[str, Any] | None = None
        ipa_path = cfg.get("workflow_ipadapter")
        if ipa_path and (here / ipa_path).exists():
            with open(here / ipa_path, "r", encoding="utf-8") as fh:
                self.workflow_ipadapter = json.load(fh)

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
        reference_image: Optional[Path] = None,
        references: Optional[list[dict[str, Any]]] = None,
    ) -> Path:
        """Generate one image and return the local path it was saved to.

        - reference_image: a single face reference (character consistency).
        - references: a list of {path, weight} user reference images, each
          conditioned via its own IPAdapter node (multi-reference chain).
        If should_cancel() becomes true while waiting, the in-flight ComfyUI
        render is interrupted and RunCancelled is raised.
        """
        if references:
            uploaded = [(self.upload_image(Path(r["path"])), float(r.get("weight", 0.7)))
                        for r in references]
            wf = self._build_ref_workflow(
                positive, negative, seed, width, height, gen_cfg, filename_prefix, uploaded)
            logger.debug("Rendering %s with %d reference image(s)", filename_prefix, len(uploaded))
        else:
            use_ipa = reference_image is not None and self.workflow_ipadapter is not None
            ref_name = self.upload_image(reference_image) if use_ipa else None
            wf = self._build_workflow(
                positive, negative, seed, width, height, gen_cfg, filename_prefix,
                reference_name=ref_name,
            )
            logger.debug("Rendering %s seed=%d %dx%d steps=%s cfg=%s ipadapter=%s",
                         filename_prefix, seed, width, height, gen_cfg.get("steps"),
                         gen_cfg.get("cfg"), bool(use_ipa))
        if self._hires_cfg(gen_cfg, positive):
            logger.info("%s: hires 二次采样已启用(画面含人物)", filename_prefix)
        if self._face_cfg(gen_cfg, positive):
            logger.info("%s: FaceDetailer 人脸精修已启用", filename_prefix)
        logger.debug("Positive prompt: %s", positive[:300])
        started = time.monotonic()
        prompt_id = self._queue(wf)
        logger.info("Queued %s as prompt %s", filename_prefix, prompt_id)
        images = self._await_images(prompt_id, should_cancel)
        logger.info("%s rendered in %.1fs (%d image(s))",
                    filename_prefix, time.monotonic() - started, len(images))
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
        reference_name: Optional[str] = None,
    ) -> dict[str, Any]:
        template = self.workflow_ipadapter if reference_name else self.workflow_template
        wf = copy.deepcopy(template)
        # Checkpoint comes from config (single source), overriding the JSON default.
        if self.ckpt_name:
            for node in wf.values():
                if node.get("class_type") == "CheckpointLoaderSimple":
                    node["inputs"]["ckpt_name"] = self.ckpt_name
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

        if reference_name:
            ipa = self.nodes_ipadapter
            wf[ipa["load_image"]]["inputs"]["image"] = reference_name
            wf[ipa["ipadapter"]]["inputs"]["weight"] = self.ipadapter_weight

        hires = self._hires_cfg(gen_cfg, positive)
        if hires:
            self._add_hires_pass(wf, n["ksampler"], gen_cfg, hires)
        face = self._face_cfg(gen_cfg, positive)
        if face:
            self._add_face_detailer(wf, gen_cfg, face, seed)
        return wf

    def _build_ref_workflow(
        self, positive: str, negative: str, seed: int, width: int, height: int,
        gen_cfg: dict[str, Any], filename_prefix: str,
        refs: list[tuple[str, float]],
    ) -> dict[str, Any]:
        """Build an SDXL workflow whose model passes through one IPAdapter node
        per user reference image (chained), each with its own weight."""
        wf: dict[str, Any] = {
            "4": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": gen_cfg.get("ckpt_name", "sd_xl_base_1.0.safetensors")}},
            "5": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage",
                  "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]}},
            "11": {"class_type": "IPAdapterUnifiedLoader",
                   "inputs": {"model": ["4", 0], "preset": "PLUS (high strength)"}},
            "3": {"class_type": "KSampler", "inputs": {
                "seed": seed, "steps": gen_cfg["steps"], "cfg": gen_cfg["cfg"],
                "sampler_name": gen_cfg["sampler_name"], "scheduler": gen_cfg["scheduler"],
                "denoise": 1.0, "positive": ["6", 0], "negative": ["7", 0],
                "latent_image": ["5", 0], "model": ["11", 0]}},
        }
        prev_model = ["11", 0]
        nid = 20
        for name, weight in refs:
            load_id, ipa_id = str(nid), str(nid + 1)
            nid += 2
            wf[load_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
            wf[ipa_id] = {"class_type": "IPAdapterAdvanced", "inputs": {
                "model": prev_model, "ipadapter": ["11", 1], "image": [load_id, 0],
                "weight": weight, "weight_type": "linear", "combine_embeds": "concat",
                "start_at": 0.0, "end_at": 1.0, "embeds_scaling": "V only"}}
            prev_model = [ipa_id, 0]
        wf["3"]["inputs"]["model"] = prev_model

        hires = self._hires_cfg(gen_cfg, positive)
        if hires:
            self._add_hires_pass(wf, "3", gen_cfg, hires)
        face = self._face_cfg(gen_cfg, positive)
        if face:
            self._add_face_detailer(wf, gen_cfg, face, seed)
        return wf

    # -- hires fix ----------------------------------------------------------

    def _hires_cfg(self, gen_cfg: dict[str, Any], positive: str) -> Optional[dict[str, Any]]:
        """本帧要不要做二次采样;返回生效的 hires 配置或 None。"""
        h = dict(gen_cfg.get("hires") or {})
        if not h.get("enabled"):
            return None
        if h.get("only_with_people", True) and not has_person(positive):
            return None
        return h

    def _add_hires_pass(self, wf: dict[str, Any], ksampler_id: str,
                        gen_cfg: dict[str, Any], hires: dict[str, Any]) -> None:
        """在首轮 KSampler 之后插「潜空间放大 + 低 denoise 二次采样」(hires fix)。

        本地没有 Impact Pack / 人脸检测模型,做不了 FaceDetailer 那种"抠脸重画";
        整图放大后低 denoise 重采样是纯核心节点能做到的等效手段——放大后脸占的像素
        变多,第二轮就能把五官画实。denoise 压在 0.5 以下,构图/配色不会跑掉。
        """
        ks = wf[ksampler_id]["inputs"]
        up_id, ks2_id = "60", "61"
        while up_id in wf or ks2_id in wf:      # 避开模板里已占用的 id
            up_id, ks2_id = str(int(up_id) + 10), str(int(ks2_id) + 10)
        wf[up_id] = {"class_type": "LatentUpscaleBy", "inputs": {
            "samples": [ksampler_id, 0],
            "upscale_method": hires.get("upscale_method", "bislerp"),
            "scale_by": float(hires.get("scale", 1.5))}}
        wf[ks2_id] = {"class_type": "KSampler", "inputs": {
            "seed": ks["seed"], "steps": int(hires.get("steps") or gen_cfg["steps"]),
            "cfg": float(hires.get("cfg") or gen_cfg["cfg"]),
            "sampler_name": gen_cfg["sampler_name"], "scheduler": gen_cfg["scheduler"],
            "denoise": float(hires.get("denoise", 0.4)),
            "model": ks["model"], "positive": ks["positive"], "negative": ks["negative"],
            "latent_image": [up_id, 0]}}
        # 把解码口改接到第二轮的输出(模板里可能不止一个 VAEDecode,只改吃这一轮的)。
        for node in wf.values():
            if node.get("class_type") == "VAEDecode" and \
                    (node["inputs"].get("samples") or [None])[0] == ksampler_id:
                node["inputs"]["samples"] = [ks2_id, 0]

    # -- face detailer ------------------------------------------------------

    def _face_cfg(self, gen_cfg: dict[str, Any], positive: str) -> Optional[dict[str, Any]]:
        """本帧要不要做人脸精修;返回生效的配置或 None。"""
        f = dict(gen_cfg.get("face_detailer") or {})
        if not f.get("enabled"):
            return None
        if f.get("only_with_people", True) and not has_person(positive):
            return None
        return f

    def _add_face_detailer(self, wf: dict[str, Any], gen_cfg: dict[str, Any],
                           face: dict[str, Any], seed: int) -> None:
        """在解码之后、保存之前插入 FaceDetailer(逐张人脸裁切重绘再贴回)。

        为什么需要它:整图放大重采样(hires)只能把已经画出来的脸画实,救不了"脸太小"
        和"多人镜头里次要人脸糊掉"——模型的算力都给了主体。FaceDetailer 用
        YOLO 检出每一张脸,把每张脸单独放大到 guide_size 重绘,再无缝贴回原图,
        所以配角的脸也能拿到与主角同等的像素预算。
        """
        # 找到最终喂给 SaveImage 的那个图像来源(可能是 hires 之后的 VAEDecode)。
        save_id = None
        for nid, node in wf.items():
            if node.get("class_type") == "SaveImage":
                save_id = nid
                break
        if save_id is None:
            logger.warning("工作流里没有 SaveImage,跳过人脸精修")
            return
        img_src = wf[save_id]["inputs"].get("images")
        ks = wf[self.nodes["ksampler"]]["inputs"]

        det_id, fd_id = "70", "71"
        while det_id in wf or fd_id in wf:
            det_id, fd_id = str(int(det_id) + 10), str(int(fd_id) + 10)
        wf[det_id] = {"class_type": "UltralyticsDetectorProvider",
                      "inputs": {"model_name": face.get("model_name", "bbox/face_yolov8m.pt")}}
        wf[fd_id] = {"class_type": "FaceDetailer", "inputs": {
            "image": img_src, "model": ks["model"], "clip": self._clip_source(wf),
            "vae": self._vae_source(wf),
            "positive": ks["positive"], "negative": ks["negative"],
            # guide_size:每张脸被放大到这个尺寸再重绘,是"脸能有多细"的直接杠杆。
            "guide_size": float(face.get("guide_size", 512)), "guide_size_for": True,
            "max_size": float(face.get("max_size", 1024)),
            "seed": seed, "steps": int(face.get("steps", 12)),
            "cfg": float(face.get("cfg") or gen_cfg["cfg"]),
            "sampler_name": gen_cfg["sampler_name"], "scheduler": gen_cfg["scheduler"],
            # denoise 别给高:0.5 以上会把脸重画成另一个人,人物一致性就没了。
            "denoise": float(face.get("denoise", 0.4)),
            "feather": int(face.get("feather", 5)), "noise_mask": True, "force_inpaint": True,
            "bbox_threshold": float(face.get("bbox_threshold", 0.5)),
            "bbox_dilation": int(face.get("bbox_dilation", 10)),
            "bbox_crop_factor": float(face.get("bbox_crop_factor", 3.0)),
            "sam_detection_hint": "center-1", "sam_dilation": 0, "sam_threshold": 0.93,
            "sam_bbox_expansion": 0, "sam_mask_hint_threshold": 0.7,
            "sam_mask_hint_use_negative": "False",
            # drop_size:小于这个像素的检出框直接丢掉,避免把远处路人的糊脸也拿去重绘。
            "drop_size": int(face.get("drop_size", 24)),
            "bbox_detector": [det_id, 0], "wildcard": "", "cycle": 1}}
        wf[save_id]["inputs"]["images"] = [fd_id, 0]

    @staticmethod
    def _clip_source(wf: dict[str, Any]) -> list[Any]:
        """正向 CLIPTextEncode 用的 clip 来源(照抄它,免得漏了 LoRA 之类的链路)。"""
        for node in wf.values():
            if node.get("class_type") == "CLIPTextEncode" and "clip" in node["inputs"]:
                return node["inputs"]["clip"]
        return ["4", 1]

    @staticmethod
    def _vae_source(wf: dict[str, Any]) -> list[Any]:
        for node in wf.values():
            if node.get("class_type") == "VAEDecode" and "vae" in node["inputs"]:
                return node["inputs"]["vae"]
        return ["4", 2]

    def upload_image(self, path: Path) -> str:
        """Upload a local image to ComfyUI's input folder; return its filename."""
        with open(path, "rb") as fh:
            resp = requests.post(
                f"{self.base}/upload/image",
                files={"image": (path.name, fh, "image/png")},
                data={"overwrite": "true"},
                timeout=30,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"ComfyUI image upload failed {resp.status_code}: {resp.text[:300]}")
        info = resp.json()
        name = info.get("name", path.name)
        sub = info.get("subfolder", "")
        logger.debug("Uploaded reference image as %s", f"{sub}/{name}" if sub else name)
        return f"{sub}/{name}" if sub else name

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
                logger.warning("Cancel requested — interrupting prompt %s", prompt_id)
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
            logger.info("Sent /interrupt to ComfyUI")
        except requests.RequestException as exc:
            logger.warning("Failed to send /interrupt: %s", exc)

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
