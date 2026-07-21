"""Legacy economy: wallets, weekly ledger, matches, and betting.

Dormant code, kept for possible future reuse — DO NOT DELETE.

Economy and betting are out of scope for standalone GodForge: ``on_message``
intercepts the ``.match``/``.bet``/``.wallet``/``.ledger`` tokens and routes
them to a deprecation notice (``_handle_deprecated_economy_command`` in
``bot.py``) before any of this module's code ever runs. This is a straight
relocation out of ``bot.py`` for organization, not a live feature extraction —
these functions still reach back into ``bot.py`` (via a deferred ``import bot``
inside each function body, the standard way to avoid a circular import) for
the live client, logger, admin check, and channel-id configuration, exactly as
they did when they were defined directly in ``bot.py``. Existing tests that
exercise this code call it directly and patch ``bot.<name>``; that continues to
work unchanged because every name here is re-exported as a ``bot.py``
module-level attribute and every cross-call is resolved through ``bot``'s
namespace at call time, not a same-module bare reference.

If this is ever reactivated, wiring it behind dependency injection (matching
the rest of ``utils/``) would be the natural next step.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone

import discord


# ---------------------------------------------------------------------------
# Team / player name extraction (pure helpers)
# ---------------------------------------------------------------------------

def _extract_team_names(message: discord.Message) -> list[str]:
    """Return up to 2 team name strings from a message.

    Priority: role mentions → user mentions → quoted strings → raw @word.
    """
    teams: list[str] = []
    for r in message.role_mentions:
        if len(teams) >= 2:
            break
        teams.append(f"@{r.name}")
    for u in message.mentions:
        if len(teams) >= 2:
            break
        name = f"@{u.display_name}"
        if name not in teams:
            teams.append(name)
    if len(teams) < 2:
        # Quoted plain-text team names: .match create "Whiskey Whales" "Shadow Council"
        for part in re.findall(r'"([^"]+)"', message.content):
            if len(teams) >= 2:
                break
            name = f"@{part}"
            if name not in teams:
                teams.append(name)
    if len(teams) < 2:
        # Legacy: raw @word (single-word, no spaces)
        for part in re.findall(r'@([^\s<>@]+)', message.content):
            if len(teams) >= 2:
                break
            name = f"@{part}"
            if name not in teams:
                teams.append(name)
    return teams[:2]


def _find_matching_team(message: discord.Message, stored_teams: list[str]) -> str | None:
    """Return which stored team name is referenced in the message, or None.

    Priority: role mentions → user mentions → plain-text (with or without @).
    Longer team names are checked first to prevent partial matches.
    """
    for r in message.role_mentions:
        name = f"@{r.name}"
        if name in stored_teams:
            return name
    for u in message.mentions:
        name = f"@{u.display_name}"
        if name in stored_teams:
            return name
    # Plain-text fallback — longest names first so "Whiskey Whales" beats "Whales"
    content_lower = message.content.lower()
    for team in sorted(stored_teams, key=len, reverse=True):
        if team.lower() in content_lower:          # legacy: "@Whiskey Whales" in content
            return team
        if team.lstrip("@").lower() in content_lower:  # new: "Whiskey Whales" in content
            return team
    return None


def _extract_player_name(message: discord.Message) -> str | None:
    """Return the first mentioned user's display name prefixed with @, or None."""
    if message.mentions:
        return f"@{message.mentions[0].display_name}"
    for part in re.findall(r'@([^\s<>@]+)', message.content):
        return f"@{part}"
    return None


