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

from utils import custom_commands, formatter, loader, parser, picker, settings
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
from utils import match_ids
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
forgelens_adapter = ForgeLensAdapter()

# Track metadata for reaction-enabled messages (sessions only).
_tracked_messages = {}
_custom_command_cooldowns: dict[tuple[str, str, int], float] = {}

_ACTIVE_DRAFTS_FILE = os.path.join("data", "active_local_drafts.json")


def _load_active_drafts() -> dict:
    try:
        with open(_ACTIVE_DRAFTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_active_draft(channel_id: int, draft_id: str):
    data = _load_active_drafts()
    data[str(channel_id)] = draft_id
    os.makedirs("data", exist_ok=True)
    with open(_ACTIVE_DRAFTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _remove_active_draft(channel_id: int):
    data = _load_active_drafts()
    if str(channel_id) in data:
        data.pop(str(channel_id))
        with open(_ACTIVE_DRAFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)

# Activity backend draft tracking (in-memory, resets on restart).
_match_ids: dict[int, str] = {}           # channel_id -> match_id
_match_channels: dict[str, int] = {}      # match_id -> channel_id
_snapshots: dict[int, dict] = {}          # channel_id -> latest state snapshot
_board_message_ids: dict[int, int] = {}   # channel_id -> embed message id
_ws_tasks: dict[int, asyncio.Task] = {}   # channel_id -> listener task

REPORTS_CHANNELS = {
    int(gid.strip()): int(cid.strip())
    for pair in os.getenv("GODFORGE_REPORTS_CHANNELS", "").split(",")
    if ":" in pair
    for gid, cid in [pair.split(":", 1)]
    if gid.strip() and cid.strip()
}


def _channel_has_active(channel_id: int) -> str | None:
    if sessions.get(channel_id):
        return "session"
    if channel_id in _match_ids or drafts.get(channel_id):
        return "draft"
    return None


def _cleanup_draft(channel_id: int) -> None:
    match_id = _match_ids.pop(channel_id, None)
    if match_id:
        _match_channels.pop(match_id, None)
    _snapshots.pop(channel_id, None)
    _board_message_ids.pop(channel_id, None)
    task = _ws_tasks.pop(channel_id, None)
    if task:
        task.cancel()


def _draft_start_options(content: str) -> dict:
    match = re.search(r"(?:^|\s)--match\s+(\S+)", content) if forgelens_enabled() else None
    game = re.search(r"(?:^|\s)--game\s+(\d+)", content)
    return {
        "forgelens_match_id": match.group(1) if match else "",
        "game_number": int(game.group(1)) if game else 1,
    }


def _draft_completion_marker(draft) -> str:
    lines = [
        "Draft complete",
        f"draft_id={draft.draft_id}",
        f"game_number={draft.current_game.game_number}",
    ]
    if forgelens_enabled():
        lines.insert(2, f"forgelens_match_id={getattr(draft, 'forgelens_match_id', '')}")
    return "\n".join(lines)


# ── Activity backend helpers ──────────────────────────────────────────────────

def _activity_headers() -> dict:
    return {"X-Api-Key": ACTIVITY_API_KEY, "Content-Type": "application/json"}


async def _activity_post(path: str, data: dict | None = None) -> dict | None:
    url = ACTIVITY_BACKEND_URL + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data or {}, headers=_activity_headers()) as resp:
                return await resp.json()
    except Exception as e:
        log.error(f"Activity backend POST {path} failed: {e}")
        return None


async def _activity_get(path: str) -> dict | None:
    url = ACTIVITY_BACKEND_URL + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_activity_headers()) as resp:
                return await resp.json()
    except Exception as e:
        log.error(f"Activity backend GET {path} failed: {e}")
        return None


async def _update_embed_from_snapshot(snapshot: dict, channel) -> None:
    channel_id = channel.id
    embed = formatter.format_board_from_snapshot(snapshot)
    msg_id = _board_message_ids.get(channel_id)
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    sent = await channel.send(embed=embed)
    _board_message_ids[channel_id] = sent.id


async def _post_export(export: dict, channel) -> None:
    if isinstance(export.get("export"), dict):
        export = export["export"]
    guild = getattr(channel, "guild", None)
    export.setdefault("guild_id", guild.id if guild else None)
    export.setdefault("channel_id", channel.id)
    export.setdefault("match_id", export.get("matchId") or export.get("draftId") or export.get("draft_id"))
    export.setdefault("draft_id", export.get("draftId") or export.get("match_id"))
    export.setdefault("forgelens_match_id", export.get("forgelensMatchId") or "")
    export.setdefault("game_number", export.get("gameNumber") or 1)
    export.setdefault("draft_sequence", export.get("draftSequence") or 1)
    export.setdefault("status", "draft_complete")
    export.setdefault("producer", "GodForge")
    draft_id = export.get("draftId") or export.get("draft_id", "unknown")
    embed = formatter.format_draft_end_from_export(export)
    await channel.send(embed=embed)
    completion_lines = [
        "Draft complete",
        f"draft_id={export.get('draft_id', draft_id)}",
        f"game_number={export.get('game_number', 1)}",
    ]
    if forgelens_enabled():
        completion_lines.insert(
            2, f"forgelens_match_id={export.get('forgelens_match_id', '')}"
        )
    await channel.send("\n".join(completion_lines))

    filename = f"draft_{draft_id}.json"
    json_bytes = json.dumps(export, indent=2).encode("utf-8")
    file = discord.File(io.BytesIO(json_bytes), filename=filename)
    await channel.send(f"📎 Draft record: `{filename}`", file=file)

    guild_id = channel.guild.id if channel.guild else None
    if guild_id and guild_id in REPORTS_CHANNELS:
        reports_ch = client.get_channel(REPORTS_CHANNELS[guild_id])
        if reports_ch:
            try:
                await reports_ch.send(embed=embed)
                report_file = discord.File(io.BytesIO(json_bytes), filename=filename)
                await reports_ch.send(f"📎 Draft record: `{filename}`", file=report_file)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"Failed to post to reports channel: {e}")


