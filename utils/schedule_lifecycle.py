"""Scheduled-night reminder delivery as a shared lifecycle hook.

Feature module for the Issue #48 refactor. Wraps the periodic reminder-DM loop
that previously lived inline in ``bot.py``'s cleanup task, registering it
through the shared ``FeatureRegistry``. Domain logic (which reminders are due)
stays in ``utils/party_schedule.py``; this module only delivers the DMs.
"""

from __future__ import annotations

import logging

import discord

from utils.lifecycle import LifecycleContext

log = logging.getLogger("godforge.schedule_lifecycle")


class ScheduleLifecycle:
    """Delivers due scheduled-night reminder DMs on the periodic cleanup pass."""

    name = "schedule"

    def __init__(self, schedule_repository):
        self._schedule_repository = schedule_repository

    async def on_cleanup(self, ctx: LifecycleContext) -> None:
        for event, minutes, occurrence in self._schedule_repository.claim_due_reminders():
            recipients = {event.organizer_id, *(rsvp.user_id for rsvp in event.rsvps)}
            recipients.update(rsvp.user_id for rsvp in event.waitlist)
            for user_id in recipients:
                try:
                    user = ctx.get_user(user_id) or await ctx.fetch_user(user_id)
                    await user.send(
                        f"**{event.title}** starts <t:{int(occurrence.timestamp())}:R> "
                        f"({minutes}-minute reminder)."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    log.info(
                        "Could not DM scheduled-night reminder to user %s", user_id
                    )
