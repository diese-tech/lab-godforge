"""The `/scrim` Discord adapter: slash commands and the challenge-response view.

Feature module for the Issue #48 refactor. It owns the `/scrim` command family
(team-create, teams, challenge, respond, checkin, lock, launch) and
``ScrimChallengeView`` — previously defined directly in ``bot.py``. Domain logic
stays in ``utils/scrims.py``; this module is strictly the Discord adapter.

Because launching a scrim hands off into the party lobby/ready-check surface,
those pieces (``launch_scrim``, ready-check and lobby-card rendering) are
injected via ``ScrimCommandDeps`` rather than imported directly, keeping the
scrim feature decoupled from the party feature's internals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import discord
from discord import app_commands

from utils.party import LobbyState
from utils.party_queue import QueueError
from utils.party_schedule import ScheduleError, parse_local_start
from utils.scrims import ScrimError, launch_scrim


def _discord_ids(value: str) -> tuple[int, ...]:
    ids = tuple(dict.fromkeys(int(match) for match in re.findall(r"\d{5,25}", value)))
    if not ids:
        raise ScrimError("mention at least one Discord member")
    return ids


def scrim_challenge_id(interaction: discord.Interaction) -> str | None:
    if not interaction.message or not interaction.message.embeds:
        return None
    footer = interaction.message.embeds[0].footer.text or ""
    return footer.removeprefix("Scrim challenge ").strip() or None


@dataclass
class ScrimCommandDeps:
    """Collaborators injected into the scrim command family.

    ``ready_check_embed``/``ready_check_view`` and ``lobby_card_embed``/
    ``lobby_card_view`` render the post-launch hand-off into the party lobby
    surface without this module importing party internals directly.
    """

    scrim_repository: object
    schedule_repository: object
    party_repository: object
    party_queue_service: object
    ready_check_embed: Callable
    ready_check_view: Callable[[], discord.ui.View]
    lobby_card_embed: Callable
    lobby_card_view: Callable[[], discord.ui.View]


def build_scrim_challenge_view(deps: ScrimCommandDeps) -> type[discord.ui.View]:
    """Return a ``ScrimChallengeView`` class bound to the given dependencies."""

    class ScrimChallengeView(discord.ui.View):
        """Restart-safe challenge controls; the durable ID lives in the embed."""

        def __init__(self):
            super().__init__(timeout=None)

        async def _respond(self, interaction: discord.Interaction, response: str):
            challenge_id = scrim_challenge_id(interaction)
            if not challenge_id:
                await interaction.response.send_message(
                    "This challenge card is missing its durable ID.", ephemeral=True
                )
                return
            try:
                existing = deps.scrim_repository.get_challenge(challenge_id)
                if existing is None or existing.guild_id != interaction.guild_id:
                    raise ScrimError("challenge not found in this server")
                challenge = deps.scrim_repository.respond(
                    challenge_id,
                    actor_id=interaction.user.id,
                    response=response,
                    operation_id=f"discord:{interaction.id}:scrim-{response}",
                )
            except ScrimError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Challenge `{challenge.challenge_id}` is now **{challenge.state.value}**."
            )

        @discord.ui.button(
            label="Accept",
            style=discord.ButtonStyle.success,
            custom_id="godforge:scrim:accept",
        )
        async def accept(self, interaction: discord.Interaction, _button):
            await self._respond(interaction, "accept")

        @discord.ui.button(
            label="Reject",
            style=discord.ButtonStyle.danger,
            custom_id="godforge:scrim:reject",
        )
        async def reject(self, interaction: discord.Interaction, _button):
            await self._respond(interaction, "reject")

        @discord.ui.button(
            label="Check in",
            style=discord.ButtonStyle.primary,
            custom_id="godforge:scrim:checkin",
        )
        async def checkin(self, interaction: discord.Interaction, _button):
            challenge_id = scrim_challenge_id(interaction)
            if not challenge_id:
                await interaction.response.send_message(
                    "Challenge ID missing.", ephemeral=True
                )
                return
            try:
                existing = deps.scrim_repository.get_challenge(challenge_id)
                if existing is None or existing.guild_id != interaction.guild_id:
                    raise ScrimError("challenge not found in this server")
                challenge = deps.scrim_repository.check_in(
                    challenge_id,
                    actor_id=interaction.user.id,
                    operation_id=f"discord:{interaction.id}:scrim-checkin",
                )
            except ScrimError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Check-in recorded ({len(challenge.checked_in_team_ids)}/2 captains)."
            )

    return ScrimChallengeView


def register_scrim_commands(
    tree: app_commands.CommandTree, deps: ScrimCommandDeps
) -> tuple[app_commands.Group, type[discord.ui.View]]:
    """Build the `/scrim` command group, register it on *tree*, and return it.

    Returns ``(group, ScrimChallengeView)``. Individual commands are reachable as
    attributes on the returned group object (e.g. ``group.team_create``) for
    direct invocation in tests, mirroring how they were reachable on ``bot.py``
    before extraction.
    """
    ChallengeView = build_scrim_challenge_view(deps)

    scrim_commands = app_commands.Group(
        name="scrim",
        description="Manage guild teams and captain challenges",
    )

    @scrim_commands.command(
        name="team-create", description="Create or update your scrim team"
    )
    async def team_create(
        interaction: discord.Interaction,
        name: str,
        roster: str,
        region: str,
        availability: str,
        substitutes: str = "",
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        try:
            active = tuple(dict.fromkeys((interaction.user.id,) + _discord_ids(roster)))
            bench = _discord_ids(substitutes) if substitutes.strip() else ()
            team = deps.scrim_repository.save_team(
                guild_id=interaction.guild_id,
                captain_id=interaction.user.id,
                name=name,
                roster=active,
                substitutes=bench,
                region=region,
                availability=availability,
                operation_id=f"discord:{interaction.id}:scrim-team",
                manager_override=bool(interaction.user.guild_permissions.manage_guild),
            )
        except ScrimError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Saved **{team.name}** (`{team.team_id}`): {len(team.roster)} active, "
            f"{len(team.substitutes)} substitutes, {team.region}.",
            ephemeral=True,
        )

    @scrim_commands.command(
        name="teams", description="List registered teams in this server"
    )
    async def teams(interaction: discord.Interaction):
        registered = deps.scrim_repository.list_teams(interaction.guild_id or 0)
        await interaction.response.send_message(
            "\n".join(
                f"`{team.team_id}` **{team.name}** — {team.region}; "
                f"{len(team.roster)} active; {team.availability}"
                for team in registered
            )
            or "No scrim teams are registered.",
            ephemeral=True,
        )

    @scrim_commands.command(
        name="challenge", description="Challenge another registered team"
    )
    async def challenge(
        interaction: discord.Interaction,
        your_team_id: str,
        opponent_team_id: str,
        when: str,
        timezone_name: str,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        try:
            your_team = deps.scrim_repository.get_team(your_team_id)
            opponent = deps.scrim_repository.get_team(opponent_team_id)
            if (
                your_team is None
                or opponent is None
                or your_team.guild_id != interaction.guild_id
                or opponent.guild_id != interaction.guild_id
            ):
                raise ScrimError("both teams must be registered in this server")
            result = deps.scrim_repository.challenge(
                challenger_team_id=your_team_id,
                recipient_team_id=opponent_team_id,
                actor_id=interaction.user.id,
                starts_at=parse_local_start(when, timezone_name),
                timezone_name=timezone_name,
                operation_id=f"discord:{interaction.id}:scrim-challenge",
            )
        except (ScrimError, ScheduleError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        embed = discord.Embed(
            title="Scrim challenge",
            description=(
                f"<@{opponent.captain_id}>, your team has been challenged for "
                f"<t:{int(result.starts_at.timestamp())}:f>.\n"
                "Accept or reject below, or use `/scrim respond` to propose another time."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Scrim challenge {result.challenge_id}")
        await interaction.response.send_message(
            content=f"<@{opponent.captain_id}>",
            embed=embed,
            view=ChallengeView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )

    @scrim_commands.command(
        name="respond", description="Accept, reject, or counter a challenge"
    )
    async def respond(
        interaction: discord.Interaction,
        challenge_id: str,
        response: str,
        proposed_when: str = "",
        timezone_name: str = "UTC",
    ):
        try:
            existing = deps.scrim_repository.get_challenge(challenge_id)
            if existing is None or existing.guild_id != interaction.guild_id:
                raise ScrimError("challenge not found in this server")
            proposed = (
                parse_local_start(proposed_when, timezone_name)
                if response.strip().lower() == "propose"
                else None
            )
            result = deps.scrim_repository.respond(
                challenge_id,
                actor_id=interaction.user.id,
                response=response,
                proposed_at=proposed,
                operation_id=f"discord:{interaction.id}:scrim-respond",
            )
        except (ScrimError, ScheduleError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Challenge `{result.challenge_id}` is **{result.state.value}** at "
            f"<t:{int(result.starts_at.timestamp())}:f>."
        )

    @scrim_commands.command(
        name="checkin", description="Check your team into an accepted scrim"
    )
    async def checkin(interaction: discord.Interaction, challenge_id: str):
        try:
            existing = deps.scrim_repository.get_challenge(challenge_id)
            if existing is None or existing.guild_id != interaction.guild_id:
                raise ScrimError("challenge not found in this server")
            result = deps.scrim_repository.check_in(
                challenge_id,
                actor_id=interaction.user.id,
                operation_id=f"discord:{interaction.id}:scrim-checkin",
            )
        except ScrimError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Check-in recorded ({len(result.checked_in_team_ids)}/2 captains)."
        )

    @scrim_commands.command(name="lock", description="Lock both checked-in rosters")
    async def lock(interaction: discord.Interaction, challenge_id: str):
        try:
            existing = deps.scrim_repository.get_challenge(challenge_id)
            if existing is None or existing.guild_id != interaction.guild_id:
                raise ScrimError("challenge not found in this server")
            result = deps.scrim_repository.lock_rosters(
                challenge_id,
                actor_id=interaction.user.id,
                organizer_override=bool(
                    interaction.guild and interaction.user.guild_permissions.manage_guild
                ),
                operation_id=f"discord:{interaction.id}:scrim-lock",
            )
        except ScrimError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Rosters locked for `{result.challenge_id}`. "
            "Edits to registered teams will not change this match."
        )

    @scrim_commands.command(
        name="launch", description="Launch a locked scrim as a GodForge lobby"
    )
    async def launch(interaction: discord.Interaction, challenge_id: str):
        found = deps.scrim_repository.get_challenge(challenge_id)
        if found is None or found.guild_id != interaction.guild_id:
            await interaction.response.send_message("Challenge not found.", ephemeral=True)
            return
        if (
            interaction.user.id != found.organizer_id
            and not interaction.user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "Only the organizer or a server manager can launch.", ephemeral=True
            )
            return
        try:
            lobby = await launch_scrim(
                found,
                repo,
                deps.schedule_repository,
                deps.party_repository,
                deps.party_queue_service,
                operation_id=f"discord:{interaction.id}:scrim-launch",
            )
        except (ScrimError, ScheduleError, QueueError, ValueError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        queue = await deps.party_queue_service.get(lobby.lobby_id)
        if lobby.state is LobbyState.READY_CHECK and queue is not None:
            await interaction.response.send_message(
                content=" ".join(f"<@{member.user_id}>" for member in queue.active),
                embed=deps.ready_check_embed(lobby.lobby_id, queue),
                view=deps.ready_check_view(),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )
        else:
            await interaction.response.send_message(
                content=f"Scrim `{found.challenge_id}` is now a GodForge lobby.",
                embed=deps.lobby_card_embed(lobby),
                view=deps.lobby_card_view(),
            )

    tree.add_command(scrim_commands)

    scrim_commands.team_create = team_create
    scrim_commands.teams = teams
    scrim_commands.challenge = challenge
    scrim_commands.respond = respond
    scrim_commands.checkin = checkin
    scrim_commands.lock = lock
    scrim_commands.launch = launch

    return scrim_commands, ChallengeView
