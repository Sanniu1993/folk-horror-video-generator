"""Central logging setup for the whole project.

Call setup_logging() once at process start (CLI entrypoint or FastAPI startup).
Every module then just does:

    import logging
    logger = logging.getLogger(__name__)

Output goes to BOTH the console and a rotating file at ``logs/app.log`` so a
debug session leaves a persistent trail without flooding the terminal.

Level resolution (first wins):
    1. explicit ``level`` argument
    2. ``LOG_LEVEL`` environment variable
    3. ``logging.level`` in config.yaml
    4. default "INFO"
Set it to DEBUG to see DeepSeek payloads, ComfyUI polling and per-frame timings.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).parent.parent
LOG_DIR = ROOT / "logs"

_CONFIGURED = False

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(config: Optional[dict[str, Any]] = None, level: Optional[str] = None) -> Path:
    """Configure root logging. Idempotent — safe to call more than once."""
    global _CONFIGURED

    log_cfg = (config or {}).get("logging", {}) if config else {}
    resolved = (
        level
        or os.environ.get("LOG_LEVEL")
        or log_cfg.get("level")
        or "INFO"
    ).upper()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / log_cfg.get("file", "app.log")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handlers decide what actually shows

    if _CONFIGURED:
        # Just adjust levels on re-invocation (e.g. tests, reload).
        for h in root.handlers:
            h.setLevel(getattr(logging, resolved, logging.INFO))
        return log_file

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # Console: honours the resolved level.
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, resolved, logging.INFO))
    console.setFormatter(formatter)
    root.addHandler(console)

    # File: always DEBUG, rotating so it never grows without bound.
    max_bytes = int(log_cfg.get("max_bytes", 5_000_000))
    backups = int(log_cfg.get("backup_count", 3))
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Tame noisy third-party loggers.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "Logging initialised (console=%s, file=%s @ DEBUG)", resolved, log_file
    )
    return log_file
