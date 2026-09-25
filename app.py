import os
import re
import secrets
import sqlite3
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from functools import wraps

import bcrypt
from dotenv import load_dotenv
from flask import (
    Flask,
    g,
    request,
    session,
    redirect,
    url_for,
    render_template_string,
    flash,
)

load_dotenv()

# ============================================================
# NATIONAL ARAB BANK
# Corrected Production-Oriented Version
# ============================================================

app = Flask(__name__)

SECRET_KEY = os.getenv("FLASK_SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY is required")

app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv(
        "SESSION_COOKIE_SECURE",
        "1"
    ) == "1",
    SESSION_COOKIE_NAME="nab_session",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

DATABASE = os.getenv(
    "NAB_DATABASE",
    "national_arab_bank.db"
)

FOUNDER_USERNAME = "founder"
FOUNDER_ACCOUNT = "5147234"
FOUNDER_INITIAL_BALANCE = 3_000_000_000_000


# ============================================================
# DATABASE
# ============================================================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            DATABASE,
            timeout=30,
            isolation_level=None,
        )

        g.db.row_factory = sqlite3.Row

        g.db.execute(
            "PRAGMA foreign_keys = ON"
        )

        g.db.execute(
            "PRAGMA journal_mode = WAL"
        )

        g.db.execute(
            "PRAGMA synchronous = FULL"
        )

        g.db.execute(
            "PRAGMA busy_timeout = 30000"
        )

    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    database = g.pop("db", None)

    if database is not None:
        database.close()


def now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def init_db():
    database = get_db()

    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            username TEXT UNIQUE NOT NULL,

            full_name TEXT NOT NULL,

            account_number TEXT UNIQUE NOT NULL,

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

            reference TEXT UNIQUE NOT NULL,

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

            username TEXT NOT NULL,

            title TEXT NOT NULL,

            message TEXT NOT NULL,

            reference TEXT,

            is_read INTEGER NOT NULL DEFAULT 0,

            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,

            username TEXT NOT NULL,

            reference TEXT NOT NULL,

            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_transactions_sender
        ON transactions(sender_account);

        CREATE INDEX IF NOT EXISTS idx_transactions_receiver
        ON transactions(receiver_account);

        CREATE INDEX IF NOT EXISTS idx_notifications_user
        ON notifications(username);

        CREATE INDEX IF NOT EXISTS idx_users_account
        ON users(account_number);

        CREATE INDEX IF NOT EXISTS idx_users_username
        ON users(username);
        """
    )

    founder = database.execute(
        """
        SELECT id
        FROM users
        WHERE username = ?
        """,
        (FOUNDER_USERNAME,),
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
                "FOUNDER_PASSWORD is required before first startup"
            )

        if not founder_pin:
            raise RuntimeError(
                "FOUNDER_PIN is required before first startup"
            )

        if not re.fullmatch(
            r"\d{4}",
            founder_pin,
        ):
            raise RuntimeError(
                "FOUNDER_PIN must contain exactly 4 digits"
            )

        # التأكد من أن رقم حساب المؤسس 7 أرقام
        if not re.fullmatch(
            r"\d{7}",
            FOUNDER_ACCOUNT,
        ):
            raise RuntimeError(
                "FOUNDER_ACCOUNT must contain exactly 7 digits"
            )

        password_hash = bcrypt.hashpw(
            founder_password.encode("utf-8"),
            bcrypt.gensalt(),
        ).decode("utf-8")

        pin_hash = bcrypt.hashpw(
            founder_pin.encode("utf-8"),
            bcrypt.gensalt(),
        ).decode("utf-8")

        database.execute(
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
                now(),
            ),
        )


# ============================================================
# TRANSACTION HELPERS
# ============================================================

def begin_transaction(database):
    database.execute(
        "BEGIN IMMEDIATE"
    )


def rollback_safely(database):
    try:
        if database.in_transaction:
            database.rollback()
    except Exception:
        pass


def commit_safely(database):
    database.commit()


def generate_reference():
    database = get_db()

    for _ in range(100):

        reference = "".join(
            str(secrets.randbelow(10))
            for _ in range(12)
        )

        exists = database.execute(
            """
            SELECT 1
            FROM transactions
            WHERE reference = ?
            """,
            (reference,),
        ).fetchone()

        if not exists:
            return reference

    raise RuntimeError(
        "تعذر إنشاء رقم عملية فريد."
    )


# ============================================================
# ACCOUNT NUMBER
# 7 DIGITS EXACTLY
# ============================================================

def generate_account_number():
    database = get_db()

    for _ in range(100):

        # رقم الحساب = 7 أرقام بالضبط
        #
        # الرقم الأول من 1 إلى 9
        # حتى لا يبدأ الحساب بصفر
        #
        # ثم 6 أرقام عشوائية
        #
        # مثال:
        # 1234567
        # 5821043
        # 9472018

        first_digit = str(
            secrets.randbelow(9) + 1
        )

        remaining_digits = "".join(
            str(secrets.randbelow(10))
            for _ in range(6)
        )

        account_number = (
            first_digit
            + remaining_digits
        )

        # تحقق إضافي:
        # يجب أن يكون الرقم 7 أرقام بالضبط.
        if not re.fullmatch(
            r"\d{7}",
            account_number,
        ):
            continue

        exists = database.execute(
            """
            SELECT 1
            FROM users
            WHERE account_number = ?
            """,
            (account_number,),
        ).fetchone()

        if not exists:
            return account_number

    raise RuntimeError(
        "تعذر إنشاء رقم حساب فريد من 7 أرقام."
    )


def create_notification(
    database,
    username,
    title,
    message,
    reference=None,
):
    database.execute(
        """
        INSERT INTO notifications (
            username,
            title,
            message,
            reference,
            is_read,
            created_at
        )
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (
            username,
            title,
            message,
            reference,
            now(),
        ),
    )


def parse_amount(value):
    value = (value or "").strip()

    if not value:
        raise ValueError(
            "الرجاء إدخال المبلغ."
        )

    try:
        amount_decimal = Decimal(value)

    except InvalidOperation:
        raise ValueError(
            "المبلغ غير صالح."
        )

    if not amount_decimal.is_finite():
        raise ValueError(
            "المبلغ غير صالح."
        )

    if amount_decimal <= 0:
        raise ValueError(
            "المبلغ يجب أن يكون أكبر من صفر."
        )

    if (
        amount_decimal
        != amount_decimal.to_integral_value()
    ):
        raise ValueError(
            "المبلغ يجب أن يكون بالجنيه الصحيح دون كسور."
        )

    amount = int(
        amount_decimal
    )

    if amount <= 0:
        raise ValueError(
            "المبلغ يجب أن يكون أكبر من صفر."
        )

    return amount


