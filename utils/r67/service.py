"""Coordination layer for the `.r67` feature.

The service owns all r67 branching logic so the Discord adapter in ``bot.py``
stays thin: it routes a parsed command (and, later, passive messages) here and
sends back whatever text is returned. The service operates on explicit inputs
(guild id, argument string, permission flag) and returns explicit results,
keeping Discord objects at the adapter boundary.

Command surface and copy are locked in Issue #47 (Gate 3 / Gate 4).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from utils.party import utc_now
from utils.r67.matcher import is_qualifying
from utils.r67.repository import GuildState, SQLiteR67Repository
from utils.r67.selector import select_command, select_passive

# -- Passive reaction tuning (Gate 2/5, locked, not admin-configurable) --

PASSIVE_TRIGGER_CHANCE = 0.07  # flat 7% roll per eligible message
PASSIVE_COOLDOWN = timedelta(minutes=5)  # guild-wide, starts on success only

# -- Command-reply copy (Gate 4, locked) ---------------------------------

PERMISSION_DENIED = "⚠️ Managing 67 reactions requires the **Manage Server** permission."
UNKNOWN_SUBCOMMAND = "Unknown `.r67` option. Try `.r67`, `.r67 reactions on|off`, or `.r67 status`."
GUILD_ONLY = "`.r67 reactions` can only be configured inside a server."

REACTIONS_ENABLED_REPLY = "**67 reactions:** Enabled\nThe forge is listening."
REACTIONS_DISABLED_REPLY = "**67 reactions:** Disabled\nThe forge sleeps."


def _status_copy(state: GuildState) -> str:
    return (
        REACTIONS_ENABLED_REPLY
        if state.reactions_enabled
        else REACTIONS_DISABLED_REPLY
    )


class R67Service:
    """Coordinates repository, selector (and later tracker/roles) for r67."""

    def __init__(
        self,
        repository: SQLiteR67Repository,
        *,
        rng: random.Random | None = None,
    ):
        self.repository = repository
        self.rng = rng or random.Random()
        # In-memory no-repeat tracking: last response text per guild. Not
        # persisted (Gate 4: temporary matching state is not stored).
        self._last_response: dict[int, str] = {}

    # -- Direct command --------------------------------------------------

    def direct_response(self, guild_id: int) -> str:
        """Return one weighted `.r67` response, never repeating the previous one."""
        exclude = self._last_response.get(guild_id)
        selection = select_command(self.rng, exclude=exclude)
        self._last_response[guild_id] = selection.text
        return selection.text

    # -- Passive reactions -----------------------------------------------

    def handle_passive_message(
        self,
        guild_id: int,
        text: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Process an ordinary guild message and return a passive reply or None.

        Order is locked by Gate 7: opt-in → matcher → (Survivor tracking, added
        with the tracker) → passive cooldown → 7% roll → weighted response →
        persist cooldown. A failed roll never starts a cooldown.
        """
        now = now or utc_now()
        state = self.repository.get_guild_state(guild_id)
        if not state.reactions_enabled:
            return None
        if not is_qualifying(text):
            return None

        # (Survivor tracking is inserted here once the tracker lands; it must run
        # regardless of the passive cooldown or the 7% roll outcome.)

        if state.passive_cooldown_until is not None and now < state.passive_cooldown_until:
            return None
        if self.rng.random() >= PASSIVE_TRIGGER_CHANCE:
            return None

        exclude = self._last_response.get(guild_id)
        selection = select_passive(self.rng, exclude=exclude)
        self._last_response[guild_id] = selection.text
        self.repository.set_passive_cooldown(guild_id, now + PASSIVE_COOLDOWN)
        return selection.text

    # -- Admin controls --------------------------------------------------

    def enable_reactions(self, guild_id: int) -> GuildState:
        return self.repository.set_reactions_enabled(guild_id, True)

    def disable_reactions(self, guild_id: int) -> GuildState:
        return self.repository.set_reactions_enabled(guild_id, False)

    def status(self, guild_id: int) -> GuildState:
        return self.repository.get_guild_state(guild_id)

    # -- Command routing -------------------------------------------------

    def handle_command(
        self,
        guild_id: int | None,
        args: str,
        *,
        can_manage_guild: bool,
    ) -> str:
        """Handle a parsed `.r67` command and return the reply text.

        ``args`` is everything after ``.r67`` (already lowercased/stripped by the
        adapter). ``can_manage_guild`` reflects the invoker's Discord permission.
        """
        tokens = args.split()

        # Bare `.r67` — always works, even in DMs and when reactions are off.
        if not tokens:
            gid = guild_id if guild_id is not None else 0
            return self.direct_response(gid)

        if tokens[0] == "status":
            if guild_id is None:
                return GUILD_ONLY
            return _status_copy(self.status(guild_id))

        if tokens[0] == "reactions" and len(tokens) >= 2 and tokens[1] in {"on", "off"}:
            if guild_id is None:
                return GUILD_ONLY
            if not can_manage_guild:
                return PERMISSION_DENIED
            state = (
                self.enable_reactions(guild_id)
                if tokens[1] == "on"
                else self.disable_reactions(guild_id)
            )
            return _status_copy(state)

        return UNKNOWN_SUBCOMMAND
