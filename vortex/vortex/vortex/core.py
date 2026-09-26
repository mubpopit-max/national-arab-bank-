"""
VORTEX CORE
===========
التداول + التعدين + سجل الصفقات + الإصدار

Maximum Supply: 25,000,000 VTX
Mining Allocation: 2,000,000 VTX
Trading Allocation: 10,000,000 VTX
Reference Price: 70 USD
"""

import hashlib
import secrets
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from .database import get_vortex_db, now
from .blockchain import (
    calculate_merkle_root,
    calculate_block_hash,
    mine_block,
    verify_block,
)


# ============================================================
# CONSTANTS
# ============================================================

MAX_SUPPLY = Decimal("25000000")

FOUNDER_ALLOCATION = Decimal("2000000")
TRADING_ALLOCATION = Decimal("10000000")
RESERVE_ALLOCATION = Decimal("10000000")
MINING_ALLOCATION = Decimal("2000000")
INCENTIVES_ALLOCATION = Decimal("1000000")

REFERENCE_PRICE_USD = Decimal("70")

INITIAL_MINING_REWARD = Decimal("10")
HALVING_INTERVAL = 210000

MIN_ORDER_AMOUNT = Decimal("0.00000001")


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid numeric value")


def wallet_address():
    raw = "VTX" + secrets.token_hex(20).upper()
    checksum = hashlib.sha256(
        raw.encode()
    ).hexdigest()[:8].upper()

    return raw + checksum


def valid_address(address):
    if not isinstance(address, str):
        return False

    if not address.startswith("VTX"):
        return False

    if len(address) != 51:
        return False

    body = address[:-8]
    checksum = address[-8:]

    expected = hashlib.sha256(
        body.encode()
    ).hexdigest()[:8].upper()

    return checksum == expected


# ============================================================
# SUPPLY
# ============================================================

def supply_status():
    db = get_vortex_db()

    try:
        row = db.execute(
            """
            SELECT *
            FROM vortex_supply
            WHERE id = 1
            """
        ).fetchone()

        if not row:
            raise ValueError(
                "VORTEX supply not initialized"
            )

        return {
            "max_supply": str(MAX_SUPPLY),
            "founder": str(
                Decimal(str(row["founder"]))
            ),
            "trading": str(
                Decimal(str(row["trading"]))
            ),
            "reserve": str(
                Decimal(str(row["reserve"]))
            ),
            "mining": str(
                Decimal(str(row["mining"]))
            ),
            "incentives": str(
                Decimal(str(row["incentives"]))
            ),
            "circulating": str(
                Decimal(str(row["circulating"]))
            ),
        }

    finally:
        db.close()


# ============================================================
# WALLET
# ============================================================

def create_vortex_wallet(
    owner_type,
    owner_id,
):
    if not owner_type:
        raise ValueError(
            "owner_type is required"
        )

    if not owner_id:
        raise ValueError(
            "owner_id is required"
        )

    db = get_vortex_db()

    try:

        while True:

            address = wallet_address()

            exists = db.execute(
                """
                SELECT id
                FROM vortex_wallets
                WHERE address = ?
                """,
                (address,),
            ).fetchone()

            if not exists:
                break

        db.execute(
            """
            INSERT INTO vortex_wallets (
                owner_type,
                owner_id,
                address,
                balance,
                created_at
            )
            VALUES (?, ?, ?, 0, ?)
            """,
            (
                owner_type,
                str(owner_id),
                address,
                now(),
            ),
        )

        return address

    finally:
        db.close()


def get_wallet(address):

    if not valid_address(address):
        return None

    db = get_vortex_db()

    try:
        return db.execute(
            """
            SELECT *
            FROM vortex_wallets
            WHERE address = ?
            """,
            (address,),
        ).fetchone()

    finally:
        db.close()


def wallet_balance(address):

    wallet = get_wallet(address)

    if wallet is None:
        raise ValueError(
            "Wallet not found"
        )

    return Decimal(
        str(wallet["balance"])
    )


# ============================================================
# INTERNAL BALANCE OPERATIONS
# ============================================================

def credit(address, amount):

    amount = decimal(amount)

    if amount <= 0:
        raise ValueError(
            "Amount must be positive"
        )

    db = get_vortex_db()

    try:

        row = db.execute(
            """
            SELECT balance
            FROM vortex_wallets
            WHERE address = ?
            """,
            (address,),
        ).fetchone()

        if not row:
            raise ValueError(
                "Wallet not found"
            )

        balance = Decimal(
            str(row["balance"])
        )

        new_balance = balance + amount

        db.execute(
            """
            UPDATE vortex_wallets
            SET balance = ?
            WHERE address = ?
            """,
            (
                float(new_balance),
                address,
            ),
        )

        return new_balance

    finally:
        db.close()


