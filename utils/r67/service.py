"""Coordination layer for the `.r67` feature.

The service owns all r67 branching logic so the Discord adapter in ``bot.py``
stays thin: it routes a parsed command (and, later, passive messages) here and
sends back whatever text is returned. The service operates on explicit inputs
(guild id, argument string, permission flag) and returns explicit results,
keeping Discord objects at the adapter boundary.

Command surface and copy are locked in Issue #47 (Gate 3 / Gate 4).
"""

from __future__ import annotations

import inspect
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from utils.party import utc_now
from utils.r67 import roles as roles_adapter
from utils.r67.matcher import is_qualifying
from utils.r67.repository import GuildState, RoleGrant, SQLiteR67Repository
from utils.r67.selector import select_command, select_passive
from utils.r67.tracker import SurvivorTracker

# -- Passive reaction tuning (Gate 2/5, locked, not admin-configurable) --

PASSIVE_TRIGGER_CHANCE = 0.07  # flat 7% roll per eligible message
PASSIVE_COOLDOWN = timedelta(minutes=5)  # guild-wide, starts on success only

# -- 67 Survivor tuning (Gate 3, locked) ---------------------------------

SURVIVOR_ROLE_DURATION = timedelta(minutes=67)
SURVIVOR_COOLDOWN = timedelta(hours=67)  # per-guild event cooldown

# -- 67 Survivor announcement copy (Gate 3, locked) ----------------------

SURVIVOR_ANNOUNCEMENT = (
    "**THE SIX HAVE SPOKEN.**\n"
    "Six voices. Seven seconds.\n\n"
    "The forge has marked its survivors for 67 minutes."
)
SURVIVOR_UNMARKED_NOTE = "(The forge could not mark them this time.)"


@dataclass(frozen=True, slots=True)
class PassiveOutcome:
    """Result of processing one ordinary guild message.

    ``response`` is passive reply text (or None); ``survivor_winners`` is the
    list of six qualifying user ids when a Survivor event fired (or None).
    """

    response: str | None = None
    survivor_winners: list[int] | None = None


@dataclass(frozen=True, slots=True)
class SurvivorGrantResult:
    marked: bool
    role_id: int | None
    granted_user_ids: list[int]


