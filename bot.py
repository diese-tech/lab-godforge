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