async def _listen_draft_ws(match_id: str, channel_id: int) -> None:
    """Connect to the Activity backend WebSocket and mirror state to the embed."""
    ws_url = ACTIVITY_BACKEND_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                await ws.send_json({"type": "join", "matchId": match_id})
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data["type"] == "state":
                            _snapshots[channel_id] = data["state"]
                            channel = client.get_channel(channel_id)
                            if channel:
                                await _update_embed_from_snapshot(data["state"], channel)
                        elif data["type"] == "export":
                            if channel_id in _match_ids:
                                channel = client.get_channel(channel_id)
                                if channel:
                                    await _post_export(data["export"], channel)
                                _cleanup_draft(channel_id)
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error(f"WS listener error for {match_id}: {e}")
    finally:
        _ws_tasks.pop(channel_id, None)


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

    orphaned = _load_active_drafts()
    if orphaned:
        try:
            os.remove(_ACTIVE_DRAFTS_FILE)
        except OSError:
            pass
        for channel_id_str in orphaned:
            ch = client.get_channel(int(channel_id_str))
            if ch:
                try:
                    await ch.send(
                        "⚠️ GodForge restarted — the active draft was lost. "
                        "Please start a new one with `.draft start`."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        log.info(f"Notified {len(orphaned)} channel(s) of lost draft(s) after restart")


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
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return
    if not message.content.startswith("."):
        return

    # ---- Deprecated economy commands (bypass parser — not handled there) ----
    _first = message.content[1:].split()[0].lower() if message.content[1:].split() else ""
    if _first in {"match", "bet", "wallet", "ledger"}:
        await _handle_deprecated_economy_command(message, _first)
        return
    # ---- End deprecated economy routing ----

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

async def _handle_session(intent: dict, channel_id: int):
    action = intent["action"]

    if action == "start":
        active = _channel_has_active(channel_id)
        if active == "draft":
            return formatter.format_error("A draft is active in this channel. Use `.draft end` first.")
        if sessions.start(channel_id):
            return "✅ Draft session started! Use `.session end` when done."
        return formatter.format_error("A session is already active. Use `.session end` first.")

    elif action == "end":
        session = sessions.end(channel_id)
        if session:
            to_remove = [mid for mid, info in _tracked_messages.items()
                         if info.get("channel_id") == channel_id]
            for mid in to_remove:
                del _tracked_messages[mid]
            return formatter.format_session_end(session.picks)
        return formatter.format_error("No active session in this channel.")

    elif action == "show":
        session = sessions.get(channel_id)
        if session:
            return formatter.format_session_show(session.picks)
        return formatter.format_error("No active session in this channel.")

    elif action == "reset":
        if sessions.reset(channel_id):
            to_remove = [mid for mid, info in _tracked_messages.items()
                         if info.get("channel_id") == channel_id]
            for mid in to_remove:
                del _tracked_messages[mid]
            return "🔄 Session picks cleared. Session is still active."
        return formatter.format_error("No active session in this channel.")


# ── Draft handlers ────────────────────────────────────────────────────────────

async def _handle_draft(intent: dict, message: discord.Message):
    """Route to Activity backend or local DraftManager based on config."""
    if ACTIVITY_BACKEND_URL:
        return await _handle_draft_activity(intent, message)
    async with drafts.get_lock(message.channel.id):
        return await _handle_draft_local(intent, message)


async def _handle_draft_activity(intent: dict, message: discord.Message):
    action = intent["action"]
    channel_id = message.channel.id

    if action == "start":
        active = _channel_has_active(channel_id)
        if active == "session":
            return formatter.format_error("A session is active. Use `.session end` first.")
        if active == "draft":
            return formatter.format_error("A draft is already active. Use `.draft end` first.")

        mentions = message.mentions
        options = _draft_start_options(message.content)
        if len(mentions) < 2:
            return formatter.format_error(
                "Usage: `.draft start @blue_captain @red_captain [--match FL-123] [--game 2]`"
            )
        blue_user, red_user = mentions[0], mentions[1]
        if blue_user.id == red_user.id:
            return formatter.format_error("Blue and red captains must be different users.")
        if options["game_number"] < 1:
            return formatter.format_error("`--game` must be 1 or greater.")

        result = await _activity_post("/api/draft/start", {
            "blueCaptainId": str(blue_user.id),
            "blueCaptainName": blue_user.display_name,
            "redCaptainId": str(red_user.id),
            "redCaptainName": red_user.display_name,
            "forgelensMatchId": options["forgelens_match_id"],
            "gameNumber": options["game_number"],
        })
        if not result or "error" in result:
            err = result.get("error") if result else "Activity backend unreachable."
            return formatter.format_error(err)

        match_id = result["matchId"]
        _match_ids[channel_id] = match_id
        _match_channels[match_id] = channel_id
        snapshot = result["state"]
        _snapshots[channel_id] = snapshot

        embed = formatter.format_board_from_snapshot(snapshot)
        sent = await message.channel.send(
            f"🎮 Draft `{match_id}` started — open the Activity and enter this ID to join",
            embed=embed,
        )
        _board_message_ids[channel_id] = sent.id

        task = asyncio.create_task(_listen_draft_ws(match_id, channel_id))
        _ws_tasks[channel_id] = task
        log.info(f"Draft {match_id} started: 🔵 {blue_user.display_name} vs 🔴 {red_user.display_name}")
        return None

    elif action == "show":
        match_id = _match_ids.get(channel_id)
        if not match_id:
            return formatter.format_error("No active draft in this channel.")
        snapshot = await _activity_get(f"/api/draft/{match_id}")
        if not snapshot or "error" in snapshot:
            return formatter.format_error("Could not retrieve draft state.")
        return formatter.format_board_from_snapshot(snapshot)

    elif action == "undo":
        match_id = _match_ids.get(channel_id)
        if not match_id:
            return formatter.format_error("No active draft in this channel.")
        result = await _activity_post(f"/api/draft/{match_id}/undo")
        if not result or "error" in result:
            return formatter.format_error(result.get("error", "Nothing to undo.") if result else "Backend unreachable.")
        return None  # WS listener updates the embed

    elif action == "next":
        match_id = _match_ids.get(channel_id)
        if not match_id:
            return formatter.format_error("No active draft in this channel.")
        result = await _activity_post(f"/api/draft/{match_id}/next")
        if not result or "error" in result:
            return formatter.format_error(result.get("error", "Cannot advance game.") if result else "Backend unreachable.")
        return None  # WS listener updates the embed

    elif action == "end":
        match_id = _match_ids.get(channel_id)
        if not match_id:
            return formatter.format_error("No active draft in this channel.")
        result = await _activity_post(f"/api/draft/{match_id}/end")
        if not result or "error" in result:
            return formatter.format_error(result.get("error", "Failed to end draft.") if result else "Backend unreachable.")
        _cleanup_draft(channel_id)
        await _post_export(result, message.channel)
        log.info(f"Draft {match_id} ended via text command")
        return None


async def _handle_draft_local(intent: dict, message: discord.Message):
    """Local DraftManager path — active when ACTIVITY_BACKEND_URL is not set."""
    action = intent["action"]
    channel_id = message.channel.id

    if action == "start":
        active = _channel_has_active(channel_id)
        if active == "session":
            return formatter.format_error("A session is active in this channel. Use `.session end` first.")
        if active == "draft":
            return formatter.format_error("A draft is already active in this channel. Use `.draft end` first.")

        options = _draft_start_options(message.content)
        mentions = message.mentions
        if len(mentions) < 2:
            return formatter.format_error(
                "Usage: `.draft start @blue_captain @red_captain [--match FL-123] [--game 2]`"
            )
        blue_user, red_user = mentions[0], mentions[1]
        if blue_user.id == red_user.id:
            return formatter.format_error("Blue and red captains must be different users.")
        if options["game_number"] < 1:
            return formatter.format_error("`--game` must be 1 or greater.")

        guild = message.guild
        draft = drafts.start(
            channel_id,
            blue_captain_id=blue_user.id,
            blue_captain_name=blue_user.display_name,
            red_captain_id=red_user.id,
            red_captain_name=red_user.display_name,
            guild_id=guild.id if guild else 0,
            guild_name=guild.name if guild else "DM",
            channel_name=message.channel.name if hasattr(message.channel, "name") else "unknown",
            forgelens_match_id=options["forgelens_match_id"],
            game_number=options["game_number"],
        )
        if not draft:
            return formatter.format_error("Failed to start draft.")

        embed = formatter.format_draft_board(draft)
        sent = await message.channel.send(embed=embed)
        draft.board_message_id = sent.id
        _save_active_draft(channel_id, draft.draft_id)
        log.info(f"Draft {draft.draft_id} started in channel {channel_id}: "
                 f"🔵 {blue_user.display_name} vs 🔴 {red_user.display_name}")
        return None

    elif action == "show":
        draft = drafts.get(channel_id)
        if not draft:
            return formatter.format_error("No active draft in this channel.")
        return formatter.format_draft_show(draft)

    elif action == "next":
        draft = drafts.get(channel_id)
        if not draft:
            return formatter.format_error("No active draft in this channel.")
        error = draft.advance_game()
        if error:
            return formatter.format_error(error)
        for team in ("blue", "red"):
            mid = draft.claim_message_ids.get(team)
            if mid:
                _tracked_messages.pop(mid, None)
        await message.channel.send(formatter.format_draft_next(draft))
        embed = formatter.format_draft_board(draft)
        sent = await message.channel.send(embed=embed)
        draft.board_message_id = sent.id
        log.info(f"Draft {draft.draft_id} advanced to Game {draft.current_game.game_number}")
        return None

    elif action == "end":
        draft = drafts.end(channel_id)
        if not draft:
            return formatter.format_error("No active draft in this channel.")
        export = draft.to_export_dict()
        embed = formatter.format_draft_end(draft, export)
        await message.channel.send(embed=embed)
        filename = draft.sanitized_filename()
        json_bytes = json.dumps(export, indent=2).encode("utf-8")
        file = discord.File(io.BytesIO(json_bytes), filename=filename)
        await message.channel.send(f"📎 Draft record: `{filename}`", file=file)
        guild_id = message.guild.id if message.guild else None
        if guild_id and guild_id in REPORTS_CHANNELS:
            reports_ch = client.get_channel(REPORTS_CHANNELS[guild_id])
            if reports_ch:
                try:
                    await reports_ch.send(embed=embed)
                    report_file = discord.File(io.BytesIO(json_bytes), filename=filename)
                    await reports_ch.send(f"📎 Draft record: `{filename}`", file=report_file)
                    log.info(f"Draft {draft.draft_id} report posted to reports channel")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"Failed to post to reports channel: {e}")
        _remove_active_draft(channel_id)
        log.info(f"Draft {draft.draft_id} ended: {len(export['games'])} game(s)")
        return None

    elif action == "undo":
        draft = drafts.get(channel_id)
        if not draft:
            return formatter.format_error("No active draft in this channel.")
        result = draft.undo()
        if result is None:
            return formatter.format_error("Nothing to undo.")
        if result["type"] == "step":
            await message.channel.send(
                formatter.format_draft_undo(result["team"], result["action"], result["god"])
            )
        elif result["type"] == "claim":
            await message.channel.send(
                formatter.format_claim_undo(result["team"], result["god"], result["user_name"])
            )
            await _update_claim_embed(draft, result["team"], message.channel)
        elif result["type"] == "next_game":
            await message.channel.send(
                f"↩️ Undid game advance. Back to **Game {result['game_number']}**."
            )
        await _update_draft_board(draft, message.channel)
        return None


