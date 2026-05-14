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
from datetime import datetime, timezone
from discord.ext import tasks
from dotenv import load_dotenv

from utils import custom_commands, formatter, loader, parser, picker
from utils.formatter import NUMBER_EMOJIS
from utils.resolver import resolve_god_name
from utils.session import SessionManager
from utils.draft import DraftManager
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
client = discord.Client(intents=intents)

sessions = SessionManager()
drafts = DraftManager()

# Track metadata for reaction-enabled messages (sessions only).
_tracked_messages = {}
_custom_command_cooldowns: dict[tuple[str, str, int], float] = {}

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
    match = re.search(r"(?:^|\s)--match\s+(\S+)", content)
    game = re.search(r"(?:^|\s)--game\s+(\d+)", content)
    return {
        "forgelens_match_id": match.group(1) if match else "",
        "game_number": int(game.group(1)) if game else 1,
    }


def _draft_completion_marker(draft) -> str:
    return "\n".join(
        [
            "Draft complete",
            f"draft_id={draft.draft_id}",
            f"forgelens_match_id={getattr(draft, 'forgelens_match_id', '')}",
            f"game_number={draft.current_game.game_number}",
        ]
    )


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
    await channel.send(
        "\n".join(
            [
                "Draft complete",
                f"draft_id={export.get('draft_id', draft_id)}",
                f"forgelens_match_id={export.get('forgelens_match_id', '')}",
                f"game_number={export.get('game_number', 1)}",
            ]
        )
    )

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
    log.info("Economy commands are deprecated in GodForge; ForgeLens owns betting and ledgers.")


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
        "ForgeLens owns betting, wallets, ledgers, results, and OCR/stat workflows. "
        "Use GodForge draft commands for match orchestration and JSON handoff."
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


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set.")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
