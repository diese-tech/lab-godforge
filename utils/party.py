"""Domain model for durable GodForge party lobbies.

Discord IDs are delivery references only.  ``lobby_id`` is the stable domain
identity and survives message, channel, and room recreation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class LobbyState(StrEnum):
    OPEN = "open"
    FULL = "full"
    READY_CHECK = "ready_check"
    FORMING = "forming"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset(
    {LobbyState.COMPLETED, LobbyState.CANCELLED, LobbyState.EXPIRED}
)
ACTIVE_STATES = frozenset(LobbyState) - TERMINAL_STATES

# Backward edges are deliberate: a player leaving or declining a ready check
# re-opens recruitment without replacing the lobby's domain identity.
ALLOWED_TRANSITIONS = MappingProxyType(
    {
        LobbyState.OPEN: frozenset(
            {LobbyState.FULL, LobbyState.CANCELLED, LobbyState.EXPIRED}
        ),
        LobbyState.FULL: frozenset(
            {
                LobbyState.OPEN,
                LobbyState.READY_CHECK,
                LobbyState.CANCELLED,
                LobbyState.EXPIRED,
            }
        ),
        LobbyState.READY_CHECK: frozenset(
            {
                LobbyState.OPEN,
                LobbyState.FULL,
                LobbyState.FORMING,
                LobbyState.CANCELLED,
                LobbyState.EXPIRED,
            }
        ),
        LobbyState.FORMING: frozenset(
            {LobbyState.READY_CHECK, LobbyState.ACTIVE, LobbyState.CANCELLED}
        ),
        LobbyState.ACTIVE: frozenset(
            {LobbyState.COMPLETED, LobbyState.CANCELLED}
        ),
        LobbyState.COMPLETED: frozenset(),
        LobbyState.CANCELLED: frozenset(),
        LobbyState.EXPIRED: frozenset(),
    }
)


class InvalidLobbyTransition(ValueError):
    """Raised when a requested lifecycle transition is not permitted."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def validate_transition(current: LobbyState, target: LobbyState) -> None:
    """Validate a transition; transitioning to the current state is idempotent."""
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidLobbyTransition(f"cannot transition lobby from {current} to {target}")


@dataclass(frozen=True, slots=True)
class DiscordDelivery:
    panel_channel_id: int | None = None
    panel_message_id: int | None = None
    voice_channel_id: int | None = None
    team_channel_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Participant:
    user_id: int
    preferences: tuple[str, ...] = ()
    ready: bool = False
    joined_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "joined_at", ensure_utc(self.joined_at))
        object.__setattr__(
            self,
            "preferences",
            tuple(dict.fromkeys(p.strip().lower() for p in self.preferences if p.strip())),
        )


@dataclass(frozen=True, slots=True)
class PartyLobby:
    lobby_id: str
    guild_id: int
    organizer_id: int
    capacity: int
    state: LobbyState = LobbyState.OPEN
    participants: tuple[Participant, ...] = ()
    delivery: DiscordDelivery = field(default_factory=DiscordDelivery)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not self.lobby_id.strip():
            raise ValueError("lobby_id is required")
        if self.capacity < 2:
            raise ValueError("capacity must be at least 2")
        if len({p.user_id for p in self.participants}) != len(self.participants):
            raise ValueError("participant user IDs must be unique")
        if len(self.participants) > self.capacity:
            raise ValueError("participants exceed lobby capacity")
        object.__setattr__(self, "state", LobbyState(self.state))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))
        object.__setattr__(self, "expires_at", ensure_utc(self.expires_at))

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def participant(self, user_id: int) -> Participant | None:
        return next((p for p in self.participants if p.user_id == user_id), None)

    def transitioned(self, target: LobbyState, *, at: datetime | None = None) -> "PartyLobby":
        validate_transition(self.state, target)
        if self.state == target:
            return self
        return replace(
            self,
            state=target,
            updated_at=ensure_utc(at) or utc_now(),
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: int
    lobby_id: str
    guild_id: int
    operation_id: str
    event_type: str
    from_state: LobbyState | None
    to_state: LobbyState
    actor_id: int | None
    occurred_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    lobby: PartyLobby
    """An active lobby and all data needed for Discord-side reconciliation."""

