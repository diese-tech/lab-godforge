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


def _truncate(text: str, max_len: int = 1900) -> str:
    """Truncate text to fit Discord's message limit."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n…(truncated)"


async def _post_wallets_to_reports(guild_id: int) -> bool:
    """Post current wallet balances to the guild's reports channel. Returns True on success."""
    if not guild or guild.id not in REPORTS_CHANNELS:
        return False
    reports_ch = client.get_channel(REPORTS_CHANNELS[guild_id])
    if not reports_ch:
        return False
    balances = wallet_utils.get_all_balances()
    if not balances:
        await reports_ch.send("💰 Wallet snapshot: no balances on record.")
        return True
    lines = [f"**{name}**: {bal} coins" for name, bal in sorted(balances.items())]
    msg = "💰 **Wallet Snapshot**\n" + "\n".join(lines)
    await reports_ch.send(_truncate(msg))
    log.info(f"Wallets posted to reports channel {REPORTS_CHANNELS[guild_id]}")
    return True


# ── Activity backend helpers ──────────────────────────────────────────────────

def _activity_headers() -> dict:
    return {"X-API-Key": ACTIVITY_API_KEY, "Content-Type": "application/json"}


async def _activity_post(session: aiohttp.ClientSession, path: str, payload: dict) -> dict | None:
    url = f"{ACTIVITY_BACKEND_URL}{path}"
    try:
        async with session.post(url, json=payload, headers=_activity_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status >= 400:
                text = await resp.text()
                log.warning(f"Activity POST {path} → {resp.status}: {text[:200]}")
                return None
            return await resp.json()
    except Exception as exc:
        log.warning(f"Activity POST {path} failed: {exc}")
        return None


async def _activity_get(session: aiohttp.ClientSession, path: str) -> dict | None:
    url = f"{ACTIVITY_BACKEND_URL}{path}"
    try:
        async with session.get(url, headers=_activity_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status >= 400:
                return None
            return await resp.json()
    except Exception as exc:
        log.warning(f"Activity GET {path} failed: {exc}")
        return None


async def _open_match(channel_id: int, blue_id: int, red_id: int) -> str | None:
    """Open a match in the Activity backend and return match_id."""
    if not ACTIVITY_BACKEND_URL:
        return None
    async with aiohttp.ClientSession() as session:
        data = await _activity_post(session, "/matches", {
            "channel_id": channel_id,
            "blue_captain_id": blue_id,
            "red_captain_id": red_id,
        })
    if data and "match_id" in data:
        return data["match_id"]
    return None


async def _get_snapshot(match_id: str) -> dict | None:
    if not ACTIVITY_BACKEND_URL:
        return None
    async with aiohttp.ClientSession() as session:
        return await _activity_get(session, f"/matches/{match_id}/state")


async def _post_draft_export(match_id: str, export: dict) -> bool:
    if not ACTIVITY_BACKEND_URL:
        return False
    async with aiohttp.ClientSession() as session:
        result = await _activity_post(session, f"/matches/{match_id}/export", export)
    return result is not None


# ── WebSocket listener for Activity state updates ─────────────────────────────

async def _ws_listener(channel_id: int, match_id: str) -> None:
    """Long-running task: listen for Activity state updates and refresh the board embed."""
    if not ACTIVITY_BACKEND_URL:
        return
    ws_url = ACTIVITY_BACKEND_URL.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/matches/{match_id}/ws"
    log.info(f"WS listener starting for match {match_id} in channel {channel_id}")

    while True:
        try:
            async with aiohttp.ClientSession() as http_session:
                async with http_session.ws_connect(ws_url, headers=_activity_headers()) as ws:
                    log.info(f"WS connected: {match_id}")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue
                            if data.get("type") == "state_update":
                                snapshot = data.get("snapshot", {})
                                _snapshots[channel_id] = snapshot
                                await _refresh_board_embed(channel_id, snapshot)
                            elif data.get("type") == "draft_complete":
                                export = data.get("export", {})
                                await _handle_draft_complete(channel_id, export)
                                return
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                            break
        except Exception as exc:
            log.warning(f"WS error for match {match_id}: {exc}")
        if channel_id not in _match_ids:
            log.info(f"WS listener stopping: match {match_id} no longer tracked")
            return
        await asyncio.sleep(5)  # reconnect delay


async def _refresh_board_embed(channel_id: int, snapshot: dict) -> None:
    msg_id = _board_message_ids.get(channel_id)
    if not msg_id:
        return
    ch = client.get_channel(channel_id)
    if not ch:
        return
    try:
        msg = await ch.fetch_message(msg_id)
        embed = formatter.format_board_from_snapshot(snapshot)
        await msg.edit(embed=embed)
    except Exception as exc:
        log.warning(f"Board embed refresh failed: {exc}")


async def _handle_draft_complete(channel_id: int, export: dict) -> None:
    """Handle Activity-driven draft completion."""
    match_id = _match_ids.pop(channel_id, None)
    _match_channels.pop(match_id, None)
    _snapshots.pop(channel_id, None)
    _board_message_ids.pop(channel_id, None)
    _ws_tasks.pop(channel_id, None)

    ch = client.get_channel(channel_id)
    if not ch:
        return

    embed = formatter.format_draft_end_from_export(export)
    await ch.send(embed=embed)

    export_json = json.dumps(export, indent=2)
    file = discord.File(
        io.BytesIO(export_json.encode()),
        filename=f"draft_{export.get('draft_id', 'unknown')}.json",
    )
    await ch.send("📦 Draft export:", file=file)


# ── Event handlers ────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    log.info(f"GodForge online as {client.user} (id={client.user.id})")


@client.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    message_id = reaction.message.id
    channel_id = reaction.message.channel.id
    emoji = str(reaction.emoji)
    author_id = user.id
    author_name = user.display_name

    info = _tracked_messages.get(message_id)
    if not info:
        return

    session = sessions.get(channel_id)
    if not session:
        _tracked_messages.pop(message_id, None)
        return

    msg = reaction.message

    if info["kind"] == "roll5":
        number_map = {e: i for i, e in enumerate(NUMBER_EMOJIS)}
        if emoji not in number_map:
            return
        selected_index = number_map[emoji]
        god = session.lock_roll5_pick(message_id, selected_index, author_id, author_name)
        if god:
            embed = formatter.format_roll5_locked(
                info["gods"], selected_index, author_name, info["role"], info["source"]
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
            discarded = session.discard_rg(message_id)
            if discarded:
                embed = formatter.format_rg_discarded(discarded, info["role"], info["source"])
                await msg.edit(embed=embed)
                try:
                    await msg.clear_reactions()
                except discord.Forbidden:
                    pass
                _tracked_messages.pop(message_id, None)


# ── Command dispatcher ────────────────────────────────────────────────────────

@client.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content.startswith("."):
        return

    channel_id = message.channel.id
    guild_id = message.guild.id if message.guild else None

    # ── .help ──────────────────────────────────────────────────────────────────
    if content == ".help":
        embed = formatter.format_help()
        help_msg = await message.channel.send(embed=embed)
        await help_msg.add_reaction("➡️")
        return

    # ── .version ──────────────────────────────────────────────────────────────
    if content == ".version":
        await message.channel.send(
            f"GodForge v{formatter.GODFORGE_VERSION} — {formatter.RELEASE_NOTES}"
        )
        return

    # ── .session ──────────────────────────────────────────────────────────────
    if content.startswith(".session"):
        parts = content.split()
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "start":
            active = _channel_has_active(channel_id)
            if active == "draft":
                await message.channel.send("⚠️ A draft is active in this channel. End it first.")
                return
            if sessions.get(channel_id):
                await message.channel.send("⚠️ A session is already active in this channel.")
                return
            sessions.start(channel_id)
            await message.channel.send("✅ Session started! `.rg` and `.roll5` will now track picks.")
            return

        if sub == "show":
            session = sessions.get(channel_id)
            if not session:
                await message.channel.send("⚠️ No active session in this channel.")
                return
            embed = formatter.format_session_show(session.picks)
            await message.channel.send(embed=embed)
            return

        if sub == "reset":
            session = sessions.get(channel_id)
            if not session:
                await message.channel.send("⚠️ No active session in this channel.")
                return
            session.reset()
            await message.channel.send("✅ Session picks cleared.")
            return

        if sub == "end":
            session = sessions.get(channel_id)
            if not session:
                await message.channel.send("⚠️ No active session in this channel.")
                return
            embed = formatter.format_session_end(session.picks)
            sessions.end(channel_id)
            await message.channel.send(embed=embed)
            return

        await message.channel.send("⚠️ Unknown session command. Try `.session start|show|reset|end`.")
        return

    # ── .draft ─────────────────────────────────────────────────────────────────
    if content.startswith(".draft"):
        parts = content.split()
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "start":
            active = _channel_has_active(channel_id)
            if active == "session":
                await message.channel.send("⚠️ A session is active in this channel. End it first.")
                return
            if active == "draft":
                await message.channel.send("⚠️ A draft is already active in this channel.")
                return

            mentions = message.mentions
            if len(mentions) < 2:
                await message.channel.send("⚠️ Usage: `.draft start @blue_captain @red_captain`")
                return

            blue_user = mentions[0]
            red_user = mentions[1]
            blue_cap = {"id": blue_user.id, "name": blue_user.display_name}
            red_cap = {"id": red_user.id, "name": red_user.display_name}

            draft = drafts.start(channel_id, blue_cap, red_cap)
            embed = formatter.format_draft_board(draft)
            board_msg = await message.channel.send(embed=embed)
            _board_message_ids[channel_id] = board_msg.id

            # Try to open Activity match
            if ACTIVITY_BACKEND_URL:
                match_id = await _open_match(channel_id, blue_user.id, red_user.id)
                if match_id:
                    _match_ids[channel_id] = match_id
                    _match_channels[match_id] = channel_id
                    task = asyncio.create_task(_ws_listener(channel_id, match_id))
                    _ws_tasks[channel_id] = task
                    log.info(f"Activity match opened: {match_id} for channel {channel_id}")
            return

        if sub == "show":
            draft = drafts.get(channel_id)
            if not draft:
                await message.channel.send("⚠️ No active draft in this channel.")
                return
            embed = formatter.format_draft_show(draft)
            await message.channel.send(embed=embed)
            return

        if sub == "next":
            draft = drafts.get(channel_id)
            if not draft:
                await message.channel.send("⚠️ No active draft in this channel.")
                return
            ok, err = draft.advance_game()
            if not ok:
                await message.channel.send(f"⚠️ {err}")
                return
            confirmation = formatter.format_draft_next(draft)
            await message.channel.send(confirmation)
            embed = formatter.format_draft_board(draft)
            board_msg = await message.channel.send(embed=embed)
            _board_message_ids[channel_id] = board_msg.id
            return

        if sub == "undo":
            draft = drafts.get(channel_id)
            if not draft:
                await message.channel.send("⚠️ No active draft in this channel.")
                return
            result = draft.undo()
            if result is None:
                await message.channel.send("⚠️ Nothing to undo.")
                return
            team, action_type, god = result
            confirmation = formatter.format_draft_undo(team, action_type, god)
            await message.channel.send(confirmation)
            embed = formatter.format_draft_board(draft)
            msg_id = _board_message_ids.get(channel_id)
            if msg_id:
                try:
                    board_msg = await message.channel.fetch_message(msg_id)
                    await board_msg.edit(embed=embed)
                except Exception:
                    board_msg = await message.channel.send(embed=embed)
                    _board_message_ids[channel_id] = board_msg.id
            return

        if sub == "end":
            draft = drafts.get(channel_id)
            if not draft:
                await message.channel.send("⚠️ No active draft in this channel.")
                return

            export = draft.export()
            embed = formatter.format_draft_end(draft, export)
            await message.channel.send(embed=embed)

            export_json = json.dumps(export, indent=2)
            file = discord.File(
                io.BytesIO(export_json.encode()),
                filename=f"draft_{draft.draft_id}.json",
            )
            await message.channel.send("📦 Draft export:", file=file)

            # Post to Activity backend
            match_id = _match_ids.get(channel_id)
            if match_id:
                await _post_draft_export(match_id, export)

            # Clean up
            drafts.end(channel_id)
            _match_ids.pop(channel_id, None)
            _match_channels.pop(match_id, None) if match_id else None
            _snapshots.pop(channel_id, None)
            _board_message_ids.pop(channel_id, None)
            task = _ws_tasks.pop(channel_id, None)
            if task:
                task.cancel()

            # Post claim embeds
            for team in ("blue", "red"):
                picks = export["games"][-1]["picks"][team] if export["games"] else []
                if picks:
                    claim_embed = formatter.format_claim_embed(
                        team, picks, {},
                        draft_id=draft.draft_id,
                        forgelens_match_id=match_id or "",
                        game_number=export["games"][-1]["game_number"] if export["games"] else 1,
                        draft_sequence=export.get("draft_sequence", 1),
                    )
                    claim_msg = await message.channel.send(embed=claim_embed)
                    for i in range(len(picks)):
                        await claim_msg.add_reaction(NUMBER_EMOJIS[i])
            return

        await message.channel.send("⚠️ Unknown draft subcommand. Try `.draft start|show|next|undo|end`.")
        return

    # ── .ban / .pick ──────────────────────────────────────────────────────────
    if content.startswith(".ban ") or content.startswith(".pick "):
        action_type = "ban" if content.startswith(".ban") else "pick"
        raw_god = content[5:].strip() if action_type == "ban" else content[6:].strip()

        draft = drafts.get(channel_id)
        if not draft:
            await message.channel.send("⚠️ No active draft in this channel.")
            return

        from utils.resolver import resolve_god_name
        god_name = resolve_god_name(raw_god)
        if not god_name:
            await message.channel.send(f"⚠️ Unknown god: `{raw_god}`. Check spelling or alias.")
            return

        ok, err = draft.do_action(action_type, god_name, message.author.id)
        if not ok:
            await message.channel.send(f"⚠️ {err}")
            return

        confirmation = formatter.format_draft_action(
            "blue" if draft.current_game.last_team == "blue" else "red",
            action_type, god_name, draft.draft_id
        )
        await message.channel.send(confirmation)

        embed = formatter.format_draft_board(draft)
        msg_id = _board_message_ids.get(channel_id)
        if msg_id:
            try:
                board_msg = await message.channel.fetch_message(msg_id)
                await board_msg.edit(embed=embed)
            except Exception:
                board_msg = await message.channel.send(embed=embed)
                _board_message_ids[channel_id] = board_msg.id
        return

    # ── .rg (random god) ──────────────────────────────────────────────────────
    rg_match = re.fullmatch(r"\.rg([jmaso]?)([tw]?)", content)
    if rg_match:
        role_char = rg_match.group(1)
        source_char = rg_match.group(2)
        role = {"j": "jungle", "m": "mid", "a": "adc", "s": "support", "o": "solo"}.get(role_char)
        source = "website" if source_char == "w" else "tab"

        god = picker.pick_random_god(role, source)
        if not god:
            await message.channel.send(formatter.format_error(f"No gods found for role={role}, source={source}."))
            return

        session = sessions.get(channel_id)
        if session:
            msg_obj = await message.channel.send(embed=formatter.format_rg_session(god, role, source))
            session.track_rg(msg_obj.id, god)
            _tracked_messages[msg_obj.id] = {"kind": "rg", "role": role, "source": source}
            await msg_obj.add_reaction("✅")
            await msg_obj.add_reaction("❌")
        else:
            await message.channel.send(embed=formatter.format_god(god, role, source))
        return

    # ── .roll5 ────────────────────────────────────────────────────────────────
    roll5_match = re.fullmatch(r"\.roll5([jmaso]?)([tw]?)(\d?)", content)
    if roll5_match:
        role_char = roll5_match.group(1)
        source_char = roll5_match.group(2)
        count_char = roll5_match.group(3)
        role = {"j": "jungle", "m": "mid", "a": "adc", "s": "support", "o": "solo"}.get(role_char)
        source = "website" if source_char == "w" else "tab"
        count = int(count_char) if count_char else 5

        gods = picker.pick_random_gods(count, role, source)
        if not gods:
            await message.channel.send(formatter.format_error(f"Not enough gods for role={role}, source={source}."))
            return

        session = sessions.get(channel_id)
        if session:
            msg_obj = await message.channel.send(embed=formatter.format_roll5_session(gods, role, source))
            session.track_roll5(msg_obj.id, gods)
            _tracked_messages[msg_obj.id] = {"kind": "roll5", "gods": gods, "role": role, "source": source}
            for i in range(len(gods)):
                await msg_obj.add_reaction(NUMBER_EMOJIS[i])
        else:
            await message.channel.send(embed=formatter.format_team(gods, role, source))
        return

    # ── Build commands ─────────────────────────────────────────────────────────
    build_match = re.fullmatch(
        r"\.(mid|jung|solo|adc|sup|rc)(int|str|hyb)?(\d?)", content
    )
    if build_match:
        role_key = build_match.group(1)
        type_key = build_match.group(2)
        count_char = build_match.group(3)

        role_map = {"mid": "mid", "jung": "jungle", "solo": "solo", "adc": "adc", "sup": "support", "rc": "chaos"}
        role = role_map[role_key]
        count = int(count_char) if count_char else None

        items = loader.get_build(role, type_key, count)
        if items is None:
            await message.channel.send(formatter.format_error(f"No build found for {role}/{type_key}."))
            return
        await message.channel.send(formatter.format_build(items, role, type_key))
        return

    # ── Legacy economy commands (gated) ───────────────────────────────────────
    if not LEGACY_ECONOMY_ENABLED:
        legacy_cmds = {".match", ".bet", ".wallet", ".ledger"}
        if any(content == cmd or content.startswith(cmd + " ") for cmd in legacy_cmds):
            await message.channel.send(
                "⚠️ Economy commands are deprecated. ForgeLens owns betting, wallets, and ledgers.\n"
                "Use `.draft start @blue @red` for match orchestration."
            )
            return

    # ── Elevated owner commands ────────────────────────────────────────────────
    if message.author.id == _GOD_USER_ID:
        if content == ".wallets":
            posted = await _post_wallets_to_reports(guild_id)
            if not posted:
                await message.channel.send("⚠️ No reports channel configured for this guild.")
            return

        if content.startswith(".announce "):
            announcement = content[10:].strip()
            if guild_id and guild_id in REPORTS_CHANNELS:
                reports_ch = client.get_channel(REPORTS_CHANNELS[guild_id])
                if reports_ch:
                    await reports_ch.send(announcement)
                    return
            await message.channel.send("⚠️ No reports channel configured.")
            return

    # ── Custom commands ────────────────────────────────────────────────────────
    cmd_key = content.lstrip(".")
    cmd_key = cmd_key.split()[0] if cmd_key else ""
    if cmd_key:
        guild_id_str = str(guild_id) if guild_id else "global"
        cooldown_key = (guild_id_str, str(channel_id), message.author.id)
        now = asyncio.get_event_loop().time()
        last = _custom_command_cooldowns.get(cooldown_key, 0)
        if now - last < 2.0:
            return
        response = custom_commands.get_response(cmd_key, guild_id_str)
        if response:
            _custom_command_cooldowns[cooldown_key] = now
            await message.channel.send(response)


# ── Reaction pagination for help ─────────────────────────────────────────────

@client.event
async def on_reaction_add_help(reaction, user):
    """Paginate .help between page 1 and page 2."""
    if user.bot:
        return
    msg = reaction.message
    if not msg.embeds:
        return
    embed = msg.embeds[0]
    emoji = str(reaction.emoji)

    if embed.title == "GodForge Commands" and emoji == "➡️":
        new_embed = formatter.format_help_page2()
        await msg.edit(embed=new_embed)
        await msg.clear_reactions()
        await msg.add_reaction("⬅️")
    elif embed.title == "GodForge Commands — Deprecated Economy" and emoji == "⬅️":
        new_embed = formatter.format_help_page1()
        await msg.edit(embed=new_embed)
        await msg.clear_reactions()
        await msg.add_reaction("➡️")


# ── Periodic wallet reporting ─────────────────────────────────────────────────

@tasks.loop(hours=24)
async def daily_wallet_report():
    for guild in client.guilds:
        if guild.id in REPORTS_CHANNELS:
            await _post_wallets_to_reports(guild.id)


@daily_wallet_report.before_loop
async def before_daily_wallet_report():
    await client.wait_until_ready()
    now = datetime.now(timezone.utc)
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= target:
        from datetime import timedelta
        target += timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())


# ── Ledger helpers (legacy economy surface) ────────────────────────────────────

async def _handle_ledger_command(message, parts: list[str]) -> None:
    """Handle .ledger subcommands (legacy economy surface)."""
    sub = parts[1] if len(parts) > 1 else ""
    guild_id = message.guild.id if message.guild else None

    if sub == "show":
        entries = ledger_utils.get_ledger()
        if not entries:
            await message.channel.send("📒 Ledger is empty.")
            return
        lines = [f"**{e['match_id']}** — {e['result']} — {e['timestamp']}" for e in entries[-20:]]
        await message.channel.send("📒 **Recent Ledger**\n" + "\n".join(lines))
        return

    if sub == "post" and guild_id and guild_id in REPORTS_CHANNELS:
        reports_ch = client.get_channel(REPORTS_CHANNELS[guild_id])
        if reports_ch:
            entries = ledger_utils.get_ledger()
            if entries:
                lines = [f"**{e['match_id']}** — {e['result']}" for e in entries[-10:]]
                await reports_ch.send("📒 **Ledger Update**\n" + "\n".join(lines))
            return

    await message.channel.send("⚠️ Unknown ledger command.")


# ── Betting helpers (legacy economy surface) ──────────────────────────────────

async def _settle_bets(match_id: str, winner: str, channel) -> None:
    """Settle all open bets for a match. winner is 'blue' or 'red'."""
    results = wallet_utils.settle_bets(match_id, winner)
    if not results:
        await channel.send(f"✅ No open bets to settle for match `{match_id}`.")
        return
    lines = []
    for r in results:
        if r["won"]:
            lines.append(f"✅ **{r['user_name']}** won {r['payout']} coins (bet {r['amount']} on {r['side']})")
        else:
            lines.append(f"❌ **{r['user_name']}** lost {r['amount']} coins (bet on {r['side']})")
    await channel.send("🏆 **Bet Settlement**\n" + "\n".join(lines))


async def _post_to_ledger_channel(content: str) -> None:
    """Post a string to the configured BETTING_LEDGER_CHANNEL_ID."""
    if not BETTING_LEDGER_CHANNEL_ID:
        return
    ch = client.get_channel(BETTING_LEDGER_CHANNEL_ID)
    if ch:
        await ch.send(content)


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set.")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
