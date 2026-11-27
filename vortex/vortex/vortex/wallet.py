import hashlib
import secrets
from decimal import Decimal, InvalidOperation

from .database import get_vortex_db, now


# ============================================================
# VORTEX WALLET
# ============================================================

ADDRESS_PREFIX = "VTX"


def generate_wallet_address():
    """
    إنشاء عنوان VORTEX فريد.
    """

    random_part = secrets.token_hex(20).upper()

    raw = f"{ADDRESS_PREFIX}{random_part}"

    checksum = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:8].upper()

    return f"{raw}{checksum}"


def is_valid_wallet_address(address):
    """
    التحقق الأساسي من صيغة عنوان VORTEX.
    """

    if not isinstance(address, str):
        return False

    if not address.startswith(ADDRESS_PREFIX):
        return False

    if len(address) != 51:
        return False

    body = address[:-8]
    checksum = address[-8:]

    expected = hashlib.sha256(
        body.encode("utf-8")
    ).hexdigest()[:8].upper()

    return checksum == expected


def create_wallet(owner_type, owner_id):
    """
    إنشاء محفظة VTX جديدة.
    """

    if not owner_type:
        raise ValueError("owner_type is required")

    if not owner_id:
        raise ValueError("owner_id is required")

    db = get_vortex_db()

    try:

        while True:

            address = generate_wallet_address()

            existing = db.execute(
                """
                SELECT id
                FROM vortex_wallets
                WHERE address = ?
                """,
                (address,),
            ).fetchone()

            if not existing:
                break

        created_at = now()

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
                created_at,
            ),
        )

        return address

    finally:
        db.close()


def get_wallet(address):
    """
    جلب محفظة بواسطة العنوان.
    """

    if not is_valid_wallet_address(address):
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


def get_wallet_balance(address):
    """
    الحصول على رصيد المحفظة.
    """

    wallet = get_wallet(address)

    if wallet is None:
        raise ValueError("Wallet not found")

    return Decimal(
        str(wallet["balance"])
    )


def normalize_amount(amount):
    """
    تحويل المبلغ إلى Decimal والتحقق منه.
    """

    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid VTX amount")

    if value <= 0:
        raise ValueError(
            "VTX amount must be greater than zero"
        )

    return value


def credit_wallet(address, amount):
    """
    إضافة VTX إلى محفظة.

    هذه الدالة لا تنشئ VTX جديدًا من نفسها.
    يجب أن يكون المصدر مصرحًا به من نظام البلوكشين.
    """

    value = normalize_amount(amount)

    db = get_vortex_db()

    try:

        wallet = db.execute(
            """
            SELECT balance
            FROM vortex_wallets
            WHERE address = ?
            """,
            (address,),
        ).fetchone()

        if wallet is None:
            raise ValueError("Wallet not found")

        current_balance = Decimal(
            str(wallet["balance"])
        )

        new_balance = current_balance + value

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


def debit_wallet(address, amount):
    """
    خصم VTX من المحفظة.

    يمنع الرصيد السالب.
    """

    value = normalize_amount(amount)

    db = get_vortex_db()

    try:

        wallet = db.execute(
            """
            SELECT balance
            FROM vortex_wallets
            WHERE address = ?
            """,
            (address,),
        ).fetchone()

        if wallet is None:
            raise ValueError("Wallet not found")

        current_balance = Decimal(
            str(wallet["balance"])
        )

        if current_balance < value:
            raise ValueError(
                "Insufficient VTX balance"
            )

        new_balance = current_balance - value

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
