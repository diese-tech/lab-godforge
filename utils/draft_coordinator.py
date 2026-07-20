"""Draft feature coordinator: local + activity-backend draft flows.

Large feature module for the Issue #48 refactor. It owns the draft command
handlers (`.draft` and `.ban`/`.pick`), the in-memory activity-backend draft
state, the activity WebSocket listener, export posting, and the claim-reaction
handler — all of which previously lived directly in ``bot.py``.

Collaborators are injected so the coordinator can be reasoned about and partially
tested without ``bot.py`` globals: the local ``DraftManager``, the activity HTTP
client, the draft renderer, the active-draft restart store, the shared
reaction-tracking map, the formatter, the god-name resolver, the reports-channel
map, and a ``channel_has_active`` predicate (which also considers sessions).

The activity-draft state maps are exposed as attributes because the party-draft
launch path also registers activity drafts through this coordinator.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

import aiohttp
import discord

from utils import draft_support
from utils.forgelens_adapter import forgelens_enabled


class DraftCoordinator:
    def __init__(
        self,
        *,
        client,
        drafts,
        activity_client,
        renderer,
        active_draft_store,
        tracked_messages: dict,
        formatter,
        resolve_god_name,
        reports_channels: dict,
        number_emojis,
        channel_has_active,
        log: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._drafts = drafts
        self._activity = activity_client
        self._renderer = renderer
        self._active_store = active_draft_store
        self._tracked = tracked_messages
        self._formatter = formatter
        self._resolve_god_name = resolve_god_name
        self._reports_channels = reports_channels
        self._number_emojis = number_emojis
        self._channel_has_active = channel_has_active
        self._log = log or logging.getLogger("godforge.draft")

        # In-memory activity-backend draft state.
        self.match_ids: dict[int, str] = {}
        self.match_channels: dict[str, int] = {}
        self.snapshots: dict[int, dict] = {}
        self.board_message_ids: dict[int, int] = {}
        self.ws_tasks: dict[int, asyncio.Task] = {}

    # -- Shared state helpers --------------------------------------------

    def has_active_draft(self, channel_id: int) -> bool:
        return channel_id in self.match_ids or bool(self._drafts.get(channel_id))

    def cleanup_draft(self, channel_id: int) -> None:
        match_id = self.match_ids.pop(channel_id, None)
        if match_id:
            self.match_channels.pop(match_id, None)
        self.snapshots.pop(channel_id, None)
        self.board_message_ids.pop(channel_id, None)
        task = self.ws_tasks.pop(channel_id, None)
        if task:
            task.cancel()

    def register_activity_draft(
        self, channel_id: int, match_id: str, snapshot: dict
    ) -> None:
        """Record a started activity draft (used by both start paths)."""
        self.match_ids[channel_id] = match_id
        self.match_channels[match_id] = channel_id
        self.snapshots[channel_id] = snapshot

    def start_ws(self, match_id: str, channel_id: int) -> asyncio.Task:
        task = asyncio.create_task(self.listen_ws(match_id, channel_id))
        self.ws_tasks[channel_id] = task
        return task

    # -- Activity backend rendering --------------------------------------

    async def update_embed_from_snapshot(self, snapshot: dict, channel) -> None:
        channel_id = channel.id
        embed = self._formatter.format_board_from_snapshot(snapshot)
        msg_id = self.board_message_ids.get(channel_id)
        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        sent = await channel.send(embed=embed)
        self.board_message_ids[channel_id] = sent.id

    async def post_export(self, export: dict, channel) -> None:
        if isinstance(export.get("export"), dict):
            export = export["export"]
        guild = getattr(channel, "guild", None)
        export.setdefault("guild_id", guild.id if guild else None)
        export.setdefault("channel_id", channel.id)
        export.setdefault(
            "match_id",
            export.get("matchId") or export.get("draftId") or export.get("draft_id"),
        )
        export.setdefault("draft_id", export.get("draftId") or export.get("match_id"))
        export.setdefault("forgelens_match_id", export.get("forgelensMatchId") or "")
        export.setdefault("game_number", export.get("gameNumber") or 1)
        export.setdefault("draft_sequence", export.get("draftSequence") or 1)
        export.setdefault("status", "draft_complete")
        export.setdefault("producer", "GodForge")
        draft_id = export.get("draftId") or export.get("draft_id", "unknown")
        embed = self._formatter.format_draft_end_from_export(export)
        await channel.send(embed=embed)
        completion_lines = [
            "Draft complete",
            f"draft_id={export.get('draft_id', draft_id)}",
            f"game_number={export.get('game_number', 1)}",
        ]
        if forgelens_enabled():
            completion_lines.insert(
                2, f"forgelens_match_id={export.get('forgelens_match_id', '')}"
            )
        await channel.send("\n".join(completion_lines))

        filename = f"draft_{draft_id}.json"
        json_bytes = json.dumps(export, indent=2).encode("utf-8")
        file = discord.File(io.BytesIO(json_bytes), filename=filename)
        await channel.send(f"📎 Draft record: `{filename}`", file=file)

        guild_id = channel.guild.id if channel.guild else None
        if guild_id and guild_id in self._reports_channels:
            reports_ch = self._client.get_channel(self._reports_channels[guild_id])
            if reports_ch:
                try:
                    await reports_ch.send(embed=embed)
                    report_file = discord.File(io.BytesIO(json_bytes), filename=filename)
                    await reports_ch.send(
                        f"📎 Draft record: `{filename}`", file=report_file
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    self._log.warning("Failed to post to reports channel: %s", exc)

    async def listen_ws(self, match_id: str, channel_id: int) -> None:
        """Connect to the activity backend WebSocket and mirror state to the embed."""
        ws_url = self._activity.ws_url()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    await ws.send_json({"type": "join", "matchId": match_id})
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data["type"] == "state":
                                self.snapshots[channel_id] = data["state"]
                                channel = self._client.get_channel(channel_id)
                                if channel:
                                    await self.update_embed_from_snapshot(
                                        data["state"], channel
                                    )
                            elif data["type"] == "export":
                                if channel_id in self.match_ids:
                                    channel = self._client.get_channel(channel_id)
                                    if channel:
                                        await self.post_export(data["export"], channel)
                                    self.cleanup_draft(channel_id)
                                break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error("WS listener error for %s: %s", match_id, exc)
        finally:
            self.ws_tasks.pop(channel_id, None)

    # -- Command dispatch ------------------------------------------------

    async def handle_draft(self, intent: dict, message: discord.Message):
        if self._activity.enabled:
            return await self._handle_draft_activity(intent, message)
        async with self._drafts.get_lock(message.channel.id):
            return await self._handle_draft_local(intent, message)

    async def handle_draft_action(self, intent: dict, message: discord.Message):
        if self._activity.enabled:
            return await self._handle_draft_action_activity(intent, message)
        async with self._drafts.get_lock(message.channel.id):
            return await self._handle_draft_action_local(intent, message)

    # -- Activity backend command handlers -------------------------------

    async def _handle_draft_activity(self, intent: dict, message: discord.Message):
        action = intent["action"]
        channel_id = message.channel.id

        if action == "start":
            active = self._channel_has_active(channel_id)
            if active == "session":
                return self._formatter.format_error(
                    "A session is active. Use `.session end` first."
                )
            if active == "draft":
                return self._formatter.format_error(
                    "A draft is already active. Use `.draft end` first."
                )

            mentions = message.mentions
            options = draft_support.draft_start_options(message.content)
            if len(mentions) < 2:
                return self._formatter.format_error(
                    "Usage: `.draft start @blue_captain @red_captain [--match FL-123] [--game 2]`"
                )
            blue_user, red_user = mentions[0], mentions[1]
            if blue_user.id == red_user.id:
                return self._formatter.format_error(
                    "Blue and red captains must be different users."
                )
            if options["game_number"] < 1:
                return self._formatter.format_error("`--game` must be 1 or greater.")

            result = await self._activity.post(
                "/api/draft/start",
                {
                    "blueCaptainId": str(blue_user.id),
                    "blueCaptainName": blue_user.display_name,
                    "redCaptainId": str(red_user.id),
                    "redCaptainName": red_user.display_name,
                    "forgelensMatchId": options["forgelens_match_id"],
                    "gameNumber": options["game_number"],
                },
            )
            if not result or "error" in result:
                err = result.get("error") if result else "Activity backend unreachable."
                return self._formatter.format_error(err)

            match_id = result["matchId"]
            snapshot = result["state"]
            self.register_activity_draft(channel_id, match_id, snapshot)

            embed = self._formatter.format_board_from_snapshot(snapshot)
            sent = await message.channel.send(
                f"🎮 Draft `{match_id}` started — open the Activity and enter this ID to join",
                embed=embed,
            )
            self.board_message_ids[channel_id] = sent.id

            self.start_ws(match_id, channel_id)
            self._log.info(
                "Draft %s started: 🔵 %s vs 🔴 %s",
                match_id,
                blue_user.display_name,
                red_user.display_name,
            )
            return None

        if action == "show":
            match_id = self.match_ids.get(channel_id)
            if not match_id:
                return self._formatter.format_error("No active draft in this channel.")
            snapshot = await self._activity.get(f"/api/draft/{match_id}")
            if not snapshot or "error" in snapshot:
                return self._formatter.format_error("Could not retrieve draft state.")
            return self._formatter.format_board_from_snapshot(snapshot)

        if action == "undo":
            match_id = self.match_ids.get(channel_id)
            if not match_id:
                return self._formatter.format_error("No active draft in this channel.")
            result = await self._activity.post(f"/api/draft/{match_id}/undo")
            if not result or "error" in result:
                return self._formatter.format_error(
                    result.get("error", "Nothing to undo.")
                    if result
                    else "Backend unreachable."
                )
            return None  # WS listener updates the embed

        if action == "next":
            match_id = self.match_ids.get(channel_id)
            if not match_id:
                return self._formatter.format_error("No active draft in this channel.")
            result = await self._activity.post(f"/api/draft/{match_id}/next")
            if not result or "error" in result:
                return self._formatter.format_error(
                    result.get("error", "Cannot advance game.")
                    if result
                    else "Backend unreachable."
                )
            return None  # WS listener updates the embed

        if action == "end":
            match_id = self.match_ids.get(channel_id)
            if not match_id:
                return self._formatter.format_error("No active draft in this channel.")
            result = await self._activity.post(f"/api/draft/{match_id}/end")
            if not result or "error" in result:
                return self._formatter.format_error(
                    result.get("error", "Failed to end draft.")
                    if result
                    else "Backend unreachable."
                )
            self.cleanup_draft(channel_id)
            await self.post_export(result, message.channel)
            self._log.info("Draft %s ended via text command", match_id)
            return None
        return None

    async def _handle_draft_action_activity(self, intent: dict, message: discord.Message):
        channel_id = message.channel.id
        match_id = self.match_ids.get(channel_id)

        if not match_id:
            return self._formatter.format_error("No active draft. Use `.draft start` first.")

        snapshot = self.snapshots.get(channel_id)
        if not snapshot:
            return self._formatter.format_error(
                "Draft state loading — try again in a moment."
            )

        if snapshot.get("isClaiming"):
            return self._formatter.format_error(
                "Claiming phase active. Use `.draft undo` to go back."
            )

        turn = snapshot.get("currentTurn")
        if not turn:
            return self._formatter.format_error(
                "Game complete. Use `.draft next` or `.draft end`."
            )

        action = intent["action"]
        if action != turn["action"]:
            return self._formatter.format_error(
                f"It's time to **{turn['action']}**, not {action}."
            )

        expected_captain_id = snapshot.get("currentCaptainId")
        if expected_captain_id and str(message.author.id) != expected_captain_id:
            team = turn["team"]
            captain_name = (
                snapshot["blueCaptain"]["name"]
                if team == "blue"
                else snapshot["redCaptain"]["name"]
            )
            return self._formatter.format_error(f"It's **{captain_name}**'s turn ({team}).")

        god, error = self._resolve_god_name(intent["god_input"])
        if error:
            return self._formatter.format_error(error)

        result = await self._activity.post(
            f"/api/draft/{match_id}/action",
            {"god": god, "userId": str(message.author.id)},
        )
        if not result or "error" in result:
            return self._formatter.format_error(
                result.get("error", f"{god} is unavailable.")
                if result
                else "Backend unreachable."
            )

        self._log.info(
            "Draft %s: %s %s %s via text command",
            match_id,
            turn["team"],
            turn["action"],
            god,
        )
        return None  # WS listener updates the embed

    # -- Local command handlers ------------------------------------------

    async def _handle_draft_local(self, intent: dict, message: discord.Message):
        action = intent["action"]
        channel_id = message.channel.id

        if action == "start":
            active = self._channel_has_active(channel_id)
            if active == "session":
                return self._formatter.format_error(
                    "A session is active in this channel. Use `.session end` first."
                )
            if active == "draft":
                return self._formatter.format_error(
                    "A draft is already active in this channel. Use `.draft end` first."
                )

            options = draft_support.draft_start_options(message.content)
            mentions = message.mentions
            if len(mentions) < 2:
                return self._formatter.format_error(
                    "Usage: `.draft start @blue_captain @red_captain [--match FL-123] [--game 2]`"
                )
            blue_user, red_user = mentions[0], mentions[1]
            if blue_user.id == red_user.id:
                return self._formatter.format_error(
                    "Blue and red captains must be different users."
                )
            if options["game_number"] < 1:
                return self._formatter.format_error("`--game` must be 1 or greater.")

            guild = message.guild
            draft = self._drafts.start(
                channel_id,
                blue_captain_id=blue_user.id,
                blue_captain_name=blue_user.display_name,
                red_captain_id=red_user.id,
                red_captain_name=red_user.display_name,
                guild_id=guild.id if guild else 0,
                guild_name=guild.name if guild else "DM",
                channel_name=message.channel.name
                if hasattr(message.channel, "name")
                else "unknown",
                forgelens_match_id=options["forgelens_match_id"],
                game_number=options["game_number"],
            )
            if not draft:
                return self._formatter.format_error("Failed to start draft.")

            embed = self._formatter.format_draft_board(draft)
            sent = await message.channel.send(embed=embed)
            draft.board_message_id = sent.id
            self._active_store.save(channel_id, draft.draft_id)
            self._log.info(
                "Draft %s started in channel %s: 🔵 %s vs 🔴 %s",
                draft.draft_id,
                channel_id,
                blue_user.display_name,
                red_user.display_name,
            )
            return None

        if action == "show":
            draft = self._drafts.get(channel_id)
            if not draft:
                return self._formatter.format_error("No active draft in this channel.")
            return self._formatter.format_draft_show(draft)

        if action == "next":
            draft = self._drafts.get(channel_id)
            if not draft:
                return self._formatter.format_error("No active draft in this channel.")
            error = draft.advance_game()
            if error:
                return self._formatter.format_error(error)
            for team in ("blue", "red"):
                mid = draft.claim_message_ids.get(team)
                if mid:
                    self._tracked.pop(mid, None)
            await message.channel.send(self._formatter.format_draft_next(draft))
            embed = self._formatter.format_draft_board(draft)
            sent = await message.channel.send(embed=embed)
            draft.board_message_id = sent.id
            self._log.info(
                "Draft %s advanced to Game %s",
                draft.draft_id,
                draft.current_game.game_number,
            )
            return None

        if action == "end":
            draft = self._drafts.end(channel_id)
            if not draft:
                return self._formatter.format_error("No active draft in this channel.")
            export = draft.to_export_dict()
            embed = self._formatter.format_draft_end(draft, export)
            await message.channel.send(embed=embed)
            filename = draft.sanitized_filename()
            json_bytes = json.dumps(export, indent=2).encode("utf-8")
            file = discord.File(io.BytesIO(json_bytes), filename=filename)
            await message.channel.send(f"📎 Draft record: `{filename}`", file=file)
            guild_id = message.guild.id if message.guild else None
            if guild_id and guild_id in self._reports_channels:
                reports_ch = self._client.get_channel(self._reports_channels[guild_id])
                if reports_ch:
                    try:
                        await reports_ch.send(embed=embed)
                        report_file = discord.File(
                            io.BytesIO(json_bytes), filename=filename
                        )
                        await reports_ch.send(
                            f"📎 Draft record: `{filename}`", file=report_file
                        )
                        self._log.info(
                            "Draft %s report posted to reports channel", draft.draft_id
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        self._log.warning("Failed to post to reports channel: %s", exc)
            self._active_store.remove(channel_id)
            self._log.info(
                "Draft %s ended: %s game(s)", draft.draft_id, len(export["games"])
            )
            return None

        if action == "undo":
            draft = self._drafts.get(channel_id)
            if not draft:
                return self._formatter.format_error("No active draft in this channel.")
            result = draft.undo()
            if result is None:
                return self._formatter.format_error("Nothing to undo.")
            if result["type"] == "step":
                await message.channel.send(
                    self._formatter.format_draft_undo(
                        result["team"], result["action"], result["god"]
                    )
                )
            elif result["type"] == "claim":
                await message.channel.send(
                    self._formatter.format_claim_undo(
                        result["team"], result["god"], result["user_name"]
                    )
                )
                await self._renderer.update_claim_embed(draft, result["team"], message.channel)
            elif result["type"] == "next_game":
                await message.channel.send(
                    f"↩️ Undid game advance. Back to **Game {result['game_number']}**."
                )
            await self._renderer.update_board(draft, message.channel)
            return None
        return None

    async def _handle_draft_action_local(self, intent: dict, message: discord.Message):
        channel_id = message.channel.id
        draft = self._drafts.get(channel_id)
        if not draft:
            return self._formatter.format_error(
                "No active draft in this channel. Use `.draft start` first."
            )
        if draft.is_claiming():
            return self._formatter.format_error(
                "Players are claiming gods. Use `.draft undo` if you need to fix something."
            )
        turn = draft.get_current_team_and_action()
        if turn is None:
            return self._formatter.format_error(
                "Current game is complete. Use `.draft next` or `.draft end`."
            )
        current_team, expected_action = turn
        action = intent["action"]
        if action != expected_action:
            return self._formatter.format_error(
                f"It's time to **{expected_action}**, not {action}."
            )
        expected_captain_id = draft.get_current_captain_id()
        if message.author.id != expected_captain_id:
            captain_name = (
                draft.blue_captain["name"]
                if current_team == "blue"
                else draft.red_captain["name"]
            )
            return self._formatter.format_error(
                f"It's **{captain_name}**'s turn ({current_team})."
            )
        god, error = self._resolve_god_name(intent["god_input"])
        if error:
            return self._formatter.format_error(error)
        unavailable = draft.get_unavailable_gods()
        if god in unavailable:
            if god in draft.fearless_pool:
                return self._formatter.format_error(
                    f"**{god}** is in the fearless pool and unavailable this set."
                )
            return self._formatter.format_error(
                f"**{god}** has already been {expected_action}ned this game."
            )
        team, action_done = draft.execute_step(god)
        await message.channel.send(
            self._formatter.format_draft_action(team, action_done, god, draft.draft_id)
        )
        await self._renderer.update_board(draft, message.channel)
        self._log.info(
            "Draft %s: %s %s %s (step %s/20)",
            draft.draft_id,
            team,
            action_done,
            god,
            draft.current_game.step,
        )
        if draft.current_game.is_complete():
            await self._renderer.post_claim_embeds(draft, message.channel)
        return None

    # -- Claim reactions -------------------------------------------------

    async def handle_claim_reaction(self, payload, info, message_id, channel_id, emoji):
        """Handle 1️⃣-5️⃣ reactions on local draft claim embeds."""
        if emoji not in self._number_emojis:
            return
        async with self._drafts.get_lock(channel_id):
            draft = self._drafts.get(channel_id)
            if not draft:
                return
            channel = self._client.get_channel(channel_id)
            if not channel:
                return
            try:
                await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self._tracked.pop(message_id, None)
                return
            team = info["team"]
            picks = info["picks"]
            index = self._number_emojis.index(emoji)
            if index >= len(picks):
                return
            god = picks[index]
            guild = self._client.get_guild(payload.guild_id) if payload.guild_id else None
            if guild:
                member = guild.get_member(payload.user_id)
                if not member:
                    try:
                        member = await guild.fetch_member(payload.user_id)
                    except (discord.NotFound, discord.Forbidden):
                        return
                user_name = member.display_name
            else:
                user = self._client.get_user(payload.user_id)
                if not user:
                    try:
                        user = await self._client.fetch_user(payload.user_id)
                    except (discord.NotFound, discord.Forbidden):
                        return
                user_name = user.display_name
            if draft.claim_god(team, god, payload.user_id, user_name):
                self._log.info(
                    "Draft %s: %s claimed %s (%s)", draft.draft_id, user_name, god, team
                )
                await self._renderer.update_claim_embed(draft, team, channel)
                if draft.current_game.is_fully_claimed():
                    await channel.send(draft_support.draft_completion_marker(draft))
                    self._log.info(
                        "Draft %s: all claims complete for Game %s",
                        draft.draft_id,
                        draft.current_game.game_number,
                    )
