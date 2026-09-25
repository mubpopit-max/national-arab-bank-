# ============================================================
# NATIONAL ARAB BANK
# app.py
# NATIONAL ARAB BANK <-> DIGITAL STORE 249 API
# ============================================================

import os
import json
import time
import hmac
import secrets
import sqlite3
import urllib.request
import urllib.error

from datetime import datetime
from decimal import Decimal, InvalidOperation

import bcrypt

from flask import (
    Flask,
    request,
    session,
    redirect,
    url_for,
    render_template_string,
    jsonify,
    abort,
)


# ============================================================
# APP CONFIGURATION
# ============================================================

app = Flask(__name__)

SECRET_KEY = os.getenv("FLASK_SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError(
        "FLASK_SECRET_KEY must be configured in environment variables"
    )

app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.getenv("COOKIE_SECURE", "true").lower() == "true"
    ),
    PERMANENT_SESSION_LIFETIME=30 * 24 * 60 * 60,
)


# ============================================================
# DATABASE
# ============================================================

DATABASE = os.getenv(
    "NAB_DATABASE",
    "national_arab_bank.db"
)


# ============================================================
# BANK SETTINGS
# ============================================================

FOUNDER_USERNAME = "founder"
FOUNDER_ACCOUNT = "5147234"

FOUNDER_INITIAL_BALANCE = 3_000_000_000_000

APP_NAME = "NATIONAL ARAB BANK"


# ============================================================
# DIGITAL STORE API SETTINGS
# ============================================================

DIGITAL_STORE_API_BASE_URL = os.getenv(
    "DIGITAL_STORE_API_BASE_URL",
    ""
).rstrip("/")

DIGITAL_STORE_API_KEY = os.getenv(
    "DIGITAL_STORE_API_KEY",
    ""
)

BANK_API_KEY = os.getenv(
    "BANK_API_KEY",
    ""
)

API_TIMEOUT = int(
    os.getenv(
        "API_TIMEOUT",
        "15"
    )
)


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False
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


def now():
    return datetime.utcnow().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():

    conn = get_db()

    try:

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                username TEXT NOT NULL UNIQUE,

                full_name TEXT NOT NULL,

                account_number TEXT NOT NULL UNIQUE,

                password_hash TEXT NOT NULL,

                pin_hash TEXT NOT NULL,

                balance INTEGER NOT NULL DEFAULT 0
                    CHECK(balance >= 0),

                currency TEXT NOT NULL DEFAULT 'SDG',

                role TEXT NOT NULL DEFAULT 'customer',

                active INTEGER NOT NULL DEFAULT 1,

                created_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                reference TEXT NOT NULL UNIQUE,

                sender_account TEXT,

                receiver_account TEXT,

                amount INTEGER NOT NULL
                    CHECK(amount > 0),

                sender_before INTEGER,

                sender_after INTEGER,

                receiver_before INTEGER,

                receiver_after INTEGER,

                type TEXT NOT NULL,

                status TEXT NOT NULL,

                comment TEXT,

                phone TEXT,

                created_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                account_number TEXT NOT NULL,

                message TEXT NOT NULL,

                reference TEXT,

                is_read INTEGER NOT NULL DEFAULT 0,

                created_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS idempotency_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                idempotency_key TEXT NOT NULL UNIQUE,

                reference TEXT,

                status TEXT NOT NULL,

                created_at TEXT NOT NULL
            );


            CREATE TABLE IF NOT EXISTS external_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                reference TEXT NOT NULL UNIQUE,

                idempotency_key TEXT NOT NULL UNIQUE,

                direction TEXT NOT NULL,

                source_account TEXT NOT NULL,

                destination_account TEXT NOT NULL,

                amount INTEGER NOT NULL
                    CHECK(amount > 0),

                status TEXT NOT NULL,

                provider_reference TEXT,

                error_message TEXT,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL
            );


            CREATE INDEX IF NOT EXISTS
            idx_transactions_sender
            ON transactions(sender_account);


            CREATE INDEX IF NOT EXISTS
            idx_transactions_receiver
            ON transactions(receiver_account);


            CREATE INDEX IF NOT EXISTS
            idx_notifications_account
            ON notifications(account_number);


            CREATE INDEX IF NOT EXISTS
            idx_external_reference
            ON external_transfers(reference);


            CREATE INDEX IF NOT EXISTS
            idx_external_status
            ON external_transfers(status);
            """
        )

        # ----------------------------------------------------
        # Founder
        # ----------------------------------------------------

        founder = conn.execute(
            """
            SELECT id
            FROM users
            WHERE account_number = ?
            """,
            (FOUNDER_ACCOUNT,)
        ).fetchone()

        if not founder:

            founder_password = os.getenv(
                "FOUNDER_PASSWORD"
            )

            founder_pin = os.getenv(
                "FOUNDER_PIN"
            )

            if not founder_password:
                raise RuntimeError(
                    "FOUNDER_PASSWORD must be configured"
                )

            if not founder_pin or not founder_pin.isdigit():
                raise RuntimeError(
                    "FOUNDER_PIN must be configured"
                )

            if len(founder_pin) != 4:
                raise RuntimeError(
                    "FOUNDER_PIN must contain exactly 4 digits"
                )

            password_hash = bcrypt.hashpw(
                founder_password.encode("utf-8"),
                bcrypt.gensalt()
            ).decode("utf-8")

            pin_hash = bcrypt.hashpw(
                founder_pin.encode("utf-8"),
                bcrypt.gensalt()
            ).decode("utf-8")

            conn.execute(
                """
                INSERT INTO users (
                    username,
                    full_name,
                    account_number,
                    password_hash,
                    pin_hash,
                    balance,
                    currency,
                    role,
                    active,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    FOUNDER_USERNAME,
                    "مبارك عبدالرحمن مبارك",
                    FOUNDER_ACCOUNT,
                    password_hash,
                    pin_hash,
                    FOUNDER_INITIAL_BALANCE,
                    "SDG",
                    "founder",
                    1,
                    now()
                )
            )

            conn.commit()

    finally:
        conn.close()


