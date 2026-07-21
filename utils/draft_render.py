"""Discord rendering for local draft boards and claim embeds.

Adapter module for the Issue #48 refactor. It owns the Discord-facing rendering
of the living draft board and the per-team claim embeds that previously lived in
``bot.py``. Collaborators (the formatter, the shared reaction-tracking map, and
the number-emoji set) are injected so rendering can be exercised with mocks.

Draft *state* stays in ``utils/draft.py``; this module only reads a draft and
writes Discord messages/reactions.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger("godforge.draft_render")


class DraftRenderer:
    """Renders and updates draft board / claim embeds for a channel."""

    def __init__(self, *, formatter, tracked_messages: dict, number_emojis) -> None:
        self._formatter = formatter
        self._tracked_messages = tracked_messages
        self._number_emojis = number_emojis

    async def update_board(self, draft, channel) -> None:
        """Edit the living draft board embed in place; fall back to posting new."""
        if draft.board_message_id:
            try:
                msg = await channel.fetch_message(draft.board_message_id)
                await msg.edit(embed=self._formatter.format_draft_board(draft))
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        sent = await channel.send(embed=self._formatter.format_draft_board(draft))
        draft.board_message_id = sent.id

    async def post_claim_embeds(self, draft, channel) -> None:
        """Post numbered claim embeds for both teams after a game completes."""
        game = draft.current_game
        for team in ("blue", "red"):
            embed = self._formatter.format_claim_embed(
                team,
                game.picks[team],
                game.claims[team],
                draft.draft_id,
                getattr(draft, "forgelens_match_id", ""),
                game.game_number,
                getattr(draft, "draft_sequence", 1),
            )
            sent = await channel.send(embed=embed)
            draft.claim_message_ids[team] = sent.id
            self._tracked_messages[sent.id] = {
                "kind": "claim",
                "team": team,
                "picks": game.picks[team],
                "channel_id": channel.id,
                "draft_id": draft.draft_id,
            }
            for emoji in self._number_emojis:
                await sent.add_reaction(emoji)
        log.info(
            "Draft %s: claim embeds posted for Game %s",
            draft.draft_id,
            game.game_number,
        )

    async def update_claim_embed(self, draft, team, channel) -> None:
        """Edit a claim embed after a player claims or unclaims."""
        msg_id = draft.claim_message_ids.get(team)
        if not msg_id:
            return
        try:
            msg = await channel.fetch_message(msg_id)
            game = draft.current_game
            embed = self._formatter.format_claim_embed(
                team,
                game.picks[team],
                game.claims[team],
                draft.draft_id,
                getattr(draft, "forgelens_match_id", ""),
                game.game_number,
                getattr(draft, "draft_sequence", 1),
            )
            await msg.edit(embed=embed)
            if all(god in game.claims[team] for god in game.picks[team]):
                try:
                    await msg.clear_reactions()
                except discord.Forbidden:
                    pass
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
