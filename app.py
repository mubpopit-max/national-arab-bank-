import os
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
# NATIONAL ARAB BANK THEME & APP
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
        "SESSION_COOKIE_SECURE", "1"
    ) == "1",
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
            timeout=30
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA synchronous = FULL")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    database = g.pop("db", None)
    if database is not None:
        database.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    database = get_db()
    database.executescript("""
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

        CREATE INDEX IF NOT EXISTS idx_transactions_sender ON transactions(sender_account);
        CREATE INDEX IF NOT EXISTS idx_transactions_receiver ON transactions(receiver_account);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(username);
    """)

    founder = database.execute(
        "SELECT 1 FROM users WHERE username = ?",
        (FOUNDER_USERNAME,)
    ).fetchone()

    if not founder:
        founder_password = os.getenv("FOUNDER_PASSWORD")
        founder_pin = os.getenv("FOUNDER_PIN")

        if not founder_password or not founder_pin:
            raise RuntimeError("FOUNDER_PASSWORD and FOUNDER_PIN are required before first startup")

        password_hash = bcrypt.hashpw(founder_password.encode(), bcrypt.gensalt()).decode()
        pin_hash = bcrypt.hashpw(founder_pin.encode(), bcrypt.gensalt()).decode()

        database.execute(
            """
            INSERT INTO users (
                username, full_name, account_number, password_hash, pin_hash,
                balance, currency, role, active, created_at
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
            )
        )
    database.commit()


# ============================================================
# HELPERS & CSRF
# ============================================================

def get_csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def verify_csrf():
    submitted = request.form.get("csrf", "")
    stored = session.get("csrf", "")
    if not submitted or not stored or not secrets.compare_digest(submitted, stored):
        raise ValueError("رمز الحماية غير صالح أو منتهي.")


def current_user():
    username = session.get("username")
    if not username:
        return None
    return get_db().execute(
        "SELECT * FROM users WHERE username = ? AND active = 1",
        (username,)
    ).fetchone()


def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return function(*args, **kwargs)
    return wrapper


def money(amount):
    return f"{int(amount):,}"


def generate_reference():
    database = get_db()
    while True:
        reference = "".join([str(secrets.randbelow(10)) for _ in range(11)])
        exists = database.execute(
            "SELECT 1 FROM transactions WHERE reference = ?",
            (reference,)
        ).fetchone()
        if not exists:
            return reference


@app.context_processor
def inject_globals():
    return {
        "csrf_token": get_csrf_token,
        "money": money,
    }


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    if current_user():
        return redirect(url_for("account"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        account_number = request.form.get("account_number", "").strip()
        password = request.form.get("password", "")
        database = get_db()

        user = database.execute(
            "SELECT * FROM users WHERE account_number = ? AND active = 1",
            (account_number,)
        ).fetchone()

        valid = False
        if user:
            try:
                valid = bcrypt.checkpw(password.encode(), user["password_hash"].encode())
            except ValueError:
                valid = False

        if valid:
            session.clear()
            session["username"] = user["username"]
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect(url_for("account"))

        flash("رقم الحساب أو كلمة المرور غير صحيحة.")

    return render_template_string(LOGIN_HTML)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        try:
            full_name = request.form.get("full_name", "").strip()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            pin = request.form.get("pin", "").strip()

            if not full_name or not username or not password or len(pin) != 4:
                raise ValueError("الرجاء تعبئة جميع الحقول بشكل صحيح (رمز PIN يجب أن يكون 4 أرقام).")

            database = get_db()
            
            while True:
                account_number = "1003" + "".join([str(secrets.randbelow(10)) for _ in range(7)])
                exists = database.execute("SELECT 1 FROM users WHERE account_number = ?", (account_number,)).fetchone()
                if not exists:
                    break

            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            pin_hash = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()

            database.execute(
                """
                INSERT INTO users (username, full_name, account_number, password_hash, pin_hash, balance, currency, role, active, created_at)
                VALUES (?, ?, ?, ?, ?, 50000, 'SDG', 'customer', 1, ?)
                """,
                (username, full_name, account_number, password_hash, pin_hash, now())
            )
            database.commit()
            flash("تم إنشاء الحساب بنجاح! يمكنك تسجيل الدخول الآن.")
            return redirect(url_for("login"))
        except Exception as e:
            flash(str(e))

    return render_template_string(REGISTER_HTML)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/account")
@login_required
def account():
    user = current_user()
    return render_template_string(ACCOUNT_HTML, user=user)


@app.get("/account_details")
@login_required
def account_details():
    user = current_user()
    return render_template_string(ACCOUNT_DETAILS_HTML, user=user)


@app.route("/transfer", methods=["GET", "POST"])
@login_required
def transfer():
    user = current_user()
    receiver_account = request.values.get("receiver_account", "").strip()
    receiver_obj = None
    database = get_db()

    if receiver_account:
        receiver_obj = database.execute(
            "SELECT * FROM users WHERE account_number = ? AND active = 1",
            (receiver_account,)
        ).fetchone()

    if request.method == "POST":
        action = request.form.get("action", "")
        
        if action == "lookup":
            if not receiver_account:
                flash("الرجاء إدخال رقم الحساب.")
            elif not receiver_obj:
                flash("رقم الحساب غير موجود أو غير نشط.")
            return render_template_string(TRANSFER_FORM_HTML, user=user, receiver=receiver_obj, receiver_account=receiver_account)

        elif action == "execute":
            try:
                verify_csrf()
                pin = request.form.get("pin", "")
                phone = request.form.get("phone", "").strip()
                comment = request.form.get("comment", "").strip()
                amount_text = request.form.get("amount", "").strip()
                idempotency_key = request.form.get("idempotency_key", "").strip()

                if not idempotency_key:
                    raise ValueError("مفتاح العملية مفقود.")

                try:
                    amount_decimal = Decimal(amount_text)
                except InvalidOperation:
                    raise ValueError("المبلغ غير صالح.")

                if amount_decimal <= 0:
                    raise ValueError("المبلغ يجب أن يكون أكبر من صفر.")

                amount = int(amount_decimal)

                if receiver_account == user["account_number"]:
                    raise ValueError("لا يمكن التحويل إلى نفس الحساب.")

                if not bcrypt.checkpw(pin.encode(), user["pin_hash"].encode()):
                    raise ValueError("رمز PIN غير صحيح.")

                database.execute("BEGIN IMMEDIATE")

                old_operation = database.execute(
                    "SELECT * FROM idempotency_keys WHERE key = ?",
                    (idempotency_key,)
                ).fetchone()

                if old_operation:
                    database.rollback()
                    return redirect(url_for("receipt", reference=old_operation["reference"]))

                sender = database.execute("SELECT * FROM users WHERE account_number = ? AND active = 1", (user["account_number"],)).fetchone()
                receiver = database.execute("SELECT * FROM users WHERE account_number = ? AND active = 1", (receiver_account,)).fetchone()

                if not sender or not receiver:
                    raise ValueError("حساب المرسل أو المستلم غير موجود.")

                if sender["balance"] < amount:
                    raise ValueError("الرصيد غير كافٍ.")

                sender_before = sender["balance"]
                receiver_before = receiver["balance"]
                sender_after = sender_before - amount
                receiver_after = receiver_before + amount

                reference = generate_reference()

                database.execute(
                    "UPDATE users SET balance = balance - ? WHERE account_number = ? AND balance >= ?",
                    (amount, sender["account_number"], amount)
                )
                database.execute(
                    "UPDATE users SET balance = balance + ? WHERE account_number = ?",
                    (amount, receiver["account_number"])
                )

                database.execute(
                    """
                    INSERT INTO transactions (
                        reference, sender_account, receiver_account, amount,
                        sender_before, sender_after, receiver_before, receiver_after,
                        type, status, comment, phone, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reference, sender["account_number"], receiver["account_number"], amount,
                        sender_before, sender_after, receiver_before, receiver_after,
                        "TRANSFER", "COMPLETED", comment[:500], phone, now(),
                    )
                )

                database.execute(
                    "INSERT INTO idempotency_keys (key, username, reference, created_at) VALUES (?, ?, ?, ?)",
                    (idempotency_key, user["username"], reference, now())
                )

                database.commit()
                return redirect(url_for("receipt", reference=reference))

            except Exception as error:
                try:
                    database.rollback()
                except Exception:
                    pass
                flash(str(error))
                return render_template_string(TRANSFER_FORM_HTML, user=user, receiver=receiver_obj, receiver_account=receiver_account)

    return render_template_string(TRANSFER_INDEX_HTML, user=user)