# ============================================================
# SECURITY HELPERS
# ============================================================

def hash_password(password):

    if len(password.encode("utf-8")) > 72:
        raise ValueError(
            "Password is too long"
        )

    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")


def verify_password(password, password_hash):

    try:

        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8")
        )

    except Exception:
        return False


def hash_pin(pin):

    return bcrypt.hashpw(
        pin.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")


def verify_pin(pin, pin_hash):

    try:

        return bcrypt.checkpw(
            pin.encode("utf-8"),
            pin_hash.encode("utf-8")
        )

    except Exception:
        return False


# ============================================================
# VALIDATION
# ============================================================

def normalize_amount(value):

    try:

        amount = Decimal(
            str(value)
        )

    except (
        InvalidOperation,
        TypeError,
        ValueError
    ):

        raise ValueError(
            "Invalid amount"
        )

    if amount <= 0:
        raise ValueError(
            "Amount must be greater than zero"
        )

    if amount != amount.to_integral_value():
        raise ValueError(
            "SDG amount must be an integer"
        )

    return int(amount)


def valid_pin(pin):

    return (
        isinstance(pin, str)
        and len(pin) == 4
        and pin.isdigit()
    )


def generate_reference(conn):

    while True:

        reference = (
            "NAB"
            + datetime.utcnow().strftime(
                "%Y%m%d%H%M%S"
            )
            + secrets.token_hex(5).upper()
        )

        exists = conn.execute(
            """
            SELECT 1
            FROM transactions
            WHERE reference = ?
            """,
            (reference,)
        ).fetchone()

        if not exists:
            return reference


def generate_account_number(conn):

    while True:

        account = (
            str(
                secrets.randbelow(
                    9000000
                ) + 1000000
            )
        )

        exists = conn.execute(
            """
            SELECT 1
            FROM users
            WHERE account_number = ?
            """,
            (account,)
        ).fetchone()

        if not exists:
            return account


# ============================================================
# SESSION HELPERS
# ============================================================

def current_user():

    account_number = session.get(
        "account_number"
    )

    if not account_number:
        return None

    conn = get_db()

    try:

        return conn.execute(
            """
            SELECT *
            FROM users
            WHERE account_number = ?
              AND active = 1
            """,
            (account_number,)
        ).fetchone()

    finally:
        conn.close()


def login_required():

    user = current_user()

    if not user:
        return redirect(
            url_for("login")
        )

    return user


# ============================================================
# API AUTHENTICATION
# ============================================================

def verify_bank_api_key():

    if not BANK_API_KEY:
        abort(
            503,
            description="BANK_API_KEY is not configured"
        )

    incoming = request.headers.get(
        "X-API-Key",
        ""
    )

    if not incoming:
        abort(
            401,
            description="Missing API key"
        )

    if not hmac.compare_digest(
        incoming,
        BANK_API_KEY
    ):
        abort(
            403,
            description="Invalid API key"
        )


def verify_digital_store_api_key():

    if not DIGITAL_STORE_API_KEY:
        abort(
            503,
            description=(
                "DIGITAL_STORE_API_KEY "
                "is not configured"
            )
        )

    incoming = request.headers.get(
        "X-API-Key",
        ""
    )

    if not incoming:
        abort(
            401,
            description="Missing API key"
        )

    if not hmac.compare_digest(
        incoming,
        DIGITAL_STORE_API_KEY
    ):
        abort(
            403,
            description="Invalid API key"
        )


# ============================================================
# HTTP CLIENT
# ============================================================

def call_digital_store(
    method,
    path,
    payload,
    idempotency_key
):

    if not DIGITAL_STORE_API_BASE_URL:

        raise RuntimeError(
            "DIGITAL_STORE_API_BASE_URL "
            "is not configured"
        )

    if not DIGITAL_STORE_API_KEY:

        raise RuntimeError(
            "DIGITAL_STORE_API_KEY "
            "is not configured"
        )

    url = (
        DIGITAL_STORE_API_BASE_URL
        + path
    )

    body = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        method=method.upper()
    )

    req.add_header(
        "Content-Type",
        "application/json"
    )

    req.add_header(
        "Accept",
        "application/json"
    )

    req.add_header(
        "X-API-Key",
        DIGITAL_STORE_API_KEY
    )

    req.add_header(
        "Idempotency-Key",
        idempotency_key
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=API_TIMEOUT
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            if not raw:
                return {
                    "success": True
                }

            return json.loads(raw)

    except urllib.error.HTTPError as exc:

        try:

            raw = (
                exc.read()
                .decode("utf-8")
            )

            data = json.loads(raw)

        except Exception:

            data = {
                "success": False,
                "error": str(exc)
            }

        raise RuntimeError(
            json.dumps(
                data,
                ensure_ascii=False
            )
        )

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "DIGITAL STORE connection failed: "
            + str(exc)
        )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    if session.get("account_number"):
        return redirect(
            url_for("account")
        )

    return redirect(
        url_for("login")
    )


@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    error = ""

    if request.method == "POST":

        account_number = (
            request.form.get(
                "account_number",
                ""
            ).strip()
        )

        password = request.form.get(
            "password",
            ""
        )

        conn = get_db()

        try:

            user = conn.execute(
                """
                SELECT *
                FROM users
                WHERE account_number = ?
                  AND active = 1
                """,
                (account_number,)
            ).fetchone()

        finally:
            conn.close()

        if (
            user
            and verify_password(
                password,
                user["password_hash"]
            )
        ):

            session.clear()

            session.permanent = True

            session[
                "account_number"
            ] = user["account_number"]

            return redirect(
                url_for("account")
            )

        error = (
            "رقم الحساب أو كلمة المرور غير صحيحة"
        )

    return render_template_string(
        LOGIN_HTML,
        error=error
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# ACCOUNT
# ============================================================

@app.route("/account")
def account():

    user = login_required()

    if not isinstance(user, sqlite3.Row):
        return user

    conn = get_db()

    try:

        transactions = conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE sender_account = ?
               OR receiver_account = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (
                user["account_number"],
                user["account_number"]
            )
        ).fetchall()

    finally:
        conn.close()

    return render_template_string(
        ACCOUNT_HTML,
        user=user,
        transactions=transactions
    )


