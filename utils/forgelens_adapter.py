"""Optional, failure-isolated compatibility adapter for ForgeLens.

Core GodForge records deliberately remain unaware of ForgeLens.  This module is
the single translation boundary into the portable draft export contract.
"""

from __future__ import annotations

import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping


PORTABLE_DRAFT_SCHEMA_VERSION = 2

Delivery = Callable[[dict[str, Any]], None | Awaitable[None]]


def forgelens_enabled() -> bool:
    """Return the explicit opt-in state for the compatibility integration."""
    return os.getenv("GODFORGE_ENABLE_FORGELENS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of an optional delivery attempt."""

    attempted: bool
    delivered: bool
    error: str | None = None


def _copy_person(person: Mapping[str, Any] | None) -> dict[str, Any]:
    person = person or {}
    return {
        "user_id": person.get("user_id"),
        "name": person.get("name", ""),
    }


def _copy_game(game: Mapping[str, Any]) -> dict[str, Any]:
    bans = game.get("bans") or {}
    picks = game.get("picks") or {}
    claims = game.get("claims") or {}
    return {
        "game_number": game.get("game_number", 1),
        "bans": {
            "blue": list(bans.get("blue") or []),
            "red": list(bans.get("red") or []),
        },
        "picks": {
            "blue": list(picks.get("blue") or []),
            "red": list(picks.get("red") or []),
        },
        "claims": {
            side: {
                god: _copy_person(person)
                for god, person in (claims.get(side) or {}).items()
            }
            for side in ("blue", "red")
        },
    }


def map_draft_record(
    record: Mapping[str, Any],
    *,
    forgelens_match_id: str = "",
) -> dict[str, Any]:
    """Translate a generic GodForge draft record to the portable v2 contract."""

    draft_id = record.get("draft_id") or record.get("match_id")
    games = [_copy_game(game) for game in (record.get("games") or [])]
    teams = record.get("teams") or {}
    blue_captain = _copy_person(
        (teams.get("blue") or {}).get("captain") or record.get("blue_captain")
    )
    red_captain = _copy_person(
        (teams.get("red") or {}).get("captain") or record.get("red_captain")
    )

    picks: list[dict[str, Any]] = []
    bans: list[dict[str, Any]] = []
    selected_gods: list[dict[str, Any]] = []
    for game in games:
        for side in ("blue", "red"):
            picks.append(
                {
                    "game_number": game["game_number"],
                    "team": side,
                    "gods": list(game["picks"][side]),
                }
            )
            bans.append(
                {
                    "game_number": game["game_number"],
                    "team": side,
                    "gods": list(game["bans"][side]),
                }
            )
            selected_gods.extend(
                {
                    "game_number": game["game_number"],
                    "team": side,
                    "god": god,
                    "claimed_by": dict(person),
                }
                for god, person in game["claims"][side].items()
            )

    started_at = record.get("started_at")
    ended_at = record.get("ended_at")
    current_game_number = (
        record.get("game_number")
        or (games[-1]["game_number"] if games else 1)
    )
    return {
        "schema_version": PORTABLE_DRAFT_SCHEMA_VERSION,
        "producer": "GodForge",
        "event_type": "draft_export",
        "status": record.get("status", "draft_complete"),
        "draft_id": draft_id,
        "forgelens_match_id": forgelens_match_id,
        "match_id": draft_id,
        "guild_id": record.get("guild_id"),
        "guild_name": record.get("guild_name", ""),
        "channel_id": record.get("channel_id"),
        "channel_name": record.get("channel_name", ""),
        "game_number": current_game_number,
        "draft_sequence": record.get("draft_sequence", 1),
        "teams": {
            "blue": {"label": "blue", "captain": blue_captain},
            "red": {"label": "red", "captain": red_captain},
        },
        "blue_captain": blue_captain,
        "red_captain": red_captain,
        "started_at": started_at,
        "ended_at": ended_at,
        "timestamps": {"started_at": started_at, "ended_at": ended_at},
        "draft_order": [dict(step) for step in (record.get("draft_order") or [])],
        "picks": picks,
        "bans": bans,
        "selected_gods": selected_gods,
        "games": games,
        "fearless_pool": sorted(record.get("fearless_pool") or []),
    }


def map_match_record(
    record: Mapping[str, Any],
    *,
    forgelens_match_id: str = "",
) -> dict[str, Any]:
    """Map a generic completed match containing a draft into the same contract."""

    draft = record.get("draft") or record
    merged = dict(draft)
    for field in (
        "match_id",
        "guild_id",
        "guild_name",
        "channel_id",
        "channel_name",
        "started_at",
        "ended_at",
    ):
        if merged.get(field) is None and record.get(field) is not None:
            merged[field] = record[field]
    if not merged.get("draft_id"):
        merged["draft_id"] = record.get("draft_id") or record.get("match_id")
    merged.setdefault("status", "draft_complete")
    return map_draft_record(merged, forgelens_match_id=forgelens_match_id)


class ForgeLensAdapter:
    """Disabled-by-default adapter whose failures never escape delivery."""

    def __init__(
        self,
        delivery: Delivery | None = None,
        *,
        enabled: bool | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.enabled = forgelens_enabled() if enabled is None else enabled
        self._delivery = delivery
        self._logger = logger or logging.getLogger(__name__)

    async def deliver_draft(
        self,
        record: Mapping[str, Any],
        *,
        forgelens_match_id: str = "",
    ) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(attempted=False, delivered=False)
        payload = map_draft_record(
            record,
            forgelens_match_id=forgelens_match_id,
        )
        return await self._deliver(payload)

    async def _deliver(self, payload: dict[str, Any]) -> DeliveryResult:
        if self._delivery is None:
            error = "ForgeLens adapter is enabled without a delivery target"
            self._logger.error(error)
            return DeliveryResult(attempted=True, delivered=False, error=error)

        try:
            outcome = self._delivery(payload)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # The compatibility service cannot block core state.
            self._logger.exception("ForgeLens delivery failed")
            return DeliveryResult(
                attempted=True,
                delivered=False,
                error=str(exc),
            )
        return DeliveryResult(attempted=True, delivered=True)

    async def deliver_match(
        self,
        record: Mapping[str, Any],
        *,
        forgelens_match_id: str = "",
    ) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(attempted=False, delivered=False)
        return await self._deliver(
            map_match_record(
                record,
                forgelens_match_id=forgelens_match_id,
            )
        )
