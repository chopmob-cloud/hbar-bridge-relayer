#!/usr/bin/env python3
# ============================================================
# ALGO → HBAR RELAYER (TREASURY MODEL + SQLITE REPLAY DB)
# ============================================================
#
# Flow:
#   1. Polls Algorand indexer for deposit logs emitted by the
#      escrow smart contract (log prefix: HBAR_DEP_V1|)
#   2. Parses: hbar_receiver_evm(20b) | amount(8b) | deposit_id(32b)
#   3. Checks Hedera Mirror Node: is receiver associated with token?
#   4. Atomically reserves the deposit in SQLite (race protection)
#   5. Sends HTS token (or HBAR) from treasury on Hedera to receiver
#   6. Marks receipt as 'sent' in SQLite
#
# Cursor:   Algorand confirmed-round (cursor_round.txt)
# Receipts: SQLite WAL-mode database
#
# Race protection: reserve_deposit() does an atomic INSERT OR IGNORE
# with status='pending'. Only the instance whose INSERT succeeds
# (rowcount==1) proceeds to send. This prevents double-sends when
# multiple relayer instances run concurrently.
# ============================================================

import os
import sys
import time
import base64
import hashlib
import sqlite3
import json
import urllib.request
import urllib.parse
from typing import Dict, Any, List, Optional


# ── Auto-load algo_to_hbar.env ────────────────────────────────
def _load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_ENV_FILE = "algo_to_hbar.env"
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Check flat layout first (Linux /opt/relayers/), then dev sub-folder
_load_env(os.path.join(_BASE_DIR, _ENV_FILE))
_load_env(os.path.join(_BASE_DIR, "env", _ENV_FILE))


from algosdk.v2client import indexer
from algosdk import mnemonic, account, encoding

# Hedera SDK
try:
    from hedera import (
        Client,
        AccountId,
        PrivateKey,
        TokenId,
        TransferTransaction,
        Hbar,
        AccountBalanceQuery,
    )
    HEDERA_SDK_AVAILABLE = True
except ImportError:
    HEDERA_SDK_AVAILABLE = False
    print("WARNING: hedera-sdk-py not found. Install with: pip install hedera-sdk-py")


# ============================================================
# ENV CONFIG
# ============================================================

ALGO_INDEXER_URL   = os.getenv("ALGO_INDEXER_URL",  "https://mainnet-idx.algonode.cloud")
ALGO_ESCROW_APP_ID = int(os.getenv("ALGO_ESCROW_APP_ID", "0"))

HEDERA_NETWORK         = os.getenv("HEDERA_NETWORK", "mainnet")          # mainnet | testnet
HEDERA_OPERATOR_ID     = os.getenv("HEDERA_OPERATOR_ID", "")             # e.g. 0.0.12345
HEDERA_OPERATOR_KEY    = os.getenv("HEDERA_OPERATOR_KEY", "")            # ED25519 DER private key
HEDERA_TOKEN_ID        = os.getenv("HEDERA_TOKEN_ID", "")                # HTS token e.g. 0.0.99999
                                                                          # leave blank for native HBAR
HEDERA_TREASURY_ID     = os.getenv("HEDERA_TREASURY_ID", "")             # treasury account
HEDERA_MIRROR_URL      = os.getenv("HEDERA_MIRROR_URL",
                                   "https://mainnet-public.mirrornode.hedera.com")

POLL_DELAY      = int(os.getenv("POLL_DELAY",      "15"))
CONFIRM_ROUNDS  = int(os.getenv("CONFIRM_ROUNDS",  "6"))
MAX_DEPOSIT               = int(os.getenv("MAX_DEPOSIT",               "0"))      # 0 = no cap (base units)
TREASURY_ALERT_THRESHOLD  = int(os.getenv("TREASURY_ALERT_THRESHOLD",  "0"))      # 0 = no alert