# ============================================================
# REGISTER
# ============================================================

@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    error = ""

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        full_name = request.form.get(
            "full_name",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        pin = request.form.get(
            "pin",
            ""
        )

        if not username:
            error = "اسم المستخدم مطلوب"

        elif not full_name:
            error = "الاسم الكامل مطلوب"

        elif not password:
            error = "كلمة المرور مطلوبة"

        elif not valid_pin(pin):
            error = (
                "الرقم السري يجب أن يكون "
                "4 أرقام"
            )

        elif len(
            password.encode("utf-8")
        ) > 72:

            error = (
                "كلمة المرور طويلة جدًا"
            )

        else:

            conn = get_db()

            try:

                exists = conn.execute(
                    """
                    SELECT 1
                    FROM users
                    WHERE username = ?
                    """,
                    (username,)
                ).fetchone()

                if exists:

                    error = (
                        "اسم المستخدم موجود مسبقًا"
                    )

                else:

                    account_number = (
                        generate_account_number(
                            conn
                        )
                    )

                    conn.execute(
                        """
                        INSERT INTO users (
                            username,
                            full_name,
                            account_number,
                            password_hash,
                            pin_hash,
                            balance,
                            currency,
                            role,
                            active,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?, 0,
                                'SDG', 'customer',
                                1, ?)
                        """,
                        (
                            username,
                            full_name,
                            account_number,
                            hash_password(
                                password
                            ),
                            hash_pin(pin),
                            now()
                        )
                    )

                    conn.commit()

                    return render_template_string(
                        REGISTER_SUCCESS_HTML,
                        account_number=account_number
                    )

            except Exception as exc:

                conn.rollback()

                error = str(exc)

            finally:
                conn.close()

    return render_template_string(
        REGISTER_HTML,
        error=error
    )


# ============================================================
# INTERNAL TRANSFER
# ============================================================

@app.route(
    "/transfer",
    methods=["GET", "POST"]
)
def transfer():

    user = login_required()

    if not isinstance(user, sqlite3.Row):
        return user

    error = ""

    if request.method == "POST":

        receiver_account = request.form.get(
            "receiver_account",
            ""
        ).strip()

        pin = request.form.get(
            "pin",
            ""
        )

        comment = request.form.get(
            "comment",
            ""
        ).strip()

        try:

            amount = normalize_amount(
                request.form.get(
                    "amount"
                )
            )

            conn = get_db()

            try:

                conn.execute(
                    "BEGIN IMMEDIATE"
                )

                sender = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE account_number = ?
                      AND active = 1
                    """,
                    (
                        user["account_number"],
                    )
                ).fetchone()

                receiver = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE account_number = ?
                      AND active = 1
                    """,
                    (
                        receiver_account,
                    )
                ).fetchone()

                if not receiver:
                    raise ValueError(
                        "حساب المستفيد غير موجود"
                    )

                if (
                    sender["account_number"]
                    == receiver["account_number"]
                ):
                    raise ValueError(
                        "لا يمكن التحويل إلى نفس الحساب"
                    )

                if not verify_pin(
                    pin,
                    sender["pin_hash"]
                ):
                    raise ValueError(
                        "الرقم السري غير صحيح"
                    )

                sender_before = int(
                    sender["balance"]
                )

                receiver_before = int(
                    receiver["balance"]
                )

                if sender_before < amount:
                    raise ValueError(
                        "الرصيد غير كافٍ"
                    )

                sender_after = (
                    sender_before - amount
                )

                receiver_after = (
                    receiver_before + amount
                )

                if sender_after < 0:
                    raise ValueError(
                        "لا يمكن أن يصبح الرصيد سالبًا"
                    )

                reference = generate_reference(
                    conn
                )

                conn.execute(
                    """
                    UPDATE users
                    SET balance = ?
                    WHERE account_number = ?
                    """,
                    (
                        sender_after,
                        sender["account_number"]
                    )
                )

                conn.execute(
                    """
                    UPDATE users
                    SET balance = ?
                    WHERE account_number = ?
                    """,
                    (
                        receiver_after,
                        receiver["account_number"]
                    )
                )

                conn.execute(
                    """
                    INSERT INTO transactions (
                        reference,
                        sender_account,
                        receiver_account,
                        amount,
                        sender_before,
                        sender_after,
                        receiver_before,
                        receiver_after,
                        type,
                        status,
                        comment,
                        phone,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                            'TRANSFER',
                            'COMPLETED',
                            ?, ?, ?)
                    """,
                    (
                        reference,
                        sender["account_number"],
                        receiver["account_number"],
                        amount,
                        sender_before,
                        sender_after,
                        receiver_before,
                        receiver_after,
                        comment,
                        "",
                        now()
                    )
                )

                conn.execute(
                    """
                    INSERT INTO notifications (
                        account_number,
                        message,
                        reference,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        receiver["account_number"],
                        f"تم استلام تحويل بقيمة {amount:,} SDG",
                        reference,
                        now()
                    )
                )

                conn.commit()

                return redirect(
                    url_for(
                        "receipt",
                        reference=reference
                    )
                )

            except Exception:

                conn.rollback()

                raise

            finally:
                conn.close()

        except Exception as exc:

            error = str(exc)

    return render_template_string(
        TRANSFER_HTML,
        user=user,
        error=error
    )


# ============================================================
# DIGITAL STORE ACCOUNT LOOKUP
# ============================================================

@app.route(
    "/api/v1/digital-store/account",
    methods=["GET"]
)
def digital_store_account_lookup():

    verify_bank_api_key()

    account_number = (
        request.args.get(
            "account_number",
            ""
        ).strip()
    )

    if not account_number:

        return jsonify({
            "success": False,
            "error": "account_number is required"
        }), 400

    try:

        result = call_digital_store(
            "GET",
            (
                "/api/v1/accounts/lookup"
                + "?account_number="
                + account_number
            ),
            {},
            secrets.token_hex(16)
        )

        return jsonify(result)

    except Exception as exc:

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 502


# ============================================================
# BANK -> DIGITAL STORE
# ============================================================

@app.route(
    "/external_transfer/digital_store",
    methods=["GET", "POST"]
)
def external_transfer_digital_store():

    user = login_required()

    if not isinstance(user, sqlite3.Row):
        return user

    error = ""
    success = ""

    if request.method == "POST":

        destination_account = (
            request.form.get(
                "destination_account",
                ""
            ).strip()
        )

        pin = request.form.get(
            "pin",
            ""
        )

        comment = request.form.get(
            "comment",
            "تحويل إلى DIGITAL STORE 249"
        ).strip()

        try:

            amount = normalize_amount(
                request.form.get(
                    "amount"
                )
            )

            if not destination_account:
                raise ValueError(
                    "رقم حساب DIGITAL STORE مطلوب"
                )

            if not valid_pin(pin):
                raise ValueError(
                    "الرقم السري يجب أن يكون 4 أرقام"
                )

            conn = get_db()

            try:

                # ------------------------------------------------
                # قفل قاعدة البنك أثناء عملية التحويل
                # ------------------------------------------------

                conn.execute(
                    "BEGIN IMMEDIATE"
                )

                sender = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE account_number = ?
                      AND active = 1
                    """,
                    (
                        user["account_number"],
                    )
                ).fetchone()

                if not sender:
                    raise ValueError(
                        "حساب المرسل غير موجود"
                    )

                if not verify_pin(
                    pin,
                    sender["pin_hash"]
                ):
                    raise ValueError(
                        "الرقم السري غير صحيح"
                    )

                sender_before = int(
                    sender["balance"]
                )

                if sender_before < amount:
                    raise ValueError(
                        "الرصيد غير كافٍ"
                    )

                # ------------------------------------------------
                # مرجع موحد
                # ------------------------------------------------

                reference = generate_reference(
                    conn
                )

                idempotency_key = (
                    "NAB-"
                    + reference
                )

                # ------------------------------------------------
                # تسجيل العملية بحالة PENDING
                # ------------------------------------------------

                conn.execute(
                    """
                    INSERT INTO external_transfers (
                        reference,
                        idempotency_key,
                        direction,
                        source_account,
                        destination_account,
                        amount,
                        status,
                        provider_reference,
                        error_message,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reference,
                        idempotency_key,
                        "BANK_TO_DIGITAL_STORE",
                        sender["account_number"],
                        destination_account,
                        amount,
                        "PENDING",
                        None,
                        None,
                        now(),
                        now()
                    )
                )

                # ------------------------------------------------
                # إرسال التحويل إلى DIGITAL STORE
                # ------------------------------------------------

                result = call_digital_store(
                    "POST",
                    "/api/v1/transfers/inbound",
                    {
                        "reference": reference,
                        "idempotency_key":
                            idempotency_key,
                        "source_account":
                            sender["account_number"],
                        "destination_account":
                            destination_account,
                        "amount":
                            amount,
                        "currency":
                            "SDG",
                        "description":
                            comment
                    },
                    idempotency_key
                )

                if not result.get("success"):
                    raise ValueError(
                        "DIGITAL STORE لم يؤكد التحويل"
                    )

                # ------------------------------------------------
                # خصم البنك بعد نجاح DIGITAL STORE
                # ------------------------------------------------

                sender_after = (
                    sender_before - amount
                )

                if sender_after < 0:
                    raise ValueError(
                        "العملية تؤدي إلى رصيد سالب"
                    )

                conn.execute(
                    """
                    UPDATE users
                    SET balance = ?
                    WHERE account_number = ?
                    """,
                    (
                        sender_after,
                        sender["account_number"]
                    )
                )

                conn.execute(
                    """
                    INSERT INTO transactions (
                        reference,
                        sender_account,
                        receiver_account,
                        amount,
                        sender_before,
                        sender_after,
                        receiver_before,
                        receiver_after,
                        type,
                        status,
                        comment,
                        phone,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL,
                            'DIGITAL_STORE_TRANSFER',
                            'COMPLETED',
                            ?, ?, ?)
                    """,
                    (
                        reference,
                        sender["account_number"],
                        destination_account,
                        amount,
                        sender_before,
                        sender_after,
                        comment,
                        "",
                        now()
                    )
                )

                provider_reference = (
                    result.get(
                        "reference",
                        reference
                    )
                )

                conn.execute(
                    """
                    UPDATE external_transfers
                    SET status = ?,
                        provider_reference = ?,
                        updated_at = ?
                    WHERE reference = ?
                    """,
                    (
                        "COMPLETED",
                        provider_reference,
                        now(),
                        reference
                    )
                )

                conn.execute(
                    """
                    INSERT INTO notifications (
                        account_number,
                        message,
                        reference,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        sender["account_number"],
                        (
                            "تم تحويل "
                            f"{amount:,} SDG "
                            "إلى DIGITAL STORE 249"
                        ),
                        reference,
                        now()
                    )
                )

                conn.commit()

                return redirect(
                    url_for(
                        "receipt",
                        reference=reference
                    )
                )

            except Exception as exc:

                conn.rollback()

                # محاولة تسجيل الفشل بشكل منفصل
                try:

                    failure_conn = get_db()

                    failure_conn.execute(
                        """
                        INSERT OR IGNORE INTO
                        external_transfers (
                            reference,
                            idempotency_key,
                            direction,
                            source_account,
                            destination_account,
                            amount,
                            status,
                            provider_reference,
                            error_message,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?,
                                'FAILED', NULL, ?, ?, ?)
                        """,
                        (
                            locals().get(
                                "reference",
                                "FAILED-"
                                + secrets.token_hex(6)
                            ),
                            locals().get(
                                "idempotency_key",
                                secrets.token_hex(16)
                            ),
                            "BANK_TO_DIGITAL_STORE",
                            user["account_number"],
                            destination_account,
                            locals().get(
                                "amount",
                                0
                            ),
                            str(exc),
                            now(),
                            now()
                        )
                    )

                    failure_conn.commit()
                    failure_conn.close()

                except Exception:
                    pass

                raise ValueError(
                    "فشل التحويل: "
                    + str(exc)
                )

            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        except Exception as exc:

            error = str(exc)

    return render_template_string(
        EXTERNAL_TRANSFER_HTML,
        user=user,
        error=error,
        success=success
    )


