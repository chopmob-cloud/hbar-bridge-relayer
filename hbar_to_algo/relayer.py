#!/usr/bin/env python3
# ============================================================
# HBAR → ALGO RELAYER  (SQLite receipts + Hedera Mirror Node)
# ============================================================
#
# Flow:
#   1. Polls Hedera Mirror Node for smart-contract logs emitted
#      by the HBAR deposit contract (topic0 = HBAR_BRIDGE_DEP_V1)
#   2. Parses log data: deposit_id(32b) | algo_receiver(32b) | amount(8b)
#   3. Checks Algorand: is receiver opted-in to the ASA?
#   4. Calls Algorand escrow app "withdraw_v2" to release tokens
#   5. Records receipt in SQLite (replay protection)
#
# Cursor:   Hedera consensus timestamp  (cursor_timestamp.txt)
#           Format: "<seconds>.<nanos>"  e.g. "1710000000.000000000"
# Receipts: SQLite WAL-mode database
# ============================================================

import base64
import json
import logging
import os
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


# ── Auto-load hbar_to_algo.env ────────────────────────────────
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

_ENV_FILE = "hbar_to_algo.env"
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Check flat layout first (Linux /opt/relayers/), then dev sub-folder
_load_env(os.path.join(_BASE_DIR, _ENV_FILE))
_load_env(os.path.join(_BASE_DIR, "env", _ENV_FILE))

from algosdk import account, encoding, mnemonic
from algosdk.v2client import algod
from algosdk.transaction import ApplicationNoOpTxn, wait_for_confirmation


# ============================================================
# LOGGING
# ============================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hbar-to-algo")


# ============================================================
# ENV HELPERS
# ============================================================

def require(key: str, allow_empty: bool = False) -> str:
    v = os.getenv(key)
    if v is None:
        raise RuntimeError(f"Missing env key: {key}")
    if not allow_empty and v.strip() == "":
        raise RuntimeError(f"Empty env key: {key}")
    return v.strip()


def getenv_int(key: str, default: Optional[int] = None) -> int:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        if default is None:
            raise RuntimeError(f"Missing env key: {key}")
        return default
    return int(v.strip())


# ============================================================
# HEDERA MIRROR NODE  REST CLIENT
# ============================================================

class MirrorNodeREST:
    """Thin wrapper around Hedera Mirror Node REST API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def get_json(self, path: str, params: Dict[str, Any] = None, timeout: int = 15) -> Dict:
        qs = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}?{qs}" if qs else f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_contract_logs(
        self,
        contract_id: str,
        topic0: str,
        after_timestamp: str,
        limit: int = 25,
    ) -> List[Dict]:
        """
        Fetch contract event logs from Mirror Node.
        Returns list of log objects sorted ascending by timestamp.

        Mirror Node endpoint:
          GET /api/v1/contracts/{contractId}/results/logs
              &timestamp=gt:{after_timestamp}
              &limit={limit}
              &order=asc

        Note: topic0 filter is NOT passed to the URL — the Mirror Node
        rejects it with 400. Filtering is done in-process by LOG_PREFIX.
        The timestamp gt: operator must NOT be urlencode'd, so we build
        the URL string manually.
        """
        # Build base params (no timestamp — added manually below)
        params: Dict[str, Any] = {
            "limit": limit,
            "order": "asc",
        }
        qs = urllib.parse.urlencode(params)
        path = f"/api/v1/contracts/{contract_id}/results/logs"
        url  = f"{self.base_url}{path}?{qs}"

        # Append timestamp filter with literal colon (must NOT be urlencode'd)
        # Use gte: (inclusive) so held-cursor retries re-fetch the pending deposit
        if after_timestamp:
            url += f"&timestamp=gte:{after_timestamp}"

        req  = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("logs", []) or []

    def get_current_timestamp(self) -> str:
        """Return the latest consensus timestamp from the network (seconds.nanos).
        Mirror Node /api/v1/blocks response nests the timestamp as:
          { "timestamp": { "from": "...", "to": "1651560386.661997287" } }
        """
        data = self.get_json("/api/v1/blocks", {"limit": 1, "order": "desc"})
        blocks = data.get("blocks", [])
        if blocks:
            ts = blocks[0].get("timestamp", {})
            return ts.get("to", "0.000000000")
        return "0.000000000"


# ============================================================
# CURSOR  (Hedera consensus timestamp string)
# ============================================================

def load_cursor(cursor_file: str) -> str:
    try:
        if os.path.exists(cursor_file):
            with open(cursor_file, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return "0.000000000"


def save_cursor(cursor_file: str, ts: str) -> None:
    os.makedirs(os.path.dirname(cursor_file), exist_ok=True)
    tmp = cursor_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(ts)
    os.replace(tmp, cursor_file)


def ts_decrement(ts: str) -> str:
    """Subtract 1 nanosecond from a Hedera timestamp string 'seconds.nanos'."""
    p = ts.split(".")
    secs  = int(p[0])
    nanos = int(p[1]) if len(p) > 1 else 0
    nanos -= 1
    if nanos < 0:
        nanos = 999_999_999
        secs -= 1
    return f"{secs}.{nanos:09d}"


def ts_is_greater(a: str, b: str) -> bool:
    """Compare two Hedera timestamp strings 'seconds.nanos'."""
    def parts(s: str):
        p = s.split(".")
        return int(p[0]), int(p[1]) if len(p) > 1 else 0
    return parts(a) > parts(b)


# ============================================================
# SQLITE RECEIPT DB
# ============================================================

def init_receipt_db(db_path: str) -> sqlite3.Connection:
    """
    Schema:
      deposit_id_hex  - hex of 32-byte deposit ID, PRIMARY KEY
      status          - 'released' | 'exceeds_max' | 'not_opted_in' | etc.
      hedera_ts       - Hedera consensus timestamp of deposit log
      hedera_contract - Hedera contract ID
      algo_txid       - Algorand transaction ID (NULL if not released)
      receiver        - Algorand destination address
      amount          - base-unit amount
      created_at      - UTC timestamp
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            deposit_id_hex  TEXT PRIMARY KEY,
            status          TEXT NOT NULL,
            hedera_ts       TEXT,
            hedera_contract TEXT,
            algo_txid       TEXT,
            receiver        TEXT NOT NULL,
            amount          INTEGER NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, deposit_id_hex: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM receipts WHERE deposit_id_hex = ?", (deposit_id_hex,)
    ).fetchone()
    return row is not None


