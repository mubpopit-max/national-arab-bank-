from decimal import Decimal

from .database import get_vortex_db, now
from .wallet import debit_wallet, credit_wallet


# ============================================================
# VORTEX EXCHANGE / ORDER BOOK
# ============================================================

MIN_ORDER_AMOUNT = Decimal("0.00000001")


def normalize_decimal(value):
    return Decimal(str(value))


def place_order(
    wallet_address,
    side,
    price_usd,
    amount,
):
    """
    إنشاء أمر شراء أو بيع في دفتر أوامر VORTEX.

    BUY  = شراء VTX
    SELL = بيع VTX
    """

    side = str(side).upper()

    if side not in ("BUY", "SELL"):
        raise ValueError("Invalid order side")

    price = normalize_decimal(price_usd)
    quantity = normalize_decimal(amount)

    if price <= 0:
        raise ValueError(
            "Price must be greater than zero"
        )

    if quantity < MIN_ORDER_AMOUNT:
        raise ValueError(
            "Order amount is too small"
        )

    db = get_vortex_db()

    try:

        wallet = db.execute(
            """
            SELECT address
            FROM vortex_wallets
            WHERE address = ?
            """,
            (wallet_address,),
        ).fetchone()

        if wallet is None:
            raise ValueError("Wallet not found")

        # في هذه المرحلة نسجل الأمر فقط.
        # الحجز المالي والمطابقة الذرية سيضافان
        # في طبقة التداول النهائية.

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
                wallet_address,
                side,
                float(price),
                float(quantity),
                float(quantity),
                now(),
            ),
        )

        return cursor.lastrowid

    finally:
        db.close()


def cancel_order(order_id, wallet_address):
    """
    إلغاء أمر يملكه صاحب المحفظة.
    """

    db = get_vortex_db()

    try:

        order = db.execute(
            """
            SELECT *
            FROM vortex_orders
            WHERE id = ?
              AND wallet_address = ?
              AND status = 'OPEN'
            """,
            (
                order_id,
                wallet_address,
            ),
        ).fetchone()

        if order is None:
            raise ValueError(
                "Open order not found"
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


def get_order_book():
    """
    قراءة دفتر أوامر VORTEX.

    BUY:
    الأعلى سعراً أولاً.

    SELL:
    الأقل سعراً أولاً.
    """

    db = get_vortex_db()

    try:

        buy_orders = db.execute(
            """
            SELECT *
            FROM vortex_orders
            WHERE side = 'BUY'
              AND status = 'OPEN'
              AND remaining > 0
            ORDER BY price_usd DESC, id ASC
            """
        ).fetchall()

        sell_orders = db.execute(
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
                dict(order)
                for order in buy_orders
            ],
            "sell": [
                dict(order)
                for order in sell_orders
            ],
        }

    finally:
        db.close()


def get_market_price():
    """
    السعر الحالي المسجل للسوق.
    """

    db = get_vortex_db()

    try:

        row = db.execute(
            """
            SELECT price_usd
            FROM vortex_market
            WHERE id = 1
            """
        ).fetchone()

        if row is None:
            return Decimal("70")

        return Decimal(
            str(row["price_usd"])
        )

    finally:
        db.close()


def update_market_price(price_usd):
    """
    تحديث السعر بعد تنفيذ صفقة حقيقية.
    """

    price = normalize_decimal(price_usd)

    if price <= 0:
        raise ValueError(
            "Market price must be greater than zero"
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


def match_orders():
    """
    مطابقة أوامر الشراء والبيع.

    تتم المطابقة فقط عندما:

        أعلى سعر شراء >= أقل سعر بيع

    هذه النسخة تنفذ المطابقة الأساسية.
    """

    db = get_vortex_db()

    try:

        while True:

            buy = db.execute(
                """
                SELECT *
                FROM vortex_orders
                WHERE side = 'BUY'
                  AND status = 'OPEN'
                  AND remaining > 0
                ORDER BY price_usd DESC, id ASC
                LIMIT 1
                """
            ).fetchone()

            sell = db.execute(
                """
                SELECT *
                FROM vortex_orders
                WHERE side = 'SELL'
                  AND status = 'OPEN'
                  AND remaining > 0
                ORDER BY price_usd ASC, id ASC
                LIMIT 1
                """
            ).fetchone()

            if not buy or not sell:
                break

            buy_price = Decimal(
                str(buy["price_usd"])
            )

            sell_price = Decimal(
                str(sell["price_usd"])
            )

            if buy_price < sell_price:
                break

            trade_amount = min(
                Decimal(str(buy["remaining"])),
                Decimal(str(sell["remaining"])),
            )

            trade_price = sell_price

            buyer_address = buy["wallet_address"]
            seller_address = sell["wallet_address"]

            total_usd = (
                trade_amount * trade_price
            )

            # ------------------------------------------------
            # تنفيذ التحويل داخل قاعدة البيانات
            # ------------------------------------------------

            buyer = db.execute(
                """
                SELECT balance
                FROM vortex_wallets
                WHERE address = ?
                """,
                (buyer_address,),
            ).fetchone()

            seller = db.execute(
                """
                SELECT balance
                FROM vortex_wallets
                WHERE address = ?
                """,
                (seller_address,),
            ).fetchone()

            if not buyer or not seller:
                break

            buyer_balance = Decimal(
                str(buyer["balance"])
            )

            seller_balance = Decimal(
                str(seller["balance"])
            )

            # ملاحظة:
            # في المرحلة النهائية يجب أن يكون للـ BUY
            # حجز فعلي للـ USD/الرصيد النقدي.
            #
            # حالياً لا نخصم USD هنا لأن محفظة VTX
            # لا تمثل الحساب النقدي للبنك.

            if seller_balance < trade_amount:
                db.execute(
                    """
                    UPDATE vortex_orders
                    SET status = 'CANCELLED'
                    WHERE id = ?
                    """,
                    (sell["id"],),
                )
                continue

            new_buyer_balance = (
                buyer_balance + trade_amount
            )

            new_seller_balance = (
                seller_balance - trade_amount
            )

            db.execute(
                """
                UPDATE vortex_wallets
                SET balance = ?
                WHERE address = ?
                """,
                (
                    float(new_buyer_balance),
                    buyer_address,
                ),
            )

            db.execute(
                """
                UPDATE vortex_wallets
                SET balance = ?
                WHERE address = ?
                """,
                (
                    float(new_seller_balance),
                    seller_address,
                ),
            )

            new_buy_remaining = (
                Decimal(str(buy["remaining"]))
                - trade_amount
            )

            new_sell_remaining = (
                Decimal(str(sell["remaining"]))
                - trade_amount
            )

            db.execute(
                """
                UPDATE vortex_orders
                SET remaining = ?,
                    status = ?
                WHERE id = ?
                """,
                (
                    float(new_buy_remaining),
                    "FILLED"
                    if new_buy_remaining <= 0
                    else "OPEN",
                    buy["id"],
                ),
            )

            db.execute(
                """
                UPDATE vortex_orders
                SET remaining = ?,
                    status = ?
                WHERE id = ?
                """,
                (
                    float(new_sell_remaining),
                    "FILLED"
                    if new_sell_remaining <= 0
                    else "OPEN",
                    sell["id"],
                ),
            )

            # السعر الذي تمت عليه الصفقة يصبح
            # آخر سعر سوق حقيقي.
            db.execute(
                """
                UPDATE vortex_market
                SET price_usd = ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (
                    float(trade_price),
                    now(),
                ),
            )

    finally:
        db.close()