async def _handle_draft_action(intent: dict, message: discord.Message):
    """Route to Activity backend or local DraftManager based on config."""
    if ACTIVITY_BACKEND_URL:
        return await _handle_draft_action_activity(intent, message)
    async with drafts.get_lock(message.channel.id):
        return await _handle_draft_action_local(intent, message)


async def _handle_draft_action_local(intent: dict, message: discord.Message):
    """Local .ban / .pick handler."""
    channel_id = message.channel.id
    draft = drafts.get(channel_id)
    if not draft:
        return formatter.format_error("No active draft in this channel. Use `.draft start` first.")
    if draft.is_claiming():
        return formatter.format_error("Players are claiming gods. Use `.draft undo` if you need to fix something.")
    turn = draft.get_current_team_and_action()
    if turn is None:
        return formatter.format_error("Current game is complete. Use `.draft next` or `.draft end`.")
    current_team, expected_action = turn
    action = intent["action"]
    if action != expected_action:
        return formatter.format_error(f"It's time to **{expected_action}**, not {action}.")
    expected_captain_id = draft.get_current_captain_id()
    if message.author.id != expected_captain_id:
        captain_name = (draft.blue_captain["name"] if current_team == "blue"
                        else draft.red_captain["name"])
        return formatter.format_error(f"It's **{captain_name}**'s turn ({current_team}).")
    god, error = resolve_god_name(intent["god_input"])
    if error:
        return formatter.format_error(error)
    unavailable = draft.get_unavailable_gods()
    if god in unavailable:
        if god in draft.fearless_pool:
            return formatter.format_error(f"**{god}** is in the fearless pool and unavailable this set.")
        return formatter.format_error(f"**{god}** has already been {expected_action}ned this game.")
    team, action_done = draft.execute_step(god)
    await message.channel.send(formatter.format_draft_action(team, action_done, god, draft.draft_id))
    await _update_draft_board(draft, message.channel)
    log.info(f"Draft {draft.draft_id}: {team} {action_done} {god} (step {draft.current_game.step}/20)")
    if draft.current_game.is_complete():
        await _post_claim_embeds(draft, message.channel)
    return None