async def _post_wallets_to_reports(guild: discord.Guild | None):
    """Post a wallets.json snapshot to #godforge-reports."""
    import bot

    if not guild or guild.id not in bot.REPORTS_CHANNELS:
        return
    reports_ch = bot.client.get_channel(bot.REPORTS_CHANNELS[guild.id])
    if not reports_ch:
        return
    data = wallet_utils.load_wallets()
    json_bytes = json.dumps(data, indent=2).encode("utf-8")
    file = discord.File(io.BytesIO(json_bytes), filename="wallets_snapshot.json")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        await reports_ch.send(f"📊 **Wallet snapshot** ({ts}):", file=file)
        bot.log.info(f"Wallets posted to reports channel {bot.REPORTS_CHANNELS[guild.id]}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        bot.log.warning(f"Could not post wallets to reports: {exc}")


# ---------------------------------------------------------------------------
# Legacy persistent betting embed
# ---------------------------------------------------------------------------

from utils import ledger as ledger_utils
from utils import wallet as wallet_utils

# In-memory page cursor — resets to 0 on restart (acceptable).
_ledger_page: int = 0


class BettingLedgerView(discord.ui.View):
    """Persistent pagination view for the #betting-ledger embed."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="⬅️", custom_id="gf_ledger_prev",
                       style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        import bot

        global _ledger_page
        data = ledger_utils.load_ledger()
        total = len(data["matches"])
        if total > 0:
            _ledger_page = max(0, _ledger_page - 1)
        embed = bot._build_ledger_embed(data, _ledger_page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="➡️", custom_id="gf_ledger_next",
                       style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        import bot

        global _ledger_page
        data = ledger_utils.load_ledger()
        total = len(data["matches"])
        if total > 0:
            _ledger_page = min(total - 1, _ledger_page + 1)
        embed = bot._build_ledger_embed(data, _ledger_page)
        await interaction.response.edit_message(embed=embed, view=self)


def _build_ledger_embed(data: dict, page: int) -> discord.Embed:
    """Build the Discord embed for one match page of the betting ledger."""
    matches = data.get("matches", [])
    if not matches:
        embed = discord.Embed(
            title="🎰 GodForge Betting Ledger",
            description="No matches scheduled this week. Check back soon!",
            color=0x2C2F33,
        )
        embed.set_footer(text="GodForge Betting • No matches yet")
        return embed

    total = len(matches)
    page = max(0, min(page, total - 1))
    m = matches[page]

    status_labels = {
        "betting_open": "🟢 Betting Open",
        "in_progress":  "🟡 In Progress",
        "completed":    "🔴 Completed",
        "settled":      "✅ Settled",
    }
    status_colors = {
        "betting_open": 0x2ECC71,
        "in_progress":  0xF1C40F,
        "completed":    0xE74C3C,
        "settled":      0x9B59B6,
    }

    t1 = m["teams"]["team1"]
    t2 = m["teams"]["team2"]
    status = status_labels.get(m["status"], m["status"])
    color = status_colors.get(m["status"], 0x2C2F33)

    embed = discord.Embed(
        title=f"🎰 {m['match_id']} — {t1} vs {t2}",
        color=color,
    )
    embed.add_field(name="Status", value=status, inline=True)
    if m.get("winner"):
        embed.add_field(name="Winner", value=m["winner"], inline=True)

    bets = m.get("bets", [])
    win_bets = [b for b in bets if b["type"] == "win"]
    t1_pool = sum(b["amount"] for b in win_bets if b["team"] == t1)
    t2_pool = sum(b["amount"] for b in win_bets if b["team"] == t2)
    embed.add_field(
        name="Win Pools",
        value=f"{t1}: **{t1_pool}** pts\n{t2}: **{t2_pool}** pts\nTotal: **{t1_pool + t2_pool}** pts",
        inline=False,
    )

    prop_bets = [b for b in bets if b["type"] == "prop"]
    if prop_bets:
        groups: dict[str, dict] = {}
        for b in prop_bets:
            key = f"{b['player']}|{b['stat']}|{b['threshold']}"
            if key not in groups:
                groups[key] = {"player": b["player"], "stat": b["stat"],
                               "threshold": b["threshold"], "over": 0, "under": 0}
            groups[key][b["direction"]] += b["amount"]
        lines = [
            f"**{p['player']}** {p['stat']} {p['threshold']} — "
            f"Over: **{p['over']}** | Under: **{p['under']}**"
            for p in groups.values()
        ]
        embed.add_field(name="Props", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Page {page + 1}/{total} • ⬅️ ➡️ to navigate • GodForge Betting")
    return embed


async def _handle_wallet_command(message: discord.Message):
    import bot

    parts = message.content.split()
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub == "check":
        target = message.mentions[0] if message.mentions else message.author
        await bot._wallet_check(message, target)
        return

    if not bot._is_admin(message):
        await message.channel.send("⚠️ This command requires admin permissions.")
        return

    if sub == "wipe":
        await bot._wallet_wipe(message)
    elif sub in ("give", "take", "set"):
        if len(parts) < 4 or not message.mentions:
            await message.channel.send(f"⚠️ Usage: `.wallet {sub} @player amount`")
            return
        target = message.mentions[0]
        try:
            amount = int(parts[-1])
        except ValueError:
            await message.channel.send(f"⚠️ Invalid amount `{parts[-1]}`.")
            return
        await bot._wallet_adjust(message, sub, target, amount)
    else:
        await message.channel.send(
            "⚠️ Usage: `.wallet check [@player]`  or  "
            "`.wallet give|take|set @player amount`  or  `.wallet wipe`"
        )


async def _wallet_adjust(message: discord.Message, action: str,
                         target: discord.Member, amount: int):
    import bot

    uid = target.id
    wallet_utils.ensure_wallet(uid, target.display_name)
    if action == "give":
        new_bal = wallet_utils.update_balance(uid, amount)
        await message.channel.send(
            f"✅ Gave **{amount}** pts to **{target.display_name}**. Balance: **{new_bal}** pts"
        )
    elif action == "take":
        new_bal = wallet_utils.update_balance(uid, -amount)
        await message.channel.send(
            f"✅ Took **{amount}** pts from **{target.display_name}**. Balance: **{new_bal}** pts"
        )
    elif action == "set":
        new_bal = wallet_utils.set_balance(uid, amount)
        await message.channel.send(
            f"✅ Set **{target.display_name}**'s balance to **{new_bal}** pts"
        )
    bot.log.info(f"Wallet {action}: {target.display_name} ({uid}), amount={amount}")


async def _wallet_check(message: discord.Message, target: discord.Member):
    wallet = wallet_utils.get_wallet(target.id)
    if wallet is None:
        await message.channel.send(
            f"No wallet found for **{target.display_name}** — they haven't placed any bets yet."
        )
        return
    await message.channel.send(
        f"**{target.display_name}** has **{wallet['balance']}** pts"
    )


async def _wallet_wipe(message: discord.Message):
    import bot

    # Safety backup to #godforge-reports before wiping.
    await bot._post_wallets_to_reports(message.guild)
    count = wallet_utils.reset_all()
    await message.channel.send(
        f"✅ Reset **{count}** wallet(s) to **{wallet_utils.SEED_AMOUNT}** pts each."
    )
    bot.log.info(f"Wallet wipe by {message.author.display_name}: {count} wallets reset")


async def _handle_ledger_command(message: discord.Message):
    import bot

    if not bot._is_admin(message):
        await message.channel.send("⚠️ This command requires admin permissions.")
        return
    parts = message.content.split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "reset":
        await bot._ledger_reset(message)
    elif sub == "post":
        if await bot.update_betting_embed(message.channel):
            await message.channel.send("✅ Ledger embed reposted.")
    else:
        await message.channel.send("⚠️ Usage: `.ledger reset` or `.ledger post`")


async def _ledger_reset(message: discord.Message):
    import bot

    # Post wallet snapshot to reports before wiping match history.
    await bot._post_wallets_to_reports(message.guild)
    ledger_utils.reset_ledger()
    await bot.update_betting_embed(message.channel)
    await message.channel.send(
        "✅ Weekly ledger reset. All matches cleared. Wallet balances untouched."
    )
    bot.log.info(f"Ledger reset by {message.author.display_name}")


async def _handle_match_command(message: discord.Message):
    import bot

    if not bot._is_admin(message):
        await message.channel.send("⚠️ This command requires admin permissions.")
        return
    parts = message.content.split()
    if len(parts) < 2:
        await message.channel.send("⚠️ Usage: `.match create|draft|resolve ...`")
        return
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "create":
        await bot._match_create(message)
    elif sub == "draft":
        await bot._match_draft(message)
    elif sub == "resolve":
        await bot._match_resolve(message)
    else:
        await message.channel.send(f"⚠️ Unknown subcommand `{sub}`. Use `create`, `draft`, or `resolve`.")


async def _match_create(message: discord.Message):
    import bot

    teams = bot._extract_team_names(message)
    if len(teams) < 2:
        await message.channel.send("⚠️ Usage: `.match create @TeamA @TeamB`")
        return
    match = ledger_utils.create_match(teams[0], teams[1])
    await message.channel.send(
        f"✅ Match **{match['match_id']}** created: **{teams[0]}** vs **{teams[1]}**\n"
        f"🟢 Betting is now open!"
    )
    try:
        await bot.update_betting_embed(message.channel)
    except Exception as exc:
        bot.log.warning(f"Ledger embed update failed after match creation: {exc}")
    bot.log.info(f"Match {match['match_id']} created: {teams[0]} vs {teams[1]}")


async def _match_draft(message: discord.Message):
    import bot

    parts = message.content.split()
    if len(parts) < 3:
        await message.channel.send("⚠️ Usage: `.match draft GF-XXXX`")
        return
    match_id = parts[2].upper()

    match = ledger_utils.get_match(match_id)
    if not match:
        await message.channel.send(f"⚠️ Match {match_id} not found.")
        return
    if match["status"] != "betting_open":
        await message.channel.send(
            f"⚠️ Match {match_id} is not open for betting (status: `{match['status']}`)."
        )
        return

    ledger_utils.set_match_status(match_id, "in_progress")
    t1, t2 = match["teams"]["team1"], match["teams"]["team2"]
    draft_note = "\nUse `.draft start @blue_captain @red_captain` to begin the draft."

    # If every match is now in_progress or beyond, post wallet snapshot to reports.
    data = ledger_utils.load_ledger()
    if ledger_utils.all_matches_in_progress(data):
        await bot._post_wallets_to_reports(message.guild)

    await message.channel.send(
        f"🟡 **{match_id}** is now **in progress** — betting locked.\n"
        f"Teams: **{t1}** vs **{t2}**{draft_note}"
    )
    await bot.update_betting_embed(message.channel)
    bot.log.info(f"Match {match_id} set to in_progress in channel {message.channel.id}")


async def _match_resolve(message: discord.Message):
    import bot

    parts = message.content.split()
    if len(parts) < 4:
        await message.channel.send(
            "⚠️ Usage: `.match resolve GF-XXXX winner @Team`  or  "
            "`.match resolve GF-XXXX prop @player stat actual_value`"
        )
        return
    match_id = parts[2].upper()
    resolve_type = parts[3].lower()
    if resolve_type == "winner":
        await bot._match_resolve_winner(message, match_id, parts)
    elif resolve_type == "prop":
        await bot._match_resolve_prop(message, match_id, parts)
    else:
        await message.channel.send(f"⚠️ Unknown resolve type `{resolve_type}`. Use `winner` or `prop`.")


async def _match_resolve_winner(message: discord.Message, match_id: str, parts: list):
    import bot

    match = ledger_utils.get_match(match_id)
    if not match:
        await message.channel.send(f"⚠️ Match {match_id} not found.")
        return
    if match["status"] not in ("in_progress", "completed"):
        await message.channel.send(
            f"⚠️ Match {match_id} must be `in_progress` or `completed` to resolve a winner "
            f"(current: `{match['status']}`)."
        )
        return
    t1, t2 = match["teams"]["team1"], match["teams"]["team2"]
    winner = bot._find_matching_team(message, [t1, t2])
    if not winner:
        await message.channel.send(
            f"⚠️ Could not identify the winner. Teams are **{t1}** and **{t2}**."
        )
        return

    payouts = ledger_utils.resolve_win_bets(match_id, winner)
    wallet_utils.apply_payouts(payouts)

    lines = [f"✅ **{match_id}** — **{winner}** wins!"]
    if payouts:
        lines.append(f"💰 Win payouts ({len(payouts)} winner(s)):")
        for p in payouts:
            lines.append(f"  • {p['username']}: +**{p['payout']}** pts")
    else:
        lines.append("No winning win-bets to pay out.")
    await message.channel.send("\n".join(lines))
    await bot.update_betting_embed(message.channel)
    bot.log.info(f"Match {match_id} resolved: winner={winner}, {len(payouts)} payout(s)")


async def _match_resolve_prop(message: discord.Message, match_id: str, parts: list):
    import bot

    # .match resolve GF-XXXX prop @player stat actual_value  → 7 tokens
    if len(parts) < 7:
        await message.channel.send(
            "⚠️ Usage: `.match resolve GF-XXXX prop @player stat actual_value`"
        )
        return
    match = ledger_utils.get_match(match_id)
    if not match:
        await message.channel.send(f"⚠️ Match {match_id} not found.")
        return

    player = bot._extract_player_name(message)
    if not player:
        await message.channel.send("⚠️ Could not identify the player. Use an @mention.")
        return

    stat = parts[5].lower()
    try:
        actual_value = float(parts[6])
    except ValueError:
        await message.channel.send(f"⚠️ Invalid value `{parts[6]}` — must be a number.")
        return

    payouts, had_bets = ledger_utils.resolve_prop_bets(match_id, player, stat, actual_value)
    if not had_bets:
        await message.channel.send(f"No bets found for that prop ({player} {stat})")
        return

    wallet_utils.apply_payouts(payouts)

    updated = ledger_utils.get_match(match_id)
    settled_note = " — match is now **settled** ✅" if updated and updated["status"] == "settled" else ""
    lines = [f"✅ **{match_id}** prop resolved: **{player}** {stat} = **{actual_value}**{settled_note}"]
    if payouts:
        lines.append(f"💰 Prop payouts ({len(payouts)} winner(s)):")
        for p in payouts:
            lines.append(f"  • {p['username']}: +**{p['payout']}** pts")
    else:
        lines.append("No winning bets on this side.")
    await message.channel.send("\n".join(lines))
    await bot.update_betting_embed(message.channel)
    bot.log.info(f"Match {match_id} prop resolved: {player} {stat}={actual_value}, {len(payouts)} payout(s)")


async def _handle_bet_command(message: discord.Message):
    import bot

    if bot.PLACE_BETS_CHANNEL_ID and message.channel.id != bot.PLACE_BETS_CHANNEL_ID:
        await message.channel.send("⚠️ Bets can only be placed in the #place-bets channel.")
        return

    parts = message.content.split()
    # Minimum: .bet GF-XXXX amount @Team win  (5 tokens)
    if len(parts) < 5:
        await message.channel.send(
            "⚠️ Usage:\n"
            "  `.bet GF-XXXX amount @Team win`\n"
            "  `.bet GF-XXXX amount @player stat over|under threshold`"
        )
        return

    match_id = parts[1].upper()
    try:
        amount = int(parts[2])
    except ValueError:
        await message.channel.send(f"⚠️ Invalid amount `{parts[2]}` — must be a whole number.")
        return
    if amount <= 0:
        await message.channel.send("⚠️ Bet amount must be greater than zero.")
        return

    match = ledger_utils.get_match(match_id)
    if not match:
        await message.channel.send(f"Match {match_id} not found.")
        return
    if match["status"] != "betting_open":
        await message.channel.send("Betting is closed for this match")
        return

    user_id = message.author.id
    username = message.author.display_name
    balance = wallet_utils.seed_wallet(user_id, username)

    if balance <= 0:
        await message.channel.send(
            f"You have {balance} points and cannot place bets. Contact an admin."
        )
        return
    if amount > balance:
        await message.channel.send(
            f"⚠️ You only have **{balance}** pts but tried to bet **{amount}**."
        )
        return

    # Route by bet shape
    if parts[4].lower() == "win":
        # .bet GF-XXXX amount @Team win
        await bot._place_win_bet(message, match, match_id, amount)
    elif len(parts) >= 7 and parts[5].lower() in ("over", "under"):
        # .bet GF-XXXX amount @player stat over|under threshold
        await bot._place_prop_bet(message, match, match_id, amount, parts)
    else:
        await message.channel.send(
            "⚠️ Unrecognised bet format.\n"
            "Win:  `.bet GF-XXXX amount @Team win`\n"
            "Prop: `.bet GF-XXXX amount @player stat over|under threshold`"
        )


async def _place_win_bet(message: discord.Message, match: dict, match_id: str, amount: int):
    import bot

    t1, t2 = match["teams"]["team1"], match["teams"]["team2"]
    team = bot._find_matching_team(message, [t1, t2])
    if not team:
        await message.channel.send(
            f"⚠️ Unknown team. Match {match_id} has **{t1}** vs **{t2}**."
        )
        return

    wallet_utils.update_balance(message.author.id, -amount)
    ledger_utils.add_bet(match_id, {
        "type": "win",
        "user_id": message.author.id,
        "username": message.author.display_name,
        "team": team,
        "amount": amount,
    })
    await message.add_reaction("✅")
    bot.log.info(f"Win bet: {message.author.display_name} bet {amount} on {team} in {match_id}")
    await bot.update_betting_embed(message.channel)


async def _place_prop_bet(message: discord.Message, match: dict, match_id: str,
                          amount: int, parts: list):
    import bot

    player = bot._extract_player_name(message)
    if not player:
        await message.channel.send("⚠️ Could not identify player — use an @mention.")
        return

    stat = parts[4].lower()
    direction = parts[5].lower()
    if direction not in ("over", "under"):
        await message.channel.send("⚠️ Direction must be `over` or `under`.")
        return
    try:
        threshold = float(parts[6])
    except ValueError:
        await message.channel.send(f"⚠️ Invalid threshold `{parts[6]}` — must be a number.")
        return

    wallet_utils.update_balance(message.author.id, -amount)
    ledger_utils.add_bet(match_id, {
        "type": "prop",
        "user_id": message.author.id,
        "username": message.author.display_name,
        "player": player,
        "stat": stat,
        "direction": direction,
        "threshold": threshold,
        "amount": amount,
    })
    await message.add_reaction("✅")
    bot.log.info(f"Prop bet: {message.author.display_name} bet {amount} {direction} "
             f"{threshold} on {player} {stat} in {match_id}")
    await bot.update_betting_embed(message.channel)


async def update_betting_embed(notify_channel: discord.abc.Messageable | None = None) -> bool:
    """Post or in-place edit the persistent betting embed in #betting-ledger.

    Returns True if the embed was successfully posted or edited, False otherwise.
    """
    import bot

    global _ledger_page
    if not bot.BETTING_LEDGER_CHANNEL_ID:
        bot.log.warning("BETTING_LEDGER_CHANNEL_ID not configured — ledger embed skipped")
        if notify_channel:
            await notify_channel.send(
                "⚠️ The betting ledger channel hasn't been configured yet. Please contact an admin."
            )
        return False

    channel = bot.client.get_channel(bot.BETTING_LEDGER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.client.fetch_channel(bot.BETTING_LEDGER_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            bot.log.warning(f"Ledger channel {bot.BETTING_LEDGER_CHANNEL_ID} not accessible: {exc}")
            if notify_channel:
                await notify_channel.send(
                    "⚠️ The betting ledger channel could not be found. "
                    "Please contact an admin to verify the channel configuration."
                )
            return False

    data = ledger_utils.load_ledger()
    total = len(data["matches"])
    _ledger_page = max(0, min(_ledger_page, total - 1)) if total > 0 else 0

    embed = bot._build_ledger_embed(data, _ledger_page)
    view = bot.BettingLedgerView()

    msg_id = data.get("embed_message_id")
    chan_id = data.get("embed_channel_id")
    if msg_id and chan_id == bot.BETTING_LEDGER_CHANNEL_ID:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return True
        except (discord.NotFound, discord.HTTPException):
            pass  # fall through to post a new message
        except discord.Forbidden as exc:
            bot.log.warning(f"Cannot edit ledger embed (no permission): {exc}")
            if notify_channel:
                await notify_channel.send(
                    "⚠️ The bot doesn't have permission to post in the betting ledger channel. "
                    "Please contact an admin."
                )
            return False

    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.Forbidden as exc:
        bot.log.warning(f"Cannot post ledger embed (no permission): {exc}")
        if notify_channel:
            await notify_channel.send(
                "⚠️ The bot doesn't have permission to post in the betting ledger channel. "
                "Please contact an admin."
            )
        return False
    ledger_utils.update_embed_info(msg.id, bot.BETTING_LEDGER_CHANNEL_ID)
    bot.log.info(f"Betting ledger embed posted to channel {bot.BETTING_LEDGER_CHANNEL_ID}")
    return True