def verify_pin(user, pin):
    pin = (pin or "").strip()

    if not re.fullmatch(
        r"\d{4}",
        pin,
    ):
        raise ValueError(
            "رمز PIN يجب أن يكون 4 أرقام."
        )

    try:
        valid = bcrypt.checkpw(
            pin.encode("utf-8"),
            user["pin_hash"].encode("utf-8"),
        )

    except Exception:
        valid = False

    if not valid:
        raise ValueError(
            "رمز PIN غير صحيح."
        )


# ============================================================
# CSRF
# ============================================================

def get_csrf_token():

    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(
            32
        )

    return session["csrf"]


def verify_csrf():

    submitted = request.form.get(
        "csrf",
        "",
    )

    stored = session.get(
        "csrf",
        "",
    )

    if (
        not submitted
        or not stored
        or not secrets.compare_digest(
            submitted,
            stored,
        )
    ):
        raise ValueError(
            "رمز الحماية غير صالح أو منتهي."
        )


# ============================================================
# AUTH
# ============================================================

def current_user():

    username = session.get(
        "username"
    )

    if not username:
        return None

    return get_db().execute(
        """
        SELECT *
        FROM users
        WHERE username = ?
          AND active = 1
        """,
        (username,),
    ).fetchone()


def login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not current_user():
            return redirect(
                url_for("login")
            )

        return function(
            *args,
            **kwargs
        )

    return wrapper


def money(amount):

    try:
        return f"{int(amount):,}"

    except Exception:
        return "0"


@app.context_processor
def inject_globals():

    return {
        "csrf_token": get_csrf_token,
        "money": money,
    }


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():

    if current_user():
        return redirect(
            url_for("account")
        )

    return redirect(
        url_for("login")
    )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        account_number = (
            request.form
            .get(
                "account_number",
                "",
            )
            .strip()
        )

        password = request.form.get(
            "password",
            "",
        )

        database = get_db()

        user = database.execute(
            """
            SELECT *
            FROM users
            WHERE account_number = ?
              AND active = 1
            """,
            (account_number,),
        ).fetchone()

        valid = False

        if user:

            try:
                valid = bcrypt.checkpw(
                    password.encode("utf-8"),
                    user[
                        "password_hash"
                    ].encode("utf-8"),
                )

            except Exception:
                valid = False

        if valid:

            session.clear()

            session.permanent = True

            session["username"] = (
                user["username"]
            )

            session["csrf"] = (
                secrets.token_urlsafe(32)
            )

            return redirect(
                url_for("account")
            )

        flash(
            "رقم الحساب أو كلمة المرور غير صحيحة."
        )

    return render_template_string(
        LOGIN_HTML
    )


# ============================================================
# REGISTER
# ============================================================

@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    if request.method == "POST":

        try:

            full_name = (
                request.form
                .get(
                    "full_name",
                    "",
                )
                .strip()
            )

            username = (
                request.form
                .get(
                    "username",
                    "",
                )
                .strip()
            )

            password = request.form.get(
                "password",
                "",
            )

            pin = (
                request.form
                .get(
                    "pin",
                    "",
                )
                .strip()
            )

            if not full_name:
                raise ValueError(
                    "الرجاء إدخال الاسم."
                )

            if not username:
                raise ValueError(
                    "الرجاء إدخال اسم المستخدم."
                )

            if len(username) > 50:
                raise ValueError(
                    "اسم المستخدم طويل جدًا."
                )

            if not password:
                raise ValueError(
                    "الرجاء إدخال كلمة المرور."
                )

            if len(
                password.encode("utf-8")
            ) > 72:
                raise ValueError(
                    "كلمة المرور طويلة جدًا."
                )

            if not re.fullmatch(
                r"\d{4}",
                pin,
            ):
                raise ValueError(
                    "رمز PIN يجب أن يكون 4 أرقام."
                )

            database = get_db()

            username_exists = database.execute(
                """
                SELECT 1
                FROM users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()

            if username_exists:
                raise ValueError(
                    "اسم المستخدم مستخدم بالفعل."
                )

            # إنشاء رقم حساب جديد
            account_number = (
                generate_account_number()
            )

            # تحقق دفاعي إضافي قبل الحفظ
            if not re.fullmatch(
                r"\d{7}",
                account_number,
            ):
                raise RuntimeError(
                    "خطأ داخلي: رقم الحساب يجب أن يتكون من 7 أرقام."
                )

            # تحقق إضافي من عدم التكرار
            account_exists = database.execute(
                """
                SELECT 1
                FROM users
                WHERE account_number = ?
                """,
                (account_number,),
            ).fetchone()

            if account_exists:
                raise RuntimeError(
                    "رقم الحساب موجود بالفعل."
                )

            password_hash = bcrypt.hashpw(
                password.encode("utf-8"),
                bcrypt.gensalt(),
            ).decode("utf-8")

            pin_hash = bcrypt.hashpw(
                pin.encode("utf-8"),
                bcrypt.gensalt(),
            ).decode("utf-8")

            # الحساب الجديد يبدأ برصيد صفر
            database.execute(
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
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    0,
                    'SDG',
                    'customer',
                    1,
                    ?
                )
                """,
                (
                    username,
                    full_name,
                    account_number,
                    password_hash,
                    pin_hash,
                    now(),
                ),
            )

            database.commit()

            flash(
                "تم إنشاء الحساب بنجاح. "
                f"رقم حسابك هو: {account_number}"
            )

            return redirect(
                url_for("login")
            )

        except sqlite3.IntegrityError:

            flash(
                "تعذر إنشاء الحساب. "
                "اسم المستخدم أو رقم الحساب مستخدم بالفعل."
            )

        except Exception as error:

            flash(
                str(error)
            )

    return render_template_string(
        REGISTER_HTML
    )


# ============================================================
# LOGOUT
# ============================================================