async def _handle_draft_action_activity(intent: dict, message: discord.Message):
    """Activity backend .ban / .pick handler."""
    channel_id = message.channel.id
    match_id = _match_ids.get(channel_id)

    if not match_id:
        return formatter.format_error("No active draft. Use `.draft start` first.")

    snapshot = _snapshots.get(channel_id)
    if not snapshot:
        return formatter.format_error("Draft state loading — try again in a moment.")

    if snapshot.get("isClaiming"):
        return formatter.format_error("Claiming phase active. Use `.draft undo` to go back.")

    turn = snapshot.get("currentTurn")
    if not turn:
        return formatter.format_error("Game complete. Use `.draft next` or `.draft end`.")

    action = intent["action"]
    if action != turn["action"]:
        return formatter.format_error(f"It's time to **{turn['action']}**, not {action}.")

    expected_captain_id = snapshot.get("currentCaptainId")
    if expected_captain_id and str(message.author.id) != expected_captain_id:
        team = turn["team"]
        captain_name = (snapshot["blueCaptain"]["name"] if team == "blue"
                        else snapshot["redCaptain"]["name"])
        return formatter.format_error(f"It's **{captain_name}**'s turn ({team}).")

    god, error = resolve_god_name(intent["god_input"])
    if error:
        return formatter.format_error(error)

    result = await _activity_post(f"/api/draft/{match_id}/action", {
        "god": god,
        "userId": str(message.author.id),
    })
    if not result or "error" in result:
        return formatter.format_error(result.get("error", f"{god} is unavailable.") if result else "Backend unreachable.")

    log.info(f"Draft {match_id}: {turn['team']} {turn['action']} {god} via text command")
    return None  # WS listener updates the embed


# ── Local draft helpers (used when ACTIVITY_BACKEND_URL is not set) ───────────

async def _update_draft_board(draft, channel):
    """Edit the living draft board embed in place; fallback to posting new."""
    if draft.board_message_id:
        try:
            msg = await channel.fetch_message(draft.board_message_id)
            await msg.edit(embed=formatter.format_draft_board(draft))
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    sent = await channel.send(embed=formatter.format_draft_board(draft))
    draft.board_message_id = sent.id


async def _post_claim_embeds(draft, channel):
    """Post numbered claim embeds for both teams after a game completes."""
    game = draft.current_game
    for team in ("blue", "red"):
        embed = formatter.format_claim_embed(
            team,
            game.picks[team],
            game.claims[team],
            draft.draft_id,
            getattr(draft, "forgelens_match_id", ""),
            game.game_number,
            getattr(draft, "draft_sequence", 1),
        )
        sent = await channel.send(embed=embed)
        draft.claim_message_ids[team] = sent.id
        _tracked_messages[sent.id] = {
            "kind": "claim",
            "team": team,
            "picks": game.picks[team],
            "channel_id": channel.id,
            "draft_id": draft.draft_id,
        }
        for emoji in NUMBER_EMOJIS:
            await sent.add_reaction(emoji)
    log.info(f"Draft {draft.draft_id}: claim embeds posted for Game {game.game_number}")


async def _update_claim_embed(draft, team, channel):
    """Edit a claim embed after a player claims or unclaims."""
    msg_id = draft.claim_message_ids.get(team)
    if not msg_id:
        return
    try:
        msg = await channel.fetch_message(msg_id)
        game = draft.current_game
        embed = formatter.format_claim_embed(
            team,
            game.picks[team],
            game.claims[team],
            draft.draft_id,
            getattr(draft, "forgelens_match_id", ""),
            game.game_number,
            getattr(draft, "draft_sequence", 1),
        )
        await msg.edit(embed=embed)
        if all(god in game.claims[team] for god in game.picks[team]):
            try:
                await msg.clear_reactions()
            except discord.Forbidden:
                pass
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass



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
    """Handle 1️⃣-5️⃣ reactions on local draft claim embeds."""
    if emoji not in NUMBER_EMOJIS:
        return
    async with drafts.get_lock(channel_id):
        draft = drafts.get(channel_id)
        if not draft:
            return
        channel = client.get_channel(channel_id)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _tracked_messages.pop(message_id, None)
            return
        team = info["team"]
        picks = info["picks"]
        index = NUMBER_EMOJIS.index(emoji)
        if index >= len(picks):
            return
        god = picks[index]
        guild = client.get_guild(payload.guild_id) if payload.guild_id else None
        if guild:
            member = guild.get_member(payload.user_id)
            if not member:
                try:
                    member = await guild.fetch_member(payload.user_id)
                except (discord.NotFound, discord.Forbidden):
                    return
            user_name = member.display_name
        else:
            user = client.get_user(payload.user_id)
            if not user:
                try:
                    user = await client.fetch_user(payload.user_id)
                except (discord.NotFound, discord.Forbidden):
                    return
            user_name = user.display_name
        if draft.claim_god(team, god, payload.user_id, user_name):
            log.info(f"Draft {draft.draft_id}: {user_name} claimed {god} ({team})")
            await _update_claim_embed(draft, team, channel)
            if draft.current_game.is_fully_claimed():
                await channel.send(_draft_completion_marker(draft))
                log.info(f"Draft {draft.draft_id}: all claims complete for "
                         f"Game {draft.current_game.game_number}")


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


