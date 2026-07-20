"""Persistent Discord UI primitives for reusable GodForge party lobbies."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

import discord


CREATE_MODAL_CUSTOM_ID = "godforge:lobby:create:v1"
JOIN_MODAL_CUSTOM_ID = "godforge:lobby:join-preferences:v1"
LOBBY_CARD_CUSTOM_ID_PREFIX = "godforge:lobby:card"
READY_CHECK_CUSTOM_ID_PREFIX = "godforge:lobby:ready-check"

LOBBY_CARD_ACTIONS = (
    ("join", "Join", discord.ButtonStyle.success),
    ("leave", "Leave", discord.ButtonStyle.secondary),
    ("edit", "Edit", discord.ButtonStyle.primary),
    ("cancel", "Cancel", discord.ButtonStyle.danger),
    ("share", "Share", discord.ButtonStyle.secondary),
    ("ready_check", "Ready Check", discord.ButtonStyle.primary),
)

READY_CHECK_ACTIONS = (
    ("ready", "Ready", discord.ButtonStyle.success),
    ("need_five", "Need 5 Minutes", discord.ButtonStyle.secondary),
    ("drop", "Drop", discord.ButtonStyle.danger),
)

ModalHandler = Callable[[discord.Interaction, dict[str, object]], Awaitable[None]]
LobbyActionHandler = Callable[[discord.Interaction, str], Awaitable[None]]

_LOGGER = logging.getLogger(__name__)
_SAFE_ERROR = "GodForge could not complete that action. Please try again."
_VALID_ROLES = {"solo", "jungle", "mid", "support", "adc", "fill"}


async def _send_safe_error(
    interaction: discord.Interaction, message: str = _SAFE_ERROR
) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)
    except (discord.HTTPException, AttributeError):
        _LOGGER.exception("Failed to send lobby interaction error")


class _ValidationError(ValueError):
    pass


def _required(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise _ValidationError(f"{label} is required.")
    return cleaned


def _yes_no(value: str, label: str) -> bool:
    cleaned = value.strip().lower()
    if cleaned in {"yes", "y", "true", "required", "1"}:
        return True
    if cleaned in {"no", "n", "false", "optional", "0"}:
        return False
    raise _ValidationError(f"{label} must be yes or no.")


class CreateLobbyModal(discord.ui.Modal):
    """Collect lobby configuration within Discord's five-component limit.

    Party size and voice preference share one compact input; optional skill band
    and notes share another.  The handler still receives seven explicit fields.
    """

    def __init__(self, handler: ModalHandler) -> None:
        super().__init__(
            title="Create a GodForge Lobby",
            custom_id=CREATE_MODAL_CUSTOM_ID,
            timeout=None,
        )
        self._handler = handler
        self.mode = discord.ui.TextInput(
            label="Mode",
            custom_id="mode",
            placeholder="Conquest, Arena, Joust...",
            max_length=40,
        )
        self.region = discord.ui.TextInput(
            label="Region",
            custom_id="region",
            placeholder="NA East, EU, Brazil...",
            max_length=40,
        )
        self.format = discord.ui.TextInput(
            label="Format",
            custom_id="format",
            placeholder="PUG, scrim, custom night...",
            max_length=40,
        )
        self.party_requirements = discord.ui.TextInput(
            label="Party size / voice",
            custom_id="party_requirements",
            placeholder="10 / yes",
            max_length=24,
        )
        self.optional_details = discord.ui.TextInput(
            label="Optional skill band / notes",
            custom_id="optional_details",
            placeholder="Skill: mixed | Notes: chill games",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        for item in (
            self.mode,
            self.region,
            self.format,
            self.party_requirements,
            self.optional_details,
        ):
            self.add_item(item)

    def payload(self) -> dict[str, object]:
        match = re.fullmatch(
            r"\s*(\d{1,2})\s*(?:[/,|;-])\s*(.+?)\s*",
            str(self.party_requirements),
        )
        if not match:
            raise _ValidationError("Party size / voice must look like `10 / yes`.")
        party_size = int(match.group(1))
        if not 2 <= party_size <= 20:
            raise _ValidationError("Party size must be between 2 and 20.")

        details = str(self.optional_details).strip()
        skill_band = ""
        notes = ""
        if details:
            labeled = re.fullmatch(
                r"\s*skill\s*:\s*(.*?)\s*(?:[|;\n]\s*notes\s*:\s*(.*))?\s*",
                details,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if labeled:
                skill_band = labeled.group(1).strip()
                notes = (labeled.group(2) or "").strip()
            else:
                notes = details

        return {
            "mode": _required(str(self.mode), "Mode"),
            "region": _required(str(self.region), "Region"),
            "format": _required(str(self.format), "Format"),
            "party_size": party_size,
            "voice_required": _yes_no(match.group(2), "Voice requirement"),
            "skill_band": skill_band or None,
            "notes": notes or None,
        }

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self._handler(interaction, self.payload())
        except _ValidationError as exc:
            await _send_safe_error(interaction, str(exc))
        except Exception:
            _LOGGER.exception("Create lobby modal handler failed")
            await _send_safe_error(interaction)


class JoinPreferencesModal(discord.ui.Modal):
    """Collect a player's lobby-specific role and captain preferences."""

    def __init__(self, handler: ModalHandler) -> None:
        super().__init__(
            title="Join Lobby",
            custom_id=JOIN_MODAL_CUSTOM_ID,
            timeout=None,
        )
        self._handler = handler
        self.primary_role = discord.ui.TextInput(
            label="Primary role",
            custom_id="primary_role",
            placeholder="Solo, Jungle, Mid, Support, ADC",
            max_length=16,
        )
        self.secondary_role = discord.ui.TextInput(
            label="Secondary role",
            custom_id="secondary_role",
            placeholder="Optional",
            required=False,
            max_length=16,
        )
        self.fill = discord.ui.TextInput(
            label="Willing to fill?",
            custom_id="fill",
            placeholder="yes / no",
            max_length=5,
        )
        self.captain = discord.ui.TextInput(
            label="Willing to captain?",
            custom_id="captain",
            placeholder="yes / no",
            max_length=5,
        )
        for item in (self.primary_role, self.secondary_role, self.fill, self.captain):
            self.add_item(item)

    def payload(self) -> dict[str, object]:
        primary = _required(str(self.primary_role), "Primary role").lower()
        secondary = str(self.secondary_role).strip().lower()
        if primary not in _VALID_ROLES:
            raise _ValidationError("Primary role is not recognized.")
        if secondary and secondary not in _VALID_ROLES:
            raise _ValidationError("Secondary role is not recognized.")
        return {
            "primary_role": primary,
            "secondary_role": secondary or None,
            "fill": _yes_no(str(self.fill), "Fill"),
            "captain": _yes_no(str(self.captain), "Captain"),
        }

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self._handler(interaction, self.payload())
        except _ValidationError as exc:
            await _send_safe_error(interaction, str(exc))
        except Exception:
            _LOGGER.exception("Join preferences modal handler failed")
            await _send_safe_error(interaction)