@app.get("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# ACCOUNT
# ============================================================

@app.get("/account")
@login_required
def account():

    user = current_user()

    return render_template_string(
        ACCOUNT_HTML,
        user=user,
    )


@app.get("/account_details")
@login_required
def account_details():

    user = current_user()

    return render_template_string(
        ACCOUNT_DETAILS_HTML,
        user=user,
    )


# ============================================================
# INTERNAL TRANSFER
# ============================================================

@app.route(
    "/transfer",
    methods=["GET", "POST"]
)
@login_required
def transfer():

    user = current_user()

    receiver_account = (
        request.values
        .get(
            "receiver_account",
            "",
        )
        .strip()
    )

    receiver_obj = None

    database = get_db()

    if receiver_account:

        receiver_obj = database.execute(
            """
            SELECT *
            FROM users
            WHERE account_number = ?
              AND active = 1
            """,
            (receiver_account,),
        ).fetchone()

    if request.method == "POST":

        action = request.form.get(
            "action",
            "",
        )

        # ----------------------------------------------------
        # LOOKUP
        # ----------------------------------------------------

        if action == "lookup":

            if not receiver_account:

                flash(
                    "الرجاء إدخال رقم الحساب."
                )

            elif not receiver_obj:

                flash(
                    "رقم الحساب غير موجود أو غير نشط."
                )

            elif (
                receiver_obj["account_number"]
                == user["account_number"]
            ):

                flash(
                    "لا يمكن التحويل إلى نفس الحساب."
                )

            else:

                return render_template_string(
                    TRANSFER_FORM_HTML,
                    user=user,
                    receiver=receiver_obj,
                    receiver_account=receiver_account,
                    idempotency_key=secrets.token_urlsafe(
                        24
                    ),
                )

            return render_template_string(
                TRANSFER_INDEX_HTML,
                user=user,
            )

        # ----------------------------------------------------
        # EXECUTE
        # ----------------------------------------------------

        if action == "execute":

            try:

                verify_csrf()

                receiver_account = (
                    request.form
                    .get(
                        "receiver_account",
                        "",
                    )
                    .strip()
                )

                if not receiver_account:
                    raise ValueError(
                        "رقم حساب المستلم مطلوب."
                    )

                if (
                    receiver_account
                    == user["account_number"]
                ):
                    raise ValueError(
                        "لا يمكن التحويل إلى نفس الحساب."
                    )

                pin = request.form.get(
                    "pin",
                    "",
                )

                phone = (
                    request.form
                    .get(
                        "phone",
                        "",
                    )
                    .strip()
                )

                comment = (
                    request.form
                    .get(
                        "comment",
                        "",
                    )
                    .strip()
                )

                amount = parse_amount(
                    request.form.get(
                        "amount",
                        "",
                    )
                )

                idempotency_key = (
                    request.form
                    .get(
                        "idempotency_key",
                        "",
                    )
                    .strip()
                )

                if not idempotency_key:
                    raise ValueError(
                        "مفتاح العملية مفقود."
                    )

                if len(
                    idempotency_key
                ) > 200:
                    raise ValueError(
                        "مفتاح العملية غير صالح."
                    )

                verify_pin(
                    user,
                    pin,
                )

                begin_transaction(
                    database
                )

                # --------------------------------------------
                # منع تكرار العملية
                # --------------------------------------------

                old_operation = database.execute(
                    """
                    SELECT *
                    FROM idempotency_keys
                    WHERE key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()

                if old_operation:

                    database.rollback()

                    return redirect(
                        url_for(
                            "receipt",
                            reference=old_operation[
                                "reference"
                            ],
                        )
                    )

                # --------------------------------------------
                # إعادة تحميل الحسابين داخل القفل
                # --------------------------------------------

                sender = database.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE account_number = ?
                      AND active = 1
                    """,
                    (
                        user[
                            "account_number"
                        ],
                    ),
                ).fetchone()

                receiver = database.execute(
                    """
                    SELECT *
                    FROM users
                    WHERE account_number = ?
                      AND active = 1
                    """,
                    (receiver_account,),
                ).fetchone()

                if not sender:

                    raise ValueError(
                        "حساب المرسل غير موجود أو غير نشط."
                    )

                if not receiver:

                    raise ValueError(
                        "حساب المستلم غير موجود أو غير نشط."
                    )

                if (
                    sender["account_number"]
                    == receiver["account_number"]
                ):

                    raise ValueError(
                        "لا يمكن التحويل إلى نفس الحساب."
                    )

                sender_before = int(
                    sender["balance"]
                )

                receiver_before = int(
                    receiver["balance"]
                )

                if sender_before < amount:

                    raise ValueError(
                        "رصيدك الحالي غير كافٍ."
                    )

                sender_after = (
                    sender_before - amount
                )

                receiver_after = (
                    receiver_before + amount
                )

                if sender_after < 0:

                    raise ValueError(
                        "لا يمكن أن يصبح رصيد الحساب سالبًا."
                    )

                reference = (
                    generate_reference()
                )

                # --------------------------------------------
                # خصم المرسل
                # --------------------------------------------

                sender_update = database.execute(
                    """
                    UPDATE users
                    SET balance = balance - ?
                    WHERE account_number = ?
                      AND active = 1
                      AND balance >= ?
                    """,
                    (
                        amount,
                        sender[
                            "account_number"
                        ],
                        amount,
                    ),
                )

                if sender_update.rowcount != 1:

                    raise ValueError(
                        "تعذر خصم المبلغ من حساب المرسل."
                    )

                # --------------------------------------------
                # إضافة المستلم
                # --------------------------------------------

                receiver_update = database.execute(
                    """
                    UPDATE users
                    SET balance = balance + ?
                    WHERE account_number = ?
                      AND active = 1
                    """,
                    (
                        amount,
                        receiver[
                            "account_number"
                        ],
                    ),
                )

                if receiver_update.rowcount != 1:

                    raise ValueError(
                        "تعذر إيداع المبلغ في حساب المستلم."
                    )

                # --------------------------------------------
                # التأكد من الرصيد بعد العملية
                # --------------------------------------------

                sender_check = database.execute(
                    """
                    SELECT balance
                    FROM users
                    WHERE account_number = ?
                    """,
                    (
                        sender[
                            "account_number"
                        ],
                    ),
                ).fetchone()

                receiver_check = database.execute(
                    """
                    SELECT balance
                    FROM users
                    WHERE account_number = ?
                    """,
                    (
                        receiver[
                            "account_number"
                        ],
                    ),
                ).fetchone()

                if not sender_check:

                    raise ValueError(
                        "تعذر التحقق من رصيد المرسل."
                    )

                if not receiver_check:

                    raise ValueError(
                        "تعذر التحقق من رصيد المستلم."
                    )

                if (
                    int(
                        sender_check[
                            "balance"
                        ]
                    )
                    != sender_after
                ):

                    raise ValueError(
                        "حدث اختلاف في رصيد المرسل."
                    )

                if (
                    int(
                        receiver_check[
                            "balance"
                        ]
                    )
                    != receiver_after
                ):

                    raise ValueError(
                        "حدث اختلاف في رصيد المستلم."
                    )

                # --------------------------------------------
                # تسجيل المعاملة
                # --------------------------------------------

                database.execute(
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
                    VALUES (
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
                        reference,
                        sender[
                            "account_number"
                        ],
                        receiver[
                            "account_number"
                        ],
                        amount,
                        sender_before,
                        sender_after,
                        receiver_before,
                        receiver_after,
                        "TRANSFER",
                        "COMPLETED",
                        comment[:500],
                        phone[:50],
                        now(),
                    ),
                )

                # --------------------------------------------
                # مفتاح منع التكرار
                # --------------------------------------------

                database.execute(
                    """
                    INSERT INTO idempotency_keys (
                        key,
                        username,
                        reference,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        sender["username"],
                        reference,
                        now(),
                    ),
                )

                # --------------------------------------------
                # إشعار المرسل
                # --------------------------------------------

                create_notification(
                    database,
                    sender["username"],
                    "تحويل صادر",
                    (
                        f"تم تحويل مبلغ "
                        f"{money(amount)} SDG "
                        f"إلى {receiver['full_name']} "
                        f"برقم حساب "
                        f"{receiver['account_number']}."
                    ),
                    reference,
                )

                # --------------------------------------------
                # إشعار المستلم
                # --------------------------------------------

                create_notification(
                    database,
                    receiver["username"],
                    "تحويل وارد",
                    (
                        f"تم استلام مبلغ "
                        f"{money(amount)} SDG "
                        f"من {sender['full_name']} "
                        f"برقم حساب "
                        f"{sender['account_number']}."
                    ),
                    reference,
                )

                commit_safely(
                    database
                )

                return redirect(
                    url_for(
                        "receipt",
                        reference=reference,
                    )
                )

            except sqlite3.IntegrityError:

                rollback_safely(
                    database
                )

                flash(
                    "تعذر تسجيل العملية بسبب تعارض في بيانات العملية."
                )

                return render_template_string(
                    TRANSFER_FORM_HTML,
                    user=user,
                    receiver=receiver_obj,
                    receiver_account=receiver_account,
                    idempotency_key=secrets.token_urlsafe(
                        24
                    ),
                )

            except Exception as error:

                rollback_safely(
                    database
                )

                flash(
                    str(error)
                )

                return render_template_string(
                    TRANSFER_FORM_HTML,
                    user=user,
                    receiver=receiver_obj,
                    receiver_account=receiver_account,
                    idempotency_key=secrets.token_urlsafe(
                        24
                    ),
                )

    return render_template_string(
        TRANSFER_INDEX_HTML,
        user=user,
    )


# ============================================================
# EXTERNAL TRANSFER MENU
# ============================================================

@app.get("/external_transfer")
@login_required
def external_transfer_menu():

    user = current_user()

    return render_template_string(
        EXTERNAL_TRANSFER_MENU_HTML,
        user=user,
    )


# ============================================================
# DIGITAL STORE EXTERNAL TRANSFER
# ============================================================

@app.route(
    "/external_transfer/digital_store",
    methods=["GET", "POST"],
)
@login_required
def external_transfer_digital_store():

    user = current_user()

    receiver_account = (
        request.values
        .get(
            "receiver_account",
            "",
        )
        .strip()
    )

    receiver_obj = None

    database = get_db()

    # لا ننشئ حسابًا وهميًا.
    # لا بد من وجود تكامل حقيقي
    # مع DIGITAL STORE.

    if request.method == "POST":

        action = request.form.get(
            "action",
            "",
        )

        if action == "lookup":

            if not receiver_account:

                flash(
                    "الرجاء إدخال رقم الحساب."
                )

            else:

                flash(
                    "لا يوجد اتصال فعلي بمنصة DIGITAL STORE "
                    "للتحقق من الحساب في هذه النسخة."
                )

            return render_template_string(
                EXTERNAL_TRANSFER_LOOKUP_HTML,
                user=user,
            )

        if action == "execute":

            try:

                verify_csrf()

                raise ValueError(
                    "التحويل إلى DIGITAL STORE غير متاح حاليًا "
                    "لعدم وجود API أو اتصال قاعدة بيانات موثوق "
                    "بين البنك والمنصة. لم يتم خصم أي مبلغ."
                )

            except Exception as error:

                rollback_safely(
                    database
                )

                flash(
                    str(error)
                )

                return render_template_string(
                    EXTERNAL_TRANSFER_LOOKUP_HTML,
                    user=user,
                )

    return render_template_string(
        EXTERNAL_TRANSFER_LOOKUP_HTML,
        user=user,
    )


# ============================================================
# RECEIPT
# ============================================================

@app.get("/receipt/<reference>")
@login_required
def receipt(reference):

    reference = (
        reference or ""
    ).strip()

    if not reference:
        return "Not found", 404

    database = get_db()

    transaction = database.execute(
        """
        SELECT *
        FROM transactions
        WHERE reference = ?
        """,
        (reference,),
    ).fetchone()

    if not transaction:
        return "Not found", 404

    user = current_user()

    # تحقق دقيق
    allowed = (
        user["role"] == "founder"
        or transaction[
            "sender_account"
        ] == user["account_number"]
        or transaction[
            "receiver_account"
        ] == user["account_number"]
    )

    if not allowed:
        return "Forbidden", 403

    receiver_acc = (
        transaction[
            "receiver_account"
        ]
    )

    receiver_user = None

    if receiver_acc:

        receiver_user = database.execute(
            """
            SELECT *
            FROM users
            WHERE account_number = ?
            """,
            (receiver_acc,),
        ).fetchone()

    if (
        not receiver_user
        and receiver_acc
        and receiver_acc.startswith(
            "DIGITAL-"
        )
    ):

        receiver_user = {
            "full_name":
                "منصة ديجيتال ستوري "
                + receiver_acc.replace(
                    "DIGITAL-",
                    "",
                    1,
                )
        }

    return render_template_string(
        RECEIPT_HTML,
        transaction=transaction,
        receiver_user=receiver_user,
    )


# ============================================================
# HISTORY
# ============================================================

@app.get("/history")
@login_required
def history():

    user = current_user()

    transactions = get_db().execute(
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
            user["account_number"],
        ),
    ).fetchall()

    return render_template_string(
        HISTORY_HTML,
        transactions=transactions,
    )


# ============================================================
# NOTIFICATIONS
# ============================================================

@app.get("/notifications")
@login_required
def notifications():

    user = current_user()

    database = get_db()

    rows = database.execute(
        """
        SELECT *
        FROM notifications
        WHERE username = ?
        ORDER BY id DESC
        LIMIT 100
        """,
        (
            user["username"],
        ),
    ).fetchall()

    database.execute(
        """
        UPDATE notifications
        SET is_read = 1
        WHERE username = ?
        """,
        (
            user["username"],
        ),
    )

    database.commit()

    return render_template_string(
        NOTIFICATIONS_HTML,
        notifications=rows,
    )


# ============================================================
# GENERIC SECTION
# ============================================================

@app.get("/generic_section")
@login_required
def generic_section():

    title = request.args.get(
        "title",
        "خدمة المصرف",
    )

    title = title[:100]

    return render_template_string(
        GENERIC_SECTION_HTML,
        title=title,
    )


# ============================================================
# HTML
# ============================================================

LOGIN_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>البنك الوطني العربي - تسجيل الدخول</title>

<style>

body {
    margin:0;
    background:linear-gradient(135deg,#0056b3,#003366);
    font-family:Arial,sans-serif;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    min-height:100vh;
}

.logo-container {
    text-align:center;
    color:white;
    margin-bottom:20px;
}

.logo-container h1 {
    margin:0;
    font-size:30px;
    font-weight:bold;
}

.box {
    background:white;
    width:90%;
    max-width:380px;
    padding:30px 20px;
    border-radius:12px;
    box-shadow:0 8px 25px rgba(0,0,0,.25);
}

input,
button {
    width:100%;
    box-sizing:border-box;
    padding:12px;
    margin:10px 0;
    border-radius:6px;
    border:1px solid #ccc;
    font-size:15px;
}

button {
    background:linear-gradient(135deg,#007bff,#0056b3);
    color:white;
    border:0;
    font-weight:bold;
    cursor:pointer;
}

.flash {
    color:#d90429;
    text-align:center;
    font-size:13px;
    font-weight:bold;
}

.links {
    display:flex;
    justify-content:space-between;
    margin-top:15px;
    font-size:13px;
}

.links a {
    color:#0056b3;
    text-decoration:none;
    font-weight:bold;
}

</style>
</head>

<body>

<div class="logo-container">
    <h1>البنك الوطني العربي</h1>
</div>

<div class="box">

{% with messages = get_flashed_messages() %}

    {% for message in messages %}

        <p class="flash">
            {{ message }}
        </p>

    {% endfor %}

{% endwith %}

<form method="post">

<input
    name="account_number"
    placeholder="رقم الحساب"
    required
>

<input
    name="password"
    type="password"
    placeholder="كلمة المرور"
    required
>

<button type="submit">
    تسجيل الدخول
</button>

</form>

<div class="links">

<a href="/register">
    تسجيل حساب جديد
</a>

<a href="#">
    نسيت كلمة المرور؟
</a>

</div>

</div>

</body>
</html>
"""


REGISTER_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
فتح حساب جديد - البنك الوطني العربي
</title>

<style>

body {
    margin:0;
    background:linear-gradient(135deg,#0056b3,#003366);
    font-family:Arial,sans-serif;
    display:flex;
    justify-content:center;
    align-items:center;
    min-height:100vh;
}

.box {
    background:white;
    width:90%;
    max-width:380px;
    padding:25px;
    border-radius:12px;
    box-shadow:0 8px 25px rgba(0,0,0,.25);
}

input,
button {
    width:100%;
    box-sizing:border-box;
    padding:12px;
    margin:8px 0;
    border-radius:6px;
    border:1px solid #ccc;
    font-size:15px;
}

button {
    background:linear-gradient(135deg,#007bff,#0056b3);
    color:white;
    border:0;
    font-weight:bold;
}

.flash {
    color:#d90429;
    text-align:center;
    font-size:13px;
    font-weight:bold;
}

a {
    display:block;
    text-align:center;
    margin-top:15px;
    color:#0056b3;
    text-decoration:none;
    font-weight:bold;
}

.note {
    background:#eef6ff;
    color:#0056b3;
    padding:10px;
    border-radius:7px;
    font-size:12px;
    line-height:1.6;
}

</style>

</head>

<body>

<div class="box">

<h2
    style="text-align:center;color:#0056b3;margin-top:0"
>
حساب جديد
</h2>

{% with messages = get_flashed_messages() %}

    {% for message in messages %}

        <p class="flash">
            {{ message }}
        </p>

    {% endfor %}

{% endwith %}

<div class="note">
الرصيد الافتتاحي للحساب الجديد هو 0 جنيه.
<br>
رقم الحساب الجديد يتكون من 7 أرقام بالضبط.
</div>

<form method="post">

<input
    name="full_name"
    placeholder="الاسم الرباعي"
    required
>

<input
    name="username"
    placeholder="اسم المستخدم"
    required
>

<input
    name="password"
    type="password"
    placeholder="كلمة المرور"
    required
>

<input
    name="pin"
    type="password"
    maxlength="4"
    inputmode="numeric"
    placeholder="رمز PIN للتحويل (4 أرقام)"
    required
>

<button type="submit">
إنشاء الحساب
</button>

</form>

<a href="/login">
العودة لتسجيل الدخول
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

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
البنك الوطني العربي - الخدمات المصرفية
</title>

<style>

body {
    margin:0;
    background:#f0f2f5;
    font-family:Arial,sans-serif;
    color:#333;
}

.bank-header {
    background:linear-gradient(135deg,#007bff,#0056b3);
    color:white;
    padding:12px 15px;
    display:flex;
    justify-content:space-between;
    align-items:center;
}

.logo-text {
    font-weight:bold;
    font-size:18px;
}

.menu-icons a {
    color:white;
    text-decoration:none;
    font-size:20px;
    margin-left:15px;
}

.account-box {
    background:white;
    margin:15px;
    padding:15px;
    border-radius:10px;
    border:1px solid #ddd;
    display:flex;
    justify-content:space-between;
    align-items:center;
}

.info div:first-child {
    font-weight:bold;
    color:#0056b3;
}

.info div:last-child {
    color:#666;
    font-size:13px;
    margin-top:3px;
}

.bal {
    font-size:16px;
    font-weight:bold;
    color:#00a65a;
    background:#e8f8f0;
    padding:6px 12px;
    border-radius:6px;
}

.grid {
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:10px;
    padding:0 15px 30px;
}

.card {
    background:white;
    border:1px solid #e5e7eb;
    border-radius:10px;
    padding:15px 5px;
    text-align:center;
    text-decoration:none;
    color:#333;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    min-height:85px;
}

.icon {
    font-size:22px;
    margin-bottom:6px;
}

.title {
    font-size:12px;
    font-weight:bold;
}

</style>

</head>

<body>

<div class="bank-header">

<div class="logo-text">
البنك الوطني العربي
</div>

<div class="menu-icons">

<a href="/notifications">
🔔
</a>

<a href="/logout">
🚪
</a>

</div>

</div>

<div class="account-box">

<div class="info">

<div>
حساب توفير
</div>

<div>
{{ user["account_number"] }}
</div>

</div>

<div class="bal">
{{ money(user["balance"]) }} جنيه
</div>

</div>

<div class="grid">

<a
    href="/account_details"
    class="card"
>
<span class="icon">👤</span>
<span class="title">
تفاصيل الحساب
</span>
</a>

<a
    href="/transfer"
    class="card"
>
<span class="icon">🔄</span>
<span class="title">
تحويلات
</span>
</a>

<a
    href="/external_transfer"
    class="card"
>
<span class="icon">🌐</span>
<span class="title">
تحويل خارجي
</span>
</a>

<a
    href="/generic_section?title=دفع+فواتير"
    class="card"
>
<span class="icon">📄</span>
<span class="title">
دفع فواتير
</span>
</a>

<a
    href="/generic_section?title=سحب+بدون+بطاقة"
    class="card"
>
<span class="icon">🏧</span>
<span class="title">
سحب بدون بطاقة
</span>
</a>

<a
    href="/generic_section?title=PAY+البنك"
    class="card"
>
<span class="icon">📱</span>
<span class="title">
PAY البنك
</span>
</a>

<a
    href="/generic_section?title=طلب+الودائع"
    class="card"
>
<span class="icon">💰</span>
<span class="title">
طلب الودائع الاستثمارية
</span>
</a>

<a
    href="/generic_section?title=إدارة+المستفيدين"
    class="card"
>
<span class="icon">👥</span>
<span class="title">
إدارة المستفيدين
</span>
</a>

<a
    href="/history"
    class="card"
>
<span class="icon">📊</span>
<span class="title">
المعاملات السابقة
</span>
</a>

<a
    href="/generic_section?title=إدارة+البطاقات"
    class="card"
>
<span class="icon">💳</span>
<span class="title">
إدارة البطاقات
</span>
</a>

<a
    href="/generic_section?title=طلبات"
    class="card"
>
<span class="icon">✍️</span>
<span class="title">
طلبات
</span>
</a>

<a
    href="/generic_section?title=أمر+دفع+دائم"
    class="card"
>
<span class="icon">⏰</span>
<span class="title">
أمر دفع دائم
</span>
</a>

</div>

</body>
</html>
"""


ACCOUNT_DETAILS_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
تفاصيل الحساب - البنك الوطني العربي
</title>

<style>

body {
    margin:0;
    background:#f0f2f5;
    font-family:Arial;
}

.top-header {
    background:linear-gradient(135deg,#007bff,#0056b3);
    padding:12px 15px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    color:white;
}

.top-header h2 {
    margin:0;
    font-size:18px;
}

.back-btn {
    background:white;
    color:#0056b3;
    padding:5px 12px;
    border-radius:4px;
    text-decoration:none;
    font-weight:bold;
    font-size:12px;
}

.content {
    max-width:450px;
    margin:15px auto;
    padding:0 10px;
}

.card {
    background:white;
    border-radius:8px;
    border:1px solid #ddd;
    overflow:hidden;
}

.card-header {
    padding:15px;
    display:flex;
    justify-content:space-between;
    align-items:center;
}

.acc-type {
    font-weight:bold;
    color:#0056b3;
}

.acc-nums {
    font-size:13px;
    color:#555;
    margin-top:3px;
}

.balance-badge {
    background:#e8f8f0;
    padding:6px 12px;
    border-radius:6px;
}

.balance-amount {
    font-size:16px;
    font-weight:bold;
    color:#00a65a;
}

.card-actions {
    display:grid;
    grid-template-columns:1fr 1fr;
    background:#fafafa;
}

.action-btn {
    padding:12px;
    text-decoration:none;
    color:#333;
    font-size:13px;
    font-weight:bold;
    text-align:center;
    border-top:1px solid #eee;
}

</style>

</head>

<body>

<div class="top-header">

<h2>
تفاصيل الحساب
</h2>

<a
    href="/account"
    class="back-btn"
>
رجوع 〉
</a>

</div>

<div class="content">

<div class="card">

<div class="card-header">

<div>

<div class="acc-type">
حساب توفير
</div>

<div class="acc-nums">

<div>
الحساب - {{ user["account_number"] }}
</div>

<div
    style="font-size:11px;margin-top:2px"
>
IBAN - SD450408{{ user["account_number"] }}
</div>

</div>

</div>

<div class="balance-badge">

<div class="balance-amount">
{{ money(user["balance"]) }}
</div>

</div>

</div>

<div class="card-actions">

<a
    href="/history"
    class="action-btn"
>
📄 عرض كشف الحساب
</a>

<a
    href="/account_details"
    class="action-btn"
>
🔲 رمز الدفع السريع QR
</a>

</div>

</div>

</div>

</body>
</html>
"""


TRANSFER_INDEX_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
التحويلات المصرفية
</title>

<style>

body {
    margin:0;
    background:#f0f2f5;
    font-family:Arial;
}

.top-header {
    background:linear-gradient(135deg,#007bff,#0056b3);
    padding:12px 15px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    color:white;
}

.back-btn {
    background:white;
    color:#0056b3;
    padding:5px 12px;
    border-radius:4px;
    text-decoration:none;
    font-weight:bold;
    font-size:12px;
}

.form-box {
    max-width:450px;
    margin:20px auto;
    background:white;
    padding:20px;
    border-radius:8px;
    border:1px solid #ddd;
}

.input-row {
    display:flex;
    align-items:center;
    border:1px solid #ccc;
    border-radius:6px;
    padding:4px 10px;
    margin-bottom:15px;
}

.input-row input {
    width:100%;
    border:0;
    outline:none;
    padding:10px;
    font-size:15px;
}

.submit-btn {
    background:linear-gradient(135deg,#007bff,#0056b3);
    color:white;
    border:0;
    padding:12px;
    border-radius:6px;
    font-weight:bold;
    width:100%;
}

.flash {
    color:#d90429;
    font-size:13px;
    font-weight:bold;
    text-align:center;
}

</style>

</head>

<body>

<div class="top-header">

<h2>
تحويل الأموال
</h2>

<a
    href="/account"
    class="back-btn"
>
رجوع 〉
</a>

</div>

<div class="form-box">

{% with messages = get_flashed_messages() %}

{% for message in messages %}

<p class="flash">
{{ message }}
</p>

{% endfor %}

{% endwith %}

<form method="post">

<input
    type="hidden"
    name="action"
    value="lookup"
>

<input
    type="hidden"
    name="csrf"
    value="{{ csrf_token() }}"
>

<div class="input-row">

<span
    style="font-size:20px;margin-left:8px"
>
🔲
</span>

<input
    name="receiver_account"
    placeholder="أدخل رقم الحساب"
    required
>

</div>

<button
    type="submit"
    class="submit-btn"
>
التحقق من الحساب
</button>

</form>

</div>

</body>
</html>
"""


TRANSFER_FORM_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
تأكيد التحويل
</title>

<style>

body {
    margin:0;
    background:#f0f2f5;
    font-family:Arial;
}

.top-header {
    background:linear-gradient(135deg,#007bff,#0056b3);
    padding:12px 15px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    color:white;
}

.back-btn {
    background:white;
    color:#0056b3;
    padding:5px 12px;
    border-radius:4px;
    text-decoration:none;
    font-weight:bold;
    font-size:12px;
}

.content {
    max-width:450px;
    margin:15px auto;
    padding:0 10px;
}

.info-card,
.form-card {
    background:white;
    border-radius:8px;
    border:1px solid #ddd;
    padding:15px;
    margin-bottom:15px;
}

.info-row {
    display:flex;
    justify-content:space-between;
    padding:8px 0;
    border-bottom:1px solid #f0f0f5;
    font-size:14px;
}

.info-row:last-child {
    border-bottom:0;
}

.info-label {
    color:#666;
    font-weight:bold;
}

.info-val {
    color:#111;
    font-weight:bold;
}

.form-group {
    display:flex;
    align-items:center;
    border:1px solid #ccc;
    border-radius:6px;
    padding:4px 10px;
    margin-bottom:12px;
}

.form-group input {
    width:100%;
    border:0;
    outline:none;
    padding:8px;
    font-size:14px;
}

.btn-container {
    display:flex;
    gap:10px;
    margin-top:15px;
}

.btn-cancel,
.btn-confirm {
    flex:1;
    padding:12px;
    border-radius:6px;
    font-weight:bold;
    text-align:center;
    text-decoration:none;
    border:0;
}

.btn-cancel {
    background:#6c757d;
    color:white;
}

.btn-confirm {
    background:linear-gradient(135deg,#007bff,#0056b3);
    color:white;
}

.flash {
    color:#d90429;
    text-align:center;
    font-weight:bold;
    font-size:13px;
}

</style>

</head>

<body>

<div class="top-header">

<h2>
تحويل الأموال
</h2>

<a
    href="/account"
    class="back-btn"
>
رجوع 〉
</a>

</div>

<div class="content">

{% with messages = get_flashed_messages() %}

{% for message in messages %}

<p class="flash">
{{ message }}
</p>

{% endfor %}

{% endwith %}

{% if receiver %}

<div class="info-card">

<div class="info-row">

<span class="info-label">
رقم الحساب
</span>

<span class="info-val">
{{ receiver["account_number"] }}
</span>

</div>

<div class="info-row">

<span class="info-label">
الاسم
</span>

<span class="info-val">
{{ receiver["full_name"] }}
</span>

</div>

<div class="info-row">

<span class="info-label">
نوع الحساب
</span>

<span class="info-val">
حساب توفير
</span>

</div>

</div>

<div class="form-card">

<form method="post">

<input
    type="hidden"
    name="csrf"
    value="{{ csrf_token() }}"
>

<input
    type="hidden"
    name="action"
    value="execute"
>

<input
    type="hidden"
    name="receiver_account"
    value="{{ receiver_account }}"
>

<input
    type="hidden"
    name="idempotency_key"
    value="{{ idempotency_key }}"
>

<div class="form-group">

<input
    name="phone"
    value=""
    placeholder="رقم الهاتف للرسالة النصية"
>

</div>

<div class="form-group">

<input
    name="amount"
    type="number"
    min="1"
    step="1"
    placeholder="أدخل المبلغ"
    required
>

</div>

<div class="form-group">

<input
    name="pin"
    type="password"
    maxlength="4"
    inputmode="numeric"
    placeholder="رمز PIN للتحويل (4 أرقام)"
    required
>

</div>

<div class="form-group">

<input
    name="comment"
    maxlength="500"
    placeholder="ملاحظات"
>

</div>

<div class="btn-container">

<a
    href="/transfer"
    class="btn-cancel"
>
إلغاء
</a>

<button
    type="submit"
    class="btn-confirm"
>
تأكيد التحويل
</button>

</div>

</form>

</div>

{% endif %}

</div>

</body>
</html>
"""


EXTERNAL_TRANSFER_MENU_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
التحويل الخارجي
</title>

<style>

body {
    margin:0;
    background:#f0f2f5;
    font-family:Arial;
}

.top-header {
    background:linear-gradient(135deg,#007bff,#0056b3);
    padding:12px 15px;
    display:flex;
    justify-content:space-between;
    color:white;
}

.back-btn {
    background:white;
    color:#0056b3;
    padding:5px 12px;
    border-radius:4px;
    text-decoration:none;
}

.content {
    max-width:450px;
    margin:20px auto;
    padding:0 10px;
}

.option-card {
    background:white;
    border:1px solid #ddd;
    border-radius:10px;
    padding:20px;
    display:flex;
    align-items:center;
    gap:15px;
    text-decoration:none;
    color:#333;
}

.option-icon {
    font-size:32px;
}

.option-info h3 {
    margin:0 0 5px;
    color:#0056b3;
    font-size:16px;
}

.option-info p {
    margin:0;
    color:#666;
    font-size:13px;
}

</style>

</head>

<body>

<div class="top-header">

<h2>
التحويل الخارجي
</h2>

<a
    href="/account"
    class="back-btn"
>
رجوع 〉
</a>

</div>

<div class="content">

<a
    href="/external_transfer/digital_store"
    class="option-card"
>

<div class="option-icon">
🛒
</div>

<div class="option-info">

<h3>
تحويل إلى منصة ديجيتال ستوري
</h3>

<p>
إرسال الأموال إلى حسابات منصة ديجيتال ستوري
</p>

</div>

</a>

</div>

</body>
</html>
"""


EXTERNAL_TRANSFER_LOOKUP_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
ديجيتال ستوري - البنك الوطني العربي
</title>

<style>

body {
    margin:0;
    background:#f0f2f5;
    font-family:Arial;
}

.top-header {
    background:linear-gradient(135deg,#007bff,#0056b3);
    padding:12px 15px;
    display:flex;
    justify-content:space-between;
    color:white;
}

.back-btn {
    background:white;
    color:#0056b3;
    padding:5px 12px;
    border-radius:4px;
    text-decoration:none;
}

.form-box {
    max-width:450px;
    margin:20px auto;
    background:white;
    padding:20px;
    border-radius:8px;
    border:1px solid #ddd;
}

.input-row {
    display:flex;
    align-items:center;
    border:1px solid #ccc;
    border-radius:6px;
    padding:4px 10px;
    margin-bottom:15px;
}

.input-row input {
    width:100%;
    border:0;
    outline:none;
    padding:10px;
    font-size:15px;
}

.submit-btn {
    background:linear-gradient(135deg,#007bff,#0056b3);
    color:white;
    border:0;
    padding:12px;
    border-radius:6px;
    font-weight:bold;
    width:100%;
}

.flash {
    color:#d90429;
    font-size:13px;
    font-weight:bold;
    text-align:center;
}

.note {
    background:#fff3cd;
    color:#856404;
    padding:10px;
    border-radius:6px;
    font-size:12px;
    line-height:1.6;
}

</style>

</head>

<body>

<div class="top-header">

<h2>
تحويل خارجي - ديجيتال ستوري
</h2>

<a
    href="/external_transfer"
    class="back-btn"
>
رجوع 〉
</a>

</div>

<div class="form-box">

{% with messages = get_flashed_messages() %}

{% for message in messages %}

<p class="flash">
{{ message }}
</p>

{% endfor %}

{% endwith %}

<div class="note">

التحويل الخارجي لن يخصم أي مبلغ من حسابك
حتى يتم ربط البنك فعليًا بمنصة DIGITAL STORE.

</div>

<form
    method="post"
    action="/external_transfer/digital_store"
>

<input
    type="hidden"
    name="action"
    value="lookup"
>

<input
    type="hidden"
    name="csrf"
    value="{{ csrf_token() }}"
>

<p
    style="font-size:13px;color:#666"
>
أدخل رقم الحساب المستلم في منصة ديجيتال ستوري:
</p>

<div class="input-row">

<span
    style="font-size:20px;margin-left:8px"
>
🛒
</span>

<input
    name="receiver_account"
    placeholder="رقم حساب المستلم"
    required
>

</div>

<button
    type="submit"
    class="submit-btn"
>
التحقق من الحساب
</button>

</form>

</div>

</body>
</html>
"""


RECEIPT_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
إيصال التحويل
</title>

<style>

body {
    margin:0;
    background:linear-gradient(135deg,#00e676,#00b0ff);
    font-family:Arial;
    color:#111;
    display:flex;
    flex-direction:column;
    align-items:center;
    min-height:100vh;
    padding-bottom:20px;
}

.check-circle {
    width:75px;
    height:75px;
    background:white;
    border-radius:50%;
    display:flex;
    align-items:center;
    justify-content:center;
    margin:20px auto 10px;
}

.check-circle span {
    color:#00c853;
    font-size:40px;
    font-weight:bold;
}

h3 {
    text-align:center;
    margin:5px 0 20px;
    font-size:20px;
    color:white;
}

.receipt-table {
    width:92%;
    max-width:450px;
    background:rgba(255,255,255,.9);
    border-radius:12px;
    padding:15px;
    margin-bottom:20px;
}

.receipt-row {
    display:flex;
    justify-content:space-between;
    gap:15px;
    padding:11px 8px;
    border-bottom:1px solid rgba(0,0,0,.1);
    font-size:14px;
}

.receipt-row:last-child {
    border-bottom:0;
}

.btn-ok {
    background:#0056b3;
    color:white;
    padding:12px 40px;
    border-radius:6px;
    font-weight:bold;
    text-decoration:none;
}

</style>

</head>

<body>

<div class="check-circle">
<span>✓</span>
</div>

<h3>
إيصال التحويل الناجح
</h3>

<div class="receipt-table">

<div class="receipt-row">

<span>
رقم العملية
</span>

<strong>
{{ transaction["reference"] }}
</strong>

</div>

<div class="receipt-row">

<span>
نوع المعاملة
</span>

<strong>
{{
    "تحويل خارجي"
    if transaction["type"] == "EXTERNAL_TRANSFER"
    else "تحويل داخلي"
}}
</strong>

</div>

<div class="receipt-row">

<span>
التاريخ والزمن
</span>

<span>
{{ transaction["created_at"][:19].replace('T',' ') }}
</span>

</div>

<div class="receipt-row">

<span>
من حساب
</span>

<span>
{{ transaction["sender_account"] }}
</span>

</div>

<div class="receipt-row">

<span>
إلى حساب
</span>

<span>
{{ transaction["receiver_account"] }}
</span>

</div>

<div class="receipt-row">

<span>
اسم المستلم
</span>

<span>
{{
    receiver_user["full_name"]
    if receiver_user
    else "N/A"
}}
</span>

</div>

<div class="receipt-row">

<span>
رقم الموبايل
</span>

<span>
{{
    transaction["phone"]
    if transaction["phone"]
    else "N/A"
}}
</span>

</div>

<div class="receipt-row">

<span>
التعليق
</span>

<span>
{{
    transaction["comment"]
    if transaction["comment"]
    else "N/A"
}}
</span>

</div>

<div class="receipt-row">

<span>
المبلغ المنقول
</span>

<strong
    style="color:#00a65a"
>
{{ money(transaction["amount"]) }} SDG
</strong>

</div>

</div>

<a
    href="/account"
    class="btn-ok"
>
موافق
</a>

</body>
</html>
"""


HISTORY_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
سجل المعاملات
</title>

<style>

body {
    background:#f0f2f5;
    font-family:Arial;
}

.container {
    max-width:500px;
    margin:auto;
    padding:20px;
}

.item {
    background:white;
    padding:15px;
    margin:10px 0;
    border-radius:8px;
    border:1px solid #ddd;
}

a {
    color:#0056b3;
    text-decoration:none;
    font-weight:bold;
    display:block;
    text-align:center;
    margin-top:20px;
}

</style>

</head>

<body>

<div class="container">

<h2>
سجل المعاملات السابقة
</h2>

{% for transaction in transactions %}

<div class="item">

<strong>
رقم العملية:
{{ transaction["reference"] }}
</strong>

<p>
المبلغ:

<span
    style="color:#00a65a;font-weight:bold"
>
{{ money(transaction["amount"]) }} SDG
</span>

</p>

<p
    style="font-size:13px;color:#666"
>
{{ transaction["sender_account"] }}
←
{{ transaction["receiver_account"] }}
</p>

<p style="font-size:13px">

الحالة:

<strong>
{{ transaction["status"] }}
</strong>

</p>

<small
    style="color:#999"
>
{{ transaction["created_at"][:19] }}
</small>

</div>

{% else %}

<p>
لا توجد معاملات سابقة.
</p>

{% endfor %}

<a href="/account">
العودة للرئيسية
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

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
الإشعارات
</title>

<style>

body {
    background:#f0f2f5;
    font-family:Arial;
}

.container {
    max-width:500px;
    margin:auto;
    padding:20px;
}

.item {
    background:white;
    padding:15px;
    margin:10px 0;
    border-radius:8px;
    border:1px solid #ddd;
}

a {
    color:#0056b3;
    text-decoration:none;
    font-weight:bold;
    display:block;
    text-align:center;
    margin-top:20px;
}

</style>

</head>

<body>

<div class="container">

<h2>
الإشعارات
</h2>

{% for notification in notifications %}

<div class="item">

<strong>
{{ notification["title"] }}
</strong>

<p>
{{ notification["message"] }}
</p>

{% if notification["reference"] %}

<p
    style="font-size:12px;color:#666"
>
رقم العملية:
{{ notification["reference"] }}
</p>

{% endif %}

<small
    style="color:#999"
>
{{ notification["created_at"][:19] }}
</small>

</div>

{% else %}

<p>
لا توجد إشعارات.
</p>

{% endfor %}

<a href="/account">
العودة للرئيسية
</a>

</div>

</body>
</html>
"""


GENERIC_SECTION_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
{{ title }} - البنك الوطني العربي
</title>

<style>

body {
    margin:0;
    background:#f0f2f5;
    font-family:Arial;
}

.top-header {
    background:linear-gradient(135deg,#007bff,#0056b3);
    padding:12px 15px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    color:white;
}

.top-header h2 {
    margin:0;
    font-size:18px;
}

.back-btn {
    background:white;
    color:#0056b3;
    padding:5px 12px;
    border-radius:4px;
    text-decoration:none;
    font-weight:bold;
    font-size:12px;
}

.content {
    max-width:450px;
    margin:30px auto;
    background:white;
    padding:30px;
    border-radius:8px;
    text-align:center;
    border:1px solid #ddd;
}

</style>

</head>

<body>

<div class="top-header">

<h2>
{{ title }}
</h2>

<a
    href="/account"
    class="back-btn"
>
رجوع 〉
</a>

</div>

<div class="content">

<h3>
قسم {{ title }}
</h3>

<p
    style="color:#666"
>
هذه الصفحة مخصصة لخدمات {{ title }}
عبر البنك الوطني العربي.
</p>

<a
    href="/account"
    style="color:#0056b3;text-decoration:none;font-weight:bold"
>
العودة للرئيسية
</a>

</div>

</body>
</html>
"""


# ============================================================
# STARTUP
# ============================================================

with app.app_context():
    init_db()


if __name__ == "__main__":

    from waitress import serve

    serve(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "5000",
            )
        ),
    )



