"""Match-result and continuity Discord interaction handlers.

Feature module for the Issue #48 refactor. Owns the two button-interaction
handlers that previously lived directly in ``bot.py``: resolving/reporting a
match result, and choosing the next post-match state (run it back, shuffle,
return to queue, invite substitutes, continue series). Domain logic stays in
``utils/match_history.py`` and ``utils/match_continuity.py``; this module is the
Discord adapter, coordinating those services with room reconciliation and the
next match's result card.

Collaborators are injected via ``MatchActionDeps`` so this module does not
import party/room internals directly (mirroring the scrim feature's pattern).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import discord

from utils import match_results
from utils.match_continuity import (
    ContinuityError,
    ContinuityStatus,
    MatchContinuityService,
)
from utils.match_history import MatchOutcome
from utils.party_queue import QueueError


@dataclass
class MatchActionDeps:
    """Collaborators injected into the match-result/continuity handlers."""

    match_history_repository: object
    match_continuity_repository: object
    party_draft_repository: object
    match_room_repository: object
    party_queue_service: object
    match_room_service_for_guild: Callable
    match_result_view: Callable[[], discord.ui.View]
    match_continuity_view: Callable[..., discord.ui.View]


async def handle_match_result_action(
    deps: MatchActionDeps,
    interaction: discord.Interaction,
    action: str,
) -> None:
    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.response.send_message("Server-only action.", ephemeral=True)
        return
    match_id = match_results.match_id_from_interaction(interaction)
    record = deps.match_history_repository.get(guild_id, match_id)
    if record is None:
        await interaction.response.send_message("Match record not found.", ephemeral=True)
        return
    outcome = MatchOutcome(action)
    operation_id = f"discord:{interaction.id}:match-result"
    try:
        if interaction.user.id == record.organizer_id:
            changed = deps.match_history_repository.resolve(
                guild_id,
                match_id,
                organizer_id=interaction.user.id,
                outcome=outcome,
                operation_id=operation_id,
            )
        else:
            if outcome not in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO}:
                raise PermissionError(
                    "Only the organizer can cancel or record no contest."
                )
            changed = deps.match_history_repository.report_winner(
                guild_id,
                match_id,
                captain_id=interaction.user.id,
                winner=outcome,
                operation_id=operation_id,
            )
    except (PermissionError, ValueError) as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.edit_message(
        embed=match_results.build_result_embed(changed),
        view=(
            deps.match_continuity_view(
                allow_continue_series=(
                    changed.series_score is not None
                    or (changed.draft_reference or "").lower().startswith("series:")
                )
            )
            if changed.outcome in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO}
            else (
                None
                if changed.outcome in {MatchOutcome.CANCELLED, MatchOutcome.NO_CONTEST}
                else deps.match_result_view()
            )
        ),
    )


async def handle_match_continuity_action(
    deps: MatchActionDeps,
    interaction: discord.Interaction,
    action: str,
) -> None:
    """Select and reconcile exactly one post-match next state."""
    if interaction.guild is None:
        await interaction.response.send_message("Server-only action.", ephemeral=True)
        return
    match_id = match_results.match_id_from_interaction(interaction)
    record = deps.match_history_repository.get(interaction.guild.id, match_id)
    launch = deps.party_draft_repository.get_by_match_id(interaction.guild.id, match_id)
    if record is None or launch is None:
        await interaction.response.send_message(
            "The source match or lobby launch could not be found.", ephemeral=True
        )
        return
    if interaction.user.id != record.organizer_id:
        await interaction.response.send_message(
            "Only the organizer can choose what happens next.", ephemeral=True
        )
        return

    async def reconcile_rooms(lobby_id, participant_ids):
        rooms = deps.match_room_repository.get(lobby_id)
        if rooms is None or not rooms.resource_ids:
            return False
        room_service = deps.match_room_service_for_guild(interaction.guild)
        await room_service.reconcile(lobby_id)
        await room_service.reconcile_participants(lobby_id, participant_ids)
        return True

    async def create_next_match(result):
        next_record = deps.match_history_repository.create(
            guild_id=record.guild_id,
            organizer_id=record.organizer_id,
            team_one=result.team_one,
            team_two=result.team_two,
            operation_id=(
                f"continuity-history:{record.guild_id}:{record.match_id}:"
                f"{result.action.value}"
            ),
            draft_reference=result.next_match_id,
            match_id=result.next_match_id,
        )
        channel = interaction.channel
        if channel is not None:
            async for message in channel.history(limit=100):
                embeds = getattr(message, "embeds", ())
                footer = embeds[0].footer.text if embeds and embeds[0].footer else ""
                if footer == f"match_id={next_record.match_id}":
                    return
            await channel.send(
                embed=match_results.build_result_embed(next_record),
                view=deps.match_result_view(),
            )

    service = MatchContinuityService(
        deps.match_continuity_repository,
        deps.party_queue_service,
        room_reconciler=reconcile_rooms,
        draft_starter=create_next_match,
    )
    try:
        result = await service.continue_match(
            record,
            lobby_id=launch.lobby_id,
            action=action,
            operation_id=f"discord:{interaction.id}:continuity",
        )
    except (ContinuityError, QueueError, ValueError) as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    lines = [
        f"**{result.action.value.replace('_', ' ').title()}** selected.",
        f"State: `{result.status.value}`",
    ]
    if result.next_match_id:
        lines.append(f"Next match: `{result.next_match_id}`")
    if result.reused_rooms:
        lines.append("Temporary rooms were reconciled and reused.")
    if result.promoted_ids:
        lines.append(
            "Promoted substitutes: "
            + " ".join(f"<@{user_id}>" for user_id in result.promoted_ids)
        )
    if result.changes:
        lines.append("Assignment changes:")
        lines.extend(
            f"- <@{change.user_id}>: "
            f"{change.previous_team or 'queue'}/{change.previous_role or 'unassigned'} "
            f"-> {change.next_team or 'queue'}/{change.next_role or 'unassigned'}"
            for change in result.changes
        )
    if result.status is ContinuityStatus.AWAITING_SUBSTITUTES:
        lines.append("The waitlist needs more eligible substitutes before launch.")
    await interaction.response.edit_message(
        content="\n".join(lines),
        view=None,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False),
    )
