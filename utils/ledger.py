"""
Match lifecycle and bet logic for the GodForge betting system.

All file I/O is encapsulated here; bot.py never touches the JSON directly.

Match statuses:
    betting_open  → bets accepted, in queue for draft
    in_progress   → draft active, betting locked
    completed     → winner resolved, win bets paid
    settled       → all props resolved, match fully closed
"""

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from utils.match_ids import next_match_id

LEDGER_PATH = Path("data/weekly_ledger.json")
_LEDGER_LOCK = threading.RLock()
log = logging.getLogger("godforge.ledger")

# ---------------------------------------------------------------------------
# Raw I/O
# ---------------------------------------------------------------------------

def _empty_ledger() -> dict:
    """Return a fresh empty ledger dict (never reuse the same list reference)."""
    return {"matches": [], "embed_message_id": None, "embed_channel_id": None}


def load_ledger() -> dict:
    if not LEDGER_PATH.exists():
        return _empty_ledger()
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_ledger()


def _atomic_write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


def save_ledger(data: dict):
    with _LEDGER_LOCK:
        _atomic_write_json(LEDGER_PATH, data)


# ---------------------------------------------------------------------------
# Match ID generation
# ---------------------------------------------------------------------------

def _next_match_id(matches: list) -> str:
    return next_match_id(m.get("match_id") for m in matches)


# ---------------------------------------------------------------------------
# Match CRUD
# ---------------------------------------------------------------------------

def create_match(team1: str, team2: str) -> dict:
    """Create a new match and persist it. Returns the match object."""
    with _LEDGER_LOCK:
        ledger = load_ledger()
        match_id = _next_match_id(ledger["matches"])
        match = {
            "match_id": match_id,
            "teams": {"team1": team1, "team2": team2},
            "status": "betting_open",
            "bets": [],
            "result": None,
            "winner": None,
            "resolved_props": [],
        }
        ledger["matches"].append(match)
        save_ledger(ledger)
    return match


def get_match(match_id: str) -> dict | None:
    """Return the match dict by ID (case-insensitive), or None."""
    ledger = load_ledger()
    target = match_id.upper()
    for m in ledger["matches"]:
        if m["match_id"].upper() == target:
            return m
    return None


def set_match_status(match_id: str, status: str):
    with _LEDGER_LOCK:
        ledger = load_ledger()
        for m in ledger["matches"]:
            if m["match_id"].upper() == match_id.upper():
                m["status"] = status
                save_ledger(ledger)
                return


# ---------------------------------------------------------------------------
# Bets
# ---------------------------------------------------------------------------

def add_bet(match_id: str, bet: dict):
    """Append a bet record to the match's bets list."""
    with _LEDGER_LOCK:
        ledger = load_ledger()
        for m in ledger["matches"]:
            if m["match_id"].upper() == match_id.upper():
                m["bets"].append(bet)
                save_ledger(ledger)
                return


# ---------------------------------------------------------------------------
# Pool helpers (read-only — used for embed display)
# ---------------------------------------------------------------------------

def get_win_pools(match: dict) -> dict:
    """Return {team1_name: int, team2_name: int} point totals from win bets."""
    t1 = match["teams"]["team1"]
    t2 = match["teams"]["team2"]
    pools = {t1: 0, t2: 0}
    for bet in match.get("bets", []):
        if bet["type"] == "win" and bet["team"] in pools:
            pools[bet["team"]] += bet["amount"]
    return pools


def get_prop_pools(match: dict, player: str, stat: str) -> dict:
    """Return {"over": int, "under": int} for a given player+stat prop."""
    pools = {"over": 0, "under": 0}
    for bet in match.get("bets", []):
        if (bet["type"] == "prop"
                and bet["player"].lower() == player.lower()
                and bet["stat"].lower() == stat.lower()):
            pools[bet["direction"]] += bet["amount"]
    return pools


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_win_bets(match_id: str, winner: str) -> list[dict]:
    """
    Settle all team win bets for the match.

    Sets match status to 'completed' and records winner.
    Returns list of {"user_id", "username", "payout"} dicts for wallet updates.
    Pool formula: payout = (user_bet / winning_pool) * total_pool
    """
    with _LEDGER_LOCK:
        ledger = load_ledger()
        match = None
        for m in ledger["matches"]:
            if m["match_id"].upper() == match_id.upper():
                match = m
                break
        if not match:
            return []

        if str(match.get("status", "")).lower() in {"completed", "settled"}:
            log.warning("Skipping win resolution for already resolved match %s (status=%s)", match_id, match.get("status"))
            return []

        win_bets = [b for b in match.get("bets", []) if b["type"] == "win"]
        winning_bets = [b for b in win_bets if b["team"].lower() == winner.lower()]

        total_pool = sum(b["amount"] for b in win_bets)
        winning_pool = sum(b["amount"] for b in winning_bets)

        payouts = []
        if winning_pool > 0:
            for bet in winning_bets:
                payout = round((bet["amount"] / winning_pool) * total_pool)
                payouts.append({
                    "user_id": bet["user_id"],
                    "username": bet["username"],
                    "payout": payout,
                })

        match["winner"] = winner
        match["status"] = "completed"
        save_ledger(ledger)
        return payouts


