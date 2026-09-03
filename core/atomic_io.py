"""
Atomic JSON/text persistence: write to a temp file in the same directory,
fsync, then rename. os.replace is atomic on POSIX, so a crash mid-write can
never leave a half-written state file behind — the reader either sees the
old complete file or the new complete file.

Every module that persists state MUST go through here. A bare open(...,"w")
on a state file is a bug.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, payload: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def atomic_write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    atomic_write_text(Path(path), json.dumps(data, indent=indent, default=str))


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, returning `default` if the file is missing.

    A corrupt file raises rather than silently returning the default — a
    corrupt state file is an incident, not a fresh start. Silently resetting
    state on a parse error is how a bot forgets its open positions.
    """
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r") as f:
        return json.load(f)


def append_jsonl(path: Path, record: dict) -> None:
    """Append one record to a JSONL audit log. Append-only by design."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
