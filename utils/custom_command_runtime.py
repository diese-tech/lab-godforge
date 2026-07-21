"""Runtime execution of dashboard-configured custom dot-commands.

Feature module for the Issue #48 refactor. It owns custom-command matching,
channel/role gating, and per-user cooldowns — the behavior that previously lived
directly in ``bot.py``. Dependencies are injected (the command store, the admin
check, and a clock) so the runtime is unit-testable without ``bot.py`` globals or
a running event loop.

Persistence of the command definitions themselves stays in
``utils/custom_commands.py`` (the store); this module is the execution adapter.
"""

from __future__ import annotations

import asyncio
import re
from typing import Callable

import discord

from utils import custom_commands


def parse_cooldown_seconds(value: str) -> int:
    """Parse a ``30s`` / ``5m`` / ``1h`` cooldown into clamped seconds."""
    match = re.fullmatch(r"\s*(\d{1,4})\s*([smhSMH]?)\s*", str(value or ""))
    if not match:
        return 0
    amount = int(match.group(1))
    unit = match.group(2).lower() or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return min(amount * multiplier, 3600)


class CustomCommandRuntime:
    """Executes custom commands for unknown dot-command triggers."""

    def __init__(
        self,
        *,
        is_admin: Callable[[object], bool],
        commands_module=custom_commands,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._is_admin = is_admin
        self._commands = commands_module
        self._clock = clock
        # (guild_id, trigger, user_id) -> monotonic expiry time.
        self.cooldowns: dict[tuple[str, str, int], float] = {}

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_running_loop().time()

    async def handle(self, message: discord.Message, trigger: str) -> bool:
        """Execute a matching custom command; return True if the trigger was ours."""
        if not trigger:
            return False

        guild = getattr(message, "guild", None)
        guild_id = str(getattr(guild, "id", "") or self._commands.DEFAULT_GUILD_ID)
        clean_trigger = f".{trigger.lower()}"
        command = self._find(guild_id, clean_trigger)
        if not command:
            return False
        if not command.get("enabled", True):
            return True

        channel_gate = str(command.get("channel") or "").strip()
        if channel_gate and not self._channel_matches(message, channel_gate):
            await message.channel.send(
                f"⚠️ `{clean_trigger}` can only be used in {channel_gate}."
            )
            return True

        if not self._role_allowed(message, str(command.get("role_gate") or "Everyone")):
            await message.channel.send(
                "⚠️ You do not have permission to use this custom command."
            )
            return True

        retry_after = self._retry_after(
            message, guild_id, clean_trigger, str(command.get("cooldown") or "0s")
        )
        if retry_after > 0:
            await message.channel.send(
                f"⏳ `{clean_trigger}` is on cooldown for {retry_after}s."
            )
            return True

        response = str(command.get("response") or "").strip()
        if not response:
            return True

        kwargs = {}
        if hasattr(discord, "AllowedMentions"):
            kwargs["allowed_mentions"] = discord.AllowedMentions.none()
        await message.channel.send(response, **kwargs)
        return True

    def _find(self, guild_id: str, trigger: str) -> dict | None:
        guild_commands = self._commands.load_commands(guild_id)
        fallback_commands = (
            self._commands.load_commands(self._commands.DEFAULT_GUILD_ID)
            if guild_id != self._commands.DEFAULT_GUILD_ID
            else []
        )
        for command in guild_commands + fallback_commands:
            if str(command.get("trigger") or "").lower() == trigger:
                return command
        return None

    @staticmethod
    def _channel_matches(message: discord.Message, channel_gate: str) -> bool:
        expected = channel_gate.strip().lower().lstrip("#")
        channel = getattr(message, "channel", None)
        channel_name = str(getattr(channel, "name", "") or "").lower()
        channel_id = str(getattr(channel, "id", "") or "")
        return expected in {channel_name, channel_id}

    def _role_allowed(self, message: discord.Message, role_gate: str) -> bool:
        if role_gate == "Everyone":
            return True
        if role_gate == "Admins":
            return self._is_admin(message)
        if role_gate == "Captains":
            if self._is_admin(message):
                return True
            roles = getattr(message.author, "roles", []) or []
            return any(str(getattr(role, "name", "")) == "Captains" for role in roles)
        return False

    def _retry_after(
        self, message: discord.Message, guild_id: str, trigger: str, cooldown: str
    ) -> int:
        seconds = parse_cooldown_seconds(cooldown)
        if seconds <= 0:
            return 0
        now = self._now()
        key = (guild_id, trigger, int(getattr(message.author, "id", 0) or 0))
        expires_at = self.cooldowns.get(key, 0)
        if expires_at > now:
            return max(1, int(expires_at - now))
        self.cooldowns[key] = now + seconds
        return 0