async def _handle_deprecated_economy_command(message: discord.Message, command: str):
    await message.channel.send(
        f"⚠️ `.{command}` is deprecated in GodForge. "
        "Economy, wallets, ledgers, and betting are not available in standalone "
        "GodForge. Use GodForge for parties, randomizers, sessions, and drafts."
    )


async def _handle_custom_command(message: discord.Message, trigger: str) -> bool:
    """Execute a dashboard-configured custom command if one matches."""
    if not trigger:
        return False

    guild = getattr(message, "guild", None)
    guild_id = str(getattr(guild, "id", "") or custom_commands.DEFAULT_GUILD_ID)
    clean_trigger = f".{trigger.lower()}"
    command = _find_custom_command(guild_id, clean_trigger)
    if not command:
        return False
    if not command.get("enabled", True):
        return True

    channel_gate = str(command.get("channel") or "").strip()
    if channel_gate and not _custom_command_channel_matches(message, channel_gate):
        await message.channel.send(f"⚠️ `{clean_trigger}` can only be used in {channel_gate}.")
        return True

    if not _custom_command_role_allowed(message, str(command.get("role_gate") or "Everyone")):
        await message.channel.send("⚠️ You do not have permission to use this custom command.")
        return True

    retry_after = _custom_command_retry_after(message, guild_id, clean_trigger, str(command.get("cooldown") or "0s"))
    if retry_after > 0:
        await message.channel.send(f"⏳ `{clean_trigger}` is on cooldown for {retry_after}s.")
        return True

    response = str(command.get("response") or "").strip()
    if not response:
        return True

    kwargs = {}
    if hasattr(discord, "AllowedMentions"):
        kwargs["allowed_mentions"] = discord.AllowedMentions.none()
    await message.channel.send(response, **kwargs)
    return True


def _find_custom_command(guild_id: str, trigger: str) -> dict | None:
    guild_commands = custom_commands.load_commands(guild_id)
    fallback_commands = (
        custom_commands.load_commands(custom_commands.DEFAULT_GUILD_ID)
        if guild_id != custom_commands.DEFAULT_GUILD_ID else []
    )
    for command in guild_commands + fallback_commands:
        if str(command.get("trigger") or "").lower() == trigger:
            return command
    return None


def _custom_command_channel_matches(message: discord.Message, channel_gate: str) -> bool:
    expected = channel_gate.strip().lower().lstrip("#")
    channel = getattr(message, "channel", None)
    channel_name = str(getattr(channel, "name", "") or "").lower()
    channel_id = str(getattr(channel, "id", "") or "")
    return expected in {channel_name, channel_id}


def _custom_command_role_allowed(message: discord.Message, role_gate: str) -> bool:
    if role_gate == "Everyone":
        return True
    if role_gate == "Admins":
        return _is_admin(message)
    if role_gate == "Captains":
        if _is_admin(message):
            return True
        roles = getattr(message.author, "roles", []) or []
        return any(str(getattr(role, "name", "")) == "Captains" for role in roles)
    return False


def _custom_command_retry_after(message: discord.Message, guild_id: str, trigger: str, cooldown: str) -> int:
    seconds = _parse_cooldown_seconds(cooldown)
    if seconds <= 0:
        return 0

    now = asyncio.get_running_loop().time()
    key = (guild_id, trigger, int(getattr(message.author, "id", 0) or 0))
    expires_at = _custom_command_cooldowns.get(key, 0)
    if expires_at > now:
        return max(1, int(expires_at - now))
    _custom_command_cooldowns[key] = now + seconds
    return 0


def _parse_cooldown_seconds(value: str) -> int:
    match = re.fullmatch(r"\s*(\d{1,4})\s*([smhSMH]?)\s*", str(value or ""))
    if not match:
        return 0
    amount = int(match.group(1))
    unit = match.group(2).lower() or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return min(amount * multiplier, 3600)


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


class _DiscordGuildSetupOperations:
    def __init__(self, guild: discord.Guild):
        self.guild = guild

    async def guild_permissions(self) -> PermissionSnapshot:
        permissions = self.guild.me.guild_permissions
        return PermissionSnapshot(manage_channels=permissions.manage_channels)

    async def channel_permissions(self, channel_id: int) -> PermissionSnapshot:
        channel = self.guild.get_channel(channel_id)
        if channel is None:
            return PermissionSnapshot()
        permissions = channel.permissions_for(self.guild.me)
        return PermissionSnapshot(
            view_channel=permissions.view_channel,
            send_messages=permissions.send_messages,
            embed_links=permissions.embed_links,
            read_message_history=permissions.read_message_history,
            manage_channels=permissions.manage_channels,
        )

    async def channel_exists(self, channel_id: int) -> bool:
        return self.guild.get_channel(channel_id) is not None

    async def message_exists(self, channel_id: int, message_id: int) -> bool:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            await channel.fetch_message(message_id)
        except discord.NotFound:
            return False
        except discord.Forbidden as exc:
            raise SetupOperationError(
                "panel_read_forbidden",
                "GodForge cannot verify its stored Play panel. Grant Read "
                "Message History and run setup again.",
            ) from exc
        except discord.HTTPException as exc:
            raise SetupOperationError(
                "panel_check_failed",
                "Discord could not verify the stored Play panel. No duplicate "
                "was created; retry setup shortly.",
            ) from exc
        return True

    async def create_play_channel(self) -> int:
        conflict = discord.utils.get(self.guild.text_channels, name="godforge-play")
        if conflict is not None:
            raise SetupOperationError(
                "channel_name_conflict",
                "A channel named #godforge-play already exists but is not managed "
                "by GodForge. Rename it or explicitly adopt it before retrying.",
            )
        channel = await self.guild.create_text_channel(
            "godforge-play",
            reason="GodForge zero-config setup",
        )
        return channel.id

    async def create_play_panel(self, channel_id: int) -> int:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise SetupOperationError(
                "invalid_play_channel",
                "The stored GodForge Play channel is not a text channel.",
            )
        message = await channel.send(
            embed=_play_panel_embed(),
            view=PlayPanelView(_handle_play_panel_action),
        )
        return message.id

    async def refresh_play_panel(self, channel_id: int, message_id: int) -> None:
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise SetupOperationError(
                "invalid_play_channel",
                "The stored GodForge Play channel is not a text channel.",
            )
        message = await channel.fetch_message(message_id)
        await message.edit(
            embed=_play_panel_embed(),
            view=PlayPanelView(_handle_play_panel_action),
        )


