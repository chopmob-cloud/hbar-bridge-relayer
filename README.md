# hbar-bridge-relayer

A custom off-chain relayer for a **treasury-model token bridge** between **Algorand** and **Hedera** (HBAR). Runs as two independent systemd services on a Linux server.

Unlike the [Wormhole NTT relayer](https://github.com/chopmob-cloud/wormhole-ntt-relayer), this bridge does not use Wormhole VAAs. Instead, it watches for deposit events emitted by smart contracts on each chain and calls the corresponding release function on the destination chain.

---

## Architecture

```
ALGO → HBAR
  Algorand escrow app          Algorand Indexer
  (user deposits token ASA)  ──────────────────► algo_to_hbar/relayer.py
                                                        │
                                           reads log: HBAR_DEP_V1|
                                           evm_receiver(20) | amount(8) | deposit_id(32)
                                                        │
                                              hedera-sdk-py TransferTransaction
                                                        │
                                                        ▼
                                             Hedera treasury account
                                          (transfers HTS token to receiver)


HBAR → ALGO
  Hedera EVM deposit contract     Hedera Mirror Node REST
  (user deposits HBAR/HTS)  ──────────────────────────► hbar_to_algo/relayer.py
                                                               │
                                              reads log: HBAR_BRIDGE_DEP_V1|
                                              deposit_id(32) | algo_receiver(32) | amount(8)
                                                               │
                                              Algorand escrow app "withdraw_v2"
                                                               │
                                                               ▼
                                                   Algorand recipient wallet
                                                   (receives token ASA)
```

### Bridge model

This is a **treasury model** bridge — not a lock-and-mint model. A treasury account on Hedera holds a supply of HTS tokens. When a deposit is detected on Algorand, the relayer transfers from that treasury. When a deposit is detected on Hedera, the relayer calls `withdraw_v2` on the Algorand escrow, which releases tokens from the escrow's balance.

The relayer holds **no user funds**. All token movement is triggered by on-chain events; the relayer only submits the destination-side transaction.

---

## Repo layout

```
algo_to_hbar/
  relayer.py           Algorand → Hedera service
  requirements.txt     Python dependencies (includes hedera-sdk-py + Java 17)

hbar_to_algo/
  relayer.py           Hedera → Algorand service
  requirements.txt     Python dependencies (pure Python, no Java)

deploy/
  algo-to-hbar.service  systemd unit for Algorand → Hedera
  hbar-to-algo.service  systemd unit for Hedera → Algorand

.env.example           All configuration variables documented
```

---

## Prerequisites

| Dependency | Required by | Notes |
|---|---|---|
| Python 3.10+ | both | system Python or venv |
| Java 17 (OpenJDK) | `algo_to_hbar` only | `hedera-sdk-py` is a JVM wrapper via pyjnius |
| `py-algorand-sdk` | both | Algorand client |
| `hedera-sdk-py` | `algo_to_hbar` only | Hedera SDK (Java bridge) |

Install Java 17 on Ubuntu:
```bash
sudo apt install openjdk-17-jre-headless
```

Install Python deps per service:
```bash
# Algo → HBAR
cd /opt/relayers/algo_to_hbar
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# HBAR → Algo
cd /opt/relayers/hbar_to_algo
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

---

## Configuration

Each service auto-loads its env file on startup. The relayer looks for the file in:
1. Same directory as `relayer.py` (flat layout)
2. `env/` subdirectory (structured layout)

Copy `.env.example` and fill in the required values:

```bash
cp .env.example /opt/relayers/algo_to_hbar/env/algo_to_hbar.env
cp .env.example /opt/relayers/hbar_to_algo/env/hbar_to_algo.env
```

### Algo → HBAR variables

| Variable | Required | Description |
|---|---|---|
| `ALGO_ESCROW_APP_ID` | Yes | Algorand escrow smart contract app ID |
| `LOG_PREFIX` | Yes | Deposit log prefix emitted by the escrow contract (default `HBAR_DEP_V1|`) |
| `HEDERA_OPERATOR_ID` | Yes | Hedera account that signs transfers e.g. `0.0.12345` |
| `HEDERA_OPERATOR_KEY` | Yes | ED25519 DER-encoded private key for the operator |
| `HEDERA_TREASURY_ID` | Yes | Hedera account holding the HTS token supply |
| `HEDERA_TOKEN_ID` | No | HTS token ID e.g. `0.0.99999`. Leave blank for native HBAR |
| `HEDERA_NETWORK` | No | `mainnet` (default) or `testnet` |
| `HEDERA_MIRROR_URL` | No | Mirror Node base URL |
| `ALGO_INDEXER_URL` | No | Algorand Indexer endpoint |
| `POLL_DELAY` | No | Seconds between Algorand polls (default `15`) |
| `CONFIRM_ROUNDS` | No | Algorand confirmation depth before processing (default `6`) |
| `MAX_DEPOSIT` | No | Per-deposit cap in base units; `0` = no cap |
| `CURSOR_FILE` | No | Path for the Algorand confirmed-round cursor file |
| `RECEIPT_DB` | No | Path for the SQLite receipt database |

### HBAR → Algo variables

| Variable | Required | Description |
|---|---|---|
| `HEDERA_CONTRACT_ID` | Yes | Hedera EVM deposit contract e.g. `0.0.55555` |
| `HBAR_LOG_PREFIX` | Yes | Log prefix packed in the contract event (default `HBAR_BRIDGE_DEP_V1|`) |
| `HEDERA_TOPIC0` | No | keccak256 of the Solidity event signature (hex). Leave blank to match all logs |
| `HEDERA_MIRROR_URL` | Yes | Hedera Mirror Node base URL |
| `ALGO_ESCROW_APP_ID` | Yes | Algorand escrow app that performs `withdraw_v2` |
| `ALGO_TOKEN_ASA_ID` | Yes | Algorand Standard Asset ID of the bridged token |
| `ALGO_ADMIN_MNEMONIC` | Yes | 25-word mnemonic of the Algorand relayer/admin wallet |
| `ALGO_ALGOD_ADDRESS` | No | Algod endpoint (default `https://mainnet-api.algonode.cloud`) |
| `ALGO_ALGOD_TOKEN` | No | Algod API token if required |
| `POLL_DELAY` | No | Seconds between Mirror Node polls (default `20`) |
| `MAX_BACKOFF` | No | Maximum exponential backoff in seconds (default `180`) |
| `ALGO_CONFIRM_ROUNDS` | No | Algorand confirmation depth (default `12`) |
| `INDEXER_LIMIT` | No | Mirror Node page size per poll (default `25`) |
| `MAX_DEPOSIT` | No | Per-deposit cap in base units; `0` = no cap |
| `AUTO_START_LOOKBACK` | No | Seconds back from chain head to start if cursor absent (default `300`) |
| `LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` (default `INFO`) |
| `CURSOR_FILE` | No | Path for the Hedera consensus timestamp cursor file |
| `RECEIPT_DB` | No | Path for the SQLite receipt database |

---

## Deployment

### Deploy files

```bash
# Copy service files
sudo cp deploy/algo-to-hbar.service /etc/systemd/system/
sudo cp deploy/hbar-to-algo.service /etc/systemd/system/

# Create relayer user and directories
sudo useradd -r -s /sbin/nologin relayer
sudo mkdir -p /opt/relayers/algo_to_hbar/env /opt/relayers/hbar_to_algo/env

# Copy relayer code
sudo cp -r algo_to_hbar/. /opt/relayers/algo_to_hbar/
sudo cp -r hbar_to_algo/. /opt/relayers/hbar_to_algo/

# Set ownership
sudo chown -R relayer:relayer /opt/relayers/
```

### Create env files

```bash
# Fill in all required values — see .env.example for reference
sudo nano /opt/relayers/algo_to_hbar/env/algo_to_hbar.env
sudo nano /opt/relayers/hbar_to_algo/env/hbar_to_algo.env
sudo chmod 600 /opt/relayers/algo_to_hbar/env/algo_to_hbar.env
sudo chmod 600 /opt/relayers/hbar_to_algo/env/hbar_to_algo.env
```

### Start services

```bash
sudo systemctl daemon-reload
sudo systemctl enable algo-to-hbar hbar-to-algo
sudo systemctl start  algo-to-hbar hbar-to-algo
sudo systemctl status algo-to-hbar hbar-to-algo
```

---

## Monitoring

### Live logs

```bash
# Algo → HBAR
journalctl -u algo-to-hbar -f

# HBAR → Algo
journalctl -u hbar-to-algo -f
```

The `algo_to_hbar` relayer also writes a local `relayer.log` file alongside `relayer.py` (the JVM can swallow stdout, so the file log is a fallback).

### Receipt databases

Both relayers maintain a SQLite receipt database for replay protection:

```bash
# Count sent receipts
sqlite3 /opt/relayers/algo_to_hbar/receipts.db \
  "SELECT status, COUNT(*) FROM receipts GROUP BY status;"

sqlite3 /opt/relayers/hbar_to_algo/receipts.db \
  "SELECT status, COUNT(*) FROM receipts GROUP BY status;"

# Inspect recent sends
sqlite3 /opt/relayers/algo_to_hbar/receipts.db \
  "SELECT deposit_id, hbar_receiver, amount, hedera_txid, created_at FROM receipts ORDER BY created_at DESC LIMIT 10;"

sqlite3 /opt/relayers/hbar_to_algo/receipts.db \
  "SELECT deposit_id_hex, receiver, amount, algo_txid, created_at FROM receipts ORDER BY created_at DESC LIMIT 10;"
```

### Cursors

| Service | Cursor file | Format |
|---|---|---|
| Algo → HBAR | `cursor_round.txt` | Algorand confirmed round (integer) |
| HBAR → Algo | `cursor_timestamp.txt` | Hedera consensus timestamp `seconds.nanos` |

To reset a relayer to reprocess from a specific point:
```bash
# Algo → HBAR: reset to Algorand round 45000000
echo 45000000 > /opt/relayers/algo_to_hbar/cursor_round.txt

# HBAR → Algo: reset to a Hedera timestamp
echo "1710000000.000000000" > /opt/relayers/hbar_to_algo/cursor_timestamp.txt
sudo systemctl restart algo-to-hbar hbar-to-algo
```

---

## Relay flows

### Algo → HBAR

1. `relayer.py` polls the Algorand Indexer for transactions touching `ALGO_ESCROW_APP_ID`.
2. For each transaction, it extracts all logs (including inner transactions).
3. Logs matching `LOG_PREFIX` are decoded: `evm_receiver(20 bytes) | amount(8 bytes) | deposit_id(32 bytes)`.
4. `deposit_id` is `SHA-256(raw log bytes)` — used as the SQLite primary key.
5. Checks Hedera Mirror Node: does the receiver's EVM address have the HTS token associated? If not, retries next poll without recording.
6. Checks treasury balance — if insufficient, retries.
7. Atomically reserves the deposit in SQLite with `INSERT OR IGNORE ... status='pending'` — only the instance whose insert succeeds proceeds (concurrent-instance protection).
8. Calls `TransferTransaction` via `hedera-sdk-py` to transfer HTS tokens from the treasury to the receiver's EVM address.
9. On success: updates receipt to `status='sent'`. On failure: removes the pending reservation so it retries.

### HBAR → Algo

1. `relayer.py` polls the Hedera Mirror Node REST API for contract logs from `HEDERA_CONTRACT_ID` with timestamp `>= cursor`.
2. Logs are filtered in-process by `HBAR_LOG_PREFIX` (Mirror Node rejects topic0 in the query URL).
3. Log data is decoded: `deposit_id(32 bytes) | algo_receiver(32 bytes) | amount(8 bytes)`.
4. Replay check: if `deposit_id_hex` is already in SQLite receipts, skip and advance cursor.
5. Checks that the Algorand receiver has opted in to `ALGO_TOKEN_ASA_ID`. If not, holds the cursor back so the deposit is re-seen next cycle.
6. Checks escrow balance — if insufficient, holds cursor.
7. Submits `ApplicationNoOpTxn` to `ALGO_ESCROW_APP_ID` with method `withdraw_v2`, passing `deposit_id`, `receiver_bytes`, and `amount`.
8. Waits for Algorand confirmation, then records receipt and advances cursor.
9. If the Algorand call fails with a "box already exists" error the deposit was already processed on-chain — records as `algo_txid="already_on_chain"`.

---

## Database schemas

### Algo → HBAR (`receipts.db`)

```sql
CREATE TABLE receipts (
    deposit_id    TEXT PRIMARY KEY,   -- hex SHA-256(raw log bytes)
    status        TEXT NOT NULL,      -- pending | sent | exceeds_max | not_associated | insufficient_treasury
    algo_round    INTEGER NOT NULL,   -- Algorand round where deposit confirmed
    hbar_receiver TEXT NOT NULL,      -- EVM address of recipient on Hedera
    amount        INTEGER NOT NULL,   -- base units
    hedera_txid   TEXT,               -- Hedera transaction ID (NULL if skipped)
    created_at    TEXT NOT NULL       -- UTC timestamp
);
```

Stale `pending` rows from crashed runs are automatically deleted on startup so those deposits are retried.

### HBAR → Algo (`receipts.db`)

```sql
CREATE TABLE receipts (
    deposit_id_hex  TEXT PRIMARY KEY,  -- hex of 32-byte deposit ID from log
    status          TEXT NOT NULL,     -- released | exceeds_max | not_opted_in | already_on_chain
    hedera_ts       TEXT,              -- Hedera consensus timestamp of deposit log
    hedera_contract TEXT,              -- Hedera contract ID
    algo_txid       TEXT,              -- Algorand transaction ID (NULL if not released)
    receiver        TEXT NOT NULL,     -- Algorand destination address
    amount          INTEGER NOT NULL,  -- base units
    created_at      TEXT NOT NULL      -- UTC timestamp
);
```

---

## On-chain contract interfaces

### Algorand escrow contract (both directions)

The Algorand escrow app must:

**Emit on deposit (Algo → HBAR side):**
```
log(LOG_PREFIX + evm_receiver_bytes(20) + amount_u64_be(8) + deposit_id_bytes(32))
```

**Accept withdrawal (HBAR → Algo side):**
```
withdraw_v2(deposit_id: bytes[32], receiver: bytes[32], amount: uint64)
```
- `deposit_id` is used as a box key for idempotency — the contract should assert the box does not already exist before releasing.
- `receiver` is the 32-byte Algorand public key of the recipient.
- The contract transfers `amount` of the token ASA to `receiver`.

### Hedera deposit contract

The Hedera EVM contract must emit a log with packed data:
```solidity
emit BridgeDeposit(abi.encodePacked(HBAR_LOG_PREFIX, deposit_id, algo_receiver, amount));
```
Where:
- `HBAR_LOG_PREFIX` — UTF-8 bytes matching `HBAR_LOG_PREFIX` config var
- `deposit_id` — `bytes32` unique ID for replay protection
- `algo_receiver` — `bytes32` Algorand 32-byte public key of destination
- `amount` — `uint64` big-endian amount in base units

---

## Technical notes

### Java requirement (Algo → HBAR only)

`hedera-sdk-py` is a thin Python wrapper around the Hedera Java SDK via `pyjnius`. Java 17 must be installed and `JAVA_HOME` set. The systemd service file sets `JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64` — adjust for your distribution.

The `algo_to_hbar` relayer writes to `relayer.log` in addition to stdout/journal because the JVM can occasionally swallow stdout lines under load.

### HBAR → Algo: Mirror Node timestamp cursor

The cursor uses Hedera's `seconds.nanos` timestamp format (e.g. `1710000000.123456789`). The relayer uses `gte:` (inclusive) when querying, so a deposit at exactly the cursor timestamp is always re-fetched — idempotency is handled by the SQLite receipt check, not by strict cursor exclusion.

When a deposit is held for retry (receiver not opted-in, escrow insufficient), the cursor is stepped back 1 nanosecond to `ts_decrement(retry_hold_ts)` so the deposit will be re-fetched on the next poll.

### Concurrent instance protection (Algo → HBAR)

`reserve_deposit()` issues `INSERT OR IGNORE INTO receipts ... status='pending'`. Only the instance whose INSERT has `rowcount == 1` owns the deposit and proceeds to send. This prevents double-sends if two relayer processes run simultaneously.