# ============================================================
# DIGITAL STORE -> BANK
# ============================================================

@app.route(
    "/api/v1/transfers/inbound",
    methods=["POST"]
)
def digital_store_inbound():

    verify_digital_store_api_key()

    payload = request.get_json(
        silent=True
    )

    if not isinstance(payload, dict):

        return jsonify({
            "success": False,
            "error": "Invalid JSON"
        }), 400

    reference = str(
        payload.get(
            "reference",
            ""
        )
    ).strip()

    idempotency_key = str(
        payload.get(
            "idempotency_key"
        )
        or request.headers.get(
            "Idempotency-Key",
            ""
        )
    ).strip()

    source_account = str(
        payload.get(
            "source_account",
            ""
        )
    ).strip()

    destination_account = str(
        payload.get(
            "destination_account",
            ""
        )
    ).strip()

    description = str(
        payload.get(
            "description",
            "تحويل من DIGITAL STORE 249"
        )
    ).strip()

    if not reference:

        return jsonify({
            "success": False,
            "error": "reference is required"
        }), 400

    if not idempotency_key:

        return jsonify({
            "success": False,
            "error": "idempotency_key is required"
        }), 400

    if not destination_account:

        return jsonify({
            "success": False,
            "error": "destination_account is required"
        }), 400

    try:

        amount = normalize_amount(
            payload.get("amount")
        )

    except ValueError as exc:

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 400

    currency = str(
        payload.get(
            "currency",
            "SDG"
        )
    ).upper()

    if currency != "SDG":

        return jsonify({
            "success": False,
            "error": "Only SDG is supported"
        }), 400

    conn = get_db()

    try:

        # ----------------------------------------------------
        # Idempotency
        # ----------------------------------------------------

        existing_key = conn.execute(
            """
            SELECT *
            FROM idempotency_keys
            WHERE idempotency_key = ?
            """,
            (idempotency_key,)
        ).fetchone()

        if existing_key:

            existing_transaction = conn.execute(
                """
                SELECT *
                FROM transactions
                WHERE reference = ?
                """,
                (
                    existing_key["reference"],
                )
            ).fetchone()

            if existing_transaction:

                receiver = conn.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE account_number = ?
                    """,
                    (
                        destination_account,
                    )
                ).fetchone()

                return jsonify({
                    "success": True,
                    "status": "COMPLETED",
                    "duplicate": True,
                    "reference":
                        existing_transaction["reference"],
                    "receiver_account":
                        destination_account,
                    "receiver_name":
                        receiver["full_name"]
                        if receiver else "",
                    "amount":
                        existing_transaction["amount"],
                    "balance_after":
                        existing_transaction[
                            "receiver_after"
                        ]
                })

            return jsonify({
                "success": False,
                "status": "PENDING",
                "reference":
                    existing_key["reference"]
            }), 409

        # ----------------------------------------------------
        # BEGIN IMMEDIATE
        # ----------------------------------------------------

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        receiver = conn.execute(
            """
            SELECT *
            FROM users
            WHERE account_number = ?
              AND active = 1
            """,
            (
                destination_account,
            )
        ).fetchone()

        if not receiver:

            conn.rollback()

            return jsonify({
                "success": False,
                "error": "Bank account not found"
            }), 404

        receiver_before = int(
            receiver["balance"]
        )

        receiver_after = (
            receiver_before + amount
        )

        if receiver_after < 0:

            conn.rollback()

            return jsonify({
                "success": False,
                "error":
                    "Operation would create negative balance"
            }), 400

        # ----------------------------------------------------
        # تسجيل Idempotency
        # ----------------------------------------------------

        conn.execute(
            """
            INSERT INTO idempotency_keys (
                idempotency_key,
                reference,
                status,
                created_at
            )
            VALUES (?, ?, 'PROCESSING', ?)
            """,
            (
                idempotency_key,
                reference,
                now()
            )
        )

        # ----------------------------------------------------
        # تحديث الرصيد
        # ----------------------------------------------------

        conn.execute(
            """
            UPDATE users
            SET balance = ?
            WHERE account_number = ?
            """,
            (
                receiver_after,
                destination_account
            )
        )

        # ----------------------------------------------------
        # Transaction
        # ----------------------------------------------------

        conn.execute(
            """
            INSERT INTO transactions (
                reference,
                sender_account,
                receiver_account,
                amount,
                sender_before,
                sender_after,
                receiver_before,
                receiver_after,
                type,
                status,
                comment,
                phone,
                created_at
            )
            VALUES (?, ?, ?, ?, NULL, NULL, ?, ?,
                    'DIGITAL_STORE_TRANSFER',
                    'COMPLETED',
                    ?, ?, ?)
            """,
            (
                reference,
                "DIGITAL_STORE_249",
                destination_account,
                amount,
                receiver_before,
                receiver_after,
                description,
                "",
                now()
            )
        )

        # ----------------------------------------------------
        # Notification
        # ----------------------------------------------------

        conn.execute(
            """
            INSERT INTO notifications (
                account_number,
                message,
                reference,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                destination_account,
                (
                    "تم استلام "
                    f"{amount:,} SDG "
                    "من DIGITAL STORE 249"
                ),
                reference,
                now()
            )
        )

        # ----------------------------------------------------
        # External transfer log
        # ----------------------------------------------------

        conn.execute(
            """
            INSERT INTO external_transfers (
                reference,
                idempotency_key,
                direction,
                source_account,
                destination_account,
                amount,
                status,
                provider_reference,
                error_message,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference,
                idempotency_key,
                "DIGITAL_STORE_TO_BANK",
                source_account or "DIGITAL_STORE_249",
                destination_account,
                amount,
                "COMPLETED",
                reference,
                None,
                now(),
                now()
            )
        )

        conn.execute(
            """
            UPDATE idempotency_keys
            SET status = 'COMPLETED'
            WHERE idempotency_key = ?
            """,
            (
                idempotency_key,
            )
        )

        conn.commit()

        return jsonify({
            "success": True,
            "status": "COMPLETED",
            "reference": reference,
            "receiver_account":
                destination_account,
            "receiver_name":
                receiver["full_name"],
            "amount": amount,
            "currency": "SDG",
            "balance_before":
                receiver_before,
            "balance_after":
                receiver_after
        })

    except sqlite3.IntegrityError as exc:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": (
                "Duplicate or conflicting operation"
            ),
            "details": str(exc)
        }), 409

    except Exception as exc:

        conn.rollback()

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500

    finally:

        conn.close()


# ============================================================
# EXTERNAL TRANSFER LOOKUP
# ============================================================

@app.route(
    "/external_transfer",
    methods=["GET"]
)
def external_transfer():

    user = login_required()

    if not isinstance(user, sqlite3.Row):
        return user

    return render_template_string(
        EXTERNAL_MENU_HTML,
        user=user
    )


# ============================================================
# TRANSACTIONS
# ============================================================

@app.route("/transactions")
def transactions():

    user = login_required()

    if not isinstance(user, sqlite3.Row):
        return user

    conn = get_db()

    try:

        rows = conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE sender_account = ?
               OR receiver_account = ?
            ORDER BY id DESC
            LIMIT 100
            """,
            (
                user["account_number"],
                user["account_number"]
            )
        ).fetchall()

    finally:
        conn.close()

    return render_template_string(
        TRANSACTIONS_HTML,
        user=user,
        rows=rows
    )