CURSOR_FILE = os.getenv("CURSOR_FILE", "/opt/relayers/algo_to_hbar/cursor_round.txt")
RECEIPT_DB  = os.getenv("RECEIPT_DB",  "/opt/relayers/algo_to_hbar/receipts.db")

# Algorand escrow log format:
#   LOG_PREFIX | hbar_receiver_evm(20 bytes) | amount_u64(8 bytes) | deposit_id(32 bytes)
LOG_PREFIX      = os.getenv("LOG_PREFIX", "HBAR_DEP_V1|").encode("utf-8")
MIN_PAYLOAD_LEN = 20 + 8 + 32


# ============================================================
# CURSOR
# ============================================================

def load_cursor() -> int:
    try:
        with open(CURSOR_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_cursor(rnd: int) -> None:
    os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(rnd))
    os.replace(tmp, CURSOR_FILE)


# ============================================================
# SQLITE RECEIPT DB
# ============================================================

def init_receipt_db(db_path: str) -> sqlite3.Connection:
    """
    Schema:
      deposit_id   - hex SHA-256(raw log), PRIMARY KEY
      status       - 'pending' | 'sent' | 'exceeds_max' | 'not_associated' | 'insufficient_treasury'
      algo_round   - Algorand round where deposit confirmed
      hbar_receiver- Hedera account / EVM address of recipient
      amount       - base units
      hedera_txid  - Hedera transaction ID of the send (NULL if skipped/pending)
      created_at   - UTC timestamp

    'pending' rows are created atomically before sending to Hedera.
    On startup, any leftover 'pending' rows from a prior crashed run
    are deleted so they can be retried cleanly.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            deposit_id    TEXT PRIMARY KEY,
            status        TEXT NOT NULL DEFAULT 'sent',
            algo_round    INTEGER NOT NULL,
            hbar_receiver TEXT NOT NULL,
            amount        INTEGER NOT NULL,
            hedera_txid   TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    # Clear any 'pending' rows left by a previous crashed run so they get retried.
    deleted = conn.execute(
        "DELETE FROM receipts WHERE status = 'pending'"
    ).rowcount
    conn.commit()
    return conn, deleted


def reserve_deposit(
    conn: sqlite3.Connection,
    deposit_id_hex: str,
    algo_round: int,
    hbar_receiver: str,
    amount: int,
) -> bool:
    """
    Atomically claim a deposit for processing by inserting a 'pending' row.
    Returns True if THIS call inserted the row (we own it).
    Returns False if the row already existed (another instance owns it, or it
    was already processed with a terminal status like 'sent' / 'exceeds_max').
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO receipts
           (deposit_id, status, algo_round, hbar_receiver, amount)
           VALUES (?, 'pending', ?, ?, ?)""",
        (deposit_id_hex, algo_round, hbar_receiver, amount),
    )
    conn.commit()
    return cur.rowcount == 1


def complete_deposit(
    conn: sqlite3.Connection,
    deposit_id_hex: str,
    hedera_txid: str,
) -> None:
    """Mark a previously reserved ('pending') deposit as successfully sent."""
    conn.execute(
        "UPDATE receipts SET status='sent', hedera_txid=? WHERE deposit_id=?",
        (hedera_txid, deposit_id_hex),
    )
    conn.commit()


def release_deposit(conn: sqlite3.Connection, deposit_id_hex: str) -> None:
    """
    Remove a 'pending' reservation so the deposit will be retried next poll.
    Called when the Hedera send fails after reservation but before confirmation.
    """
    conn.execute(
        "DELETE FROM receipts WHERE deposit_id=? AND status='pending'",
        (deposit_id_hex,),
    )
    conn.commit()


def record_skip(
    conn: sqlite3.Connection,
    deposit_id_hex: str,
    status: str,
    algo_round: int,
    hbar_receiver: str,
    amount: int,
) -> None:
    """Record a permanently skipped deposit (exceeds_max). Uses INSERT OR IGNORE."""
    conn.execute(
        """INSERT OR IGNORE INTO receipts
           (deposit_id, status, algo_round, hbar_receiver, amount)
           VALUES (?, ?, ?, ?, ?)""",
        (deposit_id_hex, status, algo_round, hbar_receiver, amount),
    )
    conn.commit()


def already_handled(conn: sqlite3.Connection, deposit_id_hex: str) -> bool:
    """
    Returns True if the deposit has a terminal status (sent / exceeds_max / etc.)
    or is currently pending in another instance.
    """
    row = conn.execute(
        "SELECT 1 FROM receipts WHERE deposit_id = ?", (deposit_id_hex,)
    ).fetchone()
    return row is not None


# ============================================================
# HEDERA MIRROR NODE HELPERS
# ============================================================

def mirror_get(path: str, params: Dict[str, Any] = None, timeout: int = 10) -> Dict:
    """Simple GET against Hedera Mirror Node REST API."""
    base = HEDERA_MIRROR_URL.rstrip("/")
    qs = urllib.parse.urlencode(params or {})
    url = f"{base}{path}?{qs}" if qs else f"{base}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def account_associated_with_token(evm_address: str, token_id: str) -> bool:
    """
    Check if a Hedera account (by EVM address) has associated the HTS token.
    Mirror Node: GET /api/v1/accounts/{idOrAliasOrEvmAddress}/tokens?token.id={tokenId}
    """
    if not token_id:
        return True   # native HBAR — no association needed
    try:
        data = mirror_get(
            f"/api/v1/accounts/{evm_address}/tokens",
            {"token.id": token_id, "limit": 1},
        )
        tokens = data.get("tokens", [])
        return any(t.get("token_id") == token_id for t in tokens)
    except Exception as e:
        print(f"  WARNING: Could not check token association for {evm_address}: {e}")
        return False   # conservative: don't send


def fetch_treasury_balance(treasury_id: str, token_id: str) -> int:
    """
    Fetch the treasury HTS token balance (or HBAR tinybars if no token).
    Returns -1 on error (caller should not skip).
    """
    try:
        if token_id:
            data = mirror_get(
                f"/api/v1/accounts/{treasury_id}/tokens",
                {"token.id": token_id, "limit": 1},
            )
            tokens = data.get("tokens", [])
            for t in tokens:
                if t.get("token_id") == token_id:
                    return int(t.get("balance", 0))
            return 0
        else:
            # native HBAR
            data = mirror_get(f"/api/v1/accounts/{treasury_id}")
            bal = data.get("balance", {})
            return int(bal.get("balance", 0))
    except Exception as e:
        print(f"  WARNING: Could not fetch treasury balance: {e}")
        return -1


# ============================================================
# HEDERA SEND
# ============================================================

def send_hedera_tokens(
    client,
    operator_id: str,
    receiver_evm: str,
    amount: int,
    token_id: str,
) -> str:
    """
    Transfer HTS token (or native HBAR) from treasury to receiver.
    Returns the Hedera transaction ID string.
    """
    sender = AccountId.fromString(operator_id)

    # hedera-sdk-py is a JVM wrapper — all methods are Java camelCase
    receiver = AccountId.fromEvmAddress(receiver_evm) if receiver_evm.startswith("0x") \
               else AccountId.fromString(receiver_evm)

    if token_id:
        tid = TokenId.fromString(token_id)
        txn_response = (
            TransferTransaction()
            .addTokenTransfer(tid, sender,   -amount)
            .addTokenTransfer(tid, receiver,  amount)
            .execute(client)
        )
    else:
        # Native HBAR (amount in tinybars)
        txn_response = (
            TransferTransaction()
            .addHbarTransfer(sender,   Hbar.fromTinybars(-amount))
            .addHbarTransfer(receiver, Hbar.fromTinybars(amount))
            .execute(client)
        )

    receipt = txn_response.getReceipt(client)
    return txn_response.transactionId.toString()


# ============================================================
# LOG EXTRACTION (all Algorand indexer locations)
# ============================================================

def extract_logs(tx: Dict[str, Any]) -> List[str]:
    logs: List[str] = []
    logs.extend(tx.get("logs", []) or [])
    appl = tx.get("application-transaction", {}) or {}
    logs.extend(appl.get("application-logs", []) or [])
    for itx in tx.get("inner-txns", []) or []:
        logs.extend(itx.get("logs", []) or [])
        itx_appl = itx.get("application-transaction", {}) or {}
        logs.extend(itx_appl.get("application-logs", []) or [])
    return logs


# ============================================================
# MAIN
# ============================================================

def main():
    # ── Validation ──
    if not HEDERA_OPERATOR_ID:
        raise SystemExit("ERROR: HEDERA_OPERATOR_ID not set")
    if not HEDERA_OPERATOR_KEY:
        raise SystemExit("ERROR: HEDERA_OPERATOR_KEY not set")
    if ALGO_ESCROW_APP_ID == 0:
        raise SystemExit("ERROR: ALGO_ESCROW_APP_ID not set")
    if not HEDERA_SDK_AVAILABLE:
        raise SystemExit("ERROR: hedera SDK not installed. Run: pip install hedera")

    # ── Clients ──
    algo_idx = indexer.IndexerClient("", ALGO_INDEXER_URL)

    if HEDERA_NETWORK == "testnet":
        client = Client.forTestnet()
    else:
        client = Client.forMainnet()
    # Java-style camelCase methods throughout hedera-sdk-py (JVM wrapper)
    client.setOperator(
        AccountId.fromString(HEDERA_OPERATOR_ID),
        PrivateKey.fromString(HEDERA_OPERATOR_KEY),
    )

    db, pending_cleared = init_receipt_db(RECEIPT_DB)

    # ── File logger (JVM can swallow stdout; write to file too) ──
    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relayer.log")
    _logfile  = open(_log_path, "a", encoding="utf-8", buffering=1)

    def log(msg: str) -> None:
        import datetime
        line = f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        _logfile.write(line + "\n")
        _logfile.flush()

    log("[START] ALGO -> HBAR relayer started (treasury model)")
    log(f"  Escrow App ID  : {ALGO_ESCROW_APP_ID}")
    log(f"  Hedera network : {HEDERA_NETWORK}")
    log(f"  Hedera token   : {HEDERA_TOKEN_ID or 'native HBAR'}")
    log(f"  Treasury ID    : {HEDERA_TREASURY_ID}")
    log(f"  Receipt DB     : {RECEIPT_DB}")
    if MAX_DEPOSIT == 0:
        log("[WARN] MAX_DEPOSIT=0 — no per-deposit cap enforced. Set MAX_DEPOSIT to limit exposure.")
    log(f"  Max deposit    : {MAX_DEPOSIT if MAX_DEPOSIT > 0 else 'unlimited'}")
    if pending_cleared > 0:
        log(f"  Cleared {pending_cleared} stale 'pending' row(s) from prior crashed run — will retry.")
    log("-" * 60)

    next_round = load_cursor()
    _backoff   = 1

    while True:
        try:
            kwargs: Dict[str, Any] = {
                "application_id": ALGO_ESCROW_APP_ID,
                "limit": 50,
            }
            if next_round > 0:
                kwargs["min_round"] = next_round

            resp = algo_idx.search_transactions(**kwargs)
            txs  = resp.get("transactions", []) or []
            txs.sort(key=lambda t: t.get("confirmed-round", 0))

            for tx in txs:
                confirmed_round = tx.get("confirmed-round", 0) or 0
                if confirmed_round >= next_round:
                    next_round = confirmed_round + 1

                for log_b64 in extract_logs(tx):
                    try:
                        raw = base64.b64decode(log_b64)
                    except Exception:
                        continue

                    if not raw.startswith(LOG_PREFIX):
                        continue

                    payload = raw[len(LOG_PREFIX):]
                    if len(payload) < MIN_PAYLOAD_LEN:
                        continue

                    # Parse payload: evm_receiver(20) | amount(8) | deposit_id(32)
                    evm_receiver_bytes = payload[0:20]
                    amount             = int.from_bytes(payload[20:28], "big")
                    deposit_id_bytes   = payload[28:60]

                    evm_receiver_hex = "0x" + evm_receiver_bytes.hex()
                    deposit_id_hex   = hashlib.sha256(raw).digest().hex()

                    # ── Reject zero address ──
                    if evm_receiver_bytes == bytes(20):
                        log(f"  [SKIP] Zero EVM address in deposit log. Ignoring.")
                        continue

                    # ── Fast skip: already handled (sent, exceeds_max, or pending in another instance) ──
                    if already_handled(db, deposit_id_hex):
                        continue

                    log(f"[IN] Deposit detected  round={confirmed_round} amount={amount} receiver={evm_receiver_hex} id={deposit_id_hex[:16]}...")

                    # ── Token association check (retry silently if not yet associated) ──
                    if HEDERA_TOKEN_ID and not account_associated_with_token(evm_receiver_hex, HEDERA_TOKEN_ID):
                        log(f"  [WAIT] Receiver not associated with token {HEDERA_TOKEN_ID}. Will retry.")
                        continue   # do NOT record — retry next scan

                    # ── Per-deposit cap ──
                    if MAX_DEPOSIT > 0 and amount > MAX_DEPOSIT:
                        log(f"  [SKIP] Amount ({amount}) exceeds MAX_DEPOSIT ({MAX_DEPOSIT}). Flagging.")
                        record_skip(db, deposit_id_hex, "exceeds_max",
                                    confirmed_round, evm_receiver_hex, amount)
                        continue

                    # ── Treasury balance check ──
                    treasury_bal = fetch_treasury_balance(HEDERA_TREASURY_ID, HEDERA_TOKEN_ID)
                    if TREASURY_ALERT_THRESHOLD > 0 and 0 <= treasury_bal < TREASURY_ALERT_THRESHOLD:
                        log(f"  [ALERT] Treasury balance {treasury_bal} below threshold {TREASURY_ALERT_THRESHOLD}!")
                    if treasury_bal >= 0 and treasury_bal < amount:
                        log(f"  [SKIP] Treasury ({treasury_bal}) < deposit ({amount}). Skipping -- will retry.")
                        log(f"     Top up treasury {HEDERA_TREASURY_ID} on Hedera!")
                        continue   # do NOT record — retry when refilled

                    # ── Atomic reservation (race protection across concurrent instances) ──
                    if not reserve_deposit(db, deposit_id_hex, confirmed_round, evm_receiver_hex, amount):
                        log(f"  [SKIP] Deposit already claimed by another instance: {deposit_id_hex[:16]}...")
                        continue

                    # ── Send on Hedera ──
                    log(f"  Sending {amount} token(s) to {evm_receiver_hex} on Hedera...")
                    try:
                        hedera_txid = send_hedera_tokens(
                            client,
                            HEDERA_OPERATOR_ID,
                            evm_receiver_hex,
                            amount,
                            HEDERA_TOKEN_ID,
                        )
                        complete_deposit(db, deposit_id_hex, hedera_txid)
                        log(f"  [OK] Sent on Hedera: {hedera_txid}")
                    except Exception as send_err:
                        # Release the reservation so this deposit is retried next poll.
                        release_deposit(db, deposit_id_hex)
                        log(f"  [ERROR] Hedera send failed, will retry: {send_err}")

            save_cursor(next_round)

        except Exception as e:
            log(f"[ERROR] Relayer error: {e}")
            time.sleep(min(POLL_DELAY * _backoff, 300))
            _backoff = min(_backoff * 2, 16)
            continue

        _backoff = 1
        time.sleep(POLL_DELAY)


if __name__ == "__main__":
    main()
