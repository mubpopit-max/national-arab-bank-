from decimal import Decimal

from .database import get_vortex_db, now
from .blockchain import mine_block


# ============================================================
# VORTEX MINING
# ============================================================

MINING_ALLOCATION = Decimal("2000000")

INITIAL_BLOCK_REWARD = Decimal("10")

HALVING_INTERVAL = 210000


def get_mining_supply():
    """
    معرفة كمية VTX التي تم توزيعها من مخصص التعدين.
    """

    db = get_vortex_db()

    try:
        row = db.execute(
            """
            SELECT mining
            FROM vortex_supply
            WHERE id = 1
            """
        ).fetchone()

        if row is None:
            raise ValueError(
                "VORTEX supply record not found"
            )

        return Decimal(str(row["mining"]))

    finally:
        db.close()


def get_mined_amount():
    """
    حساب كمية VTX التي خرجت من مخصص التعدين.
    """

    remaining = get_mining_supply()

    return MINING_ALLOCATION - remaining


def calculate_block_reward(block_height):
    """
    حساب مكافأة التعدين.

    تبدأ بـ 10 VTX
    ثم تنخفض إلى النصف كل 210,000 كتلة.
    """

    halvings = block_height // HALVING_INTERVAL

    reward = INITIAL_BLOCK_REWARD / (
        Decimal("2") ** halvings
    )

    return reward


def get_remaining_mining_supply():
    """
    الكمية المتبقية من مخصص التعدين.
    """

    db = get_vortex_db()

    try:

        row = db.execute(
            """
            SELECT mining
            FROM vortex_supply
            WHERE id = 1
            """
        ).fetchone()

        if row is None:
            raise ValueError(
                "VORTEX supply record not found"
            )

        return Decimal(
            str(row["mining"])
        )

    finally:
        db.close()


def distribute_mining_reward(
    wallet_address,
    block_height,
):
    """
    توزيع مكافأة التعدين على محفظة المعدّن.

    لا يمكن أن يتجاوز التعدين
    المخصص النهائي البالغ 2,000,000 VTX.
    """

    if not wallet_address:
        raise ValueError(
            "wallet_address is required"
        )

    reward = calculate_block_reward(
        block_height
    )

    remaining = get_remaining_mining_supply()

    if remaining <= 0:
        return Decimal("0")

    if reward > remaining:
        reward = remaining

    db = get_vortex_db()

    try:

        wallet = db.execute(
            """
            SELECT balance
            FROM vortex_wallets
            WHERE address = ?
            """,
            (wallet_address,),
        ).fetchone()

        if wallet is None:
            raise ValueError(
                "Mining wallet not found"
            )

        current_balance = Decimal(
            str(wallet["balance"])
        )

        new_balance = current_balance + reward

        new_mining_balance = remaining - reward

        db.execute(
            """
            UPDATE vortex_wallets
            SET balance = ?
            WHERE address = ?
            """,
            (
                float(new_balance),
                wallet_address,
            ),
        )

        db.execute(
            """
            UPDATE vortex_supply
            SET mining = ?
            WHERE id = 1
            """,
            (
                float(new_mining_balance),
            ),
        )

        return reward

    finally:
        db.close()


def mining_status():
    """
    إرجاع حالة التعدين الحالية.
    """

    remaining = get_remaining_mining_supply()

    mined = MINING_ALLOCATION - remaining

    return {
        "allocation": str(
            MINING_ALLOCATION
        ),
        "distributed": str(
            mined
        ),
        "remaining": str(
            remaining
        ),
        "initial_block_reward": str(
            INITIAL_BLOCK_REWARD
        ),
        "halving_interval": HALVING_INTERVAL,
  }
