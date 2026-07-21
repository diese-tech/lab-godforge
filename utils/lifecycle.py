"""Shared feature lifecycle infrastructure.

This is the shared infrastructure half of the Issue #48 architecture: features
own their startup-recovery and background-cleanup behavior, but register it
through a common interface so ``bot.py`` orchestrates lifecycle without knowing
any feature's internals.

``bot.py`` builds a single :class:`FeatureRegistry`, registers each feature's
:class:`FeatureModule`, and calls :meth:`FeatureRegistry.run_startup` from
``on_ready`` and :meth:`FeatureRegistry.run_cleanup` from the periodic cleanup
task. A :class:`LifecycleContext` carries the shared read-only Discord lookups
a feature needs (guilds, channels, users) so features never reach into bot
globals or hold a reference to the live client.

One feature's failure is isolated and logged; it never blocks the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, runtime_checkable

log = logging.getLogger("godforge.lifecycle")


@dataclass(frozen=True, slots=True)
class LifecycleContext:
    """Shared services handed to features during lifecycle phases.

    ``get_guild``/``get_channel``/``get_user`` are cache-only lookups (may
    return ``None``); ``fetch_user`` is the API fallback for a user outside the
    cache. New fields default to a no-op so existing call sites and tests that
    construct a context with only ``get_guild`` keep working.
    """

    get_guild: Callable[[int], object | None]
    get_channel: Callable[[int], object | None] = lambda channel_id: None
    get_user: Callable[[int], object | None] = lambda user_id: None
    fetch_user: Callable[[int], Awaitable[object]] | None = None
    guilds: tuple = ()


@runtime_checkable
class FeatureModule(Protocol):
    """A feature's lifecycle surface.

    Implementations own the actual recovery/cleanup work (typically by delegating
    to their service); the registry only sequences and isolates them. Both hooks
    are optional — a feature with no durable lifecycle can omit either.
    """

    name: str

    async def on_startup(self, ctx: LifecycleContext) -> None: ...

    async def on_cleanup(self, ctx: LifecycleContext) -> None: ...


class FeatureRegistry:
    """Holds registered features and runs their lifecycle hooks in order."""

    def __init__(self) -> None:
        self._features: list[FeatureModule] = []

    def register(self, feature: FeatureModule) -> None:
        self._features.append(feature)

    @property
    def features(self) -> tuple[FeatureModule, ...]:
        return tuple(self._features)

    async def run_startup(self, ctx: LifecycleContext) -> None:
        for feature in self._features:
            hook = getattr(feature, "on_startup", None)
            if hook is None:
                continue
            try:
                await hook(ctx)
            except Exception:
                log.exception("Feature %s startup recovery failed", _name(feature))

    async def run_cleanup(self, ctx: LifecycleContext) -> None:
        for feature in self._features:
            hook = getattr(feature, "on_cleanup", None)
            if hook is None:
                continue
            try:
                await hook(ctx)
            except Exception:
                log.exception("Feature %s cleanup failed", _name(feature))


def _name(feature: object) -> str:
    return getattr(feature, "name", feature.__class__.__name__)