@app.get("/receipt/<reference>")
@login_required
def receipt(reference):
    transaction = get_db().execute("SELECT * FROM transactions WHERE reference = ?", (reference,)).fetchone()
    if not transaction:
        return "Not found", 404
    user = current_user()
    allowed = user["role"] == "founder" or user["account_number"] in (transaction["sender_account"], transaction["receiver_account"])
    if not allowed:
        return "Forbidden", 403
    
    receiver_user = get_db().execute("SELECT * FROM users WHERE account_number = ?", (transaction["receiver_account"],)).fetchone()
    return render_template_string(RECEIPT_HTML, transaction=transaction, receiver_user=receiver_user)


@app.get("/history")
@login_required
def history():
    user = current_user()
    transactions = get_db().execute(
        "SELECT * FROM transactions WHERE sender_account = ? OR receiver_account = ? ORDER BY id DESC LIMIT 100",
        (user["account_number"], user["account_number"])
    ).fetchall()
    return render_template_string(HISTORY_HTML, transactions=transactions)


@app.get("/notifications")
@login_required
def notifications():
    user = current_user()
    rows = get_db().execute(
        "SELECT * FROM notifications WHERE username = ? ORDER BY id DESC LIMIT 100",
        (user["username"],)
    ).fetchall()
    get_db().execute("UPDATE notifications SET is_read = 1 WHERE username = ?", (user["username"],))
    get_db().commit()
    return render_template_string(NOTIFICATIONS_HTML, notifications=rows)


