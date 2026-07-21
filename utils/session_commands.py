"""`.session` command handling.

Feature adapter for the Issue #48 refactor. The session *state* already lives in
``utils/session.py`` (the manager); this module owns the ``.session`` command
behavior (start/end/show/reset) that previously sat inline in ``bot.py``.

Cross-cutting collaborators are injected: the session manager, the formatter, the
shared reaction-tracking map, and a ``channel_has_active`` check (which also
considers drafts/matches). Sharing the tracking map by reference is a deliberate
strangler step — a later phase can formalize that shared state.
"""

from __future__ import annotations

from typing import Callable


class SessionCommandHandler:
    """Handles the `.session start|end|show|reset` command family."""

    def __init__(
        self,
        *,
        sessions,
        formatter,
        tracked_messages: dict,
        channel_has_active: Callable[[int], str | None],
    ) -> None:
        self._sessions = sessions
        self._formatter = formatter
        self._tracked_messages = tracked_messages
        self._channel_has_active = channel_has_active

    def _clear_tracked(self, channel_id: int) -> None:
        stale = [
            mid
            for mid, info in self._tracked_messages.items()
            if info.get("channel_id") == channel_id
        ]
        for mid in stale:
            del self._tracked_messages[mid]

    async def handle(self, intent: dict, channel_id: int):
        action = intent["action"]

        if action == "start":
            if self._channel_has_active(channel_id) == "draft":
                return self._formatter.format_error(
                    "A draft is active in this channel. Use `.draft end` first."
                )
            if self._sessions.start(channel_id):
                return "✅ Draft session started! Use `.session end` when done."
            return self._formatter.format_error(
                "A session is already active. Use `.session end` first."
            )

        if action == "end":
            session = self._sessions.end(channel_id)
            if session:
                self._clear_tracked(channel_id)
                return self._formatter.format_session_end(session.picks)
            return self._formatter.format_error("No active session in this channel.")

        if action == "show":
            session = self._sessions.get(channel_id)
            if session:
                return self._formatter.format_session_show(session.picks)
            return self._formatter.format_error("No active session in this channel.")

        if action == "reset":
            if self._sessions.reset(channel_id):
                self._clear_tracked(channel_id)
                return "🔄 Session picks cleared. Session is still active."
            return self._formatter.format_error("No active session in this channel.")

        return None