# ============================================================
# RECEIPT
# ============================================================

@app.route(
    "/receipt/<reference>"
)
def receipt(reference):

    user = login_required()

    if not isinstance(user, sqlite3.Row):
        return user

    conn = get_db()

    try:

        tx = conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE reference = ?
            """,
            (reference,)
        ).fetchone()

    finally:
        conn.close()

    if not tx:
        abort(404)

    allowed = (
        user["role"] == "founder"
        or tx["sender_account"]
        == user["account_number"]
        or tx["receiver_account"]
        == user["account_number"]
    )

    if not allowed:
        abort(403)

    return render_template_string(
        RECEIPT_HTML,
        tx=tx
    )


# ============================================================
# NOTIFICATIONS
# ============================================================

@app.route("/notifications")
def notifications():

    user = login_required()

    if not isinstance(user, sqlite3.Row):
        return user

    conn = get_db()

    try:

        rows = conn.execute(
            """
            SELECT *
            FROM notifications
            WHERE account_number = ?
            ORDER BY id DESC
            LIMIT 100
            """,
            (
                user["account_number"],
            )
        ).fetchall()

    finally:
        conn.close()

    return render_template_string(
        NOTIFICATIONS_HTML,
        user=user,
        rows=rows
    )


# ============================================================
# API: CHECK EXTERNAL TRANSFER
# ============================================================

@app.route(
    "/api/v1/transfers/<reference>",
    methods=["GET"]
)
def api_transfer_status(reference):

    verify_digital_store_api_key()

    conn = get_db()

    try:

        transfer = conn.execute(
            """
            SELECT *
            FROM external_transfers
            WHERE reference = ?
            """,
            (reference,)
        ).fetchone()

        if not transfer:

            return jsonify({
                "success": False,
                "status": "NOT_FOUND",
                "reference": reference
            }), 404

        return jsonify({
            "success": True,
            "reference":
                transfer["reference"],
            "direction":
                transfer["direction"],
            "source_account":
                transfer["source_account"],
            "destination_account":
                transfer["destination_account"],
            "amount":
                transfer["amount"],
            "status":
                transfer["status"],
            "provider_reference":
                transfer["provider_reference"],
            "created_at":
                transfer["created_at"],
            "updated_at":
                transfer["updated_at"]
        })

    finally:
        conn.close()


# ============================================================
# API HEALTH
# ============================================================

@app.route(
    "/api/v1/health",
    methods=["GET"]
)
def api_health():

    return jsonify({
        "success": True,
        "service": APP_NAME,
        "api": "v1",
        "status": "online",
        "digital_store_configured":
            bool(
                DIGITAL_STORE_API_BASE_URL
                and DIGITAL_STORE_API_KEY
            )
    })


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(400)
def bad_request(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "error": getattr(
                error,
                "description",
                "Bad request"
            )
        }), 400

    return (
        "طلب غير صالح",
        400
    )


@app.errorhandler(401)
def unauthorized(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "error": getattr(
                error,
                "description",
                "Unauthorized"
            )
        }), 401

    return (
        "غير مصرح",
        401
    )


@app.errorhandler(403)
def forbidden(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "error": getattr(
                error,
                "description",
                "Forbidden"
            )
        }), 403

    return (
        "غير مصرح لك",
        403
    )


@app.errorhandler(404)
def not_found(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "error": "Not found"
        }), 404

    return (
        "الصفحة غير موجودة",
        404
    )


# ============================================================
# HTML
# ============================================================

LOGIN_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>NATIONAL ARAB BANK</title>
<style>
body {
    font-family: Arial;
    background:#f4f6f8;
    margin:0;
    padding:30px;
}
.box {
    max-width:420px;
    margin:50px auto;
    background:white;
    padding:25px;
    border-radius:15px;
    box-shadow:0 4px 20px #0001;
}
input,button {
    width:100%;
    padding:13px;
    margin:7px 0;
    box-sizing:border-box;
    border-radius:8px;
    border:1px solid #ddd;
}
button {
    background:#173b67;
    color:white;
    border:0;
    cursor:pointer;
}
.error {
    background:#ffe5e5;
    padding:10px;
    border-radius:8px;
    color:#a00;
}
a {
    text-decoration:none;
}
</style>
</head>
<body>

<div class="box">

<h2>NATIONAL ARAB BANK</h2>

{% if error %}
<div class="error">{{ error }}</div>
{% endif %}

<form method="post">

<input
 name="account_number"
 placeholder="رقم الحساب"
 required
>

<input
 type="password"
 name="password"
 placeholder="كلمة المرور"
 required
>

<button>
تسجيل الدخول
</button>

</form>

<p>
<a href="/register">
فتح حساب جديد
</a>
</p>

</div>

</body>
</html>
"""