def resolve_prop_bets(match_id: str, player: str, stat: str,
                      actual_value: float) -> tuple[list[dict], bool]:
    """
    Settle all over/under bets for a player+stat prop.

    Winning direction: over wins if actual > threshold, under wins if actual < threshold.
    On exact tie, no bets win (points absorbed by the pool — no refunds).

    Returns (payouts, had_bets). had_bets is False when no matching bets exist.
    Sets match status to 'settled' when all unique props have been resolved.
    """
    with _LEDGER_LOCK:
        ledger = load_ledger()
        match = None
        for m in ledger["matches"]:
            if m["match_id"].upper() == match_id.upper():
                match = m
                break
        if not match:
            return [], False

        resolved_key = f"{player.lower()}:{stat.lower()}"
        existing_resolved = {
            f"{p.get('player','').lower()}:{p.get('stat','').lower()}"
            for p in match.get("resolved_props", [])
        }
        if resolved_key in existing_resolved:
            log.warning("Skipping duplicate prop resolution for %s on match %s", resolved_key, match_id)
            return [], False

        prop_bets = [
            b for b in match.get("bets", [])
            if (b["type"] == "prop"
                and b["player"].lower() == player.lower()
                and b["stat"].lower() == stat.lower())
        ]

        if not prop_bets:
            return [], False

        threshold = prop_bets[0]["threshold"]

        if actual_value > threshold:
            winning_direction = "over"
        elif actual_value < threshold:
            winning_direction = "under"
        else:
            winning_direction = None  # exact tie, nobody wins

        winning_bets = [b for b in prop_bets if b["direction"] == winning_direction] if winning_direction else []
        total_pool = sum(b["amount"] for b in prop_bets)
        winning_pool = sum(b["amount"] for b in winning_bets)

        payouts = []
        if winning_pool > 0:
            for bet in winning_bets:
                payout = round((bet["amount"] / winning_pool) * total_pool)
                payouts.append({
                    "user_id": bet["user_id"],
                    "username": bet["username"],
                    "payout": payout,
                })

        if "resolved_props" not in match:
            match["resolved_props"] = []
        match["resolved_props"].append({
            "player": player,
            "stat": stat,
            "actual_value": actual_value,
            "threshold": threshold,
            "winning_direction": winning_direction,
        })

        all_prop_keys = {
            f"{b['player'].lower()}:{b['stat'].lower()}"
            for b in match.get("bets", [])
            if b["type"] == "prop"
        }
        resolved_keys = {
            f"{p['player'].lower()}:{p['stat'].lower()}"
            for p in match["resolved_props"]
        }
        if all_prop_keys and all_prop_keys == resolved_keys:
            match["status"] = "settled"

        save_ledger(ledger)
        return payouts, True


# ---------------------------------------------------------------------------
# Embed persistence
# ---------------------------------------------------------------------------

def update_embed_info(message_id: int, channel_id: int):
    with _LEDGER_LOCK:
        ledger = load_ledger()
        ledger["embed_message_id"] = message_id
        ledger["embed_channel_id"] = channel_id
        save_ledger(ledger)


def get_embed_info() -> tuple[int | None, int | None]:
    ledger = load_ledger()
    return ledger.get("embed_message_id"), ledger.get("embed_channel_id")


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

def all_matches_in_progress(ledger: dict | None = None) -> bool:
    """True when at least one match exists and none are still betting_open."""
    if ledger is None:
        ledger = load_ledger()
    matches = ledger.get("matches", [])
    if not matches:
        return False
    return all(m["status"] != "betting_open" for m in matches)


def reset_ledger(preserve_embed: bool = True):
    """Wipe all matches. Wallets are unaffected."""
    current = load_ledger() if preserve_embed else _empty_ledger()
    backup_path = LEDGER_PATH.parent / "weekly_ledger.bak.json"
    _atomic_write_json(backup_path, load_ledger())
    log.info("Ledger backup written to %s", backup_path)
    save_ledger({
        "matches": [],
        "embed_message_id": current.get("embed_message_id"),
        "embed_channel_id": current.get("embed_channel_id"),
    })