def record_receipt(
    conn: sqlite3.Connection,
    deposit_id_hex: str,
    status: str,
    hedera_ts: Optional[str],
    hedera_contract: Optional[str],
    receiver: str,
    amount: int,
    algo_txid: Optional[str] = None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO receipts
           (deposit_id_hex, status, hedera_ts, hedera_contract, algo_txid, receiver, amount)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (deposit_id_hex, status, hedera_ts, hedera_contract, algo_txid, receiver, amount),
    )
    conn.commit()


# ============================================================
# LOG DATA DECODER
# ============================================================

def decode_bridge_log(data_hex: str, log_prefix: bytes) -> Optional[Dict[str, Any]]:
    """
    Decode Hedera contract log data.

    Expected ABI-free packed encoding (matching the Solidity emit):
      bytes  LOG_PREFIX      (variable, config-driven)
      bytes32 deposit_id     (32 bytes)
      bytes32 algo_receiver  (32 bytes, Algorand 32-byte public key)
      uint64  amount         ( 8 bytes, big-endian)

    The Solidity contract should emit:
      emit BridgeDeposit(abi.encodePacked(LOG_PREFIX, deposit_id, algo_receiver, amount));

    Returns None if data doesn't match.
    """
    try:
        raw = bytes.fromhex(data_hex.removeprefix("0x"))
    except Exception:
        return None

    if not raw.startswith(log_prefix):
        return None

    payload = raw[len(log_prefix):]
    if len(payload) < 32 + 32 + 8:
        return None

    deposit_id      = payload[0:32]
    receiver_bytes  = payload[32:64]
    amount          = int.from_bytes(payload[64:72], "big")
    receiver_addr   = encoding.encode_address(receiver_bytes)

    return {
        "deposit_id":     deposit_id,
        "deposit_id_hex": deposit_id.hex(),
        "receiver_bytes": receiver_bytes,
        "receiver_addr":  receiver_addr,
        "amount":         amount,
    }


# ============================================================
# ALGORAND HELPERS
# ============================================================

def account_opted_in_asset(algo_algod: algod.AlgodClient, addr: str, asset_id: int) -> bool:
    try:
        info = algo_algod.account_info(addr)
        for a in info.get("assets", []) or []:
            if int(a.get("asset-id", 0)) == asset_id:
                return True
        return False
    except Exception:
        return False


