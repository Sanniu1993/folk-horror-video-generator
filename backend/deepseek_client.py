"""Thin wrapper around the DeepSeek chat API (OpenAI-compatible).

Every agent in the pipeline talks to DeepSeek through this one client so that
JSON-mode handling, retries and error reporting live in a single place.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# 截断重试的上限:每次加倍,到此为止(再大只会更慢,且多半是提示本身有问题)。
_MAX_TOKENS_CAP = 16384


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the project-root .env into os.environ.

    Uses python-dotenv when available, otherwise a tiny built-in parser so the
    project has no hard dependency just to read one key. Existing environment
    variables always win over the file.
    """
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.base_url = config.get("base_url", "https://api.deepseek.com").rstrip("/")
        self.model = config.get("model", "deepseek-v4-flash")
        _load_dotenv()
        self.api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise DeepSeekError(
                "DEEPSEEK_API_KEY is not set. Create a .env file in the project "
                "root (cp .env.example .env) and add:\n"
                "    DEEPSEEK_API_KEY=sk-...\n"
                "or export it in your shell."
            )

    def chat_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Send a chat request forcing a JSON object response, and parse it."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(
            "DeepSeek request: model=%s temp=%s system=%dch user=%dch",
            self.model, temperature, len(system), len(user),
        )
        started = time.monotonic()
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        elapsed = time.monotonic() - started
        if resp.status_code != 200:
            logger.error("DeepSeek HTTP %s in %.1fs: %s", resp.status_code, elapsed, resp.text[:300])
            raise DeepSeekError(
                f"DeepSeek returned {resp.status_code}: {resp.text[:500]}"
            )
        body = resp.json()
        choice = body["choices"][0]
        content = choice["message"]["content"]
        usage = body.get("usage", {})
        logger.info(
            "DeepSeek ok in %.1fs (%dch out, tokens=%s)",
            elapsed, len(content), usage.get("total_tokens", "?"),
        )
        logger.debug("DeepSeek raw content: %s", content[:1000])
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            # 推理模型(deepseek-v4-flash)先花 token 思考,提示越复杂思考越久。思考把
            # max_tokens 吃光时,HTTP 200 但输出是空串或半截 JSON(finish_reason=length)。
            # 直接抛错会让整条链路挂掉,先加倍预算重试;仍失败才把原文交给调用方抢救。
            if choice.get("finish_reason") == "length" and max_tokens < _MAX_TOKENS_CAP:
                bigger = min(max_tokens * 2, _MAX_TOKENS_CAP)
                logger.warning(
                    "DeepSeek 输出被 max_tokens 截断(reasoning=%s, 输出仅 %dch),"
                    "max_tokens %d -> %d 重试",
                    usage.get("completion_tokens_details", {}).get("reasoning_tokens", "?"),
                    len(content), max_tokens, bigger)
                return self.chat_json(system, user, temperature=temperature, max_tokens=bigger)
            logger.error("DeepSeek returned non-JSON (%dch): %s", len(content), content[:500])
            err = DeepSeekError(f"Model did not return valid JSON:\n{content[:500]}")
            err.raw_content = content  # 完整原文,供调用方抢救被截断的部分结果
            raise err from exc