def _play_panel_embed() -> discord.Embed:
    return discord.Embed(
        title="Play SMITE with GodForge",
        description=(
            "Create or join a reusable party lobby, browse active groups, or "
            "set the roles you prefer to play."
        ),
        color=0x3498DB,
    )


async def _ensure_room_category(
    guild: discord.Guild,
    stored_category_id: str,
) -> int:
    if stored_category_id:
        category = guild.get_channel(int(stored_category_id))
        if isinstance(category, discord.CategoryChannel):
            return category.id
    conflict = discord.utils.get(guild.categories, name="GodForge Rooms")
    if conflict is not None:
        raise SetupOperationError(
            "category_name_conflict",
            "A category named GodForge Rooms already exists but is not managed "
            "by GodForge. Rename it or explicitly adopt it before retrying.",
        )
    return (
        await guild.create_category(
            "GodForge Rooms",
            reason="GodForge temporary party rooms",
        )
    ).id


party_commands = app_commands.Group(
    name="party",
    description="Set up and manage GodForge parties",
)
client.tree.add_command(party_commands)


@party_commands.command(name="setup", description="Set up GodForge for this server")
@app_commands.describe(
    test_mode="Use short-lived lobbies without recording match history",
    captain_role="Create an optional self-assignable captain role",
    substitute_role="Create an optional substitute role",
    region_role="Create an optional region role",
    lfg_role="Create an optional LFG notification role",
)
async def party_setup(
    interaction: discord.Interaction,
    test_mode: bool = False,
    captain_role: bool = False,
    substitute_role: bool = False,
    region_role: bool = False,
    lfg_role: bool = False,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Run this command inside a Discord server.",
            ephemeral=True,
        )
        return
    if not getattr(interaction.user.guild_permissions, "manage_guild", False):
        await interaction.response.send_message(
            "You need Manage Server to configure GodForge.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    current = settings.get_guild_settings(str(guild.id))
    managed = current["managed"]
    try:
        enabled_role_keys = ["solo", "jungle", "mid", "support", "adc"]
        enabled_role_keys.extend(
            key
            for key, enabled in (
                ("captain", captain_role),
                ("substitute", substitute_role),
                ("region", region_role),
                ("lfg", lfg_role),
            )
            if enabled
        )
        role_result = await reconcile_roles(
            guild,
            managed["roleIds"],
            enabled_keys=enabled_role_keys,
        )
        settings.update_guild_settings(
            str(guild.id),
            {
                "managed": {
                    "roleIds": {
                        key: str(role_id)
                        for key, role_id in role_result.role_ids.items()
                    }
                }
            },
            updated_by=f"discord:{interaction.user.id}",
        )
        category_id = await _ensure_room_category(
            guild,
            managed.get("roomCategoryId", ""),
        )
        settings.update_guild_settings(
            str(guild.id),
            {"managed": {"roomCategoryId": str(category_id)}},
            updated_by=f"discord:{interaction.user.id}",
        )
        setup_result = await GuildSetupService(
            _DiscordGuildSetupOperations(guild)
        ).reconcile(
            SetupReferences(
                int(managed["playChannelId"]) if managed["playChannelId"] else None,
                int(managed["playMessageId"]) if managed["playMessageId"] else None,
            )
        )
    except (ManagedRoleError, SetupOperationError, discord.DiscordException) as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    settings.update_guild_settings(
        str(guild.id),
        {
            "managed": {
                "playChannelId": (
                    str(setup_result.references.panel_channel_id)
                    if setup_result.references.panel_channel_id
                    else ""
                ),
                "playMessageId": (
                    str(setup_result.references.panel_message_id)
                    if setup_result.references.panel_message_id
                    else ""
                ),
            }
        },
        updated_by=f"discord:{interaction.user.id}",
    )
    if not setup_result.ok:
        await interaction.followup.send(setup_result.message, ephemeral=True)
        return
    settings.update_guild_settings(
        str(guild.id),
        {
            "managed": {
                "playChannelId": str(setup_result.references.panel_channel_id),
                "playMessageId": str(setup_result.references.panel_message_id),
                "roomCategoryId": str(category_id),
                "roleIds": {
                    key: str(role_id)
                    for key, role_id in role_result.role_ids.items()
                },
                "testMode": test_mode,
            }
        },
        updated_by=f"discord:{interaction.user.id}",
    )
    created_roles = ", ".join(role_result.created_keys) or "none"
    await interaction.followup.send(
        f"GodForge Play is ready. Created roles: {created_roles}. "
        f"Test mode: {'on' if test_mode else 'off'}.",
        ephemeral=True,
    )


async def _handle_play_panel_action(
    interaction: discord.Interaction,
    action: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Server-only action.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    if action == "preferences":
        preferences = party_repository.get_player_preferences(
            guild_id,
            interaction.user.id,
        )
        selected = ", ".join(preferences.roles) or "none yet"
        await interaction.response.send_message(
            f"Your role preferences: **{selected}**. Toggle them below.",
            view=RolePreferencesView(_handle_role_preference),
            ephemeral=True,
        )
        return
    active = [
        record.lobby
        for record in party_repository.recover_active(guild_id)
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
            embed=_lobby_card_embed(lobby),
            view=LobbyCardView(_handle_lobby_card_action),
            ephemeral=True,
        )
        for additional_lobby in active[1:]:
            await interaction.followup.send(
                embed=_lobby_card_embed(additional_lobby),
                view=LobbyCardView(_handle_lobby_card_action),
                ephemeral=True,
            )
        return
    if action == "create":
        await interaction.response.send_modal(
            CreateLobbyModal(_handle_create_lobby_submission)
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
            await _join_lobby_from_preferences(
                join_interaction,
                lobby.lobby_id,
                payload,
            )

        await interaction.response.send_modal(
            JoinPreferencesModal(join_handler)
        )


async def _handle_create_lobby_submission(
    interaction: discord.Interaction,
    payload: dict[str, object],
) -> None:
    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.response.send_message("Server-only action.", ephemeral=True)
        return
    test_mode = settings.get_guild_settings(str(guild_id))["managed"].get(
        "testMode",
        False,
    )
    lobby = party_repository.create(
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
    profile = party_repository.get_player_preferences(guild_id, interaction.user.id)
    lobby = party_repository.save_participant(
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
    await _ensure_party_queue(lobby)
    await interaction.response.send_message(
        embed=_lobby_card_embed(lobby),
        view=LobbyCardView(_handle_lobby_card_action),
        ephemeral=True,
    )


async def _join_lobby_from_preferences(
    interaction: discord.Interaction,
    lobby_id: str,
    payload: dict[str, object],
) -> None:
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
    party_repository.set_player_preferences(
        guild_id,
        interaction.user.id,
        profile,
    )
    lobby = party_repository.get(guild_id, lobby_id)
    if lobby is None:
        raise ValueError("lobby no longer exists")
    await _ensure_party_queue(lobby)
    queue, destination = await party_queue_service.join(
        lobby_id,
        interaction.user.id,
        profile.roles,
    )
    if destination in {"active", "unchanged"}:
        changed = party_repository.save_participant(
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
            embed=_lobby_card_embed(changed),
            view=LobbyCardView(_handle_lobby_card_action),
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
            embed=_lobby_card_embed(changed),
            view=LobbyCardView(_handle_lobby_card_action),
            ephemeral=True,
        )
    if len(queue.active) == queue.capacity and queue.status is QueueStatus.OPEN:
        queue = await party_queue_service.start_ready_check(lobby_id)
        if changed.state is LobbyState.OPEN:
            changed = party_repository.transition(
                guild_id,
                lobby_id,
                LobbyState.FULL,
                operation_id=f"discord:{interaction.id}:full",
            )
        if changed.state is LobbyState.FULL:
            party_repository.transition(
                guild_id,
                lobby_id,
                LobbyState.READY_CHECK,
                operation_id=f"discord:{interaction.id}:ready-check",
            )
        await interaction.channel.send(
            content=" ".join(f"<@{member.user_id}>" for member in queue.active),
            embed=_ready_check_embed(lobby_id, queue),
            view=ReadyCheckView(_handle_ready_check_action),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )


async def _ensure_party_queue(lobby):
    try:
        queue = await party_queue_service.create(lobby.lobby_id, lobby.capacity)
    except QueueError:
        queue = await party_queue_service.get(lobby.lobby_id)
    if queue is None:
        raise RuntimeError("party queue could not be initialized")
    if queue.capacity != lobby.capacity:
        queue, promoted_ids = await party_queue_service.resize(
            lobby.lobby_id,
            lobby.capacity,
        )
        for promoted_id in promoted_ids:
            promoted = party_repository.get_player_preferences(
                lobby.guild_id,
                promoted_id,
            )
            party_repository.save_participant(
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
            queue, _ = await party_queue_service.join(
                lobby.lobby_id,
                participant.user_id,
                participant.preferences,
            )
    return queue


def _lobby_card_embed(lobby) -> discord.Embed:
    participants = ", ".join(f"<@{p.user_id}>" for p in lobby.participants) or "None"
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


def _lobby_id_from_interaction(interaction: discord.Interaction) -> str:
    embeds = getattr(interaction.message, "embeds", ())
    footer = embeds[0].footer.text if embeds and embeds[0].footer else ""
    if not footer.startswith("lobby_id="):
        raise ValueError("This lobby card is missing its stable identity.")
    return footer.removeprefix("lobby_id=")


async def _handle_lobby_card_action(
    interaction: discord.Interaction,
    action: str,
) -> None:
    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.response.send_message("Server-only action.", ephemeral=True)
        return
    lobby_id = _lobby_id_from_interaction(interaction)
    active_ids = {
        record.lobby.lobby_id
        for record in party_repository.recover_active(guild_id)
    }
    lobby = party_repository.get(guild_id, lobby_id)
    if lobby is None or lobby_id not in active_ids:
        await interaction.response.send_message(
            "That lobby is no longer active.",
            ephemeral=True,
        )
        return
    if action == "join":
        async def join_handler(join_interaction, payload):
            await _join_lobby_from_preferences(join_interaction, lobby_id, payload)

        await interaction.response.send_modal(JoinPreferencesModal(join_handler))
        return
    if action == "leave":
        await _ensure_party_queue(lobby)
        queue, promoted_id = await party_queue_service.leave(
            lobby_id,
            interaction.user.id,
        )
        changed = party_repository.remove_participant(
            guild_id,
            lobby_id,
            interaction.user.id,
            operation_id=f"discord:{interaction.id}:leave",
            actor_id=interaction.user.id,
        )
        if promoted_id is not None:
            promoted = party_repository.get_player_preferences(guild_id, promoted_id)
            changed = party_repository.save_participant(
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
            embed=_lobby_card_embed(changed),
            view=LobbyCardView(_handle_lobby_card_action),
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
        queue = await _ensure_party_queue(lobby)
        if queue.status is not QueueStatus.OPEN:
            await interaction.response.send_message(
                "A ready check is already active or this queue is closed.",
                ephemeral=True,
            )
            return
        queue = await party_queue_service.start_ready_check(lobby_id)
        if lobby.state is LobbyState.OPEN and len(queue.active) == lobby.capacity:
            lobby = party_repository.transition(
                guild_id,
                lobby_id,
                LobbyState.FULL,
                operation_id=f"discord:{interaction.id}:full",
            )
        if lobby.state in {LobbyState.FULL, LobbyState.OPEN}:
            party_repository.transition(
                guild_id,
                lobby_id,
                LobbyState.READY_CHECK,
                operation_id=f"discord:{interaction.id}:ready-check",
            )
        await interaction.response.send_message(
            content=" ".join(f"<@{member.user_id}>" for member in queue.active),
            embed=_ready_check_embed(lobby_id, queue),
            view=ReadyCheckView(_handle_ready_check_action),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )
        return
    if action == "launch_draft":
        await _launch_party_draft(interaction, lobby)
        return
    if action in {"edit", "cancel"} and interaction.user.id != lobby.organizer_id:
        await interaction.response.send_message(
            "Only the organizer can change or cancel this lobby.",
            ephemeral=True,
        )
        return
    if action == "cancel":
        changed = party_repository.transition(
            guild_id,
            lobby_id,
            LobbyState.CANCELLED,
            operation_id=f"discord:{interaction.id}:cancel",
            actor_id=interaction.user.id,
        )
        await interaction.response.edit_message(
            embed=_lobby_card_embed(changed),
            view=None,
        )
        return
    if action == "edit":
        async def edit_handler(edit_interaction, payload):
            changed = party_repository.update_metadata(
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
                embed=_lobby_card_embed(changed),
                view=LobbyCardView(_handle_lobby_card_action),
                ephemeral=True,
            )

        await interaction.response.send_modal(CreateLobbyModal(edit_handler))
        return
    if action == "share":
        await interaction.response.send_message("Lobby shared.", ephemeral=True)
        await interaction.channel.send(
            embed=_lobby_card_embed(lobby),
            view=LobbyCardView(_handle_lobby_card_action),
        )


async def _launch_party_draft(
    interaction: discord.Interaction,
    lobby,
) -> None:
    """Confirm deterministic teams and launch the existing draft engine."""
    if interaction.user.id != lobby.organizer_id:
        await interaction.response.send_message(
            "Only the organizer can confirm teams and launch the draft.",
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
        await interaction.response.send_message("Draft channel is unavailable.", ephemeral=True)
        return
    if _channel_has_active(channel.id):
        await interaction.response.send_message(
            "This channel already has an active session or draft.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    launch = None
    try:
        launch, should_start = party_draft_repository.begin(
            lobby,
            operation_id=f"discord:{interaction.id}:party-draft",
            channel_id=channel.id,
            match_id_factory=match_ids.reserve_match_id,
        )
        if not should_start:
            message = (
                f"Draft `{launch.match_id}` is already active."
                if launch.status == "active"
                else "That draft launch is already in progress."
            )
            await interaction.followup.send(message, ephemeral=True)
            return

        blue_member = interaction.guild.get_member(launch.blue.captain_id)
        red_member = interaction.guild.get_member(launch.red.captain_id)
        if blue_member is None or red_member is None:
            raise PartyDraftError("a selected captain is no longer in this server")

        if ACTIVITY_BACKEND_URL:
            result = await _activity_post(
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
            _match_ids[channel.id] = activity_id
            _match_channels[activity_id] = channel.id
            if result.get("state"):
                _snapshots[channel.id] = result["state"]
                sent = await channel.send(
                    f"🎮 Draft `{activity_id}` started — open the Activity and "
                    "enter this ID to join",
                    embed=formatter.format_board_from_snapshot(result["state"]),
                )
                _board_message_ids[channel.id] = sent.id
            task = asyncio.create_task(_listen_draft_ws(activity_id, channel.id))
            _ws_tasks[channel.id] = task
        else:
            draft = drafts.start(
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
            _save_active_draft(channel.id, draft.draft_id)

        launch = party_draft_repository.mark_active(lobby.lobby_id)
        party_repository.transition(
            lobby.guild_id,
            lobby.lobby_id,
            LobbyState.ACTIVE,
            operation_id=f"discord:{interaction.id}:draft-active",
            actor_id=interaction.user.id,
        )
        await channel.send(
            f"⚔️ Draft `{launch.match_id}` launched from lobby `{lobby.lobby_id[:8]}`.\n"
            f"🔵 Captain <@{launch.blue.captain_id}>: "
            + " ".join(f"<@{user_id}>" for user_id in launch.blue.participant_ids)
            + f"\n🔴 Captain <@{launch.red.captain_id}>: "
            + " ".join(f"<@{user_id}>" for user_id in launch.red.participant_ids),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )
        await interaction.followup.send(
            f"Draft `{launch.match_id}` started. Teams and lobby rules are retained.",
            ephemeral=True,
        )
    except Exception as exc:
        if launch is not None:
            party_draft_repository.mark_failed(lobby.lobby_id, str(exc))
        log.exception("Party draft launch failed for lobby %s", lobby.lobby_id)
        await interaction.followup.send(
            "Draft launch failed. The lobby is still ready to retry with "
            "`Launch Draft`. " + str(exc),
            ephemeral=True,
        )


def _ready_check_embed(lobby_id: str, queue) -> discord.Embed:
    ready_count = sum(
        status is ReadyStatus.READY for status in queue.ready.values()
    )
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


async def _handle_ready_check_action(
    interaction: discord.Interaction,
    action: str,
) -> None:
    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.response.send_message("Server-only action.", ephemeral=True)
        return
    lobby_id = _lobby_id_from_interaction(interaction)
    status = {
        "ready": ReadyStatus.READY,
        "need_five": ReadyStatus.NEED_5,
        "drop": ReadyStatus.DROP,
    }[action]
    queue, promoted_id = await party_queue_service.respond(
        lobby_id,
        interaction.user.id,
        status,
    )
    if status is ReadyStatus.DROP:
        changed = party_repository.remove_participant(
            guild_id,
            lobby_id,
            interaction.user.id,
            operation_id=f"discord:{interaction.id}:ready-drop",
            actor_id=interaction.user.id,
        )
        if promoted_id is not None:
            promoted = party_repository.get_player_preferences(guild_id, promoted_id)
            changed = party_repository.save_participant(
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
            party_repository.transition(
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
        lobby = party_repository.get(guild_id, lobby_id)
        if lobby and lobby.state is LobbyState.READY_CHECK:
            party_repository.transition(
                guild_id,
                lobby_id,
                LobbyState.FORMING,
                operation_id=f"discord:{interaction.id}:forming",
            )
        await interaction.response.edit_message(
            content="Everyone is ready. GodForge is forming the match.",
            embed=_ready_check_embed(lobby_id, queue),
            view=None,
        )
        return
    await interaction.response.edit_message(
        embed=_ready_check_embed(lobby_id, queue),
        view=ReadyCheckView(_handle_ready_check_action),
    )
    if promoted_id is not None:
        await interaction.followup.send(
            f"<@{promoted_id}> was promoted from the waitlist.",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
        )


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