REGISTER_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>فتح حساب</title>
<style>
body {
    font-family:Arial;
    background:#f4f6f8;
    padding:25px;
}
.box {
    max-width:500px;
    margin:auto;
    background:white;
    padding:25px;
    border-radius:15px;
}
input,button {
    width:100%;
    padding:12px;
    margin:7px 0;
    box-sizing:border-box;
}
button {
    background:#173b67;
    color:white;
    border:0;
}
.error {
    color:#a00;
    background:#ffe5e5;
    padding:10px;
}
</style>
</head>
<body>

<div class="box">

<h2>فتح حساب جديد</h2>

{% if error %}
<div class="error">{{ error }}</div>
{% endif %}

<form method="post">

<input
 name="full_name"
 placeholder="الاسم الكامل"
 required
>

<input
 name="username"
 placeholder="اسم المستخدم"
 required
>

<input
 type="password"
 name="password"
 placeholder="كلمة المرور"
 required
>

<input
 name="pin"
 inputmode="numeric"
 maxlength="4"
 placeholder="PIN - أربعة أرقام"
 required
>

<button>
فتح الحساب
</button>

</form>

</div>

</body>
</html>
"""


REGISTER_SUCCESS_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>تم إنشاء الحساب</title>
</head>
<body>

<div style="
max-width:500px;
margin:60px auto;
font-family:Arial;
text-align:center;
">

<h2>تم إنشاء الحساب بنجاح</h2>

<p>
رقم حسابك:
</p>

<h1>
{{ account_number }}
</h1>

<p>
الرصيد الابتدائي: 0 SDG
</p>

<a href="/login">
تسجيل الدخول
</a>

</div>

</body>
</html>
"""


