"""
GodForge — Smite 2 Discord Bot

Session system: when a session is active in a channel, .rg and .roll5
produce interactive embeds with reactions for tracking random god picks.

Draft system: integrates with an Activity backend to facilitate competitive
drafting. Captains participate in a Discord Activity while the bot mirrors
draft state as a live, updating embed in the channel.

Sessions and drafts are mutually exclusive per channel.

Run with: python bot.py
"""

import asyncio
import io
import json
import logging
import os
import re

import aiohttp
import discord
from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from utils import custom_commands, draft_support, formatter, loader, parser, picker, settings
from utils.formatter import NUMBER_EMOJIS
from utils.resolver import resolve_god_name
from utils.session import SessionManager
from utils.draft import DraftManager
from utils.forgelens_adapter import ForgeLensAdapter, forgelens_enabled
from utils.party_store import SQLitePartyRepository
from utils.party import LobbyState, Participant, PlayerPreferences
from utils.guild_setup import (
    GuildSetupService,
    PermissionSnapshot,
    SetupOperationError,
    SetupReferences,
)
from utils.managed_roles import ManagedRoleError, reconcile as reconcile_roles, set_member_role
from utils.setup_views import PlayPanelView, RolePreferencesView
from utils.lobby_views import (
    CreateLobbyModal,
    JoinPreferencesModal,
    LobbyCardView,
    MatchContinuityView,
    MatchResultView,
    ReadyCheckView,
)
from utils.party_queue import (
    PartyQueueService,
    QueueError,
    QueueStatus,
    ReadyStatus,
    SQLitePartyQueueRepository,
)
from utils.party_draft import PartyDraftError, PartyDraftLaunchRepository
from utils.party_schedule import (
    Recurrence,
    ScheduleError,
    ScheduleRepository,
    calendar_ics,
    convert_to_lobby,
    parse_local_start,
)
from utils.scrims import (
    ChallengeState,
    ScrimError,
    ScrimRepository,
    launch_scrim,
)
from utils.activity_backend import ActivityBackendClient
from utils.active_drafts import ActiveDraftStore
from utils.custom_command_runtime import CustomCommandRuntime
from utils.draft_coordinator import DraftCoordinator, DraftFeature
from utils.room_lifecycle import RoomLifecycle
from utils.draft_render import DraftRenderer
from utils import match_results
from utils.match_room_factory import MatchRoomServiceFactory
from utils.match_actions import (
    MatchActionDeps,
    handle_match_continuity_action,
    handle_match_result_action,
)
from utils.party_lobby import PartyLobbyDeps, PartyLobbyService
from utils.party_room_command import PartyRoomCommandDeps, register_party_room_command
from utils.party_setup_command import PartySetupCommandDeps, register_party_setup_command
from utils.schedule_commands import ScheduleCommandDeps, register_schedule_commands
from utils.scrim_commands import ScrimCommandDeps, register_scrim_commands
from utils.session_commands import SessionCommandHandler
from utils.lifecycle import FeatureRegistry, LifecycleContext
from utils.routing import CommandRegistry
from utils.r67.feature import R67Feature
from utils.r67.repository import SQLiteR67Repository
from utils.r67.service import R67Service, build_survivor_announcement
from utils.match_history import (
    MatchHistoryRepository,
    MatchOutcome,
    MatchPlayer,
    MatchTeam,
)
from utils.match_continuity import (
    ContinuityError,
    ContinuityStatus,
    MatchContinuityRepository,
    MatchContinuityService,
)
from utils.team_formation import FormationMode
from utils import match_ids
from utils.match_rooms import MatchRoomService, SQLiteMatchRoomRepository
from utils.discord_match_rooms import DiscordMatchRoomOperations
from utils import ledger as ledger_utils
from utils import wallet as wallet_utils

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
ACTIVITY_BACKEND_URL = os.getenv("ACTIVITY_BACKEND_URL", "").rstrip("/")
ACTIVITY_API_KEY = os.getenv("ACTIVITY_API_KEY", "")
LEGACY_ECONOMY_ENABLED = os.getenv("GODFORGE_ENABLE_LEGACY_ECONOMY", "").strip().lower() in {
    "1", "true", "yes", "on"
}

# Channel IDs for the betting system.
# Set these in .env (or leave 0 to disable that feature).
BETTING_LEDGER_CHANNEL_ID = int(os.getenv("BETTING_LEDGER_CHANNEL_ID", "0"))
PLACE_BETS_CHANNEL_ID = int(os.getenv("PLACE_BETS_CHANNEL_ID", "0"))
MATCH_DRAFT_CHANNEL_ID = int(os.getenv("MATCH_DRAFT_CHANNEL_ID", "0"))

