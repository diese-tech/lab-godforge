"""r67 lifecycle adapter for the shared feature registry.

This is the seam between the r67 feature and the shared lifecycle infrastructure
(``utils/lifecycle.py``). It owns *when* r67's durable recovery and cleanup run
relative to the shared orchestration, and delegates the actual work to
``R67Service`` — keeping ``bot.py`` free of r67 internals (Issue #48).
"""

from __future__ import annotations

import logging

from utils.lifecycle import LifecycleContext
from utils.r67.service import R67Service

log = logging.getLogger("godforge.r67")


class R67Feature:
    """Registers r67 Survivor role recovery and cleanup with the registry."""

    name = "r67"

    def __init__(self, service: R67Service) -> None:
        self.service = service

    async def on_startup(self, ctx: LifecycleContext) -> None:
        removed = await self.service.recover_role_grants(ctx.get_guild)
        if removed:
            log.info("Removed %s expired 67 Survivor role grant(s) on startup", removed)

    async def on_cleanup(self, ctx: LifecycleContext) -> None:
        removed = await self.service.cleanup_expired_role_grants(ctx.get_guild)
        if removed:
            log.info("Removed %s expired 67 Survivor role grant(s)", removed)
