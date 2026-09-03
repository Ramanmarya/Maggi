"""
Structured logging: human-readable stderr/stdout lines plus a machine-
readable JSONL event stream that the status tooling reads.

Two sinks on purpose. The text log is what you tail when something looks
wrong; the JSONL is what `scripts/status.py` parses so status never has to
regex prose.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .atomic_io import append_jsonl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVENTS_PATH = PROJECT_ROOT / "logs" / "events.jsonl"


def configure(name: str, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def event(kind: str, **fields) -> None:
    """Emit one structured event to the JSONL stream.

    `kind` is the stable machine key (e.g. "order_blocked", "cycle_complete");
    everything else is free-form context.
    """
    append_jsonl(
        EVENTS_PATH,
        {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields},
    )