@app.get("/generic_section")
@login_required
def generic_section():
    title = request.args.get("title", "خدمة المصرف")
    return render_template_string(GENERIC_SECTION_HTML, title=title)


# ============================================================
# HTML TEMPLATES (Glossy Blue / Bright Emerald Green Theme)
# ============================================================

LOGIN_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>البنك الوطني العربي - تسجيل الدخول</title>
<style>
body { margin: 0; background: linear-gradient(135deg, #0056b3, #003366); font-family: Arial, sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; }
.logo-container { text-align: center; color: white; margin-bottom: 20px; }
.logo-container h1 { margin: 0; font-size: 30px; font-weight: bold; text-shadow: 0 2px 4px rgba(0,0,0,0.3); }
.box { background: white; width: 90%; max-width: 380px; padding: 30px 20px; border-radius: 12px; box-shadow: 0 8px 25px rgba(0,0,0,0.25); }
input, button { width: 100%; box-sizing: border-box; padding: 12px; margin: 10px 0; border-radius: 6px; border: 1px solid #ccc; font-size: 15px; outline: none; }
input:focus { border-color: #0056b3; box-shadow: 0 0 5px rgba(0,86,179,0.3); }
button { background: linear-gradient(135deg, #007bff, #0056b3); color: white; border: 0; font-weight: bold; cursor: pointer; box-shadow: 0 4px 10px rgba(0,123,255,0.3); }
.flash { color: #d90429; text-align: center; font-size: 13px; font-weight: bold; margin-bottom: 10px; }
.links { display: flex; justify-content: space-between; margin-top: 15px; font-size: 13px; }
.links a { color: #0056b3; text-decoration: none; font-weight: bold; }
</style>
</head>
<body>
<div class="logo-container">
    <h1>البنك الوطني العربي</h1>
</div>
<div class="box">
    {% with messages = get_flashed_messages() %}
        {% for message in messages %}<p class="flash">{{ message }}</p>{% endfor %}
    {% endwith %}
    <form method="post">
        <input name="account_number" placeholder="رقم الحساب" required>
        <input name="password" type="password" placeholder="كلمة المرور" required>
        <button>تسجيل الدخول</button>
    </form>
    <div class="links">
        <a href="/register">تسجيل حساب جديد</a>
        <a href="#">نسيت كلمة المرور؟</a>
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>فتح حساب جديد - البنك الوطني العربي</title>
<style>
body { margin: 0; background: linear-gradient(135deg, #0056b3, #003366); font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
.box { background: white; width: 90%; max-width: 380px; padding: 25px; border-radius: 12px; box-shadow: 0 8px 25px rgba(0,0,0,0.25); }
input, button { width: 100%; box-sizing: border-box; padding: 12px; margin: 8px 0; border-radius: 6px; border: 1px solid #ccc; font-size: 15px; }
button { background: linear-gradient(135deg, #007bff, #0056b3); color: white; border: 0; font-weight: bold; cursor: pointer; box-shadow: 0 4px 10px rgba(0,123,255,0.3); }
.flash { color: #d90429; text-align: center; font-size: 13px; font-weight: bold; }
a { display: block; text-align: center; margin-top: 15px; color: #0056b3; text-decoration: none; font-weight: bold; }
</style>
</head>
<body>
<div class="box">
    <h2 style="text-align:center; color:#0056b3; margin-top:0;">حساب جديد</h2>
    {% with messages = get_flashed_messages() %}
        {% for message in messages %}<p class="flash">{{ message }}</p>{% endfor %}
    {% endwith %}
    <form method="post">
        <input name="full_name" placeholder="الاسم الرباعي" required>
        <input name="username" placeholder="اسم المستخدم" required>
        <input name="password" type="password" placeholder="كلمة المرور" required>
        <input name="pin" type="password" maxlength="4" placeholder="رمز PIN للتحويل (4 أرقام)" required>
        <button>إنشاء الحساب</button>
    </form>
    <a href="/login">العودة لتسجيل الدخول</a>
</div>
</body>
</html>
"""

ACCOUNT_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>البنك الوطني العربي - الخدمات المصرفية</title>
<style>
body { margin: 0; background: #f0f2f5; font-family: Arial, sans-serif; color: #333; }
.bank-header { background: linear-gradient(135deg, #007bff, #0056b3); color: white; padding: 12px 15px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 5px rgba(0,0,0,0.15); }
.bank-header .logo-text { font-weight: bold; font-size: 18px; }
.bank-header .menu-icons a { color: white; text-decoration: none; font-size: 20px; margin-left: 15px; }
.account-box { background: white; margin: 15px; padding: 15px; border-radius: 10px; border: 1px solid #ddd; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
.account-box .info div:first-child { font-weight: bold; color: #0056b3; font-size: 15px; }
.account-box .info div:last-child { color: #666; font-size: 13px; margin-top: 3px; }
.account-box .bal { font-size: 16px; font-weight: bold; color: #00e676; background: #e8f8f0; padding: 6px 12px; border-radius: 6px; border: 1px solid #b9f6ca; text-shadow: 0 1px 1px rgba(0,0,0,0.05); }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; padding: 0 15px 30px 15px; }
.card { background: white; border: 1px solid #e5e7eb; border-radius: 10px; padding: 15px 5px; text-align: center; text-decoration: none; color: #333; box-shadow: 0 2px 5px rgba(0,0,0,0.03); display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 85px; }
.card:active { background: #f9fafb; }
.card .icon { font-size: 22px; margin-bottom: 6px; }
.card .title { font-size: 12px; font-weight: bold; }
</style>
</head>
<body>
<div class="bank-header">
    <div class="logo-text">البنك الوطني العربي</div>
    <div class="menu-icons">
        <a href="/notifications" title="الإشعارات">🔔</a>
        <a href="/logout" title="خروج">🚪</a>
    </div>
</div>

<div class="account-box">
    <div class="info">
        <div>حساب توفير</div>
        <div>{{ user["account_number"] }}</div>
    </div>
    <div class="bal">{{ money(user["balance"]) }} جنيه</div>
</div>

<div class="grid">
    <a href="/account_details" class="card">
        <span class="icon">👤</span>
        <span class="title">تفاصيل الحساب</span>
    </a>
    <a href="/transfer" class="card">
        <span class="icon">🔄</span>
        <span class="title">تحويلات</span>
    </a>
    <a href="/generic_section?title=دفع+فواتير" class="card">
        <span class="icon">📄</span>
        <span class="title">دفع فواتير</span>
    </a>
    <a href="/generic_section?title=سحب+بدون+بطاقة" class="card">
        <span class="icon">🏧</span>
        <span class="title">سحب بدون بطاقة</span>
    </a>
    <a href="/generic_section?title=PAY+البنك" class="card">
        <span class="icon">📱</span>
        <span class="title">PAY البنك</span>
    </a>
    <a href="/generic_section?title=طلب+الودائع" class="card">
        <span class="icon">💰</span>
        <span class="title">طلب الودائع الاستثمارية</span>
    </a>
    <a href="/generic_section?title=إدارة+المستفيدين" class="card">
        <span class="icon">👥</span>
        <span class="title">إدارة المستفيدين</span>
    </a>
    <a href="/history" class="card">
        <span class="icon">📊</span>
        <span class="title">المعاملات السابقة</span>
    </a>
    <a href="/generic_section?title=إدارة+البطاقات" class="card">
        <span class="icon">💳</span>
        <span class="title">إدارة البطاقات</span>
    </a>
    <a href="/generic_section?title=طلبات" class="card">
        <span class="icon">✍️</span>
        <span class="title">طلبات</span>
    </a>
    <a href="/generic_section?title=أمر+دفع+دائم" class="card">
        <span class="icon">⏰</span>
        <span class="title">أمر دفع دائم</span>
    </a>
    <a href="/logout" class="card" style="background: #e8f8f0; color: #0056b3;">
        <span class="icon">🚪</span>
        <span class="title">خروج</span>
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>تفاصيل الحساب - البنك الوطني العربي</title>
<style>
body { margin: 0; background: #f0f2f5; font-family: Arial; }
.top-header { background: linear-gradient(135deg, #007bff, #0056b3); padding: 12px 15px; display: flex; justify-content: space-between; align-items: center; color: white; box-shadow: 0 2px 5px rgba(0,0,0,0.15); }
.top-header h2 { margin: 0; font-size: 18px; }
.back-btn { background: white; color: #0056b3; border: 0; padding: 5px 12px; border-radius: 4px; text-decoration: none; font-weight: bold; font-size: 12px; }
.content { max-width: 450px; margin: 15px auto; padding: 0 10px; }
.card { background: white; border-radius: 8px; border: 1px solid #ddd; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
.card-header { padding: 15px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee; }
.acc-type { font-weight: bold; color: #0056b3; font-size: 15px; }
.acc-nums { font-size: 13px; color: #555; margin-top: 3px; }
.balance-badge { display: flex; align-items: center; gap: 6px; background: #e8f8f0; padding: 6px 12px; border-radius: 6px; border: 1px solid #b9f6ca; }
.currency-icon { background: #0056b3; color: white; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: bold; }
.balance-amount { font-size: 16px; font-weight: bold; color: #00e676; text-shadow: 0 1px 1px rgba(0,0,0,0.05); }
.card-actions { display: grid; grid-template-columns: 1fr 1fr; background: #fafafa; text-align: center; }
.action-btn { padding: 12px; text-decoration: none; color: #333; font-size: 13px; font-weight: bold; display: flex; align-items: center; justify-content: center; gap: 6px; border-top: 1px solid #eee; }
.action-btn:first-child { border-left: 1px solid #eee; }
</style>
</head>
<body>
<div class="top-header">
    <h2>تفاصيل الحساب</h2>
    <a href="/account" class="back-btn">رجوع 〉</a>
</div>
<div class="content">
    <div class="card">
        <div class="card-header">
            <div>
                <div class="acc-type">حساب توفير</div>
                <div class="acc-nums">
                    <div>الحساب - {{ user["account_number"] }}</div>
                    <div style="color: #777; font-size: 11px; margin-top: 2px;">IBAN - SD450408{{ user["account_number"] }}</div>
                </div>
            </div>
            <div class="balance-badge">
                <div class="currency-icon">جنيه</div>
                <div class="balance-amount">{{ money(user["balance"]) }}</div>
            </div>
        </div>
        <div class="card-actions">
            <a href="/history" class="action-btn"><span>📄</span> عرض كشف الحساب</a>
            <a href="/account_details" class="action-btn"><span>🔲</span> رمز الدفع السريع QR</a>
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>التحويلات المصرفية - البنك الوطني العربي</title>
<style>
body { margin: 0; background: #f0f2f5; font-family: Arial; }
.top-header { background: linear-gradient(135deg, #007bff, #0056b3); padding: 12px 15px; display: flex; justify-content: space-between; align-items: center; color: white; box-shadow: 0 2px 5px rgba(0,0,0,0.15); }
.top-header h2 { margin: 0; font-size: 18px; }
.back-btn { background: white; color: #0056b3; border: 0; padding: 5px 12px; border-radius: 4px; text-decoration: none; font-weight: bold; font-size: 12px; }
.tabs { display: flex; background: #0056b3; }
.tab { flex: 1; text-align: center; padding: 12px; color: white; font-weight: bold; font-size: 14px; text-decoration: none; border-bottom: 3px solid transparent; }
.tab.active { border-bottom-color: white; background: #003366; }
.form-box { max-width: 450px; margin: 20px auto; background: white; padding: 20px; border-radius: 8px; border: 1px solid #ddd; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
.input-row { display: flex; align-items: center; border: 1px solid #ccc; border-radius: 6px; padding: 4px 10px; margin-bottom: 15px; background: #fff; }
.input-row input { width: 100%; border: 0; outline: none; padding: 10px; font-size: 15px; }
.submit-btn { background: linear-gradient(135deg, #007bff, #0056b3); color: white; border: 0; padding: 12px 25px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 15px; width: 100%; box-shadow: 0 4px 10px rgba(0,123,255,0.3); }
.flash { color: #d90429; font-size: 13px; font-weight: bold; margin-bottom: 10px; text-align: center; }
</style>
</head>
<body>
<div class="top-header">
    <h2>تحويل الأموال</h2>
    <a href="/account" class="back-btn">رجوع 〉</a>
</div>
<div class="tabs">
    <a href="/transfer" class="tab active">دفع مباشر</a>
    <a href="/transfer" class="tab">المستفيدين</a>
</div>
<div class="form-box">
    {% with messages = get_flashed_messages() %}
        {% for message in messages %}<p class="flash">{{ message }}</p>{% endfor %}
    {% endwith %}
    <form method="get" action="/transfer">
        <input type="hidden" name="action" value="lookup">
        <div class="input-row">
            <span style="font-size: 20px; color: #0056b3; margin-left: 8px;">🔲</span>
            <input name="receiver_account" placeholder="أدخل رقم الحساب/الرقم المرجعي" required>
        </div>
        <button class="submit-btn">إرسال</button>
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>التحويلات المصرفية - البنك الوطني العربي</title>
<style>
body { margin: 0; background: #f0f2f5; font-family: Arial; }
.top-header { background: linear-gradient(135deg, #007bff, #0056b3); padding: 12px 15px; display: flex; justify-content: space-between; align-items: center; color: white; box-shadow: 0 2px 5px rgba(0,0,0,0.15); }
.top-header h2 { margin: 0; font-size: 18px; }
.back-btn { background: white; color: #0056b3; border: 0; padding: 5px 12px; border-radius: 4px; text-decoration: none; font-weight: bold; font-size: 12px; }
.tabs { display: flex; background: #0056b3; }
.tab { flex: 1; text-align: center; padding: 12px; color: white; font-weight: bold; font-size: 14px; text-decoration: none; border-bottom: 3px solid transparent; }
.tab.active { border-bottom-color: white; background: #003366; }
.content { max-width: 450px; margin: 15px auto; padding: 0 10px; }
.info-card { background: white; border-radius: 8px; border: 1px solid #ddd; padding: 12px 15px; margin-bottom: 15px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
.info-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #f0f0f5; font-size: 14px; }
.info-row:last-child { border-bottom: 0; }
.info-label { color: #666; font-weight: bold; }
.info-val { color: #111; font-weight: bold; }
.form-card { background: white; border-radius: 8px; border: 1px solid #ddd; padding: 15px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
.form-group { display: flex; align-items: center; border: 1px solid #ccc; border-radius: 6px; padding: 4px 10px; margin-bottom: 12px; background: #fff; }
.form-group input, .form-group select { width: 100%; border: 0; outline: none; padding: 8px; font-size: 14px; background: transparent; }
.btn-container { display: flex; gap: 10px; margin-top: 15px; }
.btn-cancel { flex: 1; background: #6c757d; color: white; border: 0; padding: 12px; border-radius: 6px; font-weight: bold; cursor: pointer; text-align: center; text-decoration: none; }
.btn-confirm { flex: 1; background: linear-gradient(135deg, #007bff, #0056b3); color: white; border: 0; padding: 12px; border-radius: 6px; font-weight: bold; cursor: pointer; box-shadow: 0 4px 10px rgba(0,123,255,0.3); }
.flash { color: #d90429; font-size: 13px; font-weight: bold; text-align: center; margin-bottom: 10px; }
</style>
</head>
<body>
<div class="top-header">
    <h2>تحويل الأموال</h2>
    <a href="/account" class="back-btn">رجوع 〉</a>
</div>
<div class="tabs">
    <a href="/transfer" class="tab active">دفع مباشر</a>
    <a href="/transfer" class="tab">المستفيدين</a>
</div>
<div class="content">
    {% with messages = get_flashed_messages() %}
        {% for message in messages %}<p class="flash">{{ message }}</p>{% endfor %}
    {% endwith %}

    {% if receiver %}
    <div class="info-card">
        <div class="info-row">
            <span class="info-label">رقم الحساب</span>
            <span class="info-val">{{ receiver["account_number"] }}</span>
        </div>
        <div class="info-row">
            <span class="info-label">الاسم</span>
            <span class="info-val">{{ receiver["full_name"] }}</span>
        </div>
        <div class="info-row">
            <span class="info-label">نوع الحساب</span>
            <span class="info-val">حساب توفير</span>
        </div>
        <div class="info-row">
            <span class="info-label">الفرع</span>
            <span class="info-val">المركز الرئيسي</span>
        </div>
    </div>
    {% endif %}

    <div class="form-card">
        <form method="post">
            <input type="hidden" name="csrf" value="{{ csrf_token() }}">
            <input type="hidden" name="action" value="execute">
            <input type="hidden" name="receiver_account" value="{{ receiver_account }}">
            <input type="hidden" name="idempotency_key" value="{{ csrf_token() }}-{{ range(100000000,999999999)|random }}">

            <div class="form-group">
                <select name="sender_account">
                    <option value="{{ user['account_number'] }}">{{ user["account_number"] }}</option>
                </select>
            </div>

            <div class="form-group">
                <span style="font-size: 18px; margin-left: 8px;">📱</span>
                <input name="phone" value="249" placeholder="رقم الهاتف للرسالة النصية" required>
            </div>

            <div class="form-group">
                <span style="font-size: 16px; margin-left: 8px; font-weight:bold;">SDG</span>
                <input name="amount" type="number" min="1" placeholder="أدخل المبلغ" required>
            </div>

            <div class="form-group">
                <span style="font-size: 18px; margin-left: 8px;">🔒</span>
                <input name="pin" type="password" maxlength="4" placeholder="رمز PIN للتحويل (4 أرقام)" required>
            </div>

            <div class="form-group">
                <span style="font-size: 18px; margin-left: 8px;">💬</span>
                <input name="comment" placeholder="ملاحظات">
            </div>

            <div class="btn-container">
                <a href="/transfer" class="btn-cancel">إلغاء</a>
                <button class="btn-confirm">تأكيد</button>
            </div>
        </form>
    </div>
</div>
</body>
</html>
"""

RECEIPT_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>إيصال التحويل - البنك الوطني العربي</title>
<style>
body { margin: 0; background: linear-gradient(135deg, #00e676, #00b0ff); font-family: Arial; color: #111; display: flex; flex-direction: column; align-items: center; min-height: 100vh; padding-bottom: 20px; }
.check-circle { width: 75px; height: 75px; background: white; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 20px auto 10px auto; box-shadow: 0 6px 15px rgba(0,0,0,0.15); }
.check-circle span { color: #00c853; font-size: 40px; font-weight: bold; }
h3 { text-align: center; margin: 5px 0 20px 0; font-size: 20px; color: #fff; text-shadow: 0 2px 4px rgba(0,0,0,0.2); }
.receipt-table { width: 92%; max-width: 450px; background: rgba(255, 255, 255, 0.85); backdrop-filter: blur(10px); border-radius: 12px; padding: 15px; border-collapse: collapse; margin-bottom: 20px; box-shadow: 0 8px 25px rgba(0,0,0,0.15); }
.receipt-row { display: flex; justify-content: space-between; padding: 11px 8px; border-bottom: 1px solid rgba(0,0,0,0.1); font-size: 14px; }
.receipt-row:last-child { border-bottom: 0; }
.btn-ok { background: #0056b3; color: white; border: 0; padding: 12px 40px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 15px; text-decoration: none; display: block; text-align: center; margin: 0 auto; width: 140px; box-shadow: 0 4px 10px rgba(0,0,0,0.2); }
.footer-bar { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.3); backdrop-filter: blur(5px); color: #fff; padding: 10px; display: flex; justify-content: space-around; font-size: 12px; border-top: 1px solid rgba(255,255,255,0.2); }
</style>
</head>
<body>
<div class="check-circle">
    <span>✓</span>
</div>
<h3>إيصال التحويل الناجح</h3>
<div class="receipt-table">
    <div class="receipt-row">
        <span>رقم العملية</span>
        <strong>{{ transaction["reference"] }}</strong>
    </div>
    <div class="receipt-row">
        <span>التاريخ والزمن</span>
        <span>{{ transaction["created_at"][:19].replace('T', ' ') }}</span>
    </div>
    <div class="receipt-row">
        <span>من حساب</span>
        <span>{{ transaction["sender_account"] }}</span>
    </div>
    <div class="receipt-row">
        <span>إلى حساب</span>
        <span>{{ transaction["receiver_account"] }}</span>
    </div>
    <div class="receipt-row">
        <span>اسم المستلم</span>
        <span>{{ receiver_user["full_name"] if receiver_user else "N/A" }}</span>
    </div>
    <div class="receipt-row">
        <span>رقم الموبايل</span>
        <span>{{ transaction["phone"] if transaction["phone"] else "N/A" }}</span>
    </div>
    <div class="receipt-row">
        <span>التعليق</span>
        <span>{{ transaction["comment"] if transaction["comment"] else "N/A" }}</span>
    </div>
    <div class="receipt-row">
        <span>المبلغ</span>
        <strong style="color: #00c853;">{{ "%.2f"|format(transaction["amount"]) }} SDG</strong>
    </div>
</div>

<a href="/account" class="btn-ok">موافق</a>

<div class="footer-bar">
    <span>➕ إضافة</span>
    <span>🖨️ طباعة</span>
    <span>🔗 مشاركة</span>
    <span>🔄 تحويل</span>
</div>
<div style="font-size: 11px; margin-top: 15px; color: rgba(255,255,255,0.9); text-align: center; text-shadow: 0 1px 2px rgba(0,0,0,0.3);">
    © 2026 البنك الوطني العربي - جميع الحقوق محفوظة
</div>
</body>
</html>
"""

HISTORY_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>سجل المعاملات - البنك الوطني العربي</title>
<style>
body { background: #f0f2f5; font-family: Arial; }
.container { max-width: 500px; margin: auto; padding: 20px; }
.item { background: white; padding: 15px; margin: 10px 0; border-radius: 8px; border: 1px solid #ddd; box-shadow: 0 2px 5px rgba(0,0,0,0.03); }
a { color: #0056b3; text-decoration: none; font-weight: bold; display: block; text-align: center; margin-top: 20px; }
</style>
</head>
<body>
<div class="container">
<h2>سجل المعاملات السابقة</h2>
{% for transaction in transactions %}
<div class="item">
<strong>رقم العملية: {{ transaction["reference"] }}</strong>
<p>المبلغ: <span style="color:#00e676; font-weight:bold;">{{ money(transaction["amount"]) }} SDG</span></p>
<p style="font-size:13px; color:#666;">{{ transaction["sender_account"] }} ← {{ transaction["receiver_account"] }}</p>
<small style="color:#999;">{{ transaction["created_at"][:19] }}</small>
</div>
{% else %}
<p>لا توجد معاملات سابقة.</p>
{% endfor %}
<a href="/account">العودة للرئيسية</a>
</div>
</body>
</html>
"""

NOTIFICATIONS_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>الإشعارات - البنك الوطني العربي</title>
<style>
body { background: #f0f2f5; font-family: Arial; }
.container { max-width: 500px; margin: auto; padding: 20px; }
.item { background: white; padding: 15px; margin: 10px 0; border-radius: 8px; border: 1px solid #ddd; box-shadow: 0 2px 5px rgba(0,0,0,0.03); }
a { color: #0056b3; text-decoration: none; font-weight: bold; display: block; text-align: center; margin-top: 20px; }
</style>
</head>
<body>
<div class="container">
<h2>الإشعارات</h2>
{% for notification in notifications %}
<div class="item">
<strong>{{ notification["title"] }}</strong>
<p>{{ notification["message"] }}</p>
<small style="color:#999;">{{ notification["created_at"][:19] }}</small>
</div>
{% else %}
<p>لا توجد إشعارات جديدة.</p>
{% endfor %}
<a href="/account">العودة للرئيسية</a>
</div>
</body>
</html>
"""

GENERIC_SECTION_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} - البنك الوطني العربي</title>
<style>
body { margin: 0; background: #f0f2f5; font-family: Arial; }
.top-header { background: linear-gradient(135deg, #007bff, #0056b3); padding: 12px 15px; display: flex; justify-content: space-between; align-items: center; color: white; box-shadow: 0 2px 5px rgba(0,0,0,0.15); }
.top-header h2 { margin: 0; font-size: 18px; }
.back-btn { background: white; color: #0056b3; border: 0; padding: 5px 12px; border-radius: 4px; text-decoration: none; font-weight: bold; font-size: 12px; }
.content { max-width: 450px; margin: 30px auto; background: white; padding: 30px; border-radius: 8px; text-align: center; border: 1px solid #ddd; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
</style>
</head>
<body>
<div class="top-header">
    <h2>{{ title }}</h2>
    <a href="/account" class="back-btn">رجوع 〉</a>
</div>
<div class="content">
    <h3>قسم {{ title }}</h3>
    <p style="color: #666;">هذه الصفحة مخصصة لخدمات {{ title }} عبر البنك الوطني العربي.</p>
    <a href="/account" style="color: #0056b3; text-decoration: none; font-weight: bold; display: inline-block; margin-top: 15px;">العودة للرئيسية</a>
</div>
</body>
</html>
"""

# ============================================================
# INITIALIZE & RUN
# ============================================================

with app.app_context():
    init_db()

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
