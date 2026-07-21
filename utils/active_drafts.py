"""Durable pointer to in-progress local drafts for restart notices.

Feature-support module for the Issue #48 refactor. Local draft *state* is
in-memory (and lost on restart), but the channel→draft mapping is persisted so
``on_ready`` can notify channels whose draft was dropped by a restart. This tiny
JSON store owns that persistence; the path is configurable for testing.
"""

from __future__ import annotations

import json
import os


class ActiveDraftStore:
    """A small JSON map of ``channel_id -> draft_id`` for restart recovery."""

    def __init__(self, path: str = os.path.join("data", "active_local_drafts.json")):
        self.path = path

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, channel_id: int, draft_id: str) -> None:
        data = self.load()
        data[str(channel_id)] = draft_id
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def remove(self, channel_id: int) -> None:
        data = self.load()
        if str(channel_id) in data:
            data.pop(str(channel_id))
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f)

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass
