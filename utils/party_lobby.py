"""Play panel, lobby card, queue, ready-check, and draft-launch orchestration.

Feature module for the Issue #48 refactor. This is the largest and most
tightly-coupled surface migrated out of ``bot.py``: the Play-panel button
handler, lobby creation/join flows, the party-queue bootstrap helper, the
lobby-card button handler, the ready-check button handler, and launching the
draft engine (local or activity backend) from a formed lobby.

Unlike the smaller command features extracted earlier, these handlers already
call each other directly and share a wide set of collaborators — that
coupling is inherent to the feature (a lobby card action can trigger a ready
check, which can trigger room provisioning; joining can fill a lobby and
trigger a ready check; launching a draft reconciles the lobby and posts a
match-result card). ``PartyLobbyService`` makes that coupling explicit and
testable via one injected ``PartyLobbyDeps`` rather than scattered ``bot.py``
module globals, but does not pretend to fully decouple it — further
decomposition (e.g. splitting queue orchestration from draft launch) is future
work noted in the architecture roadmap.

Discord objects stay at this boundary; domain logic stays in
``utils/party.py``, ``utils/party_store.py``, ``utils/party_queue.py``,
``utils/party_draft.py``, and ``utils/team_formation.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import discord

from utils.lifecycle import LifecycleContext
from utils.party import LobbyState, Participant, PlayerPreferences
from utils.party_draft import PartyDraftError
from utils.party_queue import QueueError, QueueStatus, ReadyStatus
from utils.team_formation import FormationMode


@dataclass
class PartyLobbyDeps:
    """Collaborators injected into `PartyLobbyService`.

    Callable fields exist either because the collaborator is itself
    feature-owned elsewhere (draft coordinator, match-result rendering) or
    because it must be looked up lazily (view constructors, per-guild room
    service, reserved match IDs) rather than captured once at wiring time.
    """

    party_repository: object
    party_queue_service: object
    party_draft_repository: object
    scrim_repository: object
    drafts: object
    draft_coordinator: object
    activity_client: object
    formatter: object
    settings_module: object
    reserve_match_id: Callable[[], str]
    match_room_service_for_guild: Callable
    channel_has_active: Callable[[int], str | None]
    save_active_draft: Callable[[int, str], None]
    ensure_match_history: Callable
    match_result_embed: Callable
    log: object

    lobby_card_view: Callable[[], discord.ui.View]
    ready_check_view: Callable[[], discord.ui.View]
    role_preferences_view: Callable[[], discord.ui.View]
    create_lobby_modal: Callable[[Callable], discord.ui.Modal]
    join_preferences_modal: Callable[[Callable], discord.ui.Modal]
    match_result_view: Callable[[], discord.ui.View]


class PartyLobbyService:
    def __init__(self, deps: PartyLobbyDeps) -> None:
        self.deps = deps

    # -- Rendering --------------------------------------------------------

    def lobby_card_embed(self, lobby) -> discord.Embed:
        participants = (
            ", ".join(f"<@{p.user_id}>" for p in lobby.participants) or "None"
        )
        embed = discord.Embed(
            title=f"{lobby.mode.title()} · {lobby.region.upper() or 'Any region'}",
            description=lobby.notes or "Reusable GodForge party lobby",
            color=0x3498DB,
        )
        embed.add_field(name="Organizer", value=f"<@{lobby.organizer_id}>")
        embed.add_field(name="Format", value=lobby.format)
        embed.add_field(
            name="Roster",
            value=f"{len(lobby.participants)}/{lobby.capacity} · {participants}",
            inline=False,
        )
        embed.add_field(
            name="Rules",
            value=(
                f"Voice: {'required' if lobby.voice_required else 'optional'} · "
                f"Skill: {lobby.skill_band or 'open'}"
            ),
            inline=False,
        )
        embed.set_footer(text=f"lobby_id={lobby.lobby_id}")
        return embed

    def ready_check_embed(self, lobby_id: str, queue) -> discord.Embed:
        ready_count = sum(status is ReadyStatus.READY for status in queue.ready.values())
        embed = discord.Embed(
            title="GodForge Ready Check",
            description=(
                f"{ready_count}/{len(queue.active)} ready. Choose Ready, "
                "Need 5 Minutes, or Drop."
            ),
            color=0xF1C40F,
        )
        if queue.ready_deadline:
            embed.add_field(
                name="Deadline",
                value=f"<t:{int(queue.ready_deadline.timestamp())}:R>",
            )
        embed.set_footer(text=f"lobby_id={lobby_id}")
        return embed

    @staticmethod
    def lobby_id_from_interaction(interaction: discord.Interaction) -> str:
        embeds = getattr(interaction.message, "embeds", ())
        footer = embeds[0].footer.text if embeds and embeds[0].footer else ""
        if not footer.startswith("lobby_id="):
            raise ValueError("This lobby card is missing its stable identity.")
        return footer.removeprefix("lobby_id=")

    # -- Queue bootstrap ----------------------------------------------------

    async def ensure_party_queue(self, lobby):
        deps = self.deps
        try:
            queue = await deps.party_queue_service.create(lobby.lobby_id, lobby.capacity)
        except QueueError:
            queue = await deps.party_queue_service.get(lobby.lobby_id)
        if queue is None:
            raise RuntimeError("party queue could not be initialized")
        if queue.capacity != lobby.capacity:
            queue, promoted_ids = await deps.party_queue_service.resize(
                lobby.lobby_id,
                lobby.capacity,
            )
            for promoted_id in promoted_ids:
                promoted = deps.party_repository.get_player_preferences(
                    lobby.guild_id,
                    promoted_id,
                )
                deps.party_repository.save_participant(
                    lobby.guild_id,
                    lobby.lobby_id,
                    Participant(
                        promoted_id,
                        primary_role=promoted.primary_role,
                        secondary_role=promoted.secondary_role,
                        fill=promoted.fill,
                        captain=promoted.captain,
                    ),
                    operation_id=(
                        f"capacity-sync:{lobby.lobby_id}:{lobby.version}:{promoted_id}"
                    ),
                )
        existing_ids = {member.user_id for member in (*queue.active, *queue.waitlist)}
        for participant in lobby.participants:
            if participant.user_id not in existing_ids:
                queue, _ = await deps.party_queue_service.join(
                    lobby.lobby_id,
                    participant.user_id,
                    participant.preferences,
                )
        return queue

    # -- Play panel ---------------------------------------------------------

    async def handle_play_panel_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        deps = self.deps
        if interaction.guild is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        if action == "preferences":
            preferences = deps.party_repository.get_player_preferences(
                guild_id,
                interaction.user.id,
            )
            selected = ", ".join(preferences.roles) or "none yet"
            await interaction.response.send_message(
                f"Your role preferences: **{selected}**. Toggle them below.",
                view=deps.role_preferences_view(),
                ephemeral=True,
            )
            return
        active = [
            record.lobby
            for record in deps.party_repository.recover_active(guild_id)
            if record.lobby.state in {LobbyState.OPEN, LobbyState.FULL}
        ]
        if action == "browse":
            if not active:
                await interaction.response.send_message(
                    "No party lobbies are open yet.",
                    ephemeral=True,
                )
                return
            lobby = active[0]
            await interaction.response.send_message(
                embed=self.lobby_card_embed(lobby),
                view=deps.lobby_card_view(),
                ephemeral=True,
            )
            for additional_lobby in active[1:]:
                await interaction.followup.send(
                    embed=self.lobby_card_embed(additional_lobby),
                    view=deps.lobby_card_view(),
                    ephemeral=True,
                )
            return
        if action == "create":
            await interaction.response.send_modal(
                deps.create_lobby_modal(self.handle_create_lobby_submission)
            )
            return
        if action == "queue":
            lobby = next(
                (
                    candidate
                    for candidate in active
                    if candidate.state is LobbyState.OPEN
                    and len(candidate.participants) < candidate.capacity
                ),
                None,
            )
            if lobby is None:
                await interaction.response.send_message(
                    "No open lobby has space. Create one instead.",
                    ephemeral=True,
                )
                return

            async def join_handler(join_interaction, payload):
                await self.join_lobby_from_preferences(
                    join_interaction,
                    lobby.lobby_id,
                    payload,
                )

            await interaction.response.send_modal(
                deps.join_preferences_modal(join_handler)
            )

    async def handle_create_lobby_submission(
        self, interaction: discord.Interaction, payload: dict[str, object]
    ) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        test_mode = deps.settings_module.get_guild_settings(str(guild_id))["managed"].get(
            "testMode",
            False,
        )
        lobby = deps.party_repository.create(
            guild_id=guild_id,
            organizer_id=interaction.user.id,
            capacity=int(payload["party_size"]),
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=10 if test_mode else 120),
            operation_id=f"discord:{interaction.id}:create",
            mode=str(payload["mode"]),
            region=str(payload["region"]),
            format=str(payload["format"]),
            voice_required=bool(payload["voice_required"]),
            skill_band=str(payload.get("skill_band") or ""),
            notes=str(payload.get("notes") or ""),
        )
        profile = deps.party_repository.get_player_preferences(guild_id, interaction.user.id)
        lobby = deps.party_repository.save_participant(
            guild_id,
            lobby.lobby_id,
            Participant(
                interaction.user.id,
                primary_role=profile.primary_role,
                secondary_role=profile.secondary_role,
                fill=profile.fill,
                captain=profile.captain,
            ),
            operation_id=f"discord:{interaction.id}:organizer",
        )
        await self.ensure_party_queue(lobby)
        await interaction.response.send_message(
            embed=self.lobby_card_embed(lobby),
            view=deps.lobby_card_view(),
            ephemeral=True,
        )

    async def join_lobby_from_preferences(
        self,
        interaction: discord.Interaction,
        lobby_id: str,
        payload: dict[str, object],
    ) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        profile = PlayerPreferences(
            str(payload["primary_role"]),
            str(payload.get("secondary_role") or "") or None,
            bool(payload["fill"]),
            bool(payload["captain"]),
        )
        deps.party_repository.set_player_preferences(
            guild_id,
            interaction.user.id,
            profile,
        )
        lobby = deps.party_repository.get(guild_id, lobby_id)
        if lobby is None:
            raise ValueError("lobby no longer exists")
        await self.ensure_party_queue(lobby)
        queue, destination = await deps.party_queue_service.join(
            lobby_id,
            interaction.user.id,
            profile.roles,
        )
        if destination in {"active", "unchanged"}:
            changed = deps.party_repository.save_participant(
                guild_id,
                lobby_id,
                Participant(
                    interaction.user.id,
                    primary_role=profile.primary_role,
                    secondary_role=profile.secondary_role,
                    fill=profile.fill,
                    captain=profile.captain,
                ),
                operation_id=f"discord:{interaction.id}:join",
            )
        else:
            changed = lobby
        if interaction.message is not None:
            await interaction.message.edit(
                embed=self.lobby_card_embed(changed),
                view=deps.lobby_card_view(),
            )
            await interaction.response.send_message(
                (
                    f"Joined lobby `{changed.lobby_id[:8]}`."
                    if destination != "waitlist"
                    else f"Lobby is full; added to waitlist position {len(queue.waitlist)}."
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=self.lobby_card_embed(changed),
                view=deps.lobby_card_view(),
                ephemeral=True,
            )
        if len(queue.active) == queue.capacity and queue.status is QueueStatus.OPEN:
            queue = await deps.party_queue_service.start_ready_check(lobby_id)
            if changed.state is LobbyState.OPEN:
                changed = deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.FULL,
                    operation_id=f"discord:{interaction.id}:full",
                )
            if changed.state is LobbyState.FULL:
                deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.READY_CHECK,
                    operation_id=f"discord:{interaction.id}:ready-check",
                )
            await interaction.channel.send(
                content=" ".join(f"<@{member.user_id}>" for member in queue.active),
                embed=self.ready_check_embed(lobby_id, queue),
                view=deps.ready_check_view(),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )

    # -- Lobby card -----------------------------------------------------------

    async def handle_lobby_card_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        lobby_id = self.lobby_id_from_interaction(interaction)
        active_ids = {
            record.lobby.lobby_id
            for record in deps.party_repository.recover_active(guild_id)
        }
        lobby = deps.party_repository.get(guild_id, lobby_id)
        if lobby is None or lobby_id not in active_ids:
            await interaction.response.send_message(
                "That lobby is no longer active.",
                ephemeral=True,
            )
            return
        if action == "join":
            async def join_handler(join_interaction, payload):
                await self.join_lobby_from_preferences(join_interaction, lobby_id, payload)

            await interaction.response.send_modal(deps.join_preferences_modal(join_handler))
            return
        if action == "leave":
            await self.ensure_party_queue(lobby)
            queue, promoted_id = await deps.party_queue_service.leave(
                lobby_id,
                interaction.user.id,
            )
            changed = deps.party_repository.remove_participant(
                guild_id,
                lobby_id,
                interaction.user.id,
                operation_id=f"discord:{interaction.id}:leave",
                actor_id=interaction.user.id,
            )
            if promoted_id is not None:
                promoted = deps.party_repository.get_player_preferences(
                    guild_id, promoted_id
                )
                changed = deps.party_repository.save_participant(
                    guild_id,
                    lobby_id,
                    Participant(
                        promoted_id,
                        primary_role=promoted.primary_role,
                        secondary_role=promoted.secondary_role,
                        fill=promoted.fill,
                        captain=promoted.captain,
                    ),
                    operation_id=f"discord:{interaction.id}:promote:{promoted_id}",
                )
            await interaction.response.edit_message(
                embed=self.lobby_card_embed(changed),
                view=deps.lobby_card_view(),
            )
            return
        if action == "ready_check":
            if interaction.user.id != lobby.organizer_id:
                await interaction.response.send_message(
                    "Only the organizer can start a ready check.",
                    ephemeral=True,
                )
                return
            if lobby.state not in {LobbyState.OPEN, LobbyState.FULL}:
                await interaction.response.send_message(
                    "This lobby is not currently recruiting.",
                    ephemeral=True,
                )
                return
            queue = await self.ensure_party_queue(lobby)
            if queue.status is not QueueStatus.OPEN:
                await interaction.response.send_message(
                    "A ready check is already active or this queue is closed.",
                    ephemeral=True,
                )
                return
            queue = await deps.party_queue_service.start_ready_check(lobby_id)
            if lobby.state is LobbyState.OPEN and len(queue.active) == lobby.capacity:
                lobby = deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.FULL,
                    operation_id=f"discord:{interaction.id}:full",
                )
            if lobby.state in {LobbyState.FULL, LobbyState.OPEN}:
                deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.READY_CHECK,
                    operation_id=f"discord:{interaction.id}:ready-check",
                )
            await interaction.response.send_message(
                content=" ".join(f"<@{member.user_id}>" for member in queue.active),
                embed=self.ready_check_embed(lobby_id, queue),
                view=deps.ready_check_view(),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )
            return
        formation_actions = {
            # Keep the v2.3-rc.1 custom ID recoverable for cards posted before this
            # deployment; refreshed cards expose the three explicit modes.
            "launch_draft": FormationMode.ROLE_FIT,
            "teams_role_fit": FormationMode.ROLE_FIT,
            "teams_balanced": FormationMode.BALANCED,
            "teams_captains": FormationMode.CAPTAINS,
        }
        if action in formation_actions:
            await self.launch_party_draft(
                interaction, lobby, formation_mode=formation_actions[action]
            )
            return
        if action in {"edit", "cancel"} and interaction.user.id != lobby.organizer_id:
            await interaction.response.send_message(
                "Only the organizer can change or cancel this lobby.",
                ephemeral=True,
            )
            return
        if action == "cancel":
            changed = deps.party_repository.transition(
                guild_id,
                lobby_id,
                LobbyState.CANCELLED,
                operation_id=f"discord:{interaction.id}:cancel",
                actor_id=interaction.user.id,
            )
            await interaction.response.edit_message(
                embed=self.lobby_card_embed(changed),
                view=None,
            )
            return
        if action == "edit":
            async def edit_handler(edit_interaction, payload):
                changed = deps.party_repository.update_metadata(
                    guild_id,
                    lobby_id,
                    operation_id=f"discord:{edit_interaction.id}:edit",
                    actor_id=edit_interaction.user.id,
                    mode=str(payload["mode"]),
                    region=str(payload["region"]),
                    format=str(payload["format"]),
                    capacity=int(payload["party_size"]),
                    voice_required=bool(payload["voice_required"]),
                    skill_band=str(payload.get("skill_band") or ""),
                    notes=str(payload.get("notes") or ""),
                )
                await edit_interaction.response.send_message(
                    embed=self.lobby_card_embed(changed),
                    view=deps.lobby_card_view(),
                    ephemeral=True,
                )

            await interaction.response.send_modal(deps.create_lobby_modal(edit_handler))
            return
        if action == "share":
            await interaction.response.send_message("Lobby shared.", ephemeral=True)
            await interaction.channel.send(
                embed=self.lobby_card_embed(lobby),
                view=deps.lobby_card_view(),
            )

    # -- Draft launch ---------------------------------------------------------

    def reconcile_active_party_draft(self, lobby, launch, actor_id: int) -> None:
        """Idempotently project a successfully started draft onto its party lobby."""
        deps = self.deps
        current = deps.party_repository.get(lobby.guild_id, lobby.lobby_id)
        if current is None or current.state is LobbyState.ACTIVE:
            return
        if current.state is not LobbyState.FORMING:
            raise PartyDraftError(
                f"active draft cannot reconcile lobby from {current.state}"
            )
        deps.party_repository.transition(
            lobby.guild_id,
            lobby.lobby_id,
            LobbyState.ACTIVE,
            operation_id=f"party-draft-active:{lobby.lobby_id}:{launch.match_id}",
            actor_id=actor_id,
        )

    async def launch_party_draft(
        self,
        interaction: discord.Interaction,
        lobby,
        *,
        formation_mode: FormationMode = FormationMode.ROLE_FIT,
    ) -> None:
        """Confirm deterministic teams and launch the existing draft engine."""
        deps = self.deps
        if interaction.user.id != lobby.organizer_id:
            await interaction.response.send_message(
                "Only the organizer can confirm teams and launch the draft.",
                ephemeral=True,
            )
            return
        existing_launch = deps.party_draft_repository.get(lobby.lobby_id)
        if existing_launch and existing_launch.status == "active":
            deps.ensure_match_history(lobby, existing_launch)
            self.reconcile_active_party_draft(lobby, existing_launch, interaction.user.id)
            await interaction.response.send_message(
                f"Draft `{existing_launch.match_id}` is already active.",
                ephemeral=True,
            )
            return
        if lobby.state is not LobbyState.FORMING:
            await interaction.response.send_message(
                "Finish the ready check before launching a draft.",
                ephemeral=True,
            )
            return
        channel = interaction.channel
        if channel is None:
            await interaction.response.send_message(
                "Draft channel is unavailable.", ephemeral=True
            )
            return
        if deps.channel_has_active(channel.id):
            await interaction.response.send_message(
                "This channel already has an active session or draft.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        launch = None
        draft_started = False
        try:
            scrim = deps.scrim_repository.get_challenge_by_lobby(
                lobby.guild_id, lobby.lobby_id
            )
            launch, should_start = deps.party_draft_repository.begin(
                lobby,
                operation_id=f"discord:{interaction.id}:party-draft",
                channel_id=channel.id,
                match_id_factory=deps.reserve_match_id,
                formation_mode=formation_mode,
                fixed_teams=(
                    deps.scrim_repository.fixed_draft_teams(scrim) if scrim else None
                ),
            )
            if not should_start:
                if launch.status == "active":
                    self.reconcile_active_party_draft(lobby, launch, interaction.user.id)
                    message = f"Draft `{launch.match_id}` is already active."
                else:
                    message = "That draft launch is already in progress."
                await interaction.followup.send(message, ephemeral=True)
                return

            blue_member = interaction.guild.get_member(launch.blue.captain_id)
            red_member = interaction.guild.get_member(launch.red.captain_id)
            if blue_member is None or red_member is None:
                raise PartyDraftError("a selected captain is no longer in this server")

            if deps.activity_client.enabled:
                result = await deps.activity_client.post(
                    "/api/draft/start",
                    {
                        "blueCaptainId": str(blue_member.id),
                        "blueCaptainName": blue_member.display_name,
                        "redCaptainId": str(red_member.id),
                        "redCaptainName": red_member.display_name,
                        "guildId": str(lobby.guild_id),
                        "channelId": str(channel.id),
                        "godforgeMatchId": launch.match_id,
                        "party": launch.snapshot,
                    },
                )
                if not result or not result.get("matchId"):
                    raise PartyDraftError("the Activity backend did not start the draft")
                activity_id = result["matchId"]
                # Register the activity draft through the coordinator, which owns
                # the in-memory draft state and the WS listener (Issue #48).
                deps.draft_coordinator.match_ids[channel.id] = activity_id
                deps.draft_coordinator.match_channels[activity_id] = channel.id
                if result.get("state"):
                    deps.draft_coordinator.snapshots[channel.id] = result["state"]
                    sent = await channel.send(
                        f"🎮 Draft `{activity_id}` started — open the Activity and "
                        "enter this ID to join",
                        embed=deps.formatter.format_board_from_snapshot(result["state"]),
                    )
                    deps.draft_coordinator.board_message_ids[channel.id] = sent.id
                deps.draft_coordinator.start_ws(activity_id, channel.id)
            else:
                draft = deps.drafts.start(
                    channel.id,
                    blue_member.id,
                    blue_member.display_name,
                    red_member.id,
                    red_member.display_name,
                    lobby.guild_id,
                    interaction.guild.name,
                    getattr(channel, "name", "party-draft"),
                    match_id=launch.match_id,
                    party_context=launch.snapshot,
                )
                if draft is None:
                    raise PartyDraftError("this channel already has an active draft")
                deps.save_active_draft(channel.id, draft.draft_id)

            launch = deps.party_draft_repository.mark_active(lobby.lobby_id)
            draft_started = True
            match_record = deps.ensure_match_history(lobby, launch)
            self.reconcile_active_party_draft(lobby, launch, interaction.user.id)
            formation = launch.snapshot.get("formation") or {}
            assignment_lines = []
            for side, emoji in (("blue", "🔵"), ("red", "🔴")):
                assignments = formation.get(side) or []
                assignment_lines.append(
                    f"{emoji} "
                    + " · ".join(
                        f"**{str(item['role']).title()}** <@{item['user_id']}>"
                        for item in assignments
                    )
                )
            formation_summary = ""
            if formation:
                label = str(formation.get("mode", formation_mode.value))
                formation_summary = (
                    f"\n\n**{label.replace('_', ' ').title()}**\n"
                    f"{formation.get('explanation', '')}\n"
                    + "\n".join(assignment_lines)
                )
            await channel.send(
                f"⚔️ Draft `{launch.match_id}` launched from lobby `{lobby.lobby_id[:8]}`.\n"
                f"🔵 Captain <@{launch.blue.captain_id}>: "
                + " ".join(f"<@{user_id}>" for user_id in launch.blue.participant_ids)
                + f"\n🔴 Captain <@{launch.red.captain_id}>: "
                + " ".join(f"<@{user_id}>" for user_id in launch.red.participant_ids)
                + formation_summary,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )
            await channel.send(
                embed=deps.match_result_embed(match_record),
                view=deps.match_result_view(),
            )
            await interaction.followup.send(
                f"Draft `{launch.match_id}` started. Teams and lobby rules are retained.",
                ephemeral=True,
            )
        except Exception as exc:
            if launch is not None and not draft_started:
                deps.party_draft_repository.mark_failed(lobby.lobby_id, str(exc))
            deps.log.exception(
                "Party draft launch failed for lobby %s", lobby.lobby_id
            )
            if draft_started:
                await interaction.followup.send(
                    f"Draft `{launch.match_id}` started, but GodForge could not refresh "
                    "the lobby card. Press `Launch Draft` again to reconcile it safely. "
                    + str(exc),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "Draft launch failed. The lobby is still ready to retry with "
                    "`Launch Draft`. " + str(exc),
                    ephemeral=True,
                )

    # -- Ready check ------------------------------------------------------------

    async def handle_ready_check_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        deps = self.deps
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Server-only action.", ephemeral=True)
            return
        lobby_id = self.lobby_id_from_interaction(interaction)
        status = {
            "ready": ReadyStatus.READY,
            "need_five": ReadyStatus.NEED_5,
            "drop": ReadyStatus.DROP,
        }[action]
        queue, promoted_id = await deps.party_queue_service.respond(
            lobby_id,
            interaction.user.id,
            status,
        )
        if status is ReadyStatus.DROP:
            changed = deps.party_repository.remove_participant(
                guild_id,
                lobby_id,
                interaction.user.id,
                operation_id=f"discord:{interaction.id}:ready-drop",
                actor_id=interaction.user.id,
            )
            if promoted_id is not None:
                promoted = deps.party_repository.get_player_preferences(
                    guild_id, promoted_id
                )
                changed = deps.party_repository.save_participant(
                    guild_id,
                    lobby_id,
                    Participant(
                        promoted_id,
                        primary_role=promoted.primary_role,
                        secondary_role=promoted.secondary_role,
                        fill=promoted.fill,
                        captain=promoted.captain,
                    ),
                    operation_id=f"discord:{interaction.id}:ready-promote:{promoted_id}",
                )
            if changed.state is LobbyState.READY_CHECK:
                deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.OPEN,
                    operation_id=f"discord:{interaction.id}:reopen",
                )
        everyone_ready = bool(queue.active) and all(
            queue.ready.get(member.user_id) is ReadyStatus.READY
            for member in queue.active
        )
        if everyone_ready:
            room_failure = None
            lobby = deps.party_repository.get(guild_id, lobby_id)
            if lobby and interaction.guild:
                try:
                    rooms = await deps.match_room_service_for_guild(
                        interaction.guild
                    ).provision(
                        guild_id=guild_id,
                        lobby_id=lobby_id,
                        organizer_id=lobby.organizer_id,
                        participant_ids=tuple(
                            participant.user_id for participant in lobby.participants
                        ),
                        create_team_voice=lobby.voice_required,
                    )
                    room_channel = interaction.guild.get_channel(rooms.text_room_id)
                    if isinstance(room_channel, discord.TextChannel):
                        await room_channel.send(
                            f"<@{lobby.organizer_id}> temporary coordination is ready. "
                            f"Use `/party room` with lobby ID `{lobby_id}` to lock, "
                            "unlock, remove, transfer, move, or close these rooms.",
                            allowed_mentions=discord.AllowedMentions(
                                users=True, roles=False
                            ),
                        )
                except (discord.Forbidden, discord.HTTPException, RuntimeError) as exc:
                    room_failure = str(exc)
            if (
                room_failure is None
                and lobby
                and lobby.state is LobbyState.READY_CHECK
            ):
                lobby = deps.party_repository.transition(
                    guild_id,
                    lobby_id,
                    LobbyState.FORMING,
                    operation_id=f"discord:{interaction.id}:forming",
                )
            await interaction.response.edit_message(
                content=(
                    "Everyone is ready. GodForge is forming the match."
                    if room_failure is None
                    else "Everyone is ready, but GodForge could not create temporary "
                    f"rooms: {room_failure}. Fix the permission and press Ready again."
                ),
                embed=self.ready_check_embed(lobby_id, queue),
                view=(None if room_failure is None else deps.ready_check_view()),
            )
            return
        await interaction.response.edit_message(
            embed=self.ready_check_embed(lobby_id, queue),
            view=deps.ready_check_view(),
        )
        if promoted_id is not None:
            await interaction.followup.send(
                f"<@{promoted_id}> was promoted from the waitlist.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )

    # -- Ready-check expiry (periodic cleanup) -------------------------------

    async def expire_ready_checks(self, ctx: LifecycleContext) -> None:
        """Time out ready checks whose deadline has passed.

        A timed-out queue that has been cancelled also cancels its lobby; the
        organizer's play channel (if configured) is notified either way.
        """
        deps = self.deps
        for record in deps.party_repository.recover_active():
            lobby = record.lobby
            if lobby.state is not LobbyState.READY_CHECK:
                continue
            queue, timed_out = await deps.party_queue_service.expire(lobby.lobby_id)
            if not timed_out:
                continue
            if queue.status is QueueStatus.CANCELLED:
                deps.party_repository.transition(
                    lobby.guild_id,
                    lobby.lobby_id,
                    LobbyState.CANCELLED,
                    operation_id=f"ready-timeout:{lobby.lobby_id}:{queue.ready_deadline}",
                    reason="ready check timed out",
                )
            guild_settings = deps.settings_module.get_guild_settings(str(lobby.guild_id))
            channel_id = guild_settings["managed"].get("playChannelId")
            channel = ctx.get_channel(int(channel_id)) if channel_id else None
            if channel:
                await channel.send(
                    "Ready check timed out for "
                    + " ".join(f"<@{user_id}>" for user_id in timed_out),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False),
                )


class PartyLobbyFeature:
    """Registers ready-check-expiry cleanup with the shared registry."""

    name = "party_lobby"

    def __init__(self, service: PartyLobbyService) -> None:
        self.service = service

    async def on_cleanup(self, ctx: LifecycleContext) -> None:
        await self.service.expire_ready_checks(ctx)