_GOD_USER_ID = int(os.getenv("GODFORGE_OWNER_USER_ID", "0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("godforge")

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True


class GodForgeClient(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(PlayPanelView(_handle_play_panel_action))
        self.add_view(RolePreferencesView(_handle_role_preference))
        self.add_view(LobbyCardView(_handle_lobby_card_action))
        self.add_view(ReadyCheckView(_handle_ready_check_action))
        self.add_view(MatchResultView(_handle_match_result_action))
        self.add_view(MatchContinuityView(_handle_match_continuity_action))
        self.add_view(ScrimChallengeView())
        await self.tree.sync()


client = GodForgeClient(intents=intents)

sessions = SessionManager()
drafts = DraftManager()
_PARTY_DB_PATH = os.getenv("GODFORGE_PARTY_DB_PATH", "data/godforge_party.db")
party_repository = SQLitePartyRepository(_PARTY_DB_PATH)
party_queue_service = PartyQueueService(
    SQLitePartyQueueRepository(
        _PARTY_DB_PATH
    )
)
party_draft_repository = PartyDraftLaunchRepository(_PARTY_DB_PATH)
match_history_repository = MatchHistoryRepository(_PARTY_DB_PATH)
match_continuity_repository = MatchContinuityRepository(_PARTY_DB_PATH)
match_room_repository = SQLiteMatchRoomRepository(_PARTY_DB_PATH)
schedule_repository = ScheduleRepository(_PARTY_DB_PATH)
scrim_repository = ScrimRepository(_PARTY_DB_PATH)
r67_repository = SQLiteR67Repository(_PARTY_DB_PATH)
r67_service = R67Service(r67_repository)
forgelens_adapter = ForgeLensAdapter()

# Shared feature lifecycle registry (Issue #48). Features own their recovery and
# cleanup and register it here; bot.py only orchestrates the shared phases.
feature_registry = FeatureRegistry()
feature_registry.register(R67Feature(r67_service))


def _lifecycle_context() -> LifecycleContext:
    return LifecycleContext(
        get_guild=client.get_guild,
        get_channel=client.get_channel,
        get_user=client.get_user,
        fetch_user=client.fetch_user,
        guilds=tuple(client.guilds),
    )


# Shared dot-command routing seam (Issue #48). Features register the exact
# command token(s) they own; on_message resolves them in one lookup.
command_registry = CommandRegistry()

# Track metadata for reaction-enabled messages (sessions only).
_tracked_messages = {}

# Durable local-draft restart pointer is owned by ActiveDraftStore (Issue #48).
active_draft_store = ActiveDraftStore()


def _save_active_draft(channel_id: int, draft_id: str):
    active_draft_store.save(channel_id, draft_id)


def _remove_active_draft(channel_id: int):
    active_draft_store.remove(channel_id)

REPORTS_CHANNELS = {
    int(gid.strip()): int(cid.strip())
    for pair in os.getenv("GODFORGE_REPORTS_CHANNELS", "").split(",")
    if ":" in pair
    for gid, cid in [pair.split(":", 1)]
    if gid.strip() and cid.strip()
}


def _channel_has_active(channel_id: int) -> str | None:
    # Session state is bot-owned; draft state is owned by the draft coordinator.
    if sessions.get(channel_id):
        return "session"
    if draft_coordinator.has_active_draft(channel_id):
        return "draft"
    return None


def _cleanup_draft(channel_id: int) -> None:
    draft_coordinator.cleanup_draft(channel_id)


# Pure draft-command helpers are owned by utils/draft_support (Issue #48).
def _draft_start_options(content: str) -> dict:
    return draft_support.draft_start_options(content)


def _draft_completion_marker(draft) -> str:
    return draft_support.draft_completion_marker(draft)


# ── Activity backend helpers ──────────────────────────────────────────────────
# HTTP access to the optional draft activity backend is owned by the client
# (Issue #48); bot.py keeps thin delegators for the existing call sites.
activity_client = ActivityBackendClient(ACTIVITY_BACKEND_URL, ACTIVITY_API_KEY)


def _activity_headers() -> dict:
    return activity_client.headers()


async def _activity_post(path: str, data: dict | None = None) -> dict | None:
    return await activity_client.post(path, data)


async def _activity_get(path: str) -> dict | None:
    return await activity_client.get(path)


# Activity-draft rendering, export, and the WS listener are owned by the draft
# coordinator (Issue #48). bot.py keeps thin delegators for existing call sites.
async def _update_embed_from_snapshot(snapshot: dict, channel) -> None:
    await draft_coordinator.update_embed_from_snapshot(snapshot, channel)


async def _post_export(export: dict, channel) -> None:
    await draft_coordinator.post_export(export, channel)


async def _listen_draft_ws(match_id: str, channel_id: int) -> None:
    await draft_coordinator.listen_ws(match_id, channel_id)


# ── Discord events ────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} (id: {client.user.id})")
    log.info(f"Connected to {len(client.guilds)} guild(s)")
    if not cleanup_task.is_running():
        cleanup_task.start()
    log.info("Economy and betting commands are disabled in standalone GodForge.")
    recoverable_lobbies = party_repository.recover_active()
    if recoverable_lobbies:
        log.info("Recovered %s active party lobby record(s)", len(recoverable_lobbies))

    await feature_registry.run_startup(_lifecycle_context())


@tasks.loop(minutes=5)
async def cleanup_task():
    expired_sessions = sessions.cleanup_expired()
    if expired_sessions:
        to_remove = [mid for mid, info in _tracked_messages.items()
                     if info.get("channel_id") in expired_sessions]
        for mid in to_remove:
            del _tracked_messages[mid]
        log.info(f"Cleaned up {len(expired_sessions)} expired session(s)")

    expired_drafts = drafts.cleanup_expired()
    if expired_drafts:
        log.info(f"Cleaned up {len(expired_drafts)} expired local draft(s)")

    await feature_registry.run_cleanup(_lifecycle_context())

    for event, minutes, occurrence in schedule_repository.claim_due_reminders():
        recipients = {event.organizer_id, *(rsvp.user_id for rsvp in event.rsvps)}
        recipients.update(rsvp.user_id for rsvp in event.waitlist)
        for user_id in recipients:
            try:
                user = client.get_user(user_id) or await client.fetch_user(user_id)
                await user.send(
                    f"**{event.title}** starts <t:{int(occurrence.timestamp())}:R> "
                    f"({minutes}-minute reminder)."
                )
            except (discord.Forbidden, discord.HTTPException):
                log.info("Could not DM scheduled-night reminder to user %s", user_id)

    for record in party_repository.recover_active():
        lobby = record.lobby
        if lobby.state is not LobbyState.READY_CHECK:
            continue
        queue, timed_out = await party_queue_service.expire(lobby.lobby_id)
        if not timed_out:
            continue
        if queue.status is QueueStatus.CANCELLED:
            party_repository.transition(
                lobby.guild_id,
                lobby.lobby_id,
                LobbyState.CANCELLED,
                operation_id=f"ready-timeout:{lobby.lobby_id}:{queue.ready_deadline}",
                reason="ready check timed out",
            )
        guild_settings = settings.get_guild_settings(str(lobby.guild_id))
        channel_id = guild_settings["managed"].get("playChannelId")
        channel = client.get_channel(int(channel_id)) if channel_id else None
        if channel:
            await channel.send(
                "Ready check timed out for "
                + " ".join(f"<@{user_id}>" for user_id in timed_out),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False),
            )


@cleanup_task.error
async def cleanup_task_error(exc: Exception):
    log.error(f"cleanup_task crashed: {exc!r} — restarting")
    cleanup_task.restart()


@client.event
async def on_voice_state_update(member, before, after):
    changed_ids = {
        channel.id
        for channel in (before.channel, after.channel)
        if channel is not None
    }
    if not changed_ids:
        return
    for rooms in match_room_repository.active(member.guild.id):
        if not changed_ids.intersection(rooms.team_voice_ids):
            continue
        guild = member.guild
        voice_channels = [
            guild.get_channel(channel_id) for channel_id in rooms.team_voice_ids
        ]
        service = _match_room_service_for_guild(guild)
        if any(getattr(channel, "members", ()) for channel in voice_channels):
            await service.mark_occupied(rooms.lobby_id)
        else:
            await service.mark_empty(rooms.lobby_id)


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return
    if not message.content.startswith("."):
        # Ordinary chat: the only GodForge behavior here is optional passive
        # 67 reactions. Everything else requires the dot-command prefix.
        await _handle_r67_passive(message)
        return

    # ---- Feature-registered exact-token commands (see utils/routing) ----
    _first = message.content[1:].split()[0].lower() if message.content[1:].split() else ""
    _command_handler = command_registry.resolve(_first)
    if _command_handler is not None:
        await _command_handler(message)
        return

    intent = parser.parse(message.content)
    if intent is None:
        handled = await _handle_custom_command(message, _first)
        if handled:
            return
        return

    channel_id = message.channel.id

    try:
        if intent["kind"] == "help":
            await message.channel.send(embed=formatter.format_help_page1(), view=HelpView())
            return

        elif intent["kind"] == "session":
            async with sessions.get_lock(channel_id):
                response = await _handle_session(intent, channel_id)

        elif intent["kind"] == "draft":
            response = await _handle_draft(intent, message)
            if response is None:
                return

        elif intent["kind"] == "draft_action":
            response = await _handle_draft_action(intent, message)
            if response is None:
                return

        elif intent["kind"] == "god":
            async with sessions.get_lock(channel_id):
                session = sessions.get(channel_id)
                exclude = session.get_excluded_gods() if session else None
                god = picker.pick_god(loader.gods(), intent["role"], intent["source"],
                                      exclude=exclude)
                if session:
                    embed = formatter.format_rg_session(god, intent["role"], intent["source"])
                    sent = await message.channel.send(embed=embed)
                    session.register_rg(sent.id, god, intent["role"], intent["source"])
                    _tracked_messages[sent.id] = {
                        "kind": "rg", "god": god, "channel_id": channel_id,
                        "role": intent["role"], "source": intent["source"],
                        "author_id": message.author.id,
                        "author_name": message.author.display_name,
                    }
                    await sent.add_reaction("✅")
                    await sent.add_reaction("❌")
                    return
                else:
                    response = formatter.format_god(god, intent["role"], intent["source"])

        elif intent["kind"] == "roll5":
            async with sessions.get_lock(channel_id):
                session = sessions.get(channel_id)
                exclude = session.get_excluded_gods() if session else None
                gods = picker.pick_team(loader.gods(), intent["role"], intent["source"],
                                        exclude=exclude)
                if session:
                    embed = formatter.format_roll5_session(gods, intent["role"], intent["source"])
                    sent = await message.channel.send(embed=embed)
                    session.register_roll5(sent.id, gods)
                    _tracked_messages[sent.id] = {
                        "kind": "roll5", "gods": gods, "channel_id": channel_id,
                        "role": intent["role"], "source": intent["source"],
                        "author_id": message.author.id,
                        "author_name": message.author.display_name,
                    }
                    for emoji in NUMBER_EMOJIS:
                        await sent.add_reaction(emoji)
                    return
                else:
                    response = formatter.format_team(gods, intent["role"], intent["source"])

        elif intent["kind"] == "build":
            items = picker.pick_build(
                loader.builds(), intent["role"], intent["type"], intent["count"]
            )
            response = formatter.format_build(items, intent["role"], intent["type"])

        else:
            return

    except ValueError as e:
        response = formatter.format_error(str(e))
    except FileNotFoundError as e:
        log.error(f"Data file missing: {e}")
        response = formatter.format_error("Data file missing. Check bot logs.")
    except Exception as e:
        log.exception(f"Unexpected error handling '{message.content}'")
        response = formatter.format_error("Something went wrong. Check bot logs.")

    if isinstance(response, discord.Embed):
        await message.channel.send(embed=response)
    else:
        await message.channel.send(response)


# ── Session handlers ──────────────────────────────────────────────────────────

# `.session` command behavior is owned by the feature handler (Issue #48); bot.py
# injects the shared collaborators and keeps a thin delegator.
session_command_handler = SessionCommandHandler(
    sessions=sessions,
    formatter=formatter,
    tracked_messages=_tracked_messages,
    channel_has_active=_channel_has_active,
)


async def _handle_session(intent: dict, channel_id: int):
    return await session_command_handler.handle(intent, channel_id)


# ── Draft handlers ────────────────────────────────────────────────────────────

# The draft command handlers (local + activity) are owned by DraftCoordinator
# (Issue #48); bot.py keeps thin delegators.
async def _handle_draft(intent: dict, message: discord.Message):
    return await draft_coordinator.handle_draft(intent, message)


async def _handle_draft_action(intent: dict, message: discord.Message):
    return await draft_coordinator.handle_draft_action(intent, message)


# ── Local draft helpers (used when ACTIVITY_BACKEND_URL is not set) ───────────

# Draft board / claim-embed rendering is owned by DraftRenderer (Issue #48).
draft_renderer = DraftRenderer(
    formatter=formatter,
    tracked_messages=_tracked_messages,
    number_emojis=NUMBER_EMOJIS,
)


async def _update_draft_board(draft, channel):
    await draft_renderer.update_board(draft, channel)


async def _post_claim_embeds(draft, channel):
    await draft_renderer.post_claim_embeds(draft, channel)


async def _update_claim_embed(draft, team, channel):
    await draft_renderer.update_claim_embed(draft, team, channel)


# Draft feature coordinator (Issue #48): owns local + activity draft handlers,
# the activity-draft state, the WS listener, export posting, and claim reactions.
draft_coordinator = DraftCoordinator(
    client=client,
    drafts=drafts,
    activity_client=activity_client,
    renderer=draft_renderer,
    active_draft_store=active_draft_store,
    tracked_messages=_tracked_messages,
    formatter=formatter,
    resolve_god_name=resolve_god_name,
    reports_channels=REPORTS_CHANNELS,
    number_emojis=NUMBER_EMOJIS,
    channel_has_active=_channel_has_active,
    log=log,
)
feature_registry.register(DraftFeature(draft_coordinator))


# ── Reaction handler ──────────────────────────────────────────────────────────

@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return
    message_id = payload.message_id
    if message_id not in _tracked_messages:
        return
    info = _tracked_messages[message_id]
    channel_id = info["channel_id"]
    emoji = str(payload.emoji)

    if info["kind"] in ("roll5", "rg"):
        await _handle_session_reaction(payload, info, message_id, channel_id, emoji)
    elif info["kind"] == "claim":
        await _handle_claim_reaction(payload, info, message_id, channel_id, emoji)


async def _handle_claim_reaction(payload, info, message_id, channel_id, emoji):
    await draft_coordinator.handle_claim_reaction(
        payload, info, message_id, channel_id, emoji
    )


async def _handle_session_reaction(payload, info, message_id, channel_id, emoji):
    async with sessions.get_lock(channel_id):
        session = sessions.get(channel_id)
        if not session:
            return
        if session.is_reaction_processed(message_id, emoji):
            return
        session.mark_reaction_processed(message_id, emoji)

        channel = client.get_channel(channel_id)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _tracked_messages.pop(message_id, None)
            return

        author_id = info["author_id"]
        author_name = info["author_name"]

        if info["kind"] == "roll5" and emoji in NUMBER_EMOJIS:
            index = NUMBER_EMOJIS.index(emoji)
            god = session.lock_roll5_pick(message_id, index, author_id, author_name)
            if god:
                embed = formatter.format_roll5_locked(
                    info["gods"], index, author_name, info["role"], info["source"],
                )
                await msg.edit(embed=embed)
                try:
                    await msg.clear_reactions()
                except discord.Forbidden:
                    pass
                _tracked_messages.pop(message_id, None)

        elif info["kind"] == "rg":
            if emoji == "✅":
                god = session.lock_rg_pick(message_id, author_id, author_name)
                if god:
                    embed = formatter.format_rg_locked(god, author_name, info["role"], info["source"])
                    await msg.edit(embed=embed)
                    try:
                        await msg.clear_reactions()
                    except discord.Forbidden:
                        pass
                    _tracked_messages.pop(message_id, None)
            elif emoji == "❌":
                god = session.discard_rg(message_id)
                if god:
                    embed = formatter.format_rg_discarded(god, info["role"], info["source"])
                    await msg.edit(embed=embed)
                    try:
                        await msg.clear_reactions()
                    except discord.Forbidden:
                        pass
                    _tracked_messages.pop(message_id, None)
                    log.info(f"Session discard: {god} discarded "
                             f"in channel {channel_id}")


# ── Legacy economy helpers ───────────────────────────────────────────────────

def _is_admin(message: discord.Message) -> bool:
    """True if the author is the bot owner or has server administrator permission."""
    if message.author.id == _GOD_USER_ID:
        return True
    member = message.author
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _can_manage_guild(message: discord.Message) -> bool:
    """True if the author may manage guild-level r67 settings.

    Discord's ``Manage Guild`` permission (or Administrator, which implies it)
    is required per Issue #47 (Gate 4); the bot owner is always allowed.
    """
    if getattr(message.author, "id", None) == _GOD_USER_ID:
        return True
    perms = getattr(message.author, "guild_permissions", None)
    return bool(perms and (getattr(perms, "manage_guild", False) or perms.administrator))


async def _handle_r67_command(message: discord.Message):
    """Thin adapter: parse the `.r67` command and send the service reply.

    All branching and business logic lives in ``R67Service``; this boundary only
    extracts Discord inputs and delivers the returned text.
    """
    # Split on arbitrary whitespace so ".r67\nstatus" or ".r67\tstatus" parse the
    # same as ".r67 status" (matches the whitespace-aware first-token detection).
    parts = message.content[1:].split(maxsplit=1)
    remainder = parts[1].strip().lower() if len(parts) > 1 else ""
    guild_id = message.guild.id if message.guild else None
    reply = r67_service.handle_command(
        guild_id,
        remainder,
        can_manage_guild=_can_manage_guild(message),
    )
    await message.channel.send(reply)


async def _handle_r67_passive(message: discord.Message):
    """Adapter for optional passive 67 reactions on ordinary guild messages.

    Passive reactions are opt-in per guild and skipped entirely in DMs or when
    the guild has disabled GodForge. All matching, cooldown, and roll logic lives
    in ``R67Service``; this boundary only guards eligibility and delivers text.
    """
    if message.guild is None:
        return
    guild_settings = settings.get_guild_settings(str(message.guild.id))
    if not guild_settings["features"].get("botEnabled", True):
        return
    try:
        outcome = r67_service.process_passive(
            message.guild.id,
            message.channel.id,
            message.author.id,
            message.content,
        )
    except Exception:
        log.exception("r67 passive handler failed for guild %s", message.guild.id)
        return

    if outcome.survivor_winners:
        await _run_r67_survivor_event(message, outcome.survivor_winners)
    if outcome.response:
        await message.channel.send(outcome.response)


async def _run_r67_survivor_event(message: discord.Message, winners: list[int]):
    """Grant the cosmetic 67 Survivor role and post the event announcement.

    The event cooldown is already committed by ``process_passive``; role failures
    never suppress the announcement (Gate 3). Removal is handled durably by the
    periodic cleanup task and startup recovery.
    """
    try:
        result = await r67_service.grant_survivor_roles(message.guild, winners)
        marked = result.marked
    except Exception:
        log.exception("67 Survivor role grant failed for guild %s", message.guild.id)
        marked = False
    log.info(
        "67 Survivor event fired in guild %s channel %s (marked=%s)",
        message.guild.id,
        message.channel.id,
        marked,
    )
    announcement = build_survivor_announcement(winners, marked)
    try:
        await message.channel.send(
            announcement,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )
    except (discord.Forbidden, discord.HTTPException):
        log.info("Could not post 67 Survivor announcement in guild %s", message.guild.id)


async def _handle_deprecated_economy_command(message: discord.Message, command: str):
    await message.channel.send(
        f"⚠️ `.{command}` is deprecated in GodForge. "
        "Economy, wallets, ledgers, and betting are not available in standalone "
        "GodForge. Use GodForge for parties, randomizers, sessions, and drafts."
    )


async def _deprecated_economy_entry(message: discord.Message):
    """Registry entry point for deprecated economy tokens."""
    command = message.content[1:].split()[0].lower()
    await _handle_deprecated_economy_command(message, command)


# Feature-owned command registration (Issue #48). Each feature declares the exact
# dot-command tokens it owns; on_message resolves them through command_registry.
command_registry.register(("match", "bet", "wallet", "ledger"), _deprecated_economy_entry)
command_registry.register(("r67",), _handle_r67_command)


# Custom-command execution is owned by the feature runtime (Issue #48). bot.py
# constructs it with the shared admin check and keeps thin delegators so the
# adapter boundary (and existing tests) stay stable.
custom_command_runtime = CustomCommandRuntime(is_admin=_is_admin)
# Backward-compatible alias for the per-user cooldown map (same object).
_custom_command_cooldowns = custom_command_runtime.cooldowns


async def _handle_custom_command(message: discord.Message, trigger: str) -> bool:
    """Execute a dashboard-configured custom command if one matches."""
    return await custom_command_runtime.handle(message, trigger)


def _extract_team_names(message: discord.Message) -> list[str]:
    """Return up to 2 team name strings from a message.

    Priority: role mentions → user mentions → quoted strings → raw @word.
    """
    teams: list[str] = []
    for r in message.role_mentions:
        if len(teams) >= 2:
            break
        teams.append(f"@{r.name}")
    for u in message.mentions:
        if len(teams) >= 2:
            break
        name = f"@{u.display_name}"
        if name not in teams:
            teams.append(name)
    if len(teams) < 2:
        # Quoted plain-text team names: .match create "Whiskey Whales" "Shadow Council"
        for part in re.findall(r'"([^"]+)"', message.content):
            if len(teams) >= 2:
                break
            name = f"@{part}"
            if name not in teams:
                teams.append(name)
    if len(teams) < 2:
        # Legacy: raw @word (single-word, no spaces)
        for part in re.findall(r'@([^\s<>@]+)', message.content):
            if len(teams) >= 2:
                break
            name = f"@{part}"
            if name not in teams:
                teams.append(name)
    return teams[:2]


def _find_matching_team(message: discord.Message, stored_teams: list[str]) -> str | None:
    """Return which stored team name is referenced in the message, or None.

    Priority: role mentions → user mentions → plain-text (with or without @).
    Longer team names are checked first to prevent partial matches.
    """
    for r in message.role_mentions:
        name = f"@{r.name}"
        if name in stored_teams:
            return name
    for u in message.mentions:
        name = f"@{u.display_name}"
        if name in stored_teams:
            return name
    # Plain-text fallback — longest names first so "Whiskey Whales" beats "Whales"
    content_lower = message.content.lower()
    for team in sorted(stored_teams, key=len, reverse=True):
        if team.lower() in content_lower:          # legacy: "@Whiskey Whales" in content
            return team
        if team.lstrip("@").lower() in content_lower:  # new: "Whiskey Whales" in content
            return team
    return None


def _extract_player_name(message: discord.Message) -> str | None:
    """Return the first mentioned user's display name prefixed with @, or None."""
    if message.mentions:
        return f"@{message.mentions[0].display_name}"
    for part in re.findall(r'@([^\s<>@]+)', message.content):
        return f"@{part}"
    return None


async def _post_wallets_to_reports(guild: discord.Guild | None):
    """Post a wallets.json snapshot to #godforge-reports."""
    if not guild or guild.id not in REPORTS_CHANNELS:
        return
    reports_ch = client.get_channel(REPORTS_CHANNELS[guild.id])
    if not reports_ch:
        return
    data = wallet_utils.load_wallets()
    json_bytes = json.dumps(data, indent=2).encode("utf-8")
    file = discord.File(io.BytesIO(json_bytes), filename="wallets_snapshot.json")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        await reports_ch.send(f"📊 **Wallet snapshot** ({ts}):", file=file)
        log.info(f"Wallets posted to reports channel {REPORTS_CHANNELS[guild.id]}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning(f"Could not post wallets to reports: {exc}")


# ---------------------------------------------------------------------------
# Legacy persistent betting embed
# ---------------------------------------------------------------------------

# In-memory page cursor — resets to 0 on restart (acceptable).
_ledger_page: int = 0


class BettingLedgerView(discord.ui.View):
    """Persistent pagination view for the #betting-ledger embed."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="⬅️", custom_id="gf_ledger_prev",
                       style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        global _ledger_page
        data = ledger_utils.load_ledger()
        total = len(data["matches"])
        if total > 0:
            _ledger_page = max(0, _ledger_page - 1)
        embed = _build_ledger_embed(data, _ledger_page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="➡️", custom_id="gf_ledger_next",
                       style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        global _ledger_page
        data = ledger_utils.load_ledger()
        total = len(data["matches"])
        if total > 0:
            _ledger_page = min(total - 1, _ledger_page + 1)
        embed = _build_ledger_embed(data, _ledger_page)
        await interaction.response.edit_message(embed=embed, view=self)


class HelpView(discord.ui.View):
    """Two-page paginated help embed (60 s timeout — no persistence needed)."""

    def __init__(self, page: int = 0):
        super().__init__(timeout=60)
        self._page = page

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._page = max(0, self._page - 1)
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._page = min(1, self._page + 1)
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    def _current_embed(self) -> discord.Embed:
        return formatter.format_help_page1() if self._page == 0 else formatter.format_help_page2()


def _build_ledger_embed(data: dict, page: int) -> discord.Embed:
    """Build the Discord embed for one match page of the betting ledger."""
    matches = data.get("matches", [])
    if not matches:
        embed = discord.Embed(
            title="🎰 GodForge Betting Ledger",
            description="No matches scheduled this week. Check back soon!",
            color=0x2C2F33,
        )
        embed.set_footer(text="GodForge Betting • No matches yet")
        return embed

    total = len(matches)
    page = max(0, min(page, total - 1))
    m = matches[page]

    status_labels = {
        "betting_open": "🟢 Betting Open",
        "in_progress":  "🟡 In Progress",
        "completed":    "🔴 Completed",
        "settled":      "✅ Settled",
    }
    status_colors = {
        "betting_open": 0x2ECC71,
        "in_progress":  0xF1C40F,
        "completed":    0xE74C3C,
        "settled":      0x9B59B6,
    }

    t1 = m["teams"]["team1"]
    t2 = m["teams"]["team2"]
    status = status_labels.get(m["status"], m["status"])
    color = status_colors.get(m["status"], 0x2C2F33)

    embed = discord.Embed(
        title=f"🎰 {m['match_id']} — {t1} vs {t2}",
        color=color,
    )
    embed.add_field(name="Status", value=status, inline=True)
    if m.get("winner"):
        embed.add_field(name="Winner", value=m["winner"], inline=True)

    bets = m.get("bets", [])
    win_bets = [b for b in bets if b["type"] == "win"]
    t1_pool = sum(b["amount"] for b in win_bets if b["team"] == t1)
    t2_pool = sum(b["amount"] for b in win_bets if b["team"] == t2)
    embed.add_field(
        name="Win Pools",
        value=f"{t1}: **{t1_pool}** pts\n{t2}: **{t2_pool}** pts\nTotal: **{t1_pool + t2_pool}** pts",
        inline=False,
    )

    prop_bets = [b for b in bets if b["type"] == "prop"]
    if prop_bets:
        groups: dict[str, dict] = {}
        for b in prop_bets:
            key = f"{b['player']}|{b['stat']}|{b['threshold']}"
            if key not in groups:
                groups[key] = {"player": b["player"], "stat": b["stat"],
                               "threshold": b["threshold"], "over": 0, "under": 0}
            groups[key][b["direction"]] += b["amount"]
        lines = [
            f"**{p['player']}** {p['stat']} {p['threshold']} — "
            f"Over: **{p['over']}** | Under: **{p['under']}**"
            for p in groups.values()
        ]
        embed.add_field(name="Props", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Page {page + 1}/{total} • ⬅️ ➡️ to navigate • GodForge Betting")
    return embed


async def _handle_wallet_command(message: discord.Message):
    parts = message.content.split()
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub == "check":
        target = message.mentions[0] if message.mentions else message.author
        await _wallet_check(message, target)
        return

    if not _is_admin(message):
        await message.channel.send("⚠️ This command requires admin permissions.")
        return

    if sub == "wipe":
        await _wallet_wipe(message)
    elif sub in ("give", "take", "set"):
        if len(parts) < 4 or not message.mentions:
            await message.channel.send(f"⚠️ Usage: `.wallet {sub} @player amount`")
            return
        target = message.mentions[0]
        try:
            amount = int(parts[-1])
        except ValueError:
            await message.channel.send(f"⚠️ Invalid amount `{parts[-1]}`.")
            return
        await _wallet_adjust(message, sub, target, amount)
    else:
        await message.channel.send(
            "⚠️ Usage: `.wallet check [@player]`  or  "
            "`.wallet give|take|set @player amount`  or  `.wallet wipe`"
        )


async def _wallet_adjust(message: discord.Message, action: str,
                         target: discord.Member, amount: int):
    uid = target.id
    wallet_utils.ensure_wallet(uid, target.display_name)
    if action == "give":
        new_bal = wallet_utils.update_balance(uid, amount)
        await message.channel.send(
            f"✅ Gave **{amount}** pts to **{target.display_name}**. Balance: **{new_bal}** pts"
        )
    elif action == "take":
        new_bal = wallet_utils.update_balance(uid, -amount)
        await message.channel.send(
            f"✅ Took **{amount}** pts from **{target.display_name}**. Balance: **{new_bal}** pts"
        )
    elif action == "set":
        new_bal = wallet_utils.set_balance(uid, amount)
        await message.channel.send(
            f"✅ Set **{target.display_name}**'s balance to **{new_bal}** pts"
        )
    log.info(f"Wallet {action}: {target.display_name} ({uid}), amount={amount}")


async def _wallet_check(message: discord.Message, target: discord.Member):
    wallet = wallet_utils.get_wallet(target.id)
    if wallet is None:
        await message.channel.send(
            f"No wallet found for **{target.display_name}** — they haven't placed any bets yet."
        )
        return
    await message.channel.send(
        f"**{target.display_name}** has **{wallet['balance']}** pts"
    )


async def _wallet_wipe(message: discord.Message):
    # Safety backup to #godforge-reports before wiping.
    await _post_wallets_to_reports(message.guild)
    count = wallet_utils.reset_all()
    await message.channel.send(
        f"✅ Reset **{count}** wallet(s) to **{wallet_utils.SEED_AMOUNT}** pts each."
    )
    log.info(f"Wallet wipe by {message.author.display_name}: {count} wallets reset")


async def _handle_ledger_command(message: discord.Message):
    if not _is_admin(message):
        await message.channel.send("⚠️ This command requires admin permissions.")
        return
    parts = message.content.split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "reset":
        await _ledger_reset(message)
    elif sub == "post":
        if await update_betting_embed(message.channel):
            await message.channel.send("✅ Ledger embed reposted.")
    else:
        await message.channel.send("⚠️ Usage: `.ledger reset` or `.ledger post`")


async def _ledger_reset(message: discord.Message):
    # Post wallet snapshot to reports before wiping match history.
    await _post_wallets_to_reports(message.guild)
    ledger_utils.reset_ledger()
    await update_betting_embed(message.channel)
    await message.channel.send(
        "✅ Weekly ledger reset. All matches cleared. Wallet balances untouched."
    )
    log.info(f"Ledger reset by {message.author.display_name}")


async def _handle_match_command(message: discord.Message):
    if not _is_admin(message):
        await message.channel.send("⚠️ This command requires admin permissions.")
        return
    parts = message.content.split()
    if len(parts) < 2:
        await message.channel.send("⚠️ Usage: `.match create|draft|resolve ...`")
        return
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "create":
        await _match_create(message)
    elif sub == "draft":
        await _match_draft(message)
    elif sub == "resolve":
        await _match_resolve(message)
    else:
        await message.channel.send(f"⚠️ Unknown subcommand `{sub}`. Use `create`, `draft`, or `resolve`.")


async def _match_create(message: discord.Message):
    teams = _extract_team_names(message)
    if len(teams) < 2:
        await message.channel.send("⚠️ Usage: `.match create @TeamA @TeamB`")
        return
    match = ledger_utils.create_match(teams[0], teams[1])
    await message.channel.send(
        f"✅ Match **{match['match_id']}** created: **{teams[0]}** vs **{teams[1]}**\n"
        f"🟢 Betting is now open!"
    )
    try:
        await update_betting_embed(message.channel)
    except Exception as exc:
        log.warning(f"Ledger embed update failed after match creation: {exc}")
    log.info(f"Match {match['match_id']} created: {teams[0]} vs {teams[1]}")


async def _match_draft(message: discord.Message):
    parts = message.content.split()
    if len(parts) < 3:
        await message.channel.send("⚠️ Usage: `.match draft GF-XXXX`")
        return
    match_id = parts[2].upper()

    match = ledger_utils.get_match(match_id)
    if not match:
        await message.channel.send(f"⚠️ Match {match_id} not found.")
        return
    if match["status"] != "betting_open":
        await message.channel.send(
            f"⚠️ Match {match_id} is not open for betting (status: `{match['status']}`)."
        )
        return

    ledger_utils.set_match_status(match_id, "in_progress")
    t1, t2 = match["teams"]["team1"], match["teams"]["team2"]
    draft_note = "\nUse `.draft start @blue_captain @red_captain` to begin the draft."

    # If every match is now in_progress or beyond, post wallet snapshot to reports.
    data = ledger_utils.load_ledger()
    if ledger_utils.all_matches_in_progress(data):
        await _post_wallets_to_reports(message.guild)

    await message.channel.send(
        f"🟡 **{match_id}** is now **in progress** — betting locked.\n"
        f"Teams: **{t1}** vs **{t2}**{draft_note}"
    )
    await update_betting_embed(message.channel)
    log.info(f"Match {match_id} set to in_progress in channel {message.channel.id}")


async def _match_resolve(message: discord.Message):
    parts = message.content.split()
    if len(parts) < 4:
        await message.channel.send(
            "⚠️ Usage: `.match resolve GF-XXXX winner @Team`  or  "
            "`.match resolve GF-XXXX prop @player stat actual_value`"
        )
        return
    match_id = parts[2].upper()
    resolve_type = parts[3].lower()
    if resolve_type == "winner":
        await _match_resolve_winner(message, match_id, parts)
    elif resolve_type == "prop":
        await _match_resolve_prop(message, match_id, parts)
    else:
        await message.channel.send(f"⚠️ Unknown resolve type `{resolve_type}`. Use `winner` or `prop`.")


async def _match_resolve_winner(message: discord.Message, match_id: str, parts: list):
    match = ledger_utils.get_match(match_id)
    if not match:
        await message.channel.send(f"⚠️ Match {match_id} not found.")
        return
    if match["status"] not in ("in_progress", "completed"):
        await message.channel.send(
            f"⚠️ Match {match_id} must be `in_progress` or `completed` to resolve a winner "
            f"(current: `{match['status']}`)."
        )
        return
    t1, t2 = match["teams"]["team1"], match["teams"]["team2"]
    winner = _find_matching_team(message, [t1, t2])
    if not winner:
        await message.channel.send(
            f"⚠️ Could not identify the winner. Teams are **{t1}** and **{t2}**."
        )
        return

    payouts = ledger_utils.resolve_win_bets(match_id, winner)
    wallet_utils.apply_payouts(payouts)

    lines = [f"✅ **{match_id}** — **{winner}** wins!"]
    if payouts:
        lines.append(f"💰 Win payouts ({len(payouts)} winner(s)):")
        for p in payouts:
            lines.append(f"  • {p['username']}: +**{p['payout']}** pts")
    else:
        lines.append("No winning win-bets to pay out.")
    await message.channel.send("\n".join(lines))
    await update_betting_embed(message.channel)
    log.info(f"Match {match_id} resolved: winner={winner}, {len(payouts)} payout(s)")


async def _match_resolve_prop(message: discord.Message, match_id: str, parts: list):
    # .match resolve GF-XXXX prop @player stat actual_value  → 7 tokens
    if len(parts) < 7:
        await message.channel.send(
            "⚠️ Usage: `.match resolve GF-XXXX prop @player stat actual_value`"
        )
        return
    match = ledger_utils.get_match(match_id)
    if not match:
        await message.channel.send(f"⚠️ Match {match_id} not found.")
        return

    player = _extract_player_name(message)
    if not player:
        await message.channel.send("⚠️ Could not identify the player. Use an @mention.")
        return

    stat = parts[5].lower()
    try:
        actual_value = float(parts[6])
    except ValueError:
        await message.channel.send(f"⚠️ Invalid value `{parts[6]}` — must be a number.")
        return

    payouts, had_bets = ledger_utils.resolve_prop_bets(match_id, player, stat, actual_value)
    if not had_bets:
        await message.channel.send(f"No bets found for that prop ({player} {stat})")
        return

    wallet_utils.apply_payouts(payouts)

    updated = ledger_utils.get_match(match_id)
    settled_note = " — match is now **settled** ✅" if updated and updated["status"] == "settled" else ""
    lines = [f"✅ **{match_id}** prop resolved: **{player}** {stat} = **{actual_value}**{settled_note}"]
    if payouts:
        lines.append(f"💰 Prop payouts ({len(payouts)} winner(s)):")
        for p in payouts:
            lines.append(f"  • {p['username']}: +**{p['payout']}** pts")
    else:
        lines.append("No winning bets on this side.")
    await message.channel.send("\n".join(lines))
    await update_betting_embed(message.channel)
    log.info(f"Match {match_id} prop resolved: {player} {stat}={actual_value}, {len(payouts)} payout(s)")


async def _handle_bet_command(message: discord.Message):
    if PLACE_BETS_CHANNEL_ID and message.channel.id != PLACE_BETS_CHANNEL_ID:
        await message.channel.send("⚠️ Bets can only be placed in the #place-bets channel.")
        return

    parts = message.content.split()
    # Minimum: .bet GF-XXXX amount @Team win  (5 tokens)
    if len(parts) < 5:
        await message.channel.send(
            "⚠️ Usage:\n"
            "  `.bet GF-XXXX amount @Team win`\n"
            "  `.bet GF-XXXX amount @player stat over|under threshold`"
        )
        return

    match_id = parts[1].upper()
    try:
        amount = int(parts[2])
    except ValueError:
        await message.channel.send(f"⚠️ Invalid amount `{parts[2]}` — must be a whole number.")
        return
    if amount <= 0:
        await message.channel.send("⚠️ Bet amount must be greater than zero.")
        return

    match = ledger_utils.get_match(match_id)
    if not match:
        await message.channel.send(f"Match {match_id} not found.")
        return
    if match["status"] != "betting_open":
        await message.channel.send("Betting is closed for this match")
        return

    user_id = message.author.id
    username = message.author.display_name
    balance = wallet_utils.seed_wallet(user_id, username)

    if balance <= 0:
        await message.channel.send(
            f"You have {balance} points and cannot place bets. Contact an admin."
        )
        return
    if amount > balance:
        await message.channel.send(
            f"⚠️ You only have **{balance}** pts but tried to bet **{amount}**."
        )
        return

    # Route by bet shape
    if parts[4].lower() == "win":
        # .bet GF-XXXX amount @Team win
        await _place_win_bet(message, match, match_id, amount)
    elif len(parts) >= 7 and parts[5].lower() in ("over", "under"):
        # .bet GF-XXXX amount @player stat over|under threshold
        await _place_prop_bet(message, match, match_id, amount, parts)
    else:
        await message.channel.send(
            "⚠️ Unrecognised bet format.\n"
            "Win:  `.bet GF-XXXX amount @Team win`\n"
            "Prop: `.bet GF-XXXX amount @player stat over|under threshold`"
        )


async def _place_win_bet(message: discord.Message, match: dict, match_id: str, amount: int):
    t1, t2 = match["teams"]["team1"], match["teams"]["team2"]
    team = _find_matching_team(message, [t1, t2])
    if not team:
        await message.channel.send(
            f"⚠️ Unknown team. Match {match_id} has **{t1}** vs **{t2}**."
        )
        return

    wallet_utils.update_balance(message.author.id, -amount)
    ledger_utils.add_bet(match_id, {
        "type": "win",
        "user_id": message.author.id,
        "username": message.author.display_name,
        "team": team,
        "amount": amount,
    })
    await message.add_reaction("✅")
    log.info(f"Win bet: {message.author.display_name} bet {amount} on {team} in {match_id}")
    await update_betting_embed(message.channel)


async def _place_prop_bet(message: discord.Message, match: dict, match_id: str,
                          amount: int, parts: list):
    player = _extract_player_name(message)
    if not player:
        await message.channel.send("⚠️ Could not identify player — use an @mention.")
        return

    stat = parts[4].lower()
    direction = parts[5].lower()
    if direction not in ("over", "under"):
        await message.channel.send("⚠️ Direction must be `over` or `under`.")
        return
    try:
        threshold = float(parts[6])
    except ValueError:
        await message.channel.send(f"⚠️ Invalid threshold `{parts[6]}` — must be a number.")
        return

    wallet_utils.update_balance(message.author.id, -amount)
    ledger_utils.add_bet(match_id, {
        "type": "prop",
        "user_id": message.author.id,
        "username": message.author.display_name,
        "player": player,
        "stat": stat,
        "direction": direction,
        "threshold": threshold,
        "amount": amount,
    })
    await message.add_reaction("✅")
    log.info(f"Prop bet: {message.author.display_name} bet {amount} {direction} "
             f"{threshold} on {player} {stat} in {match_id}")
    await update_betting_embed(message.channel)


async def update_betting_embed(notify_channel: discord.abc.Messageable | None = None) -> bool:
    """Post or in-place edit the persistent betting embed in #betting-ledger.

    Returns True if the embed was successfully posted or edited, False otherwise.
    """
    global _ledger_page
    if not BETTING_LEDGER_CHANNEL_ID:
        log.warning("BETTING_LEDGER_CHANNEL_ID not configured — ledger embed skipped")
        if notify_channel:
            await notify_channel.send(
                "⚠️ The betting ledger channel hasn't been configured yet. Please contact an admin."
            )
        return False

    channel = client.get_channel(BETTING_LEDGER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(BETTING_LEDGER_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning(f"Ledger channel {BETTING_LEDGER_CHANNEL_ID} not accessible: {exc}")
            if notify_channel:
                await notify_channel.send(
                    "⚠️ The betting ledger channel could not be found. "
                    "Please contact an admin to verify the channel configuration."
                )
            return False

    data = ledger_utils.load_ledger()
    total = len(data["matches"])
    _ledger_page = max(0, min(_ledger_page, total - 1)) if total > 0 else 0

    embed = _build_ledger_embed(data, _ledger_page)
    view = BettingLedgerView()

    msg_id = data.get("embed_message_id")
    chan_id = data.get("embed_channel_id")
    if msg_id and chan_id == BETTING_LEDGER_CHANNEL_ID:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return True
        except (discord.NotFound, discord.HTTPException):
            pass  # fall through to post a new message
        except discord.Forbidden as exc:
            log.warning(f"Cannot edit ledger embed (no permission): {exc}")
            if notify_channel:
                await notify_channel.send(
                    "⚠️ The bot doesn't have permission to post in the betting ledger channel. "
                    "Please contact an admin."
                )
            return False

    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.Forbidden as exc:
        log.warning(f"Cannot post ledger embed (no permission): {exc}")
        if notify_channel:
            await notify_channel.send(
                "⚠️ The bot doesn't have permission to post in the betting ledger channel. "
                "Please contact an admin."
            )
        return False
    ledger_utils.update_embed_info(msg.id, BETTING_LEDGER_CHANNEL_ID)
    log.info(f"Betting ledger embed posted to channel {BETTING_LEDGER_CHANNEL_ID}")
    return True




# The /scrim command family and ScrimChallengeView are owned by the scrim
# feature module (Issue #48); bot.py wires it with injected dependencies.
_scrim_deps = ScrimCommandDeps(
    scrim_repository=scrim_repository,
    schedule_repository=schedule_repository,
    party_repository=party_repository,
    party_queue_service=party_queue_service,
    ready_check_embed=lambda lobby_id, queue: _ready_check_embed(lobby_id, queue),
    ready_check_view=lambda: ReadyCheckView(_handle_ready_check_action),
    lobby_card_embed=lambda lobby: _lobby_card_embed(lobby),
    lobby_card_view=lambda: LobbyCardView(_handle_lobby_card_action),
)
scrim_commands, ScrimChallengeView = register_scrim_commands(client.tree, _scrim_deps)
scrim_team_create = scrim_commands.team_create
scrim_teams = scrim_commands.teams
scrim_challenge = scrim_commands.challenge
scrim_respond = scrim_commands.respond
scrim_checkin = scrim_commands.checkin
scrim_lock = scrim_commands.lock
scrim_launch = scrim_commands.launch

party_commands = app_commands.Group(
    name="party",
    description="Set up and manage GodForge parties",
)
client.tree.add_command(party_commands)


# Per-guild temporary-room service construction is owned by the factory
# (Issue #48); bot.py keeps a thin delegator.
match_room_service_factory = MatchRoomServiceFactory(
    match_room_repository, settings.get_guild_settings
)


def _match_room_service_for_guild(guild: discord.Guild) -> MatchRoomService:
    return match_room_service_factory.for_guild(guild)


feature_registry.register(
    RoomLifecycle(match_room_repository, party_repository, match_room_service_factory)
)


# /party setup is owned by the party-setup-command feature module (Issue #48);
# bot.py wires it with injected dependencies.
_party_setup_deps = PartySetupCommandDeps(
    settings_module=settings,
    play_panel_view=lambda: PlayPanelView(_handle_play_panel_action),
)
register_party_setup_command(party_commands, _party_setup_deps)
party_setup = party_commands.setup


# /party room is owned by the party-room-command feature module (Issue #48);
# bot.py wires it with injected dependencies.
_party_room_deps = PartyRoomCommandDeps(
    party_repository=party_repository,
    match_room_service_for_guild=_match_room_service_for_guild,
)
register_party_room_command(party_commands, _party_room_deps)
party_room = party_commands.room


# Scheduled-night /party subcommands are owned by the schedule feature module
# (Issue #48); bot.py wires it with injected dependencies.
_schedule_deps = ScheduleCommandDeps(
    schedule_repository=schedule_repository,
    party_repository=party_repository,
    party_queue_service=party_queue_service,
    ready_check_embed=lambda lobby_id, queue: _ready_check_embed(lobby_id, queue),
    ready_check_view=lambda: ReadyCheckView(_handle_ready_check_action),
    lobby_card_embed=lambda lobby: _lobby_card_embed(lobby),
    lobby_card_view=lambda: LobbyCardView(_handle_lobby_card_action),
)
register_schedule_commands(party_commands, _schedule_deps)
party_schedule = party_commands.schedule
party_confirm = party_commands.confirm
party_rsvp = party_commands.rsvp
party_unrsvp = party_commands.unrsvp
party_events = party_commands.events
party_calendar = party_commands.calendar
party_open_scheduled = party_commands.open_scheduled




async def _handle_play_panel_action(interaction: discord.Interaction, action: str) -> None:
    await party_lobby_service.handle_play_panel_action(interaction, action)


async def _handle_create_lobby_submission(
    interaction: discord.Interaction, payload: dict[str, object]
) -> None:
    await party_lobby_service.handle_create_lobby_submission(interaction, payload)


async def _join_lobby_from_preferences(
    interaction: discord.Interaction, lobby_id: str, payload: dict[str, object]
) -> None:
    await party_lobby_service.join_lobby_from_preferences(interaction, lobby_id, payload)


async def _ensure_party_queue(lobby):
    return await party_lobby_service.ensure_party_queue(lobby)


def _lobby_card_embed(lobby) -> discord.Embed:
    return party_lobby_service.lobby_card_embed(lobby)


def _lobby_id_from_interaction(interaction: discord.Interaction) -> str:
    return party_lobby_service.lobby_id_from_interaction(interaction)


async def _handle_lobby_card_action(interaction: discord.Interaction, action: str) -> None:
    await party_lobby_service.handle_lobby_card_action(interaction, action)


async def _launch_party_draft(
    interaction: discord.Interaction,
    lobby,
    *,
    formation_mode: FormationMode = FormationMode.ROLE_FIT,
) -> None:
    await party_lobby_service.launch_party_draft(
        interaction, lobby, formation_mode=formation_mode
    )

# Match-result rendering and history creation are owned by utils/match_results
# (Issue #48); bot.py keeps thin delegators.
def _ensure_match_history(lobby, launch):
    return match_results.ensure_match_history(match_history_repository, lobby, launch)


def _match_result_embed(record) -> discord.Embed:
    return match_results.build_result_embed(record)


def _match_id_from_interaction(interaction: discord.Interaction) -> str:
    return match_results.match_id_from_interaction(interaction)


# Match-result/continuity interaction handlers are owned by the match_actions
# feature module (Issue #48); bot.py wires it with injected dependencies.
_match_action_deps = MatchActionDeps(
    match_history_repository=match_history_repository,
    match_continuity_repository=match_continuity_repository,
    party_draft_repository=party_draft_repository,
    match_room_repository=match_room_repository,
    party_queue_service=party_queue_service,
    match_room_service_for_guild=_match_room_service_for_guild,
    match_result_view=lambda: MatchResultView(_handle_match_result_action),
    match_continuity_view=lambda allow_continue_series=True: MatchContinuityView(
        _handle_match_continuity_action, allow_continue_series=allow_continue_series
    ),
)


async def _handle_match_result_action(interaction: discord.Interaction, action: str) -> None:
    await handle_match_result_action(_match_action_deps, interaction, action)


async def _handle_match_continuity_action(
    interaction: discord.Interaction, action: str
) -> None:
    await handle_match_continuity_action(_match_action_deps, interaction, action)


# Play panel, lobby card, queue, and draft-launch orchestration are owned
# by PartyLobbyService (Issue #48); bot.py wires it with injected
# dependencies and keeps thin delegators for existing call sites.
_party_lobby_deps = PartyLobbyDeps(
    party_repository=party_repository,
    party_queue_service=party_queue_service,
    party_draft_repository=party_draft_repository,
    scrim_repository=scrim_repository,
    drafts=drafts,
    draft_coordinator=draft_coordinator,
    activity_client=activity_client,
    formatter=formatter,
    settings_module=settings,
    reserve_match_id=match_ids.reserve_match_id,
    match_room_service_for_guild=_match_room_service_for_guild,
    channel_has_active=_channel_has_active,
    save_active_draft=_save_active_draft,
    ensure_match_history=_ensure_match_history,
    match_result_embed=_match_result_embed,
    log=log,
    lobby_card_view=lambda: LobbyCardView(_handle_lobby_card_action),
    ready_check_view=lambda: ReadyCheckView(_handle_ready_check_action),
    role_preferences_view=lambda: RolePreferencesView(_handle_role_preference),
    create_lobby_modal=CreateLobbyModal,
    join_preferences_modal=JoinPreferencesModal,
    match_result_view=lambda: MatchResultView(_handle_match_result_action),
)
party_lobby_service = PartyLobbyService(_party_lobby_deps)

def _reconcile_active_party_draft(lobby, launch, actor_id: int) -> None:
    party_lobby_service.reconcile_active_party_draft(lobby, launch, actor_id)


def _ready_check_embed(lobby_id: str, queue) -> discord.Embed:
    return party_lobby_service.ready_check_embed(lobby_id, queue)


async def _handle_ready_check_action(interaction: discord.Interaction, action: str) -> None:
    await party_lobby_service.handle_ready_check_action(interaction, action)


async def _handle_role_preference(
    interaction: discord.Interaction,
    role_key: str,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Server-only action.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    managed = settings.get_guild_settings(str(guild_id))["managed"]
    stored_role_ids = managed["roleIds"]
    role_id = int(stored_role_ids.get(role_key) or 0)
    enabled = role_id not in {role.id for role in interaction.user.roles}
    await set_member_role(
        interaction.guild,
        interaction.user,
        role_key,
        enabled,
        stored_role_ids,
    )
    profile = party_repository.get_player_preferences(guild_id, interaction.user.id)
    roles = list(profile.roles)
    if role_key in {"solo", "jungle", "mid", "support", "adc"}:
        if enabled and role_key not in roles:
            roles.append(role_key)
        if not enabled and role_key in roles:
            roles.remove(role_key)
    saved = party_repository.set_player_preferences(
        guild_id,
        interaction.user.id,
        PlayerPreferences(
            roles[0] if roles else None,
            roles[1] if len(roles) > 1 else None,
            profile.fill,
            enabled if role_key == "captain" else profile.captain,
        ),
    )
    await interaction.response.send_message(
        f"{role_key.title()} {'added' if enabled else 'removed'}. "
        f"Preferences: {', '.join(saved.roles) or 'none'}.",
        ephemeral=True,
    )


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set.")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
