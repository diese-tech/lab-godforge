"""Reusable GodForge match ID generation.

Match IDs are part of the orchestration handoff contract and must not depend
on the deprecated betting ledger.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

MATCH_ID_STATE_PATH = Path("data/match_ids.json")
_MATCH_ID_LOCK = threading.Lock()


def next_match_id(existing_ids) -> str:
    """Return the next GF-0001 style ID after the highest valid existing ID."""
    max_num = 0
    for raw_id in existing_ids:
        mid = str(raw_id or "")
        if not mid.upper().startswith("GF-"):
            continue
        try:
            max_num = max(max_num, int(mid[3:]))
        except ValueError:
            continue
    return f"GF-{max_num + 1:04d}"


def reserve_match_id(path: Path | None = None) -> str:
    """Reserve and persist the next orchestration match ID."""
    target = path or MATCH_ID_STATE_PATH
    with _MATCH_ID_LOCK:
        state = _load_state(target)
        last_id = state.get("last_match_id")
        match_id = next_match_id([last_id])
        _atomic_write_json(target, {"last_match_id": match_id})
        return match_id


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8"
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()