def debit(address, amount):

    amount = decimal(amount)

    if amount <= 0:
        raise ValueError(
            "Amount must be positive"
        )

    db = get_vortex_db()

    try:

        row = db.execute(
            """
            SELECT balance
            FROM vortex_wallets
            WHERE address = ?
            """,
            (address,),
        ).fetchone()

        if not row:
            raise ValueError(
                "Wallet not found"
            )

        balance = Decimal(
            str(row["balance"])
        )

        if balance < amount:
            raise ValueError(
                "Insufficient VTX balance"
            )

        new_balance = balance - amount

        db.execute(
            """
            UPDATE vortex_wallets
            SET balance = ?
            WHERE address = ?
            """,
            (
                float(new_balance),
                address,
            ),
        )

        return new_balance

    finally:
        db.close()


# ============================================================
# VTX TRANSFER
# ============================================================

def create_transaction(
    sender,
    receiver,
    amount,
):

    amount = decimal(amount)

    if amount <= 0:
        raise ValueError(
            "Amount must be positive"
        )

    if not valid_address(receiver):
        raise ValueError(
            "Invalid receiver address"
        )

    if sender != "NETWORK":

        if not valid_address(sender):
            raise ValueError(
                "Invalid sender address"
            )

        debit(sender, amount)

    credit(receiver, amount)

    tx_hash = hashlib.sha256(
        (
            f"{sender}|"
            f"{receiver}|"
            f"{amount}|"
            f"{utc_now()}|"
            f"{secrets.token_hex(16)}"
        ).encode()
    ).hexdigest()

    db = get_vortex_db()

    try:

        db.execute(
            """
            INSERT INTO vortex_transactions (
                tx_hash,
                sender,
                receiver,
                amount,
                fee,
                timestamp,
                block_height,
                status
            )
            VALUES (?, ?, ?, ?, 0, ?, NULL, 'PENDING')
            """,
            (
                tx_hash,
                sender,
                receiver,
                float(amount),
                utc_now(),
            ),
        )

        return tx_hash

    finally:
        db.close()


# ============================================================
# MINING
# ============================================================

def mining_reward(block_height):

    if block_height < 0:
        raise ValueError(
            "Invalid block height"
        )

    halvings = block_height // HALVING_INTERVAL

    reward = INITIAL_MINING_REWARD / (
        Decimal("2") ** halvings
    )

    return reward


def remaining_mining_supply():

    db = get_vortex_db()

    try:

        row = db.execute(
            """
            SELECT mining
            FROM vortex_supply
            WHERE id = 1
            """
        ).fetchone()

        if not row:
            raise ValueError(
                "Supply not initialized"
            )

        return Decimal(
            str(row["mining"])
        )

    finally:
        db.close()


def mine_reward(
    miner_address,
    block_height,
):

    if not valid_address(
        miner_address
    ):
        raise ValueError(
            "Invalid miner address"
        )

    reward = mining_reward(
        block_height
    )

    remaining = remaining_mining_supply()

    if remaining <= 0:
        return Decimal("0")

    if reward > remaining:
        reward = remaining

    db = get_vortex_db()

    try:

        db.execute("BEGIN IMMEDIATE")

        wallet = db.execute(
            """
            SELECT balance
            FROM vortex_wallets
            WHERE address = ?
            """,
            (miner_address,),
        ).fetchone()

        if not wallet:
            raise ValueError(
                "Miner wallet not found"
            )

        balance = Decimal(
            str(wallet["balance"])
        )

        db.execute(
            """
            UPDATE vortex_wallets
            SET balance = ?
            WHERE address = ?
            """,
            (
                float(balance + reward),
                miner_address,
            ),
        )

        db.execute(
            """
            UPDATE vortex_supply
            SET mining = ?
            WHERE id = 1
            """,
            (
                float(remaining - reward),
            ),
        )

        db.commit()

        return reward

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


# ============================================================
# ORDER BOOK
# ============================================================

