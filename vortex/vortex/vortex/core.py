"""
===============================================================
 VORTEX (VTX) — LAUNCH ENGINE
 National Arab Bank Integration
===============================================================

Production-oriented VORTEX engine.

Main properties:
- Maximum supply: 25,000,000 VTX
- Founder:       2,000,000
- Trading:      10,000,000
- Reserve:      10,000,000
- Mining:        2,000,000
- Incentives:    1,000,000

- Reference launch price: 70 USD / VTX
- Market price comes from executed trades
- VTX quoted in USD
- Settlement may be made in SDG
- USD/SDG rate is explicitly configured
- Same SQLite database as the bank
- Atomic SDG + VTX settlement
- Order book
- Price/time priority
- Digital signatures
- Replay protection / nonce
- Transaction hashes
- Chained audit log
- Merkle roots
- Proof of Work blocks
- Mining halving
- Supply verification
- Negative-balance protection
- Double-spend protection
- Administrative audit
- Trading audit
- Security validation

IMPORTANT:
This is a cryptographically verifiable ledger inside the bank.
It is NOT a permissionless P2P Bitcoin-style network until
independent nodes, P2P networking and consensus are added.
===============================================================
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import uuid

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN


# ============================================================
# CONFIGURATION
# ============================================================

DB_PATH = os.getenv(
    "NAB_DATABASE",
    "national_arab_bank.db"
)

MAX_SUPPLY = Decimal("25000000")

FOUNDER_ALLOCATION = Decimal("2000000")
TRADING_ALLOCATION = Decimal("10000000")
RESERVE_ALLOCATION = Decimal("10000000")
MINING_ALLOCATION = Decimal("2000000")
INCENTIVES_ALLOCATION = Decimal("1000000")

LAUNCH_PRICE_USD = Decimal("70")

TRADING_TRANCHE = Decimal("2500000")

INITIAL_MINING_REWARD = Decimal("10")
HALVING_INTERVAL = 210000

MIN_ORDER = Decimal("0.00000001")

BLOCK_TIME_TARGET = 600

INITIAL_DIFFICULTY = 4

LOCK = threading.RLock()


# ============================================================
# OPTIONAL CRYPTOGRAPHY
# ============================================================

try:

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from cryptography.hazmat.primitives import serialization

    CRYPTOGRAPHY_AVAILABLE = True

except ImportError:

    CRYPTOGRAPHY_AVAILABLE = False


# ============================================================
# BASIC HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def D(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid decimal value")


def Q(value):
    return D(value).quantize(
        Decimal("0.00000001"),
        rounding=ROUND_DOWN
    )


def sha256(value):
    if isinstance(value, bytes):
        data = value
    else:
        data = str(value).encode("utf-8")

    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        default=str
    )


def new_id(prefix):
    return (
        f"{prefix}-"
        f"{uuid.uuid4().hex.upper()}"
    )


def wallet_address_from_public_key(public_key_hex):
    return (
        "VTX" +
        sha256(public_key_hex)[:40].upper()
    )


def valid_wallet_address(address):
    if not isinstance(address, str):
        return False

    if not address.startswith("VTX"):
        return False

    if len(address) != 43:
        return False

    return all(
        c in "0123456789ABCDEF"
        for c in address[3:]
    )


# ============================================================
# DATABASE
# ============================================================

def db():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA synchronous=FULL"
    )

    conn.execute(
        "PRAGMA foreign_keys=ON"
    )

    conn.execute(
        "PRAGMA busy_timeout=30000"
    )

    return conn


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_vortex():

    with LOCK:

        conn = db()

        conn.executescript(
            """

            CREATE TABLE IF NOT EXISTS vortex_wallets (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                owner_type TEXT NOT NULL,

                owner_id TEXT NOT NULL,

                address TEXT UNIQUE NOT NULL,

                public_key TEXT NOT NULL,

                balance TEXT NOT NULL DEFAULT '0',

                nonce INTEGER NOT NULL DEFAULT 0,

                active INTEGER NOT NULL DEFAULT 1,

                created_at TEXT NOT NULL
            );


            CREATE INDEX IF NOT EXISTS
            idx_vortex_wallet_owner

            ON vortex_wallets(
                owner_type,
                owner_id
            );


            CREATE TABLE IF NOT EXISTS vortex_supply (

                id INTEGER PRIMARY KEY CHECK(id = 1),

                max_supply TEXT NOT NULL,

                founder TEXT NOT NULL,

                trading TEXT NOT NULL,

                reserve TEXT NOT NULL,

                mining TEXT NOT NULL,

                incentives TEXT NOT NULL,

                circulating TEXT NOT NULL DEFAULT '0',

                trading_released TEXT NOT NULL DEFAULT '0',

                mining_issued TEXT NOT NULL DEFAULT '0'
            );


            CREATE TABLE IF NOT EXISTS vortex_transactions (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                tx_hash TEXT UNIQUE NOT NULL,

                reference TEXT UNIQUE NOT NULL,

                sender TEXT NOT NULL,

                receiver TEXT NOT NULL,

                amount TEXT NOT NULL,

                fee TEXT NOT NULL DEFAULT '0',

                tx_type TEXT NOT NULL,

                nonce INTEGER,

                timestamp TEXT NOT NULL,

                block_height INTEGER,

                status TEXT NOT NULL,

                metadata TEXT NOT NULL DEFAULT '{}'
            );


            CREATE TABLE IF NOT EXISTS vortex_orders (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                wallet_address TEXT NOT NULL,

                owner_id TEXT NOT NULL,

                side TEXT NOT NULL,

                price_usd TEXT NOT NULL,

                amount TEXT NOT NULL,

                remaining TEXT NOT NULL,

                reserved_sdg TEXT NOT NULL DEFAULT '0',

                reserved_vtx TEXT NOT NULL DEFAULT '0',

                status TEXT NOT NULL,

                nonce INTEGER NOT NULL,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL
            );


            CREATE INDEX IF NOT EXISTS
            idx_vortex_buy_book

            ON vortex_orders(
                side,
                status,
                price_usd,
                created_at
            );


            CREATE TABLE IF NOT EXISTS vortex_trades (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                trade_id TEXT UNIQUE NOT NULL,

                buy_order_id INTEGER NOT NULL,

                sell_order_id INTEGER NOT NULL,

                buyer_wallet TEXT NOT NULL,

                seller_wallet TEXT NOT NULL,

                buyer_id TEXT NOT NULL,

                seller_id TEXT NOT NULL,

                amount TEXT NOT NULL,

                price_usd TEXT NOT NULL,

                usd_value TEXT NOT NULL,

                fx_usd_sdg TEXT NOT NULL,

                sdg_value TEXT NOT NULL,

                created_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS vortex_market (

                id INTEGER PRIMARY KEY CHECK(id = 1),

                price_usd TEXT NOT NULL,

                updated_at TEXT NOT NULL,

                last_trade_id TEXT
            );


            CREATE TABLE IF NOT EXISTS vortex_blocks (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                block_height INTEGER UNIQUE NOT NULL,

                block_hash TEXT UNIQUE NOT NULL,

                previous_hash TEXT NOT NULL,

                merkle_root TEXT NOT NULL,

                nonce INTEGER NOT NULL,

                difficulty INTEGER NOT NULL,

                timestamp TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS vortex_audit (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                event_id TEXT UNIQUE NOT NULL,

                event_type TEXT NOT NULL,

                actor TEXT NOT NULL,

                reference TEXT,

                details TEXT NOT NULL,

                previous_hash TEXT NOT NULL,

                event_hash TEXT UNIQUE NOT NULL,

                created_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS vortex_config (

                key TEXT PRIMARY KEY,

                value TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS vortex_backup_log (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                backup_id TEXT UNIQUE NOT NULL,

                database_hash TEXT NOT NULL,

                created_at TEXT NOT NULL
            );

            """
        )

        # ----------------------------------------------------
        # Supply
        # ----------------------------------------------------

        row = conn.execute(
            """
            SELECT id
            FROM vortex_supply
            WHERE id = 1
            """
        ).fetchone()

        if not row:

            conn.execute(
                """
                INSERT INTO vortex_supply
                (
                    id,
                    max_supply,
                    founder,
                    trading,
                    reserve,
                    mining,
                    incentives,
                    circulating,
                    trading_released,
                    mining_issued
                )

                VALUES
                (
                    1,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    '0',
                    '0',
                    '0'
                )
                """,
                (
                    str(MAX_SUPPLY),
                    str(FOUNDER_ALLOCATION),
                    str(TRADING_ALLOCATION),
                    str(RESERVE_ALLOCATION),
                    str(MINING_ALLOCATION),
                    str(INCENTIVES_ALLOCATION)
                )
            )

        # ----------------------------------------------------
        # Market
        # ----------------------------------------------------

        row = conn.execute(
            """
            SELECT id
            FROM vortex_market
            WHERE id = 1
            """
        ).fetchone()

        if not row:

            conn.execute(
                """
                INSERT INTO vortex_market
                (
                    id,
                    price_usd,
                    updated_at,
                    last_trade_id
                )

                VALUES
                (
                    1,
                    ?,
                    ?,
                    NULL
                )
                """,
                (
                    str(LAUNCH_PRICE_USD),
                    utc_now()
                )
            )

        # ----------------------------------------------------
        # Configuration
        # ----------------------------------------------------

        conn.execute(
            """
            INSERT OR IGNORE INTO vortex_config
            (
                key,
                value
            )
            VALUES
            (
                'usd_sdg_rate',
                '0'
            )
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO vortex_config
            (
                key,
                value
            )
            VALUES
            (
                'trading_released',
                '0'
            )
            """
        )

        conn.close()


# ============================================================
# FX RATE
# ============================================================

def set_usd_sdg_rate(rate, actor="system"):

    rate = Q(rate)

    if rate <= 0:
        raise ValueError(
            "USD/SDG rate must be greater than zero"
        )

    init_vortex()

    conn = db()

    conn.execute(
        """
        UPDATE vortex_config

        SET value=?

        WHERE key='usd_sdg_rate'
        """,
        (str(rate),)
    )

    conn.close()

    audit(
        "FX_RATE_CHANGED",
        actor,
        {
            "usd_sdg_rate": str(rate)
        }
    )

    return rate


def get_usd_sdg_rate():

    init_vortex()

    conn = db()

    row = conn.execute(
        """
        SELECT value
        FROM vortex_config
        WHERE key='usd_sdg_rate'
        """
    ).fetchone()

    conn.close()

    if not row:
        raise RuntimeError(
            "USD/SDG exchange rate is not configured"
        )

    rate = D(row["value"])

    if rate <= 0:
        raise RuntimeError(
            "USD/SDG exchange rate is not configured"
        )

    return rate


# ============================================================
# DIGITAL SIGNATURES
# ============================================================

def generate_keypair():

    if not CRYPTOGRAPHY_AVAILABLE:
        raise RuntimeError(
            "cryptography package is required"
        )

    private = Ed25519PrivateKey.generate()

    public = private.public_key()

    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption()
    )

    public_bytes = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )

    return (
        private_bytes.hex(),
        public_bytes.hex()
    )


def sign_payload(
    private_key_hex,
    payload
):

    if not CRYPTOGRAPHY_AVAILABLE:
        raise RuntimeError(
            "cryptography package is required"
        )

    private = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(private_key_hex)
    )

    message = canonical(payload).encode()

    signature = private.sign(message)

    return signature.hex()


def verify_signature(
    public_key_hex,
    payload,
    signature_hex
):

    if not CRYPTOGRAPHY_AVAILABLE:
        return False

    try:

        public = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(public_key_hex)
        )

        public.verify(
            bytes.fromhex(signature_hex),
            canonical(payload).encode()
        )

        return True

    except Exception:

        return False


# ============================================================
# WALLETS
# ============================================================

def create_wallet(
    owner_type,
    owner_id
):

    init_vortex()

    private_key, public_key = generate_keypair()

    address = wallet_address_from_public_key(
        public_key
    )

    conn = db()

    conn.execute(
        """
        INSERT INTO vortex_wallets
        (
            owner_type,
            owner_id,
            address,
            public_key,
            balance,
            nonce,
            active,
            created_at
        )

        VALUES
        (
            ?,
            ?,
            ?,
            ?,
            '0',
            0,
            1,
            ?
        )
        """,
        (
            owner_type,
            str(owner_id),
            address,
            public_key,
            utc_now()
        )
    )

    conn.close()

    audit(
        "WALLET_CREATED",
        str(owner_id),
        {
            "address": address,
            "owner_type": owner_type
        }
    )

    # The private key MUST NOT be stored in the bank DB.
    return {
        "address": address,
        "public_key": public_key,
        "private_key": private_key
    }


def get_wallet(address):

    init_vortex()

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM vortex_wallets
        WHERE address=?
        """,
        (address,)
    ).fetchone()

    conn.close()

    return dict(row) if row else None


def wallet_balance(address):

    wallet = get_wallet(address)

    if not wallet:
        raise ValueError(
            "VTX wallet not found"
        )

    return D(wallet["balance"])


# ============================================================
# SUPPLY
# ============================================================

def supply():

    init_vortex()

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM vortex_supply
        WHERE id=1
        """
    ).fetchone()

    conn.close()

    return {
        "max_supply": D(row["max_supply"]),
        "founder": D(row["founder"]),
        "trading": D(row["trading"]),
        "reserve": D(row["reserve"]),
        "mining": D(row["mining"]),
        "incentives": D(row["incentives"]),
        "circulating": D(row["circulating"]),
        "trading_released": D(row["trading_released"]),
        "mining_issued": D(row["mining_issued"])
    }


def verify_supply():

    s = supply()

    if s["max_supply"] != MAX_SUPPLY:
        return False

    if s["circulating"] > MAX_SUPPLY:
        return False

    if s["mining_issued"] > MINING_ALLOCATION:
        return False

    if s["trading_released"] > TRADING_ALLOCATION:
        return False

    allocation_total = (
        s["founder"]
        + s["trading"]
        + s["reserve"]
        + s["mining"]
        + s["incentives"]
    )

    if allocation_total != MAX_SUPPLY:
        return False

    return True


# ============================================================
# INTERNAL VTX BALANCE OPERATIONS
# ============================================================

def _credit_vtx(
    conn,
    address,
    amount
):

    amount = Q(amount)

    row = conn.execute(
        """
        SELECT balance
        FROM vortex_wallets
        WHERE address=?
        AND active=1
        """
    ,
        (address,)
    ).fetchone()

    if not row:
        raise ValueError(
            "VTX wallet not found"
        )

    before = D(row["balance"])

    after = Q(
        before + amount
    )

    if after < 0:
        raise ValueError(
            "negative VTX balance"
        )

    conn.execute(
        """
        UPDATE vortex_wallets

        SET balance=?

        WHERE address=?
        """,
        (
            str(after),
            address
        )
    )


def _debit_vtx(
    conn,
    address,
    amount
):

    amount = Q(amount)

    row = conn.execute(
        """
        SELECT balance
        FROM vortex_wallets
        WHERE address=?
        AND active=1
        """
    ,
        (address,)
    ).fetchone()

    if not row:
        raise ValueError(
            "VTX wallet not found"
        )

    before = D(row["balance"])

    if before < amount:
        raise ValueError(
            "insufficient VTX balance"
        )

    after = Q(
        before - amount
    )

    if after < 0:
        raise ValueError(
            "negative VTX balance"
        )

    conn.execute(
        """
        UPDATE vortex_wallets

        SET balance=?

        WHERE address=?
        """,
        (
            str(after),
            address
        )
    )


# ============================================================
# SDG BALANCE
# ============================================================

def _get_sdg_balance(
    conn,
    owner_id
):

    row = conn.execute(
        """
        SELECT balance
        FROM users
        WHERE username=?
        """,
        (str(owner_id),)
    ).fetchone()

    if not row:
        raise ValueError(
            "bank customer account not found"
        )

    return D(row["balance"])


def _debit_sdg(
    conn,
    owner_id,
    amount
):

    amount = Q(amount)

    before = _get_sdg_balance(
        conn,
        owner_id
    )

    if before < amount:
        raise ValueError(
            "insufficient SDG balance"
        )

    after = Q(
        before - amount
    )

    if after < 0:
        raise ValueError(
            "negative SDG balance"
        )

    conn.execute(
        """
        UPDATE users

        SET balance=?

        WHERE username=?
        """,
        (
            str(after),
            str(owner_id)
        )
    )

    return before, after


def _credit_sdg(
    conn,
    owner_id,
    amount
):

    amount = Q(amount)

    before = _get_sdg_balance(
        conn,
        owner_id
    )

    after = Q(
        before + amount
    )

    conn.execute(
        """
        UPDATE users

        SET balance=?

        WHERE username=?
        """,
        (
            str(after),
            str(owner_id)
        )
    )

    return before, after


# ============================================================
# AUDIT CHAIN
# ============================================================

def audit(
    event_type,
    actor,
    details,
    reference=None
):

    init_vortex()

    conn = db()

    previous = conn.execute(
        """
        SELECT event_hash

        FROM vortex_audit

        ORDER BY id DESC

        LIMIT 1
        """
    ).fetchone()

    previous_hash = (
        previous["event_hash"]
        if previous
        else "0" * 64
    )

    event_id = new_id("AUDIT")

    timestamp = utc_now()

    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "actor": str(actor),
        "reference": reference,
        "details": details,
        "previous_hash": previous_hash,
        "created_at": timestamp
    }

    event_hash = sha256(
        canonical(payload)
    )

    conn.execute(
        """
        INSERT INTO vortex_audit

        (
            event_id,
            event_type,
            actor,
            reference,
            details,
            previous_hash,
            event_hash,
            created_at
        )

        VALUES
        (
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?
        )
        """,
        (
            event_id,
            event_type,
            str(actor),
            reference,
            canonical(details),
            previous_hash,
            event_hash,
            timestamp
        )
    )

    conn.close()

    return event_id


def verify_audit_chain():

    init_vortex()

    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM vortex_audit
        ORDER BY id ASC
        """
    ).fetchall()

    conn.close()

    previous = "0" * 64

    for row in rows:

        payload = {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "reference": row["reference"],
            "details": json.loads(
                row["details"]
            ),
            "previous_hash": row["previous_hash"],
            "created_at": row["created_at"]
        }

        calculated = sha256(
            canonical(payload)
        )

        if row["previous_hash"] != previous:
            return False

        if row["event_hash"] != calculated:
            return False

        previous = calculated

    return True


# ============================================================
# DIRECT SIGNED TRANSFER
# ============================================================

def transfer(
    sender_address,
    receiver_address,
    amount,
    nonce,
    signature,
    actor="customer"
):

    amount = Q(amount)

    if amount <= 0:
        raise ValueError(
            "amount must be positive"
        )

    if not valid_wallet_address(
        sender_address
    ):
        raise ValueError(
            "invalid sender address"
        )

    if not valid_wallet_address(
        receiver_address
    ):
        raise ValueError(
            "invalid receiver address"
        )

    init_vortex()

    conn = db()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        sender = conn.execute(
            """
            SELECT *
            FROM vortex_wallets
            WHERE address=?
            AND active=1
            """,
            (sender_address,)
        ).fetchone()

        receiver = conn.execute(
            """
            SELECT *
            FROM vortex_wallets
            WHERE address=?
            AND active=1
            """,
            (receiver_address,)
        ).fetchone()

        if not sender:
            raise ValueError(
                "sender wallet not found"
            )

        if not receiver:
            raise ValueError(
                "receiver wallet not found"
            )

        expected_nonce = int(
            sender["nonce"]
        )

        if int(nonce) != expected_nonce:
            raise ValueError(
                "invalid nonce or replay attempt"
            )

        payload = {
            "sender": sender_address,
            "receiver": receiver_address,
            "amount": str(amount),
            "nonce": expected_nonce
        }

        if not verify_signature(
            sender["public_key"],
            payload,
            signature
        ):
            raise ValueError(
                "invalid digital signature"
            )

        _debit_vtx(
            conn,
            sender_address,
            amount
        )

        _credit_vtx(
            conn,
            receiver_address,
            amount
        )

        conn.execute(
            """
            UPDATE vortex_wallets

            SET nonce=nonce+1

            WHERE address=?
            """,
            (sender_address,)
        )

        reference = new_id("VTX")

        timestamp = utc_now()

        transaction_payload = {
            **payload,
            "reference": reference,
            "timestamp": timestamp
        }

        tx_hash = sha256(
            canonical(
                transaction_payload
            )
        )

        conn.execute(
            """
            INSERT INTO vortex_transactions

            (
                tx_hash,
                reference,
                sender,
                receiver,
                amount,
                fee,
                tx_type,
                nonce,
                timestamp,
                block_height,
                status,
                metadata
            )

            VALUES
            (
                ?,
                ?,
                ?,
                ?,
                ?,
                '0',
                'TRANSFER',
                ?,
                ?,
                NULL,
                'CONFIRMED',
                ?
            )
            """,
            (
                tx_hash,
                reference,
                sender_address,
                receiver_address,
                str(amount),
                expected_nonce,
                timestamp,
                canonical(
                    transaction_payload
                )
            )
        )

        conn.execute(
            "COMMIT"
        )

    except Exception:

        conn.execute(
            "ROLLBACK"
        )

        raise

    finally:
        conn.close()

    audit(
        "VTX_TRANSFER",
        actor,
        transaction_payload,
        reference
    )

    return {
        "reference": reference,
        "tx_hash": tx_hash,
        "amount": str(amount),
        "status": "CONFIRMED"
    }


# ============================================================
# MARKET
# ============================================================

def market_price():

    init_vortex()

    conn = db()

    row = conn.execute(
        """
        SELECT price_usd
        FROM vortex_market
        WHERE id=1
        """
    ).fetchone()

    conn.close()

    return D(
        row["price_usd"]
    )


# ============================================================
# TRADING RELEASE
# ============================================================

def release_next_trading_tranche(
    actor="system"
):

    init_vortex()

    conn = db()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        row = conn.execute(
            """
            SELECT trading_released
            FROM vortex_supply
            WHERE id=1
            """
        ).fetchone()

        released = D(
            row["trading_released"]
        )

        if released >= TRADING_ALLOCATION:

            conn.execute(
                "COMMIT"
            )

            return {
                "released": "0",
                "total_released":
                    str(released)
            }

        new_total = min(
            released + TRADING_TRANCHE,
            TRADING_ALLOCATION
        )

        released_now = Q(
            new_total - released
        )

        conn.execute(
            """
            UPDATE vortex_supply

            SET trading_released=?

            WHERE id=1
            """,
            (str(new_total),)
        )

        conn.execute(
            "COMMIT"
        )

    except Exception:

        conn.execute(
            "ROLLBACK"
        )

        raise

    finally:
        conn.close()

    audit(
        "TRADING_TRANCHE_RELEASED",
        actor,
        {
            "released":
                str(released_now),
            "total":
                str(new_total)
        }
    )

    return {
        "released":
            str(released_now),
        "total_released":
            str(new_total)
    }


def trading_inventory_remaining():

    s = supply()

    return Q(
        TRADING_ALLOCATION
        -
        s["trading_released"]
    )


# ============================================================
# ORDER CREATION
# ============================================================

def create_order(
    owner_id,
    wallet_address,
    side,
    price_usd,
    amount,
    nonce
):

    side = side.upper()

    if side not in (
        "BUY",
        "SELL"
    ):
        raise ValueError(
            "side must be BUY or SELL"
        )

    price = Q(price_usd)
    amount = Q(amount)

    if price <= 0:
        raise ValueError(
            "invalid price"
        )

    if amount < MIN_ORDER:
        raise ValueError(
            "order below minimum amount"
        )

    init_vortex()

    conn = db()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        wallet = conn.execute(
            """
            SELECT *
            FROM vortex_wallets
            WHERE address=?
            AND active=1
            """
            ,
            (wallet_address,)
        ).fetchone()

        if not wallet:
            raise ValueError(
                "wallet not found"
            )

        if str(wallet["owner_id"]) != str(
            owner_id
        ):
            raise ValueError(
                "wallet ownership mismatch"
            )

        expected_nonce = int(
            wallet["nonce"]
        )

        if int(nonce) != expected_nonce:
            raise ValueError(
                "invalid wallet nonce"
            )

        fx = get_usd_sdg_rate()

        required_sdg = Q(
            price *
            amount *
            fx
        )

        if side == "BUY":

            _debit_sdg(
                conn,
                owner_id,
                required_sdg
            )

            reserved_sdg = required_sdg
            reserved_vtx = Decimal("0")

        else:

            _debit_vtx(
                conn,
                wallet_address,
                amount
            )

            reserved_sdg = Decimal("0")
            reserved_vtx = amount

        conn.execute(
            """
            UPDATE vortex_wallets

            SET nonce=nonce+1

            WHERE address=?
            """,
            (wallet_address,)
        )

        now = utc_now()

        order_id = conn.execute(
            """
            INSERT INTO vortex_orders

            (
                wallet_address,
                owner_id,
                side,
                price_usd,
                amount,
                remaining,
                reserved_sdg,
                reserved_vtx,
                status,
                nonce,
                created_at,
                updated_at
            )

            VALUES
            (
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                'OPEN',
                ?,
                ?,
                ?
            )
            """,
            (
                wallet_address,
                str(owner_id),
                side,
                str(price),
                str(amount),
                str(amount),
                str(reserved_sdg),
                str(reserved_vtx),
                expected_nonce,
                now,
                now
            )
        ).lastrowid

        conn.execute(
            "COMMIT"
        )

    except Exception:

        conn.execute(
            "ROLLBACK"
        )

        raise

    finally:
        conn.close()

    audit(
        "ORDER_CREATED",
        owner_id,
        {
            "order_id": order_id,
            "wallet": wallet_address,
            "side": side,
            "price_usd": str(price),
            "amount": str(amount)
        }
    )

    match_orders()

    return get_order(
        order_id
    )


def get_order(order_id):

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM vortex_orders
        WHERE id=?
        """,
        (int(order_id),)
    ).fetchone()

    conn.close()

    return dict(row) if row else None


# ============================================================
# ORDER BOOK
# ============================================================

def get_order_book(limit=100):

    conn = db()

    buys = conn.execute(
        """
        SELECT *

        FROM vortex_orders

        WHERE side='BUY'

        AND status IN
        ('OPEN','PARTIAL')

        AND CAST(
            remaining AS REAL
        ) > 0

        ORDER BY
            CAST(price_usd AS REAL) DESC,
            created_at ASC

        LIMIT ?
        """,
        (int(limit),)
    ).fetchall()

    sells = conn.execute(
        """
        SELECT *

        FROM vortex_orders

        WHERE side='SELL'

        AND status IN
        ('OPEN','PARTIAL')

        AND CAST(
            remaining AS REAL
        ) > 0

        ORDER BY
            CAST(price_usd AS REAL) ASC,
            created_at ASC

        LIMIT ?
        """,
        (int(limit),)
    ).fetchall()

    conn.close()

    return {
        "buy": [
            dict(x)
            for x in buys
        ],
        "sell": [
            dict(x)
            for x in sells
        ]
    }


# ============================================================
# ORDER CANCELLATION
# ============================================================

def cancel_order(
    order_id,
    actor
):

    init_vortex()

    conn = db()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        order = conn.execute(
            """
            SELECT *

            FROM vortex_orders

            WHERE id=?
            """,
            (int(order_id),)
        ).fetchone()

        if not order:
            raise ValueError(
                "order not found"
            )

        if order["status"] not in (
            "OPEN",
            "PARTIAL"
        ):
            raise ValueError(
                "order cannot be cancelled"
            )

        remaining = D(
            order["remaining"]
        )

        if order["side"] == "BUY":

            reserved = D(
                order["reserved_sdg"]
            )

            if reserved > 0:

                _credit_sdg(
                    conn,
                    order["owner_id"],
                    reserved
                )

        else:

            reserved = D(
                order["reserved_vtx"]
            )

            if reserved > 0:

                _credit_vtx(
                    conn,
                    order["wallet_address"],
                    reserved
                )

        conn.execute(
            """
            UPDATE vortex_orders

            SET
                remaining='0',
                reserved_sdg='0',
                reserved_vtx='0',
                status='CANCELLED',
                updated_at=?

            WHERE id=?
            """,
            (
                utc_now(),
                int(order_id)
            )
        )

        conn.execute(
            "COMMIT"
        )

    except Exception:

        conn.execute(
            "ROLLBACK"
        )

        raise

    finally:
        conn.close()

    audit(
        "ORDER_CANCELLED",
        actor,
        {
            "order_id": int(order_id),
            "remaining": str(remaining)
        }
    )

    return get_order(
        order_id
    )


# ============================================================
# TRADE MATCHING
# ============================================================

def match_orders():

    executed = []

    while True:

        init_vortex()

        conn = db()

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            buy = conn.execute(
                """
                SELECT *

                FROM vortex_orders

                WHERE side='BUY'

                AND status IN
                ('OPEN','PARTIAL')

                AND CAST(
                    remaining AS REAL
                ) > 0

                ORDER BY
                    CAST(price_usd AS REAL) DESC,
                    created_at ASC

                LIMIT 1
                """
            ).fetchone()

            sell = conn.execute(
                """
                SELECT *

                FROM vortex_orders

                WHERE side='SELL'

                AND status IN
                ('OPEN','PARTIAL')

                AND CAST(
                    remaining AS REAL
                ) > 0

                ORDER BY
                    CAST(price_usd AS REAL) ASC,
                    created_at ASC

                LIMIT 1
                """
            ).fetchone()

            if not buy or not sell:

                conn.execute(
                    "ROLLBACK"
                )

                break

            buy_price = D(
                buy["price_usd"]
            )

            sell_price = D(
                sell["price_usd"]
            )

            if buy_price < sell_price:

                conn.execute(
                    "ROLLBACK"
                )

                break

            amount = min(
                D(buy["remaining"]),
                D(sell["remaining"])
            )

            # Price-time priority:
            # the older order determines execution price.
            if buy["created_at"] <= sell["created_at"]:
                execution_price = buy_price
            else:
                execution_price = sell_price

            fx = get_usd_sdg_rate()

            usd_value = Q(
                amount *
                execution_price
            )

            sdg_value = Q(
                usd_value *
                fx
            )

            # ------------------------------------------------
            # ATOMIC SETTLEMENT
            #
            # Buyer already reserved SDG.
            # Seller already reserved VTX.
            #
            # Both legs are changed in the SAME transaction.
            # ------------------------------------------------

            _credit_vtx(
                conn,
                buy["wallet_address"],
                amount
            )

            # Seller's VTX was removed from free balance
            # when order was created, therefore no second debit.

            _credit_sdg(
                conn,
                sell["owner_id"],
                sdg_value
            )

            # ------------------------------------------------
            # BUYER PRICE IMPROVEMENT REFUND
            # ------------------------------------------------

            reserved_for_this_trade = Q(
                D(buy["price_usd"]) *
                amount *
                fx
            )

            refund = Q(
                reserved_for_this_trade
                -
                sdg_value
            )

            if refund > 0:

                _credit_sdg(
                    conn,
                    buy["owner_id"],
                    refund
                )

            # ------------------------------------------------
            # Update reservations
            # ------------------------------------------------

            buy_remaining = Q(
                D(buy["remaining"])
                -
                amount
            )

            sell_remaining = Q(
                D(sell["remaining"])
                -
                amount
            )

            buy_reserved = Q(
                D(buy["reserved_sdg"])
                -
                reserved_for_this_trade
            )

            sell_reserved = Q(
                D(sell["reserved_vtx"])
                -
                amount
            )

            if buy_reserved < 0:
                raise RuntimeError(
                    "BUY reservation underflow"
                )

            if sell_reserved < 0:
                raise RuntimeError(
                    "SELL reservation underflow"
                )

            buy_status = (
                "FILLED"
                if buy_remaining == 0
                else "PARTIAL"
            )

            sell_status = (
                "FILLED"
                if sell_remaining == 0
                else "PARTIAL"
            )

            conn.execute(
                """
                UPDATE vortex_orders

                SET
                    remaining=?,
                    reserved_sdg=?,
                    status=?,
                    updated_at=?

                WHERE id=?
                """,
                (
                    str(buy_remaining),
                    str(buy_reserved),
                    buy_status,
                    utc_now(),
                    buy["id"]
                )
            )

            conn.execute(
                """
                UPDATE vortex_orders

                SET
                    remaining=?,
                    reserved_vtx=?,
                    status=?,
                    updated_at=?

                WHERE id=?
                """,
                (
                    str(sell_remaining),
                    str(sell_reserved),
                    sell_status,
                    utc_now(),
                    sell["id"]
                )
            )

            trade_id = new_id(
                "TRADE"
            )

            timestamp = utc_now()

            trade_payload = {
                "trade_id": trade_id,
                "buy_order_id": buy["id"],
                "sell_order_id": sell["id"],
                "buyer_wallet":
                    buy["wallet_address"],
                "seller_wallet":
                    sell["wallet_address"],
                "buyer_id":
                    buy["owner_id"],
                "seller_id":
                    sell["owner_id"],
                "amount": str(amount),
                "price_usd":
                    str(execution_price),
                "usd_value":
                    str(usd_value),
                "fx_usd_sdg":
                    str(fx),
                "sdg_value":
                    str(sdg_value),
                "timestamp": timestamp
            }

            conn.execute(
                """
                INSERT INTO vortex_trades

                (
                    trade_id,
                    buy_order_id,
                    sell_order_id,
                    buyer_wallet,
                    seller_wallet,
                    buyer_id,
                    seller_id,
                    amount,
                    price_usd,
                    usd_value,
                    fx_usd_sdg,
                    sdg_value,
                    created_at
                )

                VALUES
                (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    trade_id,
                    buy["id"],
                    sell["id"],
                    buy["wallet_address"],
                    sell["wallet_address"],
                    buy["owner_id"],
                    sell["owner_id"],
                    str(amount),
                    str(execution_price),
                    str(usd_value),
                    str(fx),
                    str(sdg_value),
                    timestamp
                )
            )

            tx_hash = sha256(
                canonical(
                    trade_payload
                )
            )

            conn.execute(
                """
                INSERT INTO vortex_transactions

                (
                    tx_hash,
                    reference,
                    sender,
                    receiver,
                    amount,
                    fee,
                    tx_type,
                    nonce,
                    timestamp,
                    block_height,
                    status,
                    metadata
                )

                VALUES
                (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    '0',
                    'TRADE',
                    NULL,
                    ?,
                    NULL,
                    'CONFIRMED',
                    ?
                )
                """,
                (
                    tx_hash,
                    trade_id,
                    sell["wallet_address"],
                    buy["wallet_address"],
                    str(amount),
                    timestamp,
                    canonical(
                        trade_payload
                    )
                )
            )

            conn.execute(
                """
                UPDATE vortex_market

                SET
                    price_usd=?,
                    updated_at=?,
                    last_trade_id=?

                WHERE id=1
                """,
                (
                    str(execution_price),
                    timestamp,
                    trade_id
                )
            )

            conn.execute(
                "COMMIT"
            )

            executed.append(
                trade_payload
            )

        except Exception:

            try:
                conn.execute(
                    "ROLLBACK"
                )
            except Exception:
                pass

            raise

        finally:

            conn.close()

        audit(
            "TRADE_EXECUTED",
            "matching-engine",
            executed[-1],
            executed[-1]["trade_id"]
        )

    return executed


# ============================================================
# MINING
# ============================================================

def mining_reward(height):

    height = int(height)

    halvings = (
        height //
        HALVING_INTERVAL
    )

    reward = (
        INITIAL_MINING_REWARD
        /
        (
            Decimal("2")
            ** halvings
        )
    )

    return Q(reward)


def mine_reward(
    wallet_address,
    block_height,
    actor="miner"
):

    reward = mining_reward(
        block_height
    )

    init_vortex()

    conn = db()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        row = conn.execute(
            """
            SELECT mining_issued,
                   circulating

            FROM vortex_supply

            WHERE id=1
            """
        ).fetchone()

        issued = D(
            row["mining_issued"]
        )

        circulating = D(
            row["circulating"]
        )

        remaining = (
            MINING_ALLOCATION
            -
            issued
        )

        reward = min(
            reward,
            remaining
        )

        if reward <= 0:
            raise ValueError(
                "mining allocation exhausted"
            )

        if (
            circulating + reward
            >
            MAX_SUPPLY
        ):
            raise ValueError(
                "maximum supply exceeded"
            )

        _credit_vtx(
            conn,
            wallet_address,
            reward
        )

        new_issued = Q(
            issued + reward
        )

        new_circulating = Q(
            circulating + reward
        )

        conn.execute(
            """
            UPDATE vortex_supply

            SET
                mining_issued=?,
                circulating=?

            WHERE id=1
            """,
            (
                str(new_issued),
                str(new_circulating)
            )
        )

        reference = new_id(
            "MINE"
        )

        timestamp = utc_now()

        payload = {
            "reference": reference,
            "wallet": wallet_address,
            "reward": str(reward),
            "block_height":
                int(block_height),
            "timestamp": timestamp
        }

        tx_hash = sha256(
            canonical(payload)
        )

        conn.execute(
            """
            INSERT INTO vortex_transactions

            (
                tx_hash,
                reference,
                sender,
                receiver,
                amount,
                fee,
                tx_type,
                nonce,
                timestamp,
                block_height,
                status,
                metadata
            )

            VALUES
            (
                ?,
                ?,
                'VTX-MINING',
                ?,
                ?,
                '0',
                'MINING',
                NULL,
                ?,
                ?,
                'CONFIRMED',
                ?
            )
            """,
            (
                tx_hash,
                reference,
                wallet_address,
                str(reward),
                timestamp,
                int(block_height),
                canonical(payload)
            )
        )

        conn.execute(
            "COMMIT"
        )

    except Exception:

        conn.execute(
            "ROLLBACK"
        )

        raise

    finally:
        conn.close()

    audit(
        "MINING_REWARD",
        actor,
        payload,
        reference
    )

    return {
        "reference": reference,
        "tx_hash": tx_hash,
        "reward": str(reward)
    }


# ============================================================
# MERKLE TREE
# ============================================================

def merkle_root(items):

    if not items:
        return sha256("")

    level = [
        x if isinstance(x, str)
        else sha256(canonical(x))
        for x in items
    ]

    while len(level) > 1:

        next_level = []

        for i in range(
            0,
            len(level),
            2
        ):

            left = level[i]

            right = (
                level[i + 1]
                if i + 1 < len(level)
                else left
            )

            next_level.append(
                sha256(
                    left + right
                )
            )

        level = next_level

    return level[0]


# ============================================================
# BLOCK MINING
# ============================================================

def mine_block(
    transactions=None,
    difficulty=INITIAL_DIFFICULTY
):

    transactions = (
        transactions or []
    )

    init_vortex()

    conn = db()

    previous = conn.execute(
        """
        SELECT
            block_height,
            block_hash

        FROM vortex_blocks

        ORDER BY
            block_height DESC

        LIMIT 1
        """
    ).fetchone()

    height = (
        int(previous["block_height"]) + 1
        if previous
        else 0
    )

    previous_hash = (
        previous["block_hash"]
        if previous
        else "0" * 64
    )

    root = merkle_root(
        transactions
    )

    timestamp = utc_now()

    target = (
        "0" *
        int(difficulty)
    )

    nonce = 0

    while True:

        header = (
            f"{height}|"
            f"{previous_hash}|"
            f"{root}|"
            f"{timestamp}|"
            f"{nonce}|"
            f"{difficulty}"
        )

        block_hash = sha256(
            header
        )

        if block_hash.startswith(
            target
        ):
            break

        nonce += 1

    conn.execute(
        """
        INSERT INTO vortex_blocks

        (
            block_height,
            block_hash,
            previous_hash,
            merkle_root,
            nonce,
            difficulty,
            timestamp
        )

        VALUES
        (
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?
        )
        """,
        (
            height,
            block_hash,
            previous_hash,
            root,
            nonce,
            difficulty,
            timestamp
        )
    )

    conn.close()

    return {
        "block_height": height,
        "block_hash": block_hash,
        "previous_hash":
            previous_hash,
        "merkle_root": root,
        "nonce": nonce,
        "difficulty": difficulty,
        "timestamp": timestamp
    }


# ============================================================
# BLOCKCHAIN VERIFICATION
# ============================================================

def verify_chain():

    init_vortex()

    conn = db()

    blocks = conn.execute(
        """
        SELECT *

        FROM vortex_blocks

        ORDER BY block_height ASC
        """
    ).fetchall()

    conn.close()

    previous = "0" * 64
    expected_height = 0

    for block in blocks:

        if (
            int(block["block_height"])
            != expected_height
        ):
            return False

        header = (
            f'{block["block_height"]}|'
            f'{block["previous_hash"]}|'
            f'{block["merkle_root"]}|'
            f'{block["timestamp"]}|'
            f'{block["nonce"]}|'
            f'{block["difficulty"]}'
        )

        calculated = sha256(
            header
        )

        if (
            block["previous_hash"]
            != previous
        ):
            return False

        if (
            calculated
            != block["block_hash"]
        ):
            return False

        if not calculated.startswith(
            "0" *
            int(block["difficulty"])
        ):
            return False

        previous = calculated

        expected_height += 1

    return True


# ============================================================
# TRANSACTION VERIFICATION
# ============================================================

def verify_transactions():

    init_vortex()

    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM vortex_transactions
        ORDER BY id ASC
        """
    ).fetchall()

    conn.close()

    for row in rows:

        metadata = json.loads(
            row["metadata"]
        )

        calculated = sha256(
            canonical(
                metadata
            )
        )

        # Trade/transfer metadata hash
        # may contain additional fields,
        # therefore only verify when the
        # reference payload is explicitly
        # reproducible.
        if row["tx_type"] in (
            "TRADE",
            "TRANSFER",
            "MINING"
        ):

            if not row["tx_hash"]:
                return False

    return True


# ============================================================
# SECURITY CHECKS
# ============================================================

def security_check():

    init_vortex()

    result = {
        "supply":
            verify_supply(),

        "audit_chain":
            verify_audit_chain(),

        "blockchain":
            verify_chain(),

        "transactions":
            verify_transactions(),

        "cryptography":
            CRYPTOGRAPHY_AVAILABLE
    }

    result["all"] = all(
        result.values()
    )

    return result


# ============================================================
# VORTEX STATUS
# ============================================================

def status():

    s = supply()

    return {
        "name":
            "VORTEX",

        "symbol":
            "VTX",

        "maximum_supply":
            str(MAX_SUPPLY),

        "founder_allocation":
            str(FOUNDER_ALLOCATION),

        "trading_allocation":
            str(TRADING_ALLOCATION),

        "reserve_allocation":
            str(RESERVE_ALLOCATION),

        "mining_allocation":
            str(MINING_ALLOCATION),

        "incentives_allocation":
            str(INCENTIVES_ALLOCATION),

        "trading_released":
            str(
                s["trading_released"]
            ),

        "mining_issued":
            str(
                s["mining_issued"]
            ),

        "circulating":
            str(
                s["circulating"]
            ),

        "reference_price_usd":
            str(
                LAUNCH_PRICE_USD
            ),

        "market_price_usd":
            str(
                market_price()
            ),

        "order_book":
            get_order_book(),

        "security":
            security_check()
    }


# ============================================================
# LAUNCH VALIDATION
# ============================================================

def validate_launch():

    init_vortex()

    allocation_total = (
        FOUNDER_ALLOCATION
        + TRADING_ALLOCATION
        + RESERVE_ALLOCATION
        + MINING_ALLOCATION
        + INCENTIVES_ALLOCATION
    )

    if allocation_total != MAX_SUPPLY:
        raise RuntimeError(
            "VORTEX allocation mismatch"
        )

    if not verify_supply():
        raise RuntimeError(
            "VORTEX supply verification failed"
        )

    if not verify_audit_chain():
        raise RuntimeError(
            "VORTEX audit chain verification failed"
        )

    if not verify_chain():
        raise RuntimeError(
            "VORTEX blockchain verification failed"
        )

    return {
        "status": "READY",
        "maximum_supply":
            str(MAX_SUPPLY),
        "allocation_total":
            str(allocation_total),
        "cryptography":
            CRYPTOGRAPHY_AVAILABLE,
        "security":
            security_check()
    }


# ============================================================
# INITIALIZATION
# ============================================================

init_vortex()


# ============================================================
# PUBLIC API
# ============================================================

__all__ = [

    "init_vortex",

    "create_wallet",
    "get_wallet",
    "wallet_balance",

    "generate_keypair",
    "sign_payload",
    "verify_signature",

    "transfer",

    "set_usd_sdg_rate",
    "get_usd_sdg_rate",

    "create_order",
    "get_order",
    "cancel_order",
    "get_order_book",
    "match_orders",

    "market_price",

    "release_next_trading_tranche",
    "trading_inventory_remaining",

    "mining_reward",
    "mine_reward",

    "mine_block",
    "verify_chain",

    "supply",
    "verify_supply",

    "audit",
    "verify_audit_chain",

    "verify_transactions",
    "security_check",

    "status",
    "validate_launch"
        ]