def build_survivor_announcement(winner_ids: list[int], marked: bool) -> str:
    """Assemble the dramatic event announcement with participant mentions."""
    lines = [SURVIVOR_ANNOUNCEMENT]
    if not marked:
        lines.append(SURVIVOR_UNMARKED_NOTE)
    lines.append(" ".join(f"<@{uid}>" for uid in winner_ids))
    return "\n".join(lines)

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
        self.tracker = SurvivorTracker()
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

    # -- Passive reactions + Survivor tracking ---------------------------

    def process_passive(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        text: str,
        *,
        now: datetime | None = None,
    ) -> PassiveOutcome:
        """Process one ordinary guild message (Gate 7 order).

        opt-in → matcher → Survivor tracking → passive cooldown → 7% roll →
        weighted response → persist cooldown. Survivor tracking runs *before* the
        passive cooldown check and does not depend on the 7% roll, so qualifying
        messages still advance the Survivor window while passive reactions are on
        cooldown. A failed roll never starts a passive cooldown.
        """
        now = now or utc_now()
        state = self.repository.get_guild_state(guild_id)
        if not state.reactions_enabled:
            return PassiveOutcome()
        if not is_qualifying(text):
            return PassiveOutcome()

        winners = self._track_survivor(state, guild_id, channel_id, user_id, now)
        response = self._roll_passive_response(state, guild_id, now)
        return PassiveOutcome(response=response, survivor_winners=winners)

    def _track_survivor(
        self,
        state: GuildState,
        guild_id: int,
        channel_id: int,
        user_id: int,
        now: datetime,
    ) -> list[int] | None:
        if (
            state.survivor_cooldown_until is not None
            and now < state.survivor_cooldown_until
        ):
            return None
        winners = self.tracker.record(guild_id, channel_id, user_id, now)
        if winners is None:
            return None
        self.repository.set_survivor_cooldown(guild_id, now + SURVIVOR_COOLDOWN)
        return winners

    def _roll_passive_response(
        self, state: GuildState, guild_id: int, now: datetime
    ) -> str | None:
        if (
            state.passive_cooldown_until is not None
            and now < state.passive_cooldown_until
        ):
            return None
        if self.rng.random() >= PASSIVE_TRIGGER_CHANCE:
            return None
        exclude = self._last_response.get(guild_id)
        selection = select_passive(self.rng, exclude=exclude)
        self._last_response[guild_id] = selection.text
        self.repository.set_passive_cooldown(guild_id, now + PASSIVE_COOLDOWN)
        return selection.text

    # -- Survivor role lifecycle -----------------------------------------

    async def grant_survivor_roles(
        self,
        guild,
        winner_ids: list[int],
        *,
        now: datetime | None = None,
    ) -> SurvivorGrantResult:
        """Ensure and assign the cosmetic role, persisting durable grants.

        The event cooldown has already been set by ``process_passive``; role
        failures never undo it. When the role cannot be created or assigned the
        result reports ``marked=False`` so the adapter still announces the event.
        """
        now = now or utc_now()
        expires_at = now + SURVIVOR_ROLE_DURATION
        try:
            role = await roles_adapter.ensure_role(guild)
        except roles_adapter.SurvivorRoleError:
            return SurvivorGrantResult(marked=False, role_id=None, granted_user_ids=[])
        if not roles_adapter.can_assign(guild, role):
            return SurvivorGrantResult(
                marked=False, role_id=int(role.id), granted_user_ids=[]
            )
        granted = await roles_adapter.assign(guild, role, winner_ids)
        if granted:
            self.repository.add_role_grants(
                [
                    RoleGrant(
                        guild_id=int(guild.id),
                        user_id=uid,
                        role_id=int(role.id),
                        expires_at=expires_at,
                        created_at=now,
                    )
                    for uid in granted
                ]
            )
        return SurvivorGrantResult(
            marked=bool(granted),
            role_id=int(role.id),
            granted_user_ids=granted,
        )

    async def cleanup_expired_role_grants(
        self,
        get_guild: Callable[[int], object | None],
        *,
        now: datetime | None = None,
    ) -> int:
        """Remove expired Survivor roles and clear their grants.

        Used both by startup recovery and by the periodic cleanup fallback so a
        missed in-process timer can never make a temporary role permanent. Guilds
        that are momentarily unavailable, and transient removal failures, are left
        for a later pass.
        """
        now = now or utc_now()
        removed = 0
        for grant in self.repository.due_role_grants(now):
            guild = get_guild(grant.guild_id)
            if guild is None:
                continue
            try:
                result = roles_adapter.remove_member_role(
                    guild, grant.role_id, grant.user_id
                )
                if inspect.isawaitable(result):
                    await result
            except roles_adapter.SurvivorRoleError as exc:
                self.repository.record_removal_failure(
                    grant.guild_id, grant.user_id, grant.role_id, str(exc)
                )
                continue
            self.repository.remove_role_grant(
                grant.guild_id, grant.user_id, grant.role_id
            )
            removed += 1
        return removed

    async def recover_role_grants(
        self, get_guild: Callable[[int], object | None]
    ) -> int:
        """Startup recovery: drop roles whose 67-minute window elapsed offline.

        Grants still within their window remain stored and are removed by the
        periodic cleanup fallback once due.
        """
        return await self.cleanup_expired_role_grants(get_guild)

    # -- Admin controls --------------------------------------------------

    def enable_reactions(self, guild_id: int) -> GuildState:
        return self.repository.set_reactions_enabled(guild_id, True)

    def disable_reactions(self, guild_id: int) -> GuildState:
        # Disabling reactions also disables Survivor tracking (Gate 4): drop any
        # in-flight rolling windows for the guild.
        self.tracker.clear_guild(guild_id)
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