def fetch_escrow_balance(algo_algod: algod.AlgodClient, app_id: int, asset_id: int) -> int:
    """Fetch the escrow app account's ASA balance. Returns -1 on error."""
    try:
        from algosdk.logic import get_application_address
        app_addr = get_application_address(app_id)
        info = algo_algod.account_info(app_addr)
        for a in info.get("assets", []) or []:
            if int(a.get("asset-id", 0)) == asset_id:
                return int(a.get("amount", 0))
        return 0
    except Exception as e:
        log.warning("Could not fetch escrow balance: %s", e)
        return -1


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    log.info("Starting HBAR -> Algorand relayer (SQLite mode)")

    # ── Hedera (source) ──
    HEDERA_CONTRACT_ID  = require("HEDERA_CONTRACT_ID")       # e.g. 0.0.12345
    HEDERA_TOPIC0       = require("HEDERA_TOPIC0", allow_empty=True)   # keccak256 of event sig (hex)
    HEDERA_MIRROR_URL   = require("HEDERA_MIRROR_URL")
    HBAR_LOG_PREFIX     = require("HBAR_LOG_PREFIX").encode("utf-8")   # e.g. HBAR_BRIDGE_DEP_V1|

    # ── Algorand (destination) ──
    ALGO_ALGOD_ADDRESS  = require("ALGO_ALGOD_ADDRESS")
    ALGO_ALGOD_TOKEN    = require("ALGO_ALGOD_TOKEN",  allow_empty=True)
    ALGO_ESCROW_APP_ID  = int(require("ALGO_ESCROW_APP_ID"))
    ALGO_TOKEN_ASA_ID   = int(require("ALGO_TOKEN_ASA_ID"))
    ALGO_ADMIN_MNEMONIC = require("ALGO_ADMIN_MNEMONIC")

    # ── Relayer controls ──
    CURSOR_FILE         = require("CURSOR_FILE")
    RECEIPT_DB          = require("RECEIPT_DB")
    INDEXER_LIMIT       = getenv_int("INDEXER_LIMIT",       25)
    POLL_DELAY          = getenv_int("POLL_DELAY",          20)
    MAX_BACKOFF         = getenv_int("MAX_BACKOFF",         180)
    ALGO_CONFIRM_ROUNDS = getenv_int("ALGO_CONFIRM_ROUNDS", 12)
    MAX_DEPOSIT         = getenv_int("MAX_DEPOSIT",         0)
    AUTO_START_LOOKBACK = getenv_int("AUTO_START_LOOKBACK", 300)  # seconds back from head

    # ── Clients ──
    algo_algod  = algod.AlgodClient(ALGO_ALGOD_TOKEN, ALGO_ALGOD_ADDRESS)
    mirror_node = MirrorNodeREST(HEDERA_MIRROR_URL)

    admin_sk    = mnemonic.to_private_key(ALGO_ADMIN_MNEMONIC)
    admin_addr  = account.address_from_private_key(admin_sk)

    # ── Receipt DB ──
    db = init_receipt_db(RECEIPT_DB)

    # ── Cursor ──
    cursor = load_cursor(CURSOR_FILE)
    if cursor == "0.000000000":
        try:
            head_ts = mirror_node.get_current_timestamp()
            head_secs = int(head_ts.split(".")[0])
            start_secs = max(0, head_secs - AUTO_START_LOOKBACK)
            cursor = f"{start_secs}.000000000"
            save_cursor(CURSOR_FILE, cursor)
            log.info("Auto-start: head=%s  cursor=%s", head_ts, cursor)
        except Exception as e:
            log.warning("Could not fetch head timestamp: %s", e)
            cursor = "0.000000000"

    log.info("Hedera contract  : %s", HEDERA_CONTRACT_ID)
    log.info("Algo escrow app  : %d", ALGO_ESCROW_APP_ID)
    log.info("Algo ASA         : %d", ALGO_TOKEN_ASA_ID)
    log.info("Algo admin       : %s", admin_addr)
    log.info("Receipt DB       : %s", RECEIPT_DB)
    log.info("Max deposit      : %s", MAX_DEPOSIT if MAX_DEPOSIT > 0 else "unlimited")
    log.info("Scanning Hedera from timestamp %s", cursor)
    log.info("-" * 60)

    backoff = 2

    while True:
        try:
            logs = mirror_node.get_contract_logs(
                contract_id=HEDERA_CONTRACT_ID,
                topic0=HEDERA_TOPIC0,
                after_timestamp=cursor,
                limit=INDEXER_LIMIT,
            )

            found_any = False
            # Track the earliest timestamp we must NOT advance past
            # (a deposit we want to retry next cycle — e.g. receiver not opted-in).
            # If set, cursor is held back to just before that deposit.
            retry_hold_ts: Optional[str] = None

            for entry in logs:
                ts          = entry.get("timestamp", "0.000000000")
                data_hex    = entry.get("data", "0x")
                contract_id = entry.get("contract_id", HEDERA_CONTRACT_ID)

                decoded = decode_bridge_log(data_hex, HBAR_LOG_PREFIX)
                if not decoded:
                    # Not a bridge log — safe to advance cursor past it
                    if ts_is_greater(ts, cursor):
                        cursor = ts
                    continue

                found_any = True
                dep_hex   = decoded["deposit_id_hex"]

                # ── Replay check ──
                if is_processed(db, dep_hex):
                    if ts_is_greater(ts, cursor):
                        cursor = ts
                    continue

                if decoded["amount"] <= 0:
                    if ts_is_greater(ts, cursor):
                        cursor = ts
                    continue

                log.info(
                    "HBAR deposit: amount=%d receiver=%s deposit_id=%s ts=%s",
                    decoded["amount"], decoded["receiver_addr"], dep_hex[:16] + "…", ts,
                )

                # ── Opt-in check on Algorand ──
                if not account_opted_in_asset(algo_algod, decoded["receiver_addr"], ALGO_TOKEN_ASA_ID):
                    log.warning(
                        "Receiver %s not opted-in to ASA %d on Algorand. Will retry.",
                        decoded["receiver_addr"], ALGO_TOKEN_ASA_ID,
                    )
                    # Hold cursor back so this deposit is re-seen next cycle
                    if retry_hold_ts is None:
                        retry_hold_ts = ts
                    continue   # do NOT record — retry next cycle

                # ── Per-deposit cap ──
                if MAX_DEPOSIT > 0 and decoded["amount"] > MAX_DEPOSIT:
                    log.warning("Amount %d exceeds MAX_DEPOSIT %d. Flagging.", decoded["amount"], MAX_DEPOSIT)
                    record_receipt(db, dep_hex, "exceeds_max", ts, contract_id,
                                   decoded["receiver_addr"], decoded["amount"])
                    if ts_is_greater(ts, cursor):
                        cursor = ts
                    continue

                # ── Escrow balance check ──
                escrow_bal = fetch_escrow_balance(algo_algod, ALGO_ESCROW_APP_ID, ALGO_TOKEN_ASA_ID)
                if escrow_bal >= 0 and escrow_bal < decoded["amount"]:
                    log.warning(
                        "Escrow balance (%d) < deposit amount (%d). Will retry next cycle.",
                        escrow_bal, decoded["amount"],
                    )
                    # Hold cursor so this deposit is re-seen once escrow is refilled
                    if retry_hold_ts is None:
                        retry_hold_ts = ts
                    continue   # do NOT record — retry when escrow is refilled

                # ── Algorand withdraw_v2 ──
                sp = algo_algod.suggested_params()
                sp.flat_fee = True
                sp.fee = 4000

                app_args = [
                    b"withdraw_v2",
                    decoded["deposit_id"],                            # 32-byte box key
                    decoded["receiver_bytes"],                        # 32-byte Algorand pubkey
                    int(decoded["amount"]).to_bytes(8, "big"),        # amount u64
                ]

                withdraw_txn = ApplicationNoOpTxn(
                    sender=admin_addr,
                    sp=sp,
                    index=ALGO_ESCROW_APP_ID,
                    app_args=app_args,
                    foreign_assets=[ALGO_TOKEN_ASA_ID],
                    accounts=[decoded["receiver_addr"]],
                    boxes=[(ALGO_ESCROW_APP_ID, decoded["deposit_id"])],
                )

                try:
                    algo_txid = algo_algod.send_transaction(withdraw_txn.sign(admin_sk))
                    wait_for_confirmation(algo_algod, algo_txid, ALGO_CONFIRM_ROUNDS)
                except Exception as withdraw_err:
                    err_msg = str(withdraw_err).lower()
                    # Box already exists → deposit already processed on-chain
                    if "box" in err_msg and ("exist" in err_msg or "already" in err_msg):
                        log.info("Deposit already processed on-chain (box exists). Marking done.")
                        record_receipt(db, dep_hex, "released", ts, contract_id,
                                       decoded["receiver_addr"], decoded["amount"],
                                       algo_txid="already_on_chain")
                        continue
                    raise  # re-raise unexpected errors

                record_receipt(
                    db, dep_hex, "released", ts, contract_id,
                    decoded["receiver_addr"], decoded["amount"], algo_txid,
                )
                log.info("Released on Algorand: %s", algo_txid)
                # Advance cursor past this successfully processed deposit
                if ts_is_greater(ts, cursor):
                    cursor = ts

            # If any deposits are pending retry, hold cursor back to just before
            # the earliest one so they are re-seen on the next poll cycle.
            if retry_hold_ts is not None:
                # Step cursor back 1 ns so gt:cursor re-fetches the pending deposit
                cursor = ts_decrement(retry_hold_ts)
                log.info("Holding cursor at %s for pending retries.", cursor)

            save_cursor(CURSOR_FILE, cursor)

            if not found_any:
                log.info("No new HBAR deposits. cursor=%s", cursor)

            backoff = 2
            time.sleep(POLL_DELAY)

        except Exception as e:
            log.warning("Relayer error: %s", e)
            sleep_for = min(MAX_BACKOFF, backoff) + random.uniform(0, 1.0)
            log.info("Backing off %.1fs …", sleep_for)
            time.sleep(sleep_for)
            backoff = min(MAX_BACKOFF, backoff * 2)


if __name__ == "__main__":
    main()