def place_order(
    wallet,
    side,
    price_usd,
    amount,
):

    side = str(side).upper()

    if side not in (
        "BUY",
        "SELL",
    ):
        raise ValueError(
            "Side must be BUY or SELL"
        )

    if not valid_address(wallet):
        raise ValueError(
            "Invalid wallet address"
        )

    price = decimal(price_usd)
    amount = decimal(amount)

    if price <= 0:
        raise ValueError(
            "Price must be positive"
        )

    if amount < MIN_ORDER_AMOUNT:
        raise ValueError(
            "Amount is too small"
        )

    if get_wallet(wallet) is None:
        raise ValueError(
            "Wallet not found"
        )

    db = get_vortex_db()

    try:

        cursor = db.execute(
            """
            INSERT INTO vortex_orders (
                wallet_address,
                side,
                price_usd,
                amount,
                remaining,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                wallet,
                side,
                float(price),
                float(amount),
                float(amount),
                now(),
            ),
        )

        return cursor.lastrowid

    finally:
        db.close()


def cancel_order(
    order_id,
    wallet,
):

    db = get_vortex_db()

    try:

        row = db.execute(
            """
            SELECT id
            FROM vortex_orders
            WHERE id = ?
              AND wallet_address = ?
              AND status = 'OPEN'
            """,
            (
                order_id,
                wallet,
            ),
        ).fetchone()

        if not row:
            raise ValueError(
                "Order not found"
            )

        db.execute(
            """
            UPDATE vortex_orders
            SET status = 'CANCELLED'
            WHERE id = ?
            """,
            (order_id,),
        )

        return True

    finally:
        db.close()


def order_book():

    db = get_vortex_db()

    try:

        buys = db.execute(
            """
            SELECT *
            FROM vortex_orders
            WHERE side = 'BUY'
              AND status = 'OPEN'
              AND remaining > 0
            ORDER BY price_usd DESC, id ASC
            """
        ).fetchall()

        sells = db.execute(
            """
            SELECT *
            FROM vortex_orders
            WHERE side = 'SELL'
              AND status = 'OPEN'
              AND remaining > 0
            ORDER BY price_usd ASC, id ASC
            """
        ).fetchall()

        return {
            "buy": [
                dict(x)
                for x in buys
            ],
            "sell": [
                dict(x)
                for x in sells
            ],
        }

    finally:
        db.close()


# ============================================================
# MARKET PRICE
# ============================================================

def market_price():

    db = get_vortex_db()

    try:

        row = db.execute(
            """
            SELECT price_usd
            FROM vortex_market
            WHERE id = 1
            """
        ).fetchone()

        if not row:
            return REFERENCE_PRICE_USD

        return Decimal(
            str(row["price_usd"])
        )

    finally:
        db.close()


def set_market_price(price):

    price = decimal(price)

    if price <= 0:
        raise ValueError(
            "Invalid market price"
        )

    db = get_vortex_db()

    try:

        db.execute(
            """
            UPDATE vortex_market
            SET price_usd = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (
                float(price),
                now(),
            ),
        )

    finally:
        db.close()


# ============================================================
# BLOCKCHAIN BLOCK CREATION
# ============================================================

def create_mined_block(
    block_height,
    previous_hash,
    transactions,
    difficulty=4,
):

    block = mine_block(
        block_height=block_height,
        previous_hash=previous_hash,
        transactions=transactions,
        difficulty=difficulty,
    )

    if not verify_block(block):
        raise ValueError(
            "Generated block failed verification"
        )

    db = get_vortex_db()

    try:

        db.execute(
            """
            INSERT INTO vortex_blocks (
                block_height,
                block_hash,
                previous_hash,
                merkle_root,
                nonce,
                difficulty,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                block["block_height"],
                block["block_hash"],
                block["previous_hash"],
                block["merkle_root"],
                block["nonce"],
                block["difficulty"],
                block["timestamp"],
            ),
        )

        for tx in transactions:

            if isinstance(tx, dict):
                tx_hash = tx.get(
                    "tx_hash"
                )

                if tx_hash:

                    db.execute(
                        """
                        UPDATE vortex_transactions
                        SET block_height = ?,
                            status = 'CONFIRMED'
                        WHERE tx_hash = ?
                        """,
                        (
                            block_height,
                            tx_hash,
                        ),
                    )

        return block

    finally:
        db.close()


# ============================================================
# STATUS
# ============================================================

def vortex_status():

    supply = supply_status()

    return {
        "name": "VORTEX",
        "symbol": "VTX",
        "max_supply": str(MAX_SUPPLY),
        "reference_price_usd": str(
            REFERENCE_PRICE_USD
        ),
        "current_price_usd": str(
            market_price()
        ),
        "supply": supply,
        "mining_reward": str(
            mining_reward(0)
        ),
        "halving_interval": HALVING_INTERVAL,
      }