ACCOUNT_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>الحساب</title>
<style>
body {
font-family:Arial;
background:#f4f6f8;
margin:0;
padding:20px;
}
.box {
max-width:850px;
margin:auto;
background:white;
padding:25px;
border-radius:15px;
}
.balance {
font-size:32px;
font-weight:bold;
}
a,button {
display:inline-block;
padding:10px 14px;
margin:5px;
background:#173b67;
color:white;
text-decoration:none;
border-radius:8px;
}
table {
width:100%;
border-collapse:collapse;
margin-top:20px;
}
td,th {
padding:10px;
border-bottom:1px solid #eee;
}
</style>
</head>
<body>

<div class="box">

<h2>
NATIONAL ARAB BANK
</h2>

<h3>
{{ user["full_name"] }}
</h3>

<p>
رقم الحساب:
<b>{{ user["account_number"] }}</b>
</p>

<div class="balance">
{{ "{:,}".format(user["balance"]) }} SDG
</div>

<hr>

<a href="/transfer">
تحويل داخلي
</a>

<a href="/external_transfer">
تحويل إلى DIGITAL STORE 249
</a>

<a href="/transactions">
الحركات
</a>

<a href="/notifications">
الإشعارات
</a>

<a href="/logout">
خروج
</a>

<h3>
آخر العمليات
</h3>

<table>

<tr>
<th>المرجع</th>
<th>المبلغ</th>
<th>النوع</th>
<th>الحالة</th>
</tr>

{% for tx in transactions %}

<tr>
<td>
<a href="/receipt/{{ tx['reference'] }}">
{{ tx["reference"] }}
</a>
</td>

<td>
{{ "{:,}".format(tx["amount"]) }} SDG
</td>

<td>
{{ tx["type"] }}
</td>

<td>
{{ tx["status"] }}
</td>
</tr>

{% endfor %}

</table>

</div>

