"""In-memory rolling-window tracker for the hidden `67 Survivor` event.

Pure and Discord-free. It records qualifying passive participants keyed by
``(guild_id, channel_id)`` and reports when six unique human users have each
contributed a qualifying 67 reference inside a rolling seven-second window
(Issue #47, Gate 3/5).

State here is intentionally *not* persisted: rolling participant windows are
temporary matching state (Gate 4). Only the resulting 67-hour cooldown and role
grants are durable, and those are owned by the repository.
"""

from __future__ import annotations

from datetime import datetime, timedelta

WINDOW = timedelta(seconds=7)
REQUIRED_PARTICIPANTS = 6


class SurvivorTracker:
    """Tracks unique qualifying participants per guild+channel over a 7s window."""

    def __init__(self):
        # (guild_id, channel_id) -> list of (user_id, qualified_at), first-seen
        # order preserved, one entry per unique user.
        self._windows: dict[tuple[int, int], list[tuple[int, datetime]]] = {}

    def record(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        now: datetime,
    ) -> list[int] | None:
        """Record a qualifying participant.

        Returns the list of six winning ``user_id``s when this record completes
        an event (the sixth unique user within the window), otherwise ``None``.
        A repeated message from an already-counted user does not add a
        participant. On a completed event the channel window is cleared so a
        seventh near-simultaneous message cannot start a second event.
        """
        key = (guild_id, channel_id)
        cutoff = now - WINDOW
        # Drop entries that have aged out of the rolling window.
        entries = [
            (uid, ts) for (uid, ts) in self._windows.get(key, ()) if ts > cutoff
        ]

        if all(uid != user_id for uid, _ in entries):
            entries.append((user_id, now))

        if len(entries) >= REQUIRED_PARTICIPANTS:
            winners = [uid for uid, _ in entries[:REQUIRED_PARTICIPANTS]]
            self._windows.pop(key, None)
            return winners

        self._windows[key] = entries
        return None

    def clear_guild(self, guild_id: int) -> None:
        """Drop all tracked windows for a guild (e.g. when reactions are disabled)."""
        for key in [k for k in self._windows if k[0] == guild_id]:
            self._windows.pop(key, None)
