"""Pure helpers for the local draft command flow.

Small, dependency-light functions extracted from ``bot.py`` for the Issue #48
refactor: parsing ``.draft start`` options and formatting the completion marker.
Both respect the ForgeLens toggle (injected as a predicate for testability, and
defaulting to the real one).
"""

from __future__ import annotations

import re
from typing import Callable

from utils.forgelens_adapter import forgelens_enabled

_MATCH_RE = re.compile(r"(?:^|\s)--match\s+(\S+)")
_GAME_RE = re.compile(r"(?:^|\s)--game\s+(\d+)")


def draft_start_options(
    content: str, *, forgelens: Callable[[], bool] = forgelens_enabled
) -> dict:
    """Parse ``--match <id>`` (ForgeLens only) and ``--game <n>`` from a command."""
    match = _MATCH_RE.search(content) if forgelens() else None
    game = _GAME_RE.search(content)
    return {
        "forgelens_match_id": match.group(1) if match else "",
        "game_number": int(game.group(1)) if game else 1,
    }


def draft_completion_marker(
    draft, *, forgelens: Callable[[], bool] = forgelens_enabled
) -> str:
    """Build the machine-readable ``Draft complete`` marker for a finished draft."""
    lines = [
        "Draft complete",
        f"draft_id={draft.draft_id}",
        f"game_number={draft.current_game.game_number}",
    ]
    if forgelens():
        lines.insert(2, f"forgelens_match_id={getattr(draft, 'forgelens_match_id', '')}")
    return "\n".join(lines)