class _LobbyActionButton(discord.ui.Button):
    def __init__(
        self,
        action: str,
        label: str,
        style: discord.ButtonStyle,
        handler: LobbyActionHandler,
        *,
        custom_id_prefix: str = LOBBY_CARD_CUSTOM_ID_PREFIX,
        row: int | None = None,
    ) -> None:
        super().__init__(
            label=label,
            style=style,
            custom_id=f"{custom_id_prefix}:{action}:v1",
            row=row,
        )
        self.action = action
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._handler(interaction, self.action)
        except Exception:
            _LOGGER.exception("Lobby card action failed")
            await _send_safe_error(interaction)


class LobbyCardView(discord.ui.View):
    """Reusable persistent action row for a lobby card message."""

    def __init__(self, handler: LobbyActionHandler) -> None:
        super().__init__(timeout=None)
        for index, (action, label, style) in enumerate(LOBBY_CARD_ACTIONS):
            self.add_item(
                _LobbyActionButton(
                    action,
                    label,
                    style,
                    handler,
                    row=0 if index < 5 else 1,
                )
            )


class ReadyCheckView(discord.ui.View):
    """Persistent participant responses for an active lobby ready check."""

    def __init__(self, handler: LobbyActionHandler) -> None:
        super().__init__(timeout=None)
        for action, label, style in READY_CHECK_ACTIONS:
            self.add_item(
                _LobbyActionButton(
                    action,
                    label,
                    style,
                    handler,
                    custom_id_prefix=READY_CHECK_CUSTOM_ID_PREFIX,
                    row=0,
                )
            )
