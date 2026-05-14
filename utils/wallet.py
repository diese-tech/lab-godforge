"""
Wallet persistence for the GodForge betting system.

All read/write operations on wallets.json are encapsulated here.
Balances survive ledger resets and season transitions.
They are only wiped by an explicit .wallet wipe command.

Starting balance: 500 points (auto-seeded on first bet).
Balances CAN go negative via admin .wallet take — no floor.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

WALLETS_PATH = Path("data/wallets.json")
SEED_AMOUNT = 500
_WALLET_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Raw I/O
# ---------------------------------------------------------------------------

def load_wallets() -> dict:
    if not WALLETS_PATH.exists():
        return {}
    try:
        with open(WALLETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_wallets(data: dict):
    WALLETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _WALLET_LOCK:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=WALLETS_PATH.parent, delete=False, suffix=".tmp", encoding="utf-8") as tmp:
                json.dump(data, tmp, indent=2)
                tmp.flush()
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, WALLETS_PATH)
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()


# ---------------------------------------------------------------------------
# Wallet operations
# ---------------------------------------------------------------------------

def get_wallet(user_id: int) -> dict | None:
    """Return the wallet dict for user_id, or None if no wallet exists."""
    return load_wallets().get(str(user_id))


def seed_wallet(user_id: int, username: str) -> int:
    """
    Auto-seed wallet at SEED_AMOUNT if this user has no entry.
    Returns the current balance (whether seeded or pre-existing).
    """
    uid = str(user_id)
    with _WALLET_LOCK:
        wallets = load_wallets()
        if uid not in wallets:
            wallets[uid] = {"username": username, "balance": SEED_AMOUNT}
        else:
            wallets[uid]["username"] = username
        save_wallets(wallets)
        return wallets[uid]["balance"]


def get_balance(user_id: int) -> int | None:
    wallet = get_wallet(user_id)
    return wallet["balance"] if wallet else None


def update_balance(user_id: int, delta: int) -> int:
    """Add delta to user's balance (use negative delta to subtract). Returns new balance."""
    uid = str(user_id)
    with _WALLET_LOCK:
        wallets = load_wallets()
        if uid not in wallets:
            raise KeyError(f"No wallet for user_id {user_id}. Call seed_wallet() first.")
        wallets[uid]["balance"] += delta
        _atomic_write_json(WALLETS_PATH, wallets)
        return wallets[uid]["balance"]


def set_balance(user_id: int, amount: int) -> int:
    """Set user's balance to an exact amount. Returns new balance."""
    uid = str(user_id)
    with _WALLET_LOCK:
        wallets = load_wallets()
        wallets[uid]["balance"] = amount
        save_wallets(wallets)
    return amount


def ensure_wallet(user_id: int, username: str) -> int:
    """
    Ensure a wallet entry exists for user_id (creates with 0 balance if absent,
    rather than the seed amount — for admin give/take/set on non-betters).
    Returns current balance.
    """
    uid = str(user_id)
    with _WALLET_LOCK:
        wallets = load_wallets()
        if uid not in wallets:
            wallets[uid] = {"username": username, "balance": 0}
            save_wallets(wallets)
        return wallets[uid]["balance"]


def apply_payouts(payouts: list[dict]):
    """Bulk-add payout amounts to wallets. Skips unknown user IDs gracefully."""
    if not payouts:
        return
    with _WALLET_LOCK:
        wallets = load_wallets()
        for p in payouts:
            uid = str(p["user_id"])
            if uid in wallets:
                wallets[uid]["balance"] += p["payout"]
        save_wallets(wallets)


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


def reset_all() -> int:
    """
    Reset every wallet balance to SEED_AMOUNT.
    Does not remove players — only resets balances.
    Returns count of wallets reset.
    """
    wallets = load_wallets()
    backup_path = WALLETS_PATH.parent / "wallets.bak.json"
    _atomic_write_json(backup_path, wallets)
    for uid in wallets:
        wallets[uid]["balance"] = SEED_AMOUNT
    save_wallets(wallets)
    return len(wallets)