</body>
</html>
"""


TRANSFER_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>تحويل</title>
<style>
body {
font-family:Arial;
background:#f4f6f8;
padding:20px;
}
.box {
max-width:500px;
margin:auto;
background:white;
padding:25px;
border-radius:15px;
}
input,button {
width:100%;
padding:13px;
margin:7px 0;
box-sizing:border-box;
}
button {
background:#173b67;
color:white;
border:0;
}
.error {
background:#ffe5e5;
color:#a00;
padding:10px;
}
</style>
</head>
<body>

<div class="box">

<h2>تحويل داخلي</h2>

{% if error %}
<div class="error">
{{ error }}
</div>
{% endif %}

<p>
رصيدك:
<b>{{ "{:,}".format(user["balance"]) }} SDG</b>
</p>

<form method="post">

<input
name="receiver_account"
placeholder="رقم حساب المستفيد"
required
>

<input
type="number"
name="amount"
min="1"
step="1"
placeholder="المبلغ"
required
>

<input
type="password"
name="pin"
maxlength="4"
inputmode="numeric"
placeholder="PIN"
required
>

<input
name="comment"
placeholder="ملاحظات"
>

<button>
تنفيذ التحويل
</button>

</form>

<a href="/account">
العودة
</a>

</div>

</body>
</html>
"""


EXTERNAL_MENU_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>التحويل الخارجي</title>
</head>
<body>

<div style="
max-width:500px;
margin:50px auto;
font-family:Arial;
">

<h2>
تحويل إلى DIGITAL STORE 249
</h2>

<p>
رصيدك:
<b>
{{ "{:,}".format(user["balance"]) }} SDG
</b>
</p>

<a href="/external_transfer/digital_store">
تنفيذ تحويل إلى DIGITAL STORE
</a>

<br><br>

<a href="/account">
العودة للحساب
</a>

</div>

</body>
</html>
"""


EXTERNAL_TRANSFER_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>DIGITAL STORE 249</title>
<style>
body {
font-family:Arial;
background:#f4f6f8;
padding:20px;
}
.box {
max-width:550px;
margin:auto;
background:white;
padding:25px;
border-radius:15px;
}
input,button {
width:100%;
padding:13px;
margin:7px 0;
box-sizing:border-box;
}
button {
background:#173b67;
color:white;
border:0;
}
.error {
background:#ffe5e5;
color:#a00;
padding:12px;
border-radius:8px;
}
</style>
</head>
<body>

<div class="box">

<h2>
تحويل إلى DIGITAL STORE 249
</h2>

{% if error %}
<div class="error">
{{ error }}
</div>
{% endif %}

<p>
حساب المرسل:
<b>{{ user["account_number"] }}</b>
</p>

<p>
الرصيد:
<b>{{ "{:,}".format(user["balance"]) }} SDG</b>
</p>

<form method="post">

<input
name="destination_account"
placeholder="رقم حساب DIGITAL STORE"
required
>

<input
type="number"
name="amount"
min="1"
step="1"
placeholder="المبلغ بالجنيه السوداني"
required
>

<input
type="password"
name="pin"
maxlength="4"
inputmode="numeric"
placeholder="PIN"
required
>

<input
name="comment"
value="تحويل إلى DIGITAL STORE 249"
>

<button>
تأكيد التحويل
</button>

</form>

<a href="/account">
العودة
</a>

</div>

</body>
</html>
"""


TRANSACTIONS_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>الحركات</title>
</head>
<body>

<div style="
max-width:900px;
margin:30px auto;
font-family:Arial;
">

<h2>
سجل العمليات
</h2>

<table style="
width:100%;
border-collapse:collapse;
">

<tr>
<th>المرجع</th>
<th>من</th>
<th>إلى</th>
<th>المبلغ</th>
<th>النوع</th>
<th>الحالة</th>
<th>التاريخ</th>
</tr>

{% for tx in rows %}

<tr style="border-bottom:1px solid #ddd">

<td>
<a href="/receipt/{{ tx['reference'] }}">
{{ tx["reference"] }}
</a>
</td>

<td>
{{ tx["sender_account"] or "-" }}
</td>

<td>
{{ tx["receiver_account"] or "-" }}
</td>

<td>
{{ "{:,}".format(tx["amount"]) }} SDG
</td>

<td>
{{ tx["type"] }}
</td>

<td>
{{ tx["status"] }}
</td>

<td>
{{ tx["created_at"] }}
</td>

</tr>

{% endfor %}

</table>

<br>

<a href="/account">
العودة للحساب
</a>

</div>

</body>
</html>
"""


RECEIPT_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>إيصال</title>
</head>
<body>

<div style="
max-width:600px;
margin:40px auto;
font-family:Arial;
background:#fff;
padding:25px;
border-radius:15px;
box-shadow:0 4px 20px #0001;
">

<h2>
إيصال العملية
</h2>

<p>
المرجع:
<b>{{ tx["reference"] }}</b>
</p>

<p>
من:
<b>{{ tx["sender_account"] or "-" }}</b>
</p>

<p>
إلى:
<b>{{ tx["receiver_account"] or "-" }}</b>
</p>

<p>
المبلغ:
<b>
{{ "{:,}".format(tx["amount"]) }} SDG
</b>
</p>

<p>
الحالة:
<b>{{ tx["status"] }}</b>
</p>

<p>
النوع:
{{ tx["type"] }}
</p>

<p>
التاريخ:
{{ tx["created_at"] }}
</p>

<p>
{{ tx["comment"] or "" }}
</p>

<a href="/account">
العودة للحساب
</a>

</div>

</body>
</html>
"""


NOTIFICATIONS_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>الإشعارات</title>
</head>
<body>

<div style="
max-width:700px;
margin:30px auto;
font-family:Arial;
">

<h2>
الإشعارات
</h2>

{% for row in rows %}

<div style="
padding:15px;
margin:10px 0;
background:#f4f6f8;
border-radius:10px;
">

<b>
{{ row["message"] }}
</b>

<br>

<small>
{{ row["created_at"] }}
</small>

{% if row["reference"] %}
<br>
المرجع:
{{ row["reference"] }}
{% endif %}

</div>

{% endfor %}

<a href="/account">
العودة للحساب
</a>

</div>

</body>
</html>
"""


# ============================================================
# STARTUP
# ============================================================

init_db()


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
