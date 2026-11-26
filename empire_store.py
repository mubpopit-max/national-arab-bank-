"""Empire E-Story commerce foundation.

This module is deliberately isolated from the legacy banking/VORTEX code.
It adds marketplace listings and controlled agent onboarding without moving
or fabricating customer funds. Payment capture, escrow release, EPC issuance,
and external Binance verification remain disabled until dedicated verified
payment integrations are configured.
"""
import os
import re
import sqlite3
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template_string, request, session, url_for

empire_bp = Blueprint("empire", __name__, url_prefix="/store")
DATABASE = os.getenv("NAB_DATABASE", "national_arab_bank.db")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _db():
    if "empire_db" not in __import__("flask").g:
        from flask import g
        g.empire_db = sqlite3.connect(DATABASE, timeout=30, isolation_level=None)
        g.empire_db.row_factory = sqlite3.Row
        g.empire_db.execute("PRAGMA foreign_keys=ON")
        g.empire_db.execute("PRAGMA busy_timeout=30000")
    from flask import g
    return g.empire_db


def init_empire_store_db():
    db = _db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS empire_market_listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        seller_username TEXT NOT NULL,
        game TEXT NOT NULL DEFAULT 'PUBG Mobile',
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        price_sdg INTEGER NOT NULL CHECK(price_sdg > 0),
        status TEXT NOT NULL DEFAULT 'PUBLISHED'
            CHECK(status IN ('PUBLISHED','PAUSED','SOLD','REMOVED')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_empire_listings_status
        ON empire_market_listings(status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_empire_listings_seller
        ON empire_market_listings(seller_username, created_at DESC);

    CREATE TABLE IF NOT EXISTS empire_agent_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        requested_services TEXT NOT NULL DEFAULT 'game_topups,pubg_marketplace',
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','APPROVED','REJECTED','SUSPENDED')),
        commission_rate_bps INTEGER NOT NULL DEFAULT 0
            CHECK(commission_rate_bps BETWEEN 0 AND 10000),
        reviewed_by TEXT,
        reviewed_at TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS empire_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_username TEXT NOT NULL,
        action TEXT NOT NULL,
        object_type TEXT NOT NULL,
        object_id TEXT,
        details TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """)


def _user():
    username = session.get("username")
    if not username:
        return None
    return _db().execute(
        "SELECT username, full_name, role, active FROM users WHERE username=?",
        (username,),
    ).fetchone()


def _require_login():
    user = _user()
    if not user or not user["active"]:
        return None
    return user


def _founder():
    user = _require_login()
    if not user or user["role"] != "founder":
        abort(403)
    return user


def _csrf():
    supplied = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        abort(400, "رمز الحماية غير صالح.")


def _audit(actor, action, object_type, object_id="", details=""):
    _db().execute(
        """INSERT INTO empire_audit_log
           (actor_username,action,object_type,object_id,details,created_at)
           VALUES(?,?,?,?,?,?)""",
        (actor, action, object_type, str(object_id), details[:500], _now()),
    )


BASE = """
<!doctype html><html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} | الإمبراطورية إستوري</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#090909;color:#f4f0e6;font-family:Arial,sans-serif}
header{padding:18px max(16px,calc((100% - 980px)/2));background:linear-gradient(110deg,#17130a,#090909);border-bottom:1px solid #9e7b35}
.brand{color:#e5c16b;font-weight:800;font-size:21px}.sub{color:#b9aa88;font-size:12px;margin-top:5px}
main{max-width:980px;margin:24px auto;padding:0 16px}.panel{background:#151515;border:1px solid #3c321e;border-radius:14px;padding:18px;margin:14px 0}
h1,h2{color:#e7c777}a{color:#e8c979}input,textarea,select{width:100%;padding:12px;border-radius:8px;border:1px solid #51462c;background:#0d0d0d;color:#fff;margin:7px 0 13px}
button,.btn{display:inline-block;background:#c49a43;color:#111;font-weight:bold;border:0;border-radius:8px;padding:11px 16px;text-decoration:none;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}.muted{color:#aaa}.flash{color:#f3c86e}
small{color:#aaa}.tag{display:inline-block;border:1px solid #6e592d;color:#e8c979;padding:4px 8px;border-radius:30px;font-size:12px}
</style></head><body><header><div class="brand">♛ الإمبراطورية إستوري</div><div class="sub">EMPIRE E-STORY · سوقك الرقمي</div></header>
<main>{% with messages=get_flashed_messages() %}{% for m in messages %}<p class="flash">{{m}}</p>{% endfor %}{% endwith %}
{{ body|safe }}
<p><a href="/account">العودة إلى حسابك</a> · <a href="/logout">تسجيل الخروج</a></p></main></body></html>
"""


@empire_bp.get("/")
def store_home():
    user = _require_login()
    if not user:
        return redirect("/login")
    db = _db()
    listings = db.execute(
        "SELECT * FROM empire_market_listings WHERE status='PUBLISHED' ORDER BY id DESC LIMIT 30"
    ).fetchall()
    body = render_template_string("""
    <section class="panel"><h1>الإمبراطورية إستوري</h1>
    <p>منصة التجارة الرقمية وشحن الألعاب وسوق المستخدمين.</p>
    <div class="grid">
      <a class="btn" href="{{url_for('empire.marketplace')}}">سوق حسابات PUBG Mobile</a>
      <a class="btn" href="{{url_for('empire.new_listing')}}">اعرض حسابك للبيع</a>
      <a class="btn" href="{{url_for('empire.agents')}}">برنامج الوكلاء</a>
    </div></section>
    <section class="panel"><h2>أحدث العروض</h2><div class="grid">
    {% for x in listings %}<article class="panel"><span class="tag">{{x['game']}}</span>
    <h3>{{x['title']}}</h3><p class="muted">{{x['description']}}</p>
    <strong>{{"{:,}".format(x['price_sdg'])}} SDG</strong><p><small>البائع: {{x['seller_username']}}</small></p></article>
    {% else %}<p class="muted">لا توجد عروض منشورة حالياً.</p>{% endfor %}
    </div></section>
    <p class="muted">الدفع والضمان المالي لن يُفعّلا قبل ربط وسيلة دفع والتحقق منها فعلياً.</p>
    """, listings=listings)
    return render_template_string(BASE, title="المتجر", body=body)


@empire_bp.get("/marketplace")
def marketplace():
    user = _require_login()
    if not user:
        return redirect("/login")
    rows = _db().execute(
        "SELECT * FROM empire_market_listings WHERE status='PUBLISHED' ORDER BY id DESC LIMIT 100"
    ).fetchall()
    body = render_template_string("""
    <section class="panel"><h1>سوق حسابات PUBG Mobile</h1>
    <p class="muted">عروض ينشرها المستخدمون. لا تشارك كلمات المرور أو رموز الاسترداد في وصف الإعلان.</p>
    <a class="btn" href="{{url_for('empire.new_listing')}}">إضافة إعلان</a></section>
    <div class="grid">{% for x in rows %}<article class="panel">
      <span class="tag">{{x['game']}}</span><h2>{{x['title']}}</h2>
      <p>{{x['description']}}</p><strong>{{"{:,}".format(x['price_sdg'])}} SDG</strong>
      <p><small>البائع: {{x['seller_username']}}</small></p>
    </article>{% else %}<p class="muted">لا توجد إعلانات حالياً.</p>{% endfor %}</div>
    """, rows=rows)
    return render_template_string(BASE, title="السوق", body=body)


@empire_bp.route("/listing/new", methods=["GET", "POST"])
def new_listing():
    user = _require_login()
    if not user:
        return redirect("/login")
    if request.method == "POST":
        _csrf()
        game = request.form.get("game", "PUBG Mobile").strip()
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        raw_price = request.form.get("price_sdg", "").strip()
        if game not in {"PUBG Mobile"}:
            abort(400, "اللعبة غير مفعلة حالياً.")
        if not title or len(title) > 100 or len(description) < 10 or len(description) > 2000:
            flash("أدخل عنواناً ووصفاً صحيحين (10 إلى 2000 حرف).")
            return redirect(url_for("empire.new_listing"))
        if not re.fullmatch(r"[1-9][0-9]{0,11}", raw_price):
            flash("السعر يجب أن يكون رقماً صحيحاً موجباً.")
            return redirect(url_for("empire.new_listing"))
        public_id = secrets.token_urlsafe(9)
        now = _now()
        db = _db()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("""INSERT INTO empire_market_listings
                (public_id,seller_username,game,title,description,price_sdg,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'PUBLISHED',?,?)""",
                (public_id,user["username"],game,title,description,int(raw_price),now,now))
            listing_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            _audit(user["username"],"LISTING_CREATED","listing",listing_id,title)
            db.commit()
        except Exception:
            db.rollback()
            raise
        flash("تم نشر إعلانك في السوق.")
        return redirect(url_for("empire.marketplace"))
    body = render_template_string("""
    <section class="panel"><h1>عرض حساب للبيع</h1>
    <p class="muted">لا تضع كلمة المرور أو البريد أو بيانات الاسترداد في الإعلان. تتم عملية التسليم عبر مسار آمن منفصل بعد تفعيل الضمان.</p>
    <form method="post"><input type="hidden" name="csrf" value="{{csrf}}">
    <label>اللعبة</label><select name="game"><option>PUBG Mobile</option></select>
    <label>عنوان الإعلان</label><input name="title" maxlength="100" required>
    <label>تفاصيل الحساب (من دون بيانات الدخول)</label><textarea name="description" minlength="10" maxlength="2000" required></textarea>
    <label>السعر بالجنيه السوداني SDG</label><input name="price_sdg" type="number" min="1" step="1" required>
    <button>نشر الإعلان</button></form></section>
    """, csrf=session.get("csrf",""))
    return render_template_string(BASE, title="إعلان جديد", body=body)


@empire_bp.route("/agents", methods=["GET", "POST"])
def agents():
    user = _require_login()
    if not user:
        return redirect("/login")
    db = _db()
    current = db.execute("SELECT * FROM empire_agent_applications WHERE username=?", (user["username"],)).fetchone()
    if request.method == "POST":
        _csrf()
        if user["role"] == "founder":
            flash("حساب المالك لا يحتاج إلى طلب وكالة.")
        elif current and current["status"] in ("PENDING","APPROVED"):
            flash("لديك طلب وكالة قائم بالفعل.")
        else:
            db.execute("""INSERT INTO empire_agent_applications(username,status,created_at)
                VALUES(?,'PENDING',?) ON CONFLICT(username) DO UPDATE SET
                status='PENDING', reviewed_by=NULL, reviewed_at=NULL, created_at=excluded.created_at""",
                (user["username"],_now()))
            _audit(user["username"],"AGENT_APPLIED","agent",user["username"])
            flash("تم إرسال طلب الوكالة إلى إدارة الإمبراطورية.")
            current = db.execute("SELECT * FROM empire_agent_applications WHERE username=?", (user["username"],)).fetchone()
    body = render_template_string("""
    <section class="panel"><h1>برنامج الوكلاء</h1>
    <p>الوكلاء المعتمدون يمكنهم العمل في خدمات شحن الألعاب وسوق الحسابات وفق الصلاحيات والعمولات التي يحددها المالك.</p>
    {% if current %}<p>حالة طلبك: <span class="tag">{{current['status']}}</span></p>
    {% if current['status']=='APPROVED' %}<p>نسبة العمولة المعتمدة: {{'%.2f'|format(current['commission_rate_bps']/100)}}%</p>{% endif %}
    {% else %}<form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><button>التقديم كوكيل</button></form>{% endif %}
    </section>
    """, current=current, csrf=session.get("csrf",""))
    return render_template_string(BASE, title="الوكلاء", body=body)


@empire_bp.route("/admin/agents", methods=["GET", "POST"])
def admin_agents():
    user = _founder()
    db = _db()
    if request.method == "POST":
        _csrf()
        app_id = request.form.get("application_id", "")
        action = request.form.get("action", "")
        rate = request.form.get("commission_rate", "0")
        if not app_id.isdigit() or action not in {"approve","reject","suspend"}:
            abort(400)
        if not re.fullmatch(r"(?:0|[1-9][0-9]?|100)(?:\.[0-9]{1,2})?", rate):
            flash("نسبة العمولة غير صحيحة.")
            return redirect(url_for("empire.admin_agents"))
        bps = int(round(float(rate)*100))
        status = {"approve":"APPROVED","reject":"REJECTED","suspend":"SUSPENDED"}[action]
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM empire_agent_applications WHERE id=?", (int(app_id),)).fetchone()
            if not row:
                abort(404)
            db.execute("""UPDATE empire_agent_applications SET status=?,commission_rate_bps=?,
                reviewed_by=?,reviewed_at=? WHERE id=?""",
                (status,bps if action=="approve" else 0,user["username"],_now(),int(app_id)))
            _audit(user["username"],"AGENT_"+action.upper(),"agent",app_id,"commission_bps="+str(bps))
            db.commit()
        except Exception:
            db.rollback()
            raise
        flash("تم تحديث حالة الوكيل.")
        return redirect(url_for("empire.admin_agents"))
    rows = db.execute("SELECT * FROM empire_agent_applications ORDER BY id DESC").fetchall()
    body = render_template_string("""
    <section class="panel"><h1>إدارة الوكلاء</h1><div class="grid">
    {% for x in rows %}<article class="panel"><h3>{{x['username']}}</h3><p>الحالة: {{x['status']}}</p>
    <p>العمولة الحالية: {{'%.2f'|format(x['commission_rate_bps']/100)}}%</p>
    <form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="application_id" value="{{x['id']}}">
    <label>العمولة %</label><input name="commission_rate" value="{{'%.2f'|format(x['commission_rate_bps']/100)}}" type="number" min="0" max="100" step="0.01">
    <button name="action" value="approve">اعتماد</button> <button name="action" value="reject">رفض</button> <button name="action" value="suspend">إيقاف</button>
    </form></article>{% else %}<p>لا توجد طلبات وكلاء.</p>{% endfor %}
    </div></section>
    """, rows=rows, csrf=session.get("csrf",""))
    return render_template_string(BASE, title="إدارة الوكلاء", body=body)


def register_empire_store(app):
    app.register_blueprint(empire_bp)
    with app.app_context():
        init_empire_store_db()

    @app.teardown_appcontext
    def close_empire_db(exception=None):
        from flask import g
        conn = g.pop("empire_db", None)
        if conn is not None:
            conn.close()
