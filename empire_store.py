"""Empire E-Story commerce module.

Uses a separate double-entry store ledger in the existing SQLite database.
No legacy bank balance is imported or treated as store money. External payment
verification is manual until a supported Binance/payment API is configured.
"""
import os, re, json, sqlite3, secrets, io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template_string, request, session, url_for, send_from_directory
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image
from werkzeug.utils import secure_filename

empire_bp = Blueprint("empire", __name__, url_prefix="/store")
DATABASE = os.getenv("NAB_DATABASE", "national_arab_bank.db")
IMAGE_DIR = os.path.abspath(os.getenv("EMPIRE_IMAGE_DIR", os.path.join("static", "uploads", "empire")))
os.makedirs(IMAGE_DIR, exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    from flask import g
    if "empire_db" not in g:
        g.empire_db = sqlite3.connect(DATABASE, timeout=30, isolation_level=None)
        g.empire_db.row_factory = sqlite3.Row
        g.empire_db.execute("PRAGMA foreign_keys=ON")
        g.empire_db.execute("PRAGMA busy_timeout=30000")
    return g.empire_db

def close_db(exception=None):
    from flask import g
    conn = g.pop("empire_db", None)
    if conn is not None:
        conn.close()

def init_db():
    db().executescript("""
    CREATE TABLE IF NOT EXISTS es_listings(
      id INTEGER PRIMARY KEY, public_id TEXT UNIQUE NOT NULL, seller TEXT NOT NULL,
      game TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
      price_sdg INTEGER NOT NULL CHECK(price_sdg>0), image_path TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'PUBLISHED'
        CHECK(status IN ('PUBLISHED','PAUSED','SOLD','REMOVED')),
      created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS es_listings_status ON es_listings(status,created_at DESC);

    CREATE TABLE IF NOT EXISTS es_agents(
      username TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING','APPROVED','REJECTED','SUSPENDED')),
      commission_bps INTEGER NOT NULL DEFAULT 0 CHECK(commission_bps BETWEEN 0 AND 10000),
      services TEXT NOT NULL DEFAULT 'topups,marketplace',
      created_at TEXT NOT NULL, reviewed_at TEXT, reviewed_by TEXT);

    CREATE TABLE IF NOT EXISTS es_products(
      id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, game TEXT NOT NULL,
      title TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT 'أدخل معرف اللاعب بدقة.',
      price_sdg INTEGER NOT NULL CHECK(price_sdg>0), cost_sdg INTEGER NOT NULL DEFAULT 0 CHECK(cost_sdg>=0), active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_topup_orders(
      id INTEGER PRIMARY KEY, reference TEXT UNIQUE NOT NULL, buyer TEXT NOT NULL,
      product_id INTEGER NOT NULL REFERENCES es_products(id), player_id TEXT NOT NULL,
      amount_sdg INTEGER NOT NULL CHECK(amount_sdg>0),
      status TEXT NOT NULL DEFAULT 'PENDING_PAYMENT'
        CHECK(status IN ('PENDING_PAYMENT','PAID','ASSIGNED','PROCESSING','COMPLETED','REJECTED','REFUNDED')),
      agent TEXT, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

    CREATE TABLE IF NOT EXISTS es_wallets(
      username TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency IN ('SDG','EPC')),
      balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0),
      PRIMARY KEY(username,currency));
    CREATE TABLE IF NOT EXISTS es_ledger(
      id INTEGER PRIMARY KEY, reference TEXT NOT NULL, line_no INTEGER NOT NULL,
      account TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency IN ('SDG','EPC')),
      delta INTEGER NOT NULL, memo TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(reference,line_no));
    CREATE INDEX IF NOT EXISTS es_ledger_account ON es_ledger(account,currency,id);

    CREATE TABLE IF NOT EXISTS es_payments(
      id INTEGER PRIMARY KEY, reference TEXT UNIQUE NOT NULL, username TEXT NOT NULL,
      method TEXT NOT NULL CHECK(method IN ('SDG_WALLET','BINANCE_USDT')),
      currency TEXT NOT NULL CHECK(currency IN ('SDG','USDT')),
      amount INTEGER NOT NULL CHECK(amount>0), quantity_epc INTEGER NOT NULL DEFAULT 0, operation_id TEXT UNIQUE,
      purpose TEXT NOT NULL CHECK(purpose IN ('WALLET_DEPOSIT','EPC_PURCHASE','TOPUP','MARKETPLACE')),
      status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','CONFIRMED','REJECTED')),
      proof_note TEXT NOT NULL DEFAULT '', reviewer TEXT, created_at TEXT NOT NULL, reviewed_at TEXT);
    CREATE TABLE IF NOT EXISTS es_market_orders(
      id INTEGER PRIMARY KEY, reference TEXT UNIQUE NOT NULL, listing_id INTEGER NOT NULL REFERENCES es_listings(id),
      buyer TEXT NOT NULL, seller TEXT NOT NULL, price_sdg INTEGER NOT NULL CHECK(price_sdg>0),
      status TEXT NOT NULL CHECK(status IN ('ESCROWED','DELIVERED','RELEASED','REFUNDED','DISPUTED')),
      delivery_cipher TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_audit(
      id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
      object_type TEXT NOT NULL, object_ref TEXT NOT NULL DEFAULT '',
      details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_disputes(
      id INTEGER PRIMARY KEY, order_ref TEXT NOT NULL UNIQUE, opened_by TEXT NOT NULL,
      reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK(status IN ('OPEN','RESOLVED_BUYER','RESOLVED_SELLER')),
      resolution TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, resolved_at TEXT);
    """)
    payment_cols={r["name"] for r in db().execute("PRAGMA table_info(es_payments)").fetchall()}
    if "quantity_epc" not in payment_cols:
        db().execute("ALTER TABLE es_payments ADD COLUMN quantity_epc INTEGER NOT NULL DEFAULT 0")
    listing_cols={r["name"] for r in db().execute("PRAGMA table_info(es_listings)").fetchall()}
    if "image_path" not in listing_cols:
        db().execute("ALTER TABLE es_listings ADD COLUMN image_path TEXT NOT NULL DEFAULT ''")
    product_cols={r["name"] for r in db().execute("PRAGMA table_info(es_products)").fetchall()}
    if "cost_sdg" not in product_cols:
        db().execute("ALTER TABLE es_products ADD COLUMN cost_sdg INTEGER NOT NULL DEFAULT 0")
    defaults = {
      "epc_price_sdg":"1000", "epc_price_usdt_micros":"1000000",
      "binance_id":os.getenv("EMPIRE_BINANCE_ID",""),
      "store_name":"الإمبراطورية إستوري", "market_fee_bps":"500",
      "topups_enabled":"0", "epc_sales_enabled":"0"
    }
    for k,v in defaults.items():
        db().execute("INSERT OR IGNORE INTO es_settings(key,value) VALUES(?,?)",(k,v))

def user():
    username=session.get("username")
    if not username: return None
    return db().execute("SELECT username,full_name,role,active FROM users WHERE username=?",(username,)).fetchone()

def login_required(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        u=user()
        if not u or not u["active"]: return redirect("/login")
        return fn(*a,**kw)
    return wrapped

def founder_required(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        u=user()
        if not u or not u["active"] or u["role"]!="founder": abort(403)
        return fn(*a,**kw)
    return wrapped

def csrf():
    v=request.form.get("csrf",""); s=session.get("csrf","")
    if not v or not s or not secrets.compare_digest(v,s): abort(400,"رمز الحماية غير صالح")

def audit(actor,action,kind,ref="",details=""):
    db().execute("INSERT INTO es_audit(actor,action,object_type,object_ref,details,created_at) VALUES(?,?,?,?,?,?)",
                 (actor,action,kind,str(ref),str(details)[:500],now()))

def setting(key,default=""):
    row=db().execute("SELECT value FROM es_settings WHERE key=?",(key,)).fetchone()
    return row["value"] if row else default

def money_int(value):
    if not re.fullmatch(r"[1-9][0-9]{0,11}",str(value or "")): raise ValueError("المبلغ غير صالح")
    return int(value)

def ref():
    return secrets.token_hex(8).upper()

def ensure_wallet(username,currency):
    db().execute("INSERT OR IGNORE INTO es_wallets(username,currency,balance) VALUES(?,?,0)",(username,currency))

def post(reference, entries, memo):
    """Post balanced signed lines atomically. Positive = credit, negative = debit."""
    totals={}
    for account,currency,delta in entries:
        totals[currency]=totals.get(currency,0)+delta
    if any(v!=0 for v in totals.values()): raise ValueError("القيد المحاسبي غير متوازن")
    for line_no,(account,currency,delta) in enumerate(entries, start=1):
        if account.startswith("user:"):
            name=account[5:]
            ensure_wallet(name,currency)
            row=db().execute("SELECT balance FROM es_wallets WHERE username=? AND currency=?",(name,currency)).fetchone()
            new=row["balance"]+delta
            if new<0: raise ValueError("الرصيد غير كافٍ")
            db().execute("UPDATE es_wallets SET balance=? WHERE username=? AND currency=?",(new,name,currency))
        db().execute("INSERT INTO es_ledger(reference,line_no,account,currency,delta,memo,created_at) VALUES(?,?,?,?,?,?,?)",
                     (reference,line_no,account,currency,delta,memo,now()))

def balance(username,currency):
    row=db().execute("SELECT balance FROM es_wallets WHERE username=? AND currency=?",(username,currency)).fetchone()
    return row["balance"] if row else 0

def encryption():
    key=os.getenv("EMPIRE_DATA_ENCRYPTION_KEY","")
    if not key: raise RuntimeError("تسليم بيانات الحسابات متوقف حتى ضبط EMPIRE_DATA_ENCRYPTION_KEY")
    return Fernet(key.encode())

def page(title,body,**ctx):
    shell="""<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>
    <meta name='theme-color' content='#0b0b0b'><link rel='manifest' href='/store/manifest.webmanifest'><link rel='apple-touch-icon' href='/store/icon.svg'>
    <title>{{title}} | الإمبراطورية إستوري</title><style>
    *{box-sizing:border-box}body{margin:0;background:#090909;color:#f6f1e6;font-family:system-ui,Arial}
    header{padding:18px max(16px,calc((100% - 1050px)/2));background:#111;border-bottom:1px solid #a78338}
    .brand{font-size:22px;font-weight:900;color:#e8c878}main{max-width:1050px;margin:22px auto;padding:0 14px}
    .panel{background:#151515;border:1px solid #42351e;border-radius:15px;padding:17px;margin:12px 0}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
    h1,h2,h3{color:#e8c878}a{color:#e8c878}.btn,button{display:inline-block;background:#d1aa56;color:#111;border:0;border-radius:9px;padding:11px 15px;font-weight:800;text-decoration:none;cursor:pointer}
    input,textarea,select{display:block;width:100%;padding:12px;margin:7px 0 13px;background:#0c0c0c;border:1px solid #53452b;border-radius:8px;color:white}
    label{color:#d8c68f}.muted,small{color:#aaa}.tag{border:1px solid #80652d;border-radius:30px;padding:4px 9px;color:#e8c878;font-size:12px}
    .nav{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.nav a{padding:7px 10px;border:1px solid #514225;border-radius:8px;text-decoration:none}
    </style><script>if('serviceWorker' in navigator){window.addEventListener('load',()=>navigator.serviceWorker.register('/store/sw.js'));}</script></head><body><header><div class='brand'>♛ الإمبراطورية إستوري</div><small>EMPIRE E-STORY · سوقك الرقمي</small></header>
    <main><nav class='nav'><a href='/store/'>الرئيسية</a><a href='/store/marketplace'>سوق الحسابات</a><a href='/store/topups'>شحن الألعاب</a><a href='/store/wallet'>المحفظة</a><a href='/store/agents'>الوكلاء</a><a href='/store/orders'>طلباتي</a>{% if is_founder %}<a href='/store/admin'>مركز المالك</a>{% endif %}</nav>
    {% with ms=get_flashed_messages() %}{% for m in ms %}<p>{{m}}</p>{% endfor %}{% endwith %}
    {{body|safe}}<p><a href='/account'>حساب المستخدم</a> · <a href='/logout'>خروج</a></p></main></body></html>"""
    current=user()
    ctx.setdefault("is_founder", bool(current and current["role"]=="founder"))
    return render_template_string(shell,title=title,body=body,**ctx)

@empire_bp.get("/healthz")
def healthz():
    try:
        db().execute("SELECT 1").fetchone()
        return {"status":"ok","service":"empire-estory"},200
    except Exception:
        return {"status":"unavailable"},503

@empire_bp.get("/manifest.webmanifest")
def manifest():
    return {"name":"الإمبراطورية إستوري","short_name":"Empire E-Story","start_url":"/store/","display":"standalone",
            "background_color":"#090909","theme_color":"#0b0b0b","lang":"ar","dir":"rtl",
            "icons":[{"src":"/store/icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any maskable"}]},200,{"Content-Type":"application/manifest+json"}

@empire_bp.get("/icon.svg")
def app_icon():
    svg="""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='96' fill='#090909'/><path d='M256 68l44 70 82-22-12 84 72 45-72 45 12 84-82-22-44 70-44-70-82 22 12-84-72-45 72-45-12-84 82 22z' fill='none' stroke='#d8b45e' stroke-width='16' stroke-linejoin='round'/><path d='M160 310h192l-18 54H178z' fill='#d8b45e'/><path d='M180 284l-16-92 66 52 26-112 26 112 66-52-16 92z' fill='#d8b45e'/></svg>"""
    return svg,200,{"Content-Type":"image/svg+xml"}

@empire_bp.get("/sw.js")
def service_worker():
    js="""const CACHE='empire-shell-v1';self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.add('/store/offline'))));self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin)return;if(u.pathname==='/store/offline'){e.respondWith(caches.match('/store/offline').then(r=>r||fetch(e.request)));return;}e.respondWith(fetch(e.request).catch(()=>caches.match('/store/offline')));});"""
    return js,200,{"Content-Type":"application/javascript","Service-Worker-Allowed":"/store/"}

@empire_bp.get("/offline")
def offline():
    return page("غير متصل", "<section class='panel'><h1>لا يوجد اتصال بالإنترنت</h1><p>أعد الاتصال لمتابعة عملياتك. لا يتم تخزين صفحات الدفع أو الأرصدة دون اتصال.</p></section>")

@empire_bp.get("/")
@login_required
def home():
    u=user()
    rows=db().execute("SELECT * FROM es_listings WHERE status='PUBLISHED' ORDER BY id DESC LIMIT 8").fetchall()
    body=render_template_string("""<section class='panel'><h1>مرحباً بك في الإمبراطورية</h1>
    <p>منصة شحن الألعاب والتجارة الرقمية وسوق المستخدمين.</p><div class='grid'>
    <a class='btn' href='/store/marketplace'>سوق حسابات PUBG</a><a class='btn' href='/store/topups'>شحن الألعاب</a>
    <a class='btn' href='/store/wallet'>محفظتي وEPC</a><a class='btn' href='/store/agents'>برنامج الوكلاء</a></div>
    <p class='muted'>الرصيد التجاري منفصل عن أي رصيد قديم في النظام السابق.</p></section>
    <section class='panel'><h2>أحدث العروض</h2><div class='grid'>{% for x in rows %}
    <article class='panel'>{% if x['image_path'] %}<img src='/store/listing-image/{{x['id']}}' alt='صورة العرض' style='width:100%;max-height:220px;object-fit:cover;border-radius:10px'>{% endif %}<span class='tag'>{{x['game']}}</span><h3>{{x['title']}}</h3><p>{{x['description']}}</p><b>{{'{:,}'.format(x['price_sdg'])}} SDG</b></article>
    {% else %}<p>لا توجد عروض حالياً.</p>{% endfor %}</div></section>""",rows=rows)
    return page("الرئيسية",body)

@empire_bp.get("/wallet")
@login_required
def wallet():
    u=user(); ensure_wallet(u["username"],"SDG"); ensure_wallet(u["username"],"EPC")
    body=render_template_string("""<section class='panel'><h1>المحفظة التجارية</h1>
    <div class='grid'><article class='panel'><small>رصيد SDG</small><h2>{{'{:,}'.format(sdg)}}</h2></article>
    <article class='panel'><small>رصيد EPC</small><h2>{{'{:,}'.format(epc)}}</h2></article></div>
    <a class='btn' href='/store/deposit'>طلب إيداع SDG</a> <a class='btn' href='/store/buy-epc'>شراء EPC</a> <a class='btn' href='/store/transfer-epc'>تحويل EPC</a> <a class='btn' href='/store/payment/reference'>ربط تحويل Binance</a>
    <p class='muted'>EPC رصيد داخلي تجريبي ولا يمثل حالياً أصلاً قابلاً للسحب أو التداول خارج المنصة.</p></section>
    <section class='panel'><h2>آخر القيود</h2>{% for x in rows %}<p>{{x['created_at']}} · {{x['memo']}} · {{x['delta']}} {{x['currency']}}</p>{% else %}<p>لا توجد عمليات.</p>{% endfor %}</section>""",
      sdg=balance(u["username"],"SDG"),epc=balance(u["username"],"EPC"),
      rows=db().execute("SELECT * FROM es_ledger WHERE account=? ORDER BY id DESC LIMIT 30",("user:"+u["username"],)).fetchall())
    return page("المحفظة",body)

@empire_bp.route("/deposit",methods=["GET","POST"])
@login_required
def deposit():
    u=user()
    if request.method=="POST":
        csrf()
        amount=money_int(request.form.get("amount"))
        note=request.form.get("note","").strip()[:250]
        operation=request.form.get("operation_id","").strip()[:100] or None
        r=ref()
        db().execute("INSERT INTO es_payments(reference,username,method,currency,amount,operation_id,purpose,proof_note,created_at) VALUES(?,?,'SDG_WALLET','SDG',?,?,'WALLET_DEPOSIT',?,?)",
                     (r,u["username"],amount,operation,note,now()))
        audit(u["username"],"DEPOSIT_REQUEST","payment",r,"manual review required")
        flash("تم تسجيل طلب الإيداع للمراجعة. لن يضاف الرصيد قبل التحقق الفعلي.")
        return redirect(url_for("empire.wallet"))
    body=render_template_string("""<section class='panel'><h1>طلب إيداع SDG</h1>
    <p>أدخل المبلغ ورقم العملية أو تفاصيل التحويل. لا ترفع إثباتاً غير حقيقي. المراجعة يدوية حالياً.</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>المبلغ SDG</label><input name='amount' type='number' min='1' required>
    <label>رقم العملية (إن وجد)</label><input name='operation_id' maxlength='100'><label>تفاصيل الدفع</label><textarea name='note' maxlength='250'></textarea><button>إرسال للمراجعة</button></form></section>""",csrf=session.get("csrf",""))
    return page("إيداع",body)

@empire_bp.get("/topups")
@login_required
def topups():
    products=db().execute("SELECT * FROM es_products WHERE active=1 ORDER BY game,title").fetchall()
    body=render_template_string("""<section class='panel'><h1>شحن الألعاب</h1><p>طلبات الشحن متاحة فقط للمنتجات التي يضيفها المالك، والتنفيذ يتطلب مورداً أو وكيلاً معتمداً.</p></section>
    <div class='grid'>{% for p in products %}<article class='panel'><span class='tag'>{{p['game']}}</span><h3>{{p['title']}}</h3><p>{{p['instructions']}}</p><b>{{'{:,}'.format(p['price_sdg'])}} SDG</b>
    <form method='post' action='/store/topups/new'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='product_id' value='{{p['id']}}'>
    <label>معرف اللاعب</label><input name='player_id' maxlength='100' required><button>طلب الشحن</button></form></article>{% else %}<p>لم يضف المالك باقات بعد.</p>{% endfor %}</div>""",products=products,csrf=session.get("csrf",""))
    return page("شحن الألعاب",body)

@empire_bp.post("/topups/new")
@login_required
def new_topup():
    csrf(); u=user()
    if setting("topups_enabled","0")!="1": abort(503,"طلبات الشحن متوقفة حالياً")
    p=db().execute("SELECT * FROM es_products WHERE id=? AND active=1",(request.form.get("product_id"),)).fetchone()
    if not p: abort(404)
    player=request.form.get("player_id","").strip()
    if not player or len(player)>100: abort(400)
    r=ref()
    db().execute("INSERT INTO es_topup_orders(reference,buyer,product_id,player_id,amount_sdg,status,created_at,updated_at) VALUES(?,?,?,?,?,'PENDING_PAYMENT',?,?)",
                 (r,u["username"],p["id"],player,p["price_sdg"],now(),now()))
    audit(u["username"],"TOPUP_ORDER_CREATED","topup",r)
    flash("تم إنشاء طلب الشحن. سيظهر كطلب غير مدفوع حتى اعتماد وسيلة الدفع.")
    return redirect(url_for("empire.orders"))

@empire_bp.get("/marketplace")
@login_required
def marketplace():
    rows=db().execute("SELECT * FROM es_listings WHERE status='PUBLISHED' ORDER BY id DESC LIMIT 100").fetchall()
    body=render_template_string("""<section class='panel'><h1>سوق حسابات PUBG Mobile</h1>
    <p>البيع بين المستخدمين. لا تضع كلمات المرور أو رموز الاسترداد في الإعلان.</p><a class='btn' href='/store/listing/new'>اعرض حسابك</a></section>
    <div class='grid'>{% for x in rows %}<article class='panel'><span class='tag'>{{x['game']}}</span><h3>{{x['title']}}</h3><p>{{x['description']}}</p><b>{{'{:,}'.format(x['price_sdg'])}} SDG</b><p><small>البائع: {{x['seller']}}</small></p>
    <form method='post' action='/store/marketplace/buy'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='listing_id' value='{{x['id']}}'><button>شراء مع ضمان</button></form></article>{% else %}<p>لا توجد عروض حالياً.</p>{% endfor %}</div>""",rows=rows,csrf=session.get("csrf",""))
    return page("السوق",body)

@empire_bp.get("/listing-image/<int:listing_id>")
def listing_image(listing_id):
    row=db().execute("SELECT image_path FROM es_listings WHERE id=?",(listing_id,)).fetchone()
    if not row or not row["image_path"]: abort(404)
    return send_from_directory(IMAGE_DIR,row["image_path"],max_age=3600)

@empire_bp.route("/listing/new",methods=["GET","POST"])
@login_required
def listing_new():
    u=user()
    if request.method=="POST":
        csrf()
        title=request.form.get("title","").strip(); desc=request.form.get("description","").strip()
        price=money_int(request.form.get("price_sdg"))
        if not title or len(title)>100 or len(desc)<10 or len(desc)>1500: abort(400)
        image_path=""
        uploaded=request.files.get("image")
        if uploaded and uploaded.filename:
            raw=uploaded.read(5*1024*1024+1)
            if len(raw)>5*1024*1024: abort(413,"حجم الصورة يتجاوز 5 ميغابايت")
            try:
                source=Image.open(io.BytesIO(raw)); source.verify()
                source=Image.open(io.BytesIO(raw))
                if source.width>4000 or source.height>4000: abort(400,"أبعاد الصورة كبيرة جداً")
                image_path=secrets.token_hex(16)+".jpg"
                source.convert("RGB").save(os.path.join(IMAGE_DIR,image_path),"JPEG",quality=88,optimize=True)
            except Exception:
                abort(400,"ملف الصورة غير صالح")
        r=ref()
        db().execute("INSERT INTO es_listings(public_id,seller,game,title,description,price_sdg,image_path,status,created_at) VALUES(?,?,'PUBG Mobile',?,?,?,?,'PUBLISHED',?)",
                     (r,u["username"],title,desc,price,image_path,now()))
        audit(u["username"],"LISTING_CREATED","listing",r)
        return redirect(url_for("empire.marketplace"))
    body=render_template_string("""<section class='panel'><h1>إضافة حساب للبيع</h1><form method='post' enctype='multipart/form-data'><input type='hidden' name='csrf' value='{{csrf}}'>
    <label>صورة الحساب (اختياري، JPG/PNG/WebP، حتى 5MB)</label><input type='file' name='image' accept='image/jpeg,image/png,image/webp'>
    <label>عنوان العرض</label><input name='title' maxlength='100' required><label>وصف الحساب دون بيانات الدخول</label><textarea name='description' minlength='10' maxlength='1500' required></textarea>
    <label>السعر SDG</label><input name='price_sdg' type='number' min='1' required><button>نشر الإعلان</button></form></section>""",csrf=session.get("csrf",""))
    return page("إعلان جديد",body)

@empire_bp.post("/marketplace/buy")
@login_required
def buy_listing():
    csrf(); u=user(); listing=db().execute("SELECT * FROM es_listings WHERE id=? AND status='PUBLISHED'",(request.form.get("listing_id"),)).fetchone()
    if not listing: abort(404)
    if listing["seller"]==u["username"]: abort(400,"لا يمكنك شراء إعلانك")
    r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
    try:
        listing=d.execute("SELECT * FROM es_listings WHERE id=? AND status='PUBLISHED'",(listing["id"],)).fetchone()
        if not listing: raise ValueError("الإعلان لم يعد متاحاً")
        post(r,[("user:"+u["username"],"SDG",-listing["price_sdg"]),("escrow:market","SDG",listing["price_sdg"])],"حجز قيمة صفقة سوق")
        d.execute("INSERT INTO es_market_orders(reference,listing_id,buyer,seller,price_sdg,status,created_at,updated_at) VALUES(?,?,?,?,?,'ESCROWED',?,?)",
                  (r,listing["id"],u["username"],listing["seller"],listing["price_sdg"],now(),now()))
        d.execute("UPDATE es_listings SET status='SOLD' WHERE id=?",(listing["id"],))
        audit(u["username"],"MARKET_ORDER_ESCROWED","market_order",r)
        d.commit()
    except Exception:
        d.rollback(); raise
    flash("تم حجز المبلغ من محفظتك التجارية. على البائع تسليم الحساب عبر صفحة الطلب.")
    return redirect(url_for("empire.orders"))

@empire_bp.route("/payment/reference",methods=["GET","POST"])
@login_required
def payment_reference():
    u=user()
    if request.method=="POST":
        csrf()
        r=request.form.get("reference","").strip()
        op=request.form.get("operation_id","").strip()
        row=db().execute("SELECT * FROM es_payments WHERE reference=? AND username=? AND status='PENDING'",(r,u["username"])).fetchone()
        if not row or not op or len(op)>100: abort(400)
        db().execute("UPDATE es_payments SET operation_id=?,proof_note=? WHERE reference=?",(op,"Binance transfer ID submitted by customer",r))
        audit(u["username"],"BINANCE_REFERENCE_SUBMITTED","payment",r)
        flash("تم تسجيل رقم التحويل. سيظل الطلب معلقاً حتى التحقق من حساب الاستقبال.")
        return redirect(url_for("empire.wallet"))
    rows=db().execute("SELECT reference,amount,currency,quantity_epc,created_at FROM es_payments WHERE username=? AND status='PENDING' AND method='BINANCE_USDT' ORDER BY id DESC",(u["username"],)).fetchall()
    body=render_template_string("""<section class='panel'><h1>ربط تحويل Binance</h1><p>حوّل إلى Binance ID: <b>{{bid or 'لم يضبطه المالك بعد'}}</b>. أدخل رقم التحويل الحقيقي بعد إتمامه.</p>
    {% for x in rows %}<article class='panel'><p>المرجع {{x['reference']}} · {{x['amount']/1000000}} USDT · EPC المطلوبة {{x['quantity_epc']}}</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'>
    <label>رقم التحويل من Binance</label><input name='operation_id' maxlength='100' required><button>إرسال للمراجعة</button></form></article>{% else %}<p>لا توجد طلبات Binance معلقة.</p>{% endfor %}</section>""",
      bid=setting("binance_id"),rows=rows,csrf=session.get("csrf",""))
    return page("تحويل Binance",body)

@empire_bp.post("/topups/pay")
@login_required
def pay_topup():
    csrf(); u=user(); r=request.form.get("reference","")
    d=db(); d.execute("BEGIN IMMEDIATE")
    try:
        row=d.execute("SELECT * FROM es_topup_orders WHERE reference=? AND buyer=? AND status='PENDING_PAYMENT'",(r,u["username"])).fetchone()
        if not row: raise ValueError("طلب الشحن غير متاح للدفع")
        post(r,[("user:"+u["username"],"SDG",-row["amount_sdg"]),("clearing:topups","SDG",row["amount_sdg"])],"دفع طلب شحن ألعاب")
        d.execute("UPDATE es_topup_orders SET status='PAID',updated_at=? WHERE reference=?",(now(),r))
        audit(u["username"],"TOPUP_PAID","topup",r); d.commit()
    except Exception: d.rollback(); raise
    flash("تم دفع طلب الشحن من محفظة المتجر. ينتظر تعيين وكيل لتنفيذه.")
    return redirect(url_for("empire.orders"))

@empire_bp.post("/agent/topup/claim")
@login_required
def claim_topup():
    csrf(); u=user()
    agent=db().execute("SELECT status FROM es_agents WHERE username=?",(u["username"],)).fetchone()
    if not agent or agent["status"]!="APPROVED": abort(403)
    r=request.form.get("reference","")
    changed=db().execute("UPDATE es_topup_orders SET status='ASSIGNED',agent=?,updated_at=? WHERE reference=? AND status='PAID' AND agent IS NULL",
                         (u["username"],now(),r)).rowcount
    if changed!=1: abort(409,"الطلب لم يعد متاحاً")
    audit(u["username"],"TOPUP_CLAIMED","topup",r)
    return redirect(url_for("empire.orders"))

@empire_bp.post("/agent/topup/complete")
@login_required
def complete_topup():
    csrf(); u=user()
    agent=db().execute("SELECT status FROM es_agents WHERE username=?",(u["username"],)).fetchone()
    if not agent or agent["status"]!="APPROVED": abort(403)
    r=request.form.get("reference",""); note=request.form.get("note","").strip()
    if len(note)<4 or len(note)>300: abort(400)
    d=db(); d.execute("BEGIN IMMEDIATE")
    try:
        order=d.execute("SELECT t.*,p.cost_sdg FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id WHERE t.reference=? AND t.agent=? AND t.status='ASSIGNED'",
                        (r,u["username"])).fetchone()
        if not order: raise ValueError("الطلب لم يعد مسنداً إليك")
        margin=order["amount_sdg"]-order["cost_sdg"]
        if margin<0: raise ValueError("تكلفة المورد أعلى من سعر البيع؛ أوقف المالك هذه العملية للمراجعة")
        agent_row=d.execute("SELECT commission_bps FROM es_agents WHERE username=? AND status='APPROVED'",(u["username"],)).fetchone()
        commission=(margin*agent_row["commission_bps"])//10000
        platform_profit=margin-commission
        post(r+"T",[("clearing:topups","SDG",-order["amount_sdg"]),("supplier:topup_cost","SDG",order["cost_sdg"]),
                    ("user:"+u["username"],"SDG",commission),("platform:revenue","SDG",platform_profit)],
             "تسوية شحن لعبة وتسجيل عمولة الوكيل")
        d.execute("UPDATE es_topup_orders SET status='COMPLETED',note=?,updated_at=? WHERE reference=? AND agent=? AND status='ASSIGNED'",
                  (note,now(),r,u["username"]))
        audit(u["username"],"TOPUP_COMPLETED","topup",r,"commission="+str(commission)+"; "+note)
        d.commit()
    except Exception: d.rollback(); raise
    flash("تم إغلاق الطلب وتسجيل عمولتك بعد احتساب تكلفة المورد.")
    return redirect(url_for("empire.orders"))

@empire_bp.get("/orders")
@login_required
def orders():
    u=user()
    market_rows=db().execute("SELECT * FROM es_market_orders WHERE buyer=? OR seller=? ORDER BY id DESC LIMIT 100",(u["username"],u["username"])).fetchall()
    markets=[]
    for row in market_rows:
        item=dict(row); item["delivery_text"]=""
        if item["buyer"]==u["username"] and item["status"]=="DELIVERED" and item["delivery_cipher"]:
            try: item["delivery_text"]=encryption().decrypt(item["delivery_cipher"].encode()).decode()
            except Exception: item["delivery_text"]="تعذر فك بيانات التسليم؛ تواصل مع الدعم."
        markets.append(item)
    top=db().execute("SELECT t.*,p.title,p.game FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id WHERE t.buyer=? OR t.agent=? ORDER BY t.id DESC LIMIT 100",(u["username"],u["username"])).fetchall()
    agent=db().execute("SELECT status FROM es_agents WHERE username=?",(u["username"],)).fetchone()
    open_jobs=db().execute("SELECT t.*,p.title,p.game FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id WHERE t.status='PAID' AND t.agent IS NULL ORDER BY t.id LIMIT 50").fetchall() if agent and agent["status"]=="APPROVED" else []
    body=render_template_string("""<section class='panel'><h1>طلباتي</h1><h2>صفقات الحسابات</h2>{% for x in markets %}<article class='panel'><b>{{x['reference']}}</b> · {{x['price_sdg']}} SDG · {{x['status']}}<p>المشتري: {{x['buyer']}} — البائع: {{x['seller']}}</p>
    {% if x['seller']==me and x['status']=='ESCROWED' %}<form method='post' action='/store/order/deliver'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><textarea name='delivery' minlength='10' maxlength='3000' placeholder='بيانات التسليم الآمنة' required></textarea><button>تسليم للمشتري</button></form>{% endif %}
    {% if x['buyer']==me and x['status']=='DELIVERED' %}<div class='panel'><b>بيانات التسليم</b><pre style='white-space:pre-wrap;overflow-wrap:anywhere'>{{x['delivery_text']}}</pre></div><form method='post' action='/store/order/confirm'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>تأكيد الاستلام وإطلاق مستحقات البائع</button></form>{% endif %}
    {% if x['status'] in ('ESCROWED','DELIVERED') and (x['buyer']==me or x['seller']==me) %}<form method='post' action='/store/order/dispute'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><input name='reason' maxlength='500' required placeholder='سبب النزاع'><button>فتح نزاع</button></form>{% endif %}</article>{% else %}<p>لا توجد صفقات.</p>{% endfor %}
    <h2>طلبات شحن الألعاب</h2>{% for x in top %}<article class='panel'><p>{{x['reference']}} · {{x['game']}} {{x['title']}} · {{x['status']}} · {{x['amount_sdg']}} SDG · اللاعب {{x['player_id']}}</p>
    {% if x['buyer']==me and x['status']=='PENDING_PAYMENT' %}<form method='post' action='/store/topups/pay'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>الدفع من محفظة SDG</button></form>{% endif %}
    {% if agent and agent['status']=='APPROVED' and x['status']=='PAID' and not x['agent'] %}<form method='post' action='/store/agent/topup/claim'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>استلام الطلب كوكيل</button></form>{% endif %}
    {% if x['agent']==me and x['status']=='ASSIGNED' %}<form method='post' action='/store/agent/topup/complete'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><input name='note' placeholder='مرجع تنفيذ المورد' required><button>تأكيد التنفيذ</button></form>{% endif %}
    </article>{% else %}<p>لا توجد طلبات.</p>{% endfor %}
    {% if agent and agent['status']=='APPROVED' %}<h2>طلبات متاحة للوكلاء</h2>{% for x in open_jobs %}<article class='panel'><p>{{x['reference']}} · {{x['game']}} {{x['title']}} · {{x['amount_sdg']}} SDG · اللاعب {{x['player_id']}}</p>
    <form method='post' action='/store/agent/topup/claim'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>استلام الطلب</button></form></article>{% else %}<p>لا توجد طلبات متاحة.</p>{% endfor %}{% endif %}</section>""",markets=markets,top=top,agent=agent,open_jobs=open_jobs,me=u["username"],csrf=session.get("csrf",""))
    return page("طلباتي",body)

@empire_bp.post("/order/deliver")
@login_required
def deliver_order():
    csrf(); u=user(); r=request.form.get("reference","")
    encrypted=encryption().encrypt(request.form.get("delivery","").encode()).decode()
    changed=db().execute("UPDATE es_market_orders SET delivery_cipher=?,status='DELIVERED',updated_at=? WHERE reference=? AND seller=? AND status='ESCROWED'",
                         (encrypted,now(),r,u["username"])).rowcount
    if changed!=1: abort(409,"الصفقة لم تعد قابلة للتسليم")
    audit(u["username"],"MARKET_DELIVERY_SUBMITTED","market_order",r)
    flash("تم إرسال بيانات التسليم بشكل مشفر للمشتري.")
    return redirect(url_for("empire.orders"))

@empire_bp.post("/order/confirm")
@login_required
def confirm_order():
    csrf(); u=user(); r=request.form.get("reference",""); d=db()
    d.execute("BEGIN IMMEDIATE")
    try:
        row=d.execute("SELECT * FROM es_market_orders WHERE reference=? AND buyer=? AND status='DELIVERED'",(r,u["username"])).fetchone()
        if not row: raise ValueError("الصفقة لم تعد بانتظار تأكيد الاستلام")
        fee=(row["price_sdg"]*int(setting("market_fee_bps","500")))//10000
        seller_amount=row["price_sdg"]-fee
        post(r+"R",[("escrow:market","SDG",-row["price_sdg"]),("user:"+row["seller"],"SDG",seller_amount),("platform:revenue","SDG",fee)],"تحرير صفقة بعد تأكيد المشتري")
        d.execute("UPDATE es_market_orders SET status='RELEASED',updated_at=? WHERE reference=?",(now(),r))
        d.execute("UPDATE es_listings SET status='SOLD' WHERE id=?",(row["listing_id"],))
        audit(u["username"],"ESCROW_RELEASED","market_order",r,"seller credit="+str(seller_amount))
        d.commit()
    except Exception: d.rollback(); raise
    flash("تم تأكيد الاستلام وإطلاق مستحقات البائع بعد خصم العمولة.")
    return redirect(url_for("empire.orders"))

@empire_bp.post("/order/dispute")
@login_required
def dispute():
    csrf(); u=user(); r=request.form.get("reference",""); reason=request.form.get("reason","").strip()
    if len(reason)<5: abort(400)
    d=db(); d.execute("BEGIN IMMEDIATE")
    try:
        row=d.execute("SELECT * FROM es_market_orders WHERE reference=? AND (buyer=? OR seller=?) AND status IN ('ESCROWED','DELIVERED')",
                      (r,u["username"],u["username"])).fetchone()
        if not row: raise ValueError("الصفقة لا تقبل فتح نزاع")
        d.execute("UPDATE es_market_orders SET status='DISPUTED',updated_at=? WHERE reference=?",(now(),r))
        d.execute("INSERT INTO es_disputes(order_ref,opened_by,reason,created_at) VALUES(?,?,?,?)",(r,u["username"],reason,now()))
        audit(u["username"],"DISPUTE_OPENED","market_order",r,reason)
        d.commit()
    except Exception: d.rollback(); raise
    flash("تم فتح النزاع وتجميد تحرير المبلغ لحين مراجعة المالك.")
    return redirect(url_for("empire.orders"))

@empire_bp.route("/agents",methods=["GET","POST"])
@login_required
def agents():
    u=user(); current=db().execute("SELECT * FROM es_agents WHERE username=?",(u["username"],)).fetchone()
    if request.method=="POST":
        csrf()
        db().execute("INSERT OR IGNORE INTO es_agents(username,status,created_at) VALUES(?,'PENDING',?)",(u["username"],now()))
        audit(u["username"],"AGENT_APPLIED","agent",u["username"])
        flash("تم تقديم طلب الوكالة.")
        current=db().execute("SELECT * FROM es_agents WHERE username=?",(u["username"],)).fetchone()
    body=render_template_string("""<section class='panel'><h1>برنامج الوكلاء</h1><p>يشمل شحن الألعاب وسوق الحسابات، وتحدد الإدارة نطاق العمل والعمولات.</p>
    {% if current %}<p>الحالة: {{current['status']}} · العمولة {{current['commission_bps']/100}}%</p>{% else %}<form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><button>التقديم كوكيل</button></form>{% endif %}</section>""",current=current,csrf=session.get("csrf",""))
    return page("الوكلاء",body)

@empire_bp.route("/transfer-epc",methods=["GET","POST"])
@login_required
def transfer_epc():
    u=user()
    if request.method=="POST":
        csrf(); recipient=request.form.get("recipient","").strip(); qty=money_int(request.form.get("quantity"))
        target=db().execute("SELECT username,active FROM users WHERE username=?",(recipient,)).fetchone()
        if not target or not target["active"] or recipient==u["username"]: abort(400,"المستلم غير صالح")
        r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            post(r,[("user:"+u["username"],"EPC",-qty),("user:"+recipient,"EPC",qty)],"تحويل EPC بين المستخدمين")
            audit(u["username"],"EPC_TRANSFER","wallet",r,"to="+recipient); d.commit()
        except Exception: d.rollback(); raise
        flash("تم تحويل EPC.")
        return redirect(url_for("empire.wallet"))
    body=render_template_string("""<section class='panel'><h1>تحويل EPC</h1><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>
    <label>اسم المستخدم المستلم</label><input name='recipient' required><label>الكمية EPC</label><input name='quantity' type='number' min='1' required><button>تحويل</button></form></section>""",csrf=session.get("csrf",""))
    return page("تحويل EPC",body)

@empire_bp.get("/admin")
@login_required
@founder_required
def admin():
    stats={
      "listings":db().execute("SELECT COUNT(*) n FROM es_listings WHERE status='PUBLISHED'").fetchone()["n"],
      "escrow":db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='escrow:market'").fetchone()["n"],
      "revenue":db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='platform:revenue'").fetchone()["n"],
      "pending":db().execute("SELECT COUNT(*) n FROM es_payments WHERE status='PENDING'").fetchone()["n"],
      "agents":db().execute("SELECT COUNT(*) n FROM es_agents WHERE status='APPROVED'").fetchone()["n"],
      "topups":db().execute("SELECT COUNT(*) n FROM es_topup_orders WHERE status IN ('PAID','ASSIGNED')").fetchone()["n"]
    }
    body=render_template_string("""<section class='panel'><h1>مركز إدارة الإمبراطورية</h1><div class='grid'>
    <article class='panel'><small>الإعلانات النشطة</small><h2>{{stats['listings']}}</h2></article>
    <article class='panel'><small>أموال صفقات السوق المحتجزة (SDG)</small><h2>{{'{:,}'.format(stats['escrow'])}}</h2></article>
    <article class='panel'><small>صافي إيراد المتجر المسجل (SDG)</small><h2>{{'{:,}'.format(stats['revenue'])}}</h2></article>
    <article class='panel'><small>طلبات الدفع المعلقة</small><h2>{{stats['pending']}}</h2></article>
    <article class='panel'><small>الوكلاء المعتمدون</small><h2>{{stats['agents']}}</h2></article>
    <article class='panel'><small>طلبات الشحن قيد التنفيذ</small><h2>{{stats['topups']}}</h2></article></div>
    <div class='grid'><a class='btn' href='/store/admin/agents'>إدارة الوكلاء</a><a class='btn' href='/store/admin/products'>باقات الشحن والتكلفة</a>
    <a class='btn' href='/store/admin/payments'>طلبات الدفع</a><a class='btn' href='/store/admin/disputes'>النزاعات</a>
    <a class='btn' href='/store/admin/settings'>الإعدادات والأسعار</a><a class='btn' href='/store/admin/audit'>سجل التدقيق</a></div></section>""",stats=stats)
    return page("الإدارة",body)

@empire_bp.route("/admin/agents",methods=["GET","POST"])
@login_required
@founder_required
def admin_agents():
    u=user()
    if request.method=="POST":
        csrf(); name=request.form.get("username",""); action=request.form.get("action","")
        rate=request.form.get("commission","0")
        if action not in ("APPROVED","REJECTED","SUSPENDED") or not re.fullmatch(r"(?:0|[1-9][0-9]?|100)(?:\.[0-9]{1,2})?",rate): abort(400)
        bps=int(Decimal(rate)*100)
        db().execute("UPDATE es_agents SET status=?,commission_bps=?,reviewed_at=?,reviewed_by=? WHERE username=?",(action,bps if action=="APPROVED" else 0,now(),u["username"],name))
        audit(u["username"],"AGENT_"+action,"agent",name)
    rows=db().execute("SELECT * FROM es_agents ORDER BY created_at DESC").fetchall()
    body=render_template_string("""<section class='panel'><h1>إدارة الوكلاء</h1>{% for x in rows %}<article class='panel'><b>{{x['username']}}</b> · {{x['status']}} · {{x['commission_bps']/100}}%
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='username' value='{{x['username']}}'>
    <input name='commission' type='number' min='0' max='100' step='.01' value='{{x['commission_bps']/100}}'>
    <button name='action' value='APPROVED'>اعتماد</button><button name='action' value='REJECTED'>رفض</button><button name='action' value='SUSPENDED'>إيقاف</button></form></article>{% else %}<p>لا توجد طلبات.</p>{% endfor %}</section>""",rows=rows,csrf=session.get("csrf",""))
    return page("إدارة الوكلاء",body)

@empire_bp.route("/admin/products",methods=["GET","POST"])
@login_required
@founder_required
def admin_products():
    if request.method=="POST":
        csrf(); game=request.form.get("game","").strip(); title=request.form.get("title","").strip()
        price=money_int(request.form.get("price_sdg")); cost=money_int(request.form.get("cost_sdg")); code=ref()
        if not game or not title or len(title)>120 or cost>price: abort(400)
        db().execute("INSERT INTO es_products(code,game,title,price_sdg,cost_sdg,created_at) VALUES(?,?,?,?,?,?)",(code,game,title,price,cost,now()))
        audit(user()["username"],"PRODUCT_CREATED","product",code)
    rows=db().execute("SELECT * FROM es_products ORDER BY id DESC").fetchall()
    body=render_template_string("""<section class='panel'><h1>باقات شحن الألعاب</h1><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>
    <input name='game' placeholder='اللعبة' required><input name='title' placeholder='اسم الباقة' required><input name='price_sdg' type='number' min='1' placeholder='سعر البيع SDG' required><input name='cost_sdg' type='number' min='0' placeholder='تكلفة المورد SDG' required><button>إضافة باقة</button></form>
    {% for x in rows %}<p>{{x['game']}} · {{x['title']}} · البيع {{x['price_sdg']}} SDG · التكلفة {{x['cost_sdg']}} SDG</p>{% endfor %}</section>""",rows=rows,csrf=session.get("csrf",""))
    return page("باقات الشحن",body)

@empire_bp.route("/admin/payments",methods=["GET","POST"])
@login_required
@founder_required
def admin_payments():
    u=user()
    if request.method=="POST":
        csrf(); pid=request.form.get("id",""); action=request.form.get("action","")
        row=db().execute("SELECT * FROM es_payments WHERE id=? AND status='PENDING'",(pid,)).fetchone()
        if not row: abort(404)
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            if action=="CONFIRMED":
                if row["currency"]=="USDT" and not row["operation_id"]:
                    raise ValueError("لا يمكن اعتماد تحويل Binance دون رقم عملية")
                if row["purpose"]=="WALLET_DEPOSIT":
                    post(row["reference"],[("user:"+row["username"],"SDG",row["amount"]),("external:verified_deposits","SDG",-row["amount"])],"إيداع SDG مؤكد يدوياً")
                elif row["purpose"]=="EPC_PURCHASE":
                    epc=int(row["quantity_epc"])
                    if epc<=0 or row["currency"]!="USDT": raise ValueError("بيانات طلب EPC غير صالحة")
                    post(row["reference"],[("asset:binance_usdt","USDT",row["amount"]),("external:usdt_receipts","USDT",-row["amount"]),
                                          ("user:"+row["username"],"EPC",epc),("issuer:epc","EPC",-epc)],
                         "تأكيد استلام USDT وإصدار EPC")
                d.execute("UPDATE es_payments SET status='CONFIRMED',reviewer=?,reviewed_at=? WHERE id=?",(u["username"],now(),pid))
            elif action=="REJECTED":
                d.execute("UPDATE es_payments SET status='REJECTED',reviewer=?,reviewed_at=? WHERE id=?",(u["username"],now(),pid))
            else: abort(400)
            audit(u["username"],"PAYMENT_"+action,"payment",row["reference"])
            d.commit()
        except Exception: d.rollback(); raise
    rows=db().execute("SELECT * FROM es_payments ORDER BY id DESC LIMIT 200").fetchall()
    body=render_template_string("""<section class='panel'><h1>طلبات الدفع</h1>{% for x in rows %}<article class='panel'>#{{x['id']}} · {{x['reference']}} · {{x['username']}} · {{(x['amount']/1000000) if x['currency']=='USDT' else x['amount']}} {{x['currency']}} · {{x['purpose']}} · {{x['status']}}<p>{{x['proof_note']}} · العملية: {{x['operation_id']}}</p>
    {% if x['status']=='PENDING' %}<form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='id' value='{{x['id']}}'><button name='action' value='CONFIRMED'>تأكيد بعد التحقق</button><button name='action' value='REJECTED'>رفض</button></form>{% endif %}</article>{% else %}<p>لا توجد طلبات.</p>{% endfor %}</section>""",rows=rows,csrf=session.get("csrf",""))
    return page("طلبات الدفع",body)

@empire_bp.route("/buy-epc",methods=["GET","POST"])
@login_required
def buy_epc():
    u=user()
    if request.method=="POST":
        csrf(); qty=money_int(request.form.get("quantity"))
        method=request.form.get("method","")
        if setting("epc_sales_enabled","0")!="1": abort(403,"شراء EPC متوقف من الإدارة حالياً")
        if method=="SDG_WALLET":
            rate=int(setting("epc_price_sdg","1000")); cost=qty*rate
            r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
            try:
                post(r,[("user:"+u["username"],"SDG",-cost),("platform:epc_sales","SDG",cost),("user:"+u["username"],"EPC",qty),("issuer:epc","EPC",-qty)],"شراء EPC من محفظة SDG")
                audit(u["username"],"EPC_PURCHASE","wallet",r); d.commit()
            except Exception: d.rollback(); raise
            flash("تم شراء EPC وإضافتها إلى محفظتك.")
        elif method=="BINANCE_USDT":
            bid=setting("binance_id","")
            if not bid: abort(503,"لم يضبط المالك Binance ID بعد")
            micros=int(setting("epc_price_usdt_micros","1000000"))
            amount=qty*micros
            r=ref()
            db().execute("INSERT INTO es_payments(reference,username,method,currency,amount,quantity_epc,purpose,proof_note,created_at) VALUES(?,?,'BINANCE_USDT','USDT',?,?,'EPC_PURCHASE',?,?)",
                         (r,u["username"],amount,qty,"Binance ID: "+bid,now()))
            flash("تم إنشاء طلب شراء. حوّل USDT إلى Binance ID المعروض، ثم أرسل رقم التحويل في نموذج ربط إثبات الدفع. لن تصدر EPC قبل التحقق اليدوي.")
            session["last_epc_payment_ref"]=r
        else: abort(400)
        return redirect(url_for("empire.wallet"))
    body=render_template_string("""<section class='panel'><h1>شراء Empire Coin (EPC)</h1><p>السعر الحالي: {{price}} SDG للوحدة. البيع {{enabled}}.</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>الكمية EPC</label><input name='quantity' type='number' min='1' required>
    <label>طريقة الدفع</label><select name='method'><option value='SDG_WALLET'>محفظة SDG</option><option value='BINANCE_USDT'>USDT عبر Binance ID</option></select><button>إنشاء الشراء</button></form>
    <p class='muted'>لا يمكن سحب EPC أو تداولها خارج المتجر حالياً. مدفوعات Binance تخضع للتحقق اليدوي.</p></section>""",
      price=setting("epc_price_sdg","1000"),enabled="مفعل" if setting("epc_sales_enabled")=="1" else "متوقف",csrf=session.get("csrf",""))
    return page("شراء EPC",body)

@empire_bp.route("/admin/settings",methods=["GET","POST"])
@login_required
@founder_required
def admin_settings():
    if request.method=="POST":
        csrf()
        keys=("epc_price_sdg","epc_price_usdt_micros","binance_id","market_fee_bps","epc_sales_enabled","topups_enabled")
        for k in keys:
            v=request.form.get(k,"").strip()
            if k in ("epc_price_sdg","epc_price_usdt_micros","market_fee_bps") and not re.fullmatch(r"[0-9]{1,12}",v): abort(400)
            if k in ("epc_price_sdg","epc_price_usdt_micros") and int(v)<1: abort(400)
            if k=="market_fee_bps" and int(v)>10000: abort(400)
            if k in ("epc_sales_enabled","topups_enabled") and v not in ("0","1"): abort(400)
            db().execute("INSERT INTO es_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,v))
        audit(user()["username"],"SETTINGS_UPDATED","settings","global")
        flash("تم حفظ الإعدادات.")
    body=render_template_string("""<section class='panel'><h1>إعدادات المنصة</h1><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>
    {% for key,label in fields %}<label>{{label}}</label><input name='{{key}}' value='{{values[key]}}'>{% endfor %}<button>حفظ الإعدادات</button></form></section>""",
      fields=[("epc_price_sdg","سعر EPC بالجنيه SDG"),("epc_price_usdt_micros","سعر EPC بالـ USDT بوحدة مليونية"),("binance_id","Binance ID للاستقبال"),("market_fee_bps","عمولة السوق بالنقاط الأساسية"),("epc_sales_enabled","تفعيل بيع EPC: 1 أو 0"),("topups_enabled","تفعيل طلبات الشحن: 1 أو 0")],
      values={k:setting(k) for k in ("epc_price_sdg","epc_price_usdt_micros","binance_id","market_fee_bps","epc_sales_enabled","topups_enabled")},csrf=session.get("csrf",""))
    return page("الإعدادات",body)

@empire_bp.route("/admin/disputes",methods=["GET","POST"])
@login_required
@founder_required
def admin_disputes():
    u=user()
    if request.method=="POST":
        csrf(); r=request.form.get("reference",""); action=request.form.get("action","")
        order=db().execute("SELECT * FROM es_market_orders WHERE reference=? AND status='DISPUTED'",(r,)).fetchone()
        if not order or action not in ("BUYER","SELLER"): abort(400)
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            if action=="BUYER":
                post(r+"B",[("escrow:market","SDG",-order["price_sdg"]),("user:"+order["buyer"],"SDG",order["price_sdg"])],"استرداد صفقة بعد قرار النزاع")
                d.execute("UPDATE es_market_orders SET status='REFUNDED',updated_at=? WHERE reference=?",(now(),r))
                d.execute("UPDATE es_listings SET status='PAUSED' WHERE id=?",(order["listing_id"],))
                d.execute("UPDATE es_disputes SET status='RESOLVED_BUYER',resolution='refund',resolved_at=? WHERE order_ref=?",(now(),r))
            else:
                fee=(order["price_sdg"]*int(setting("market_fee_bps","500")))//10000
                post(r+"S",[("escrow:market","SDG",-order["price_sdg"]),("user:"+order["seller"],"SDG",order["price_sdg"]-fee),("platform:revenue","SDG",fee)],"تحرير مستحقات البائع بعد قرار النزاع")
                d.execute("UPDATE es_market_orders SET status='RELEASED',updated_at=? WHERE reference=?",(now(),r))
                d.execute("UPDATE es_disputes SET status='RESOLVED_SELLER',resolution='release',resolved_at=? WHERE order_ref=?",(now(),r))
            audit(u["username"],"DISPUTE_RESOLVED_"+action,"market_order",r)
            d.commit()
        except Exception: d.rollback(); raise
    rows=db().execute("SELECT * FROM es_disputes WHERE status='OPEN' ORDER BY id").fetchall()
    body=render_template_string("""<section class='panel'><h1>النزاعات</h1>{% for x in rows %}<article class='panel'>{{x['order_ref']}} · {{x['opened_by']}}<p>{{x['reason']}}</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['order_ref']}}'>
    <button name='action' value='BUYER'>استرداد المبلغ للمشتري</button><button name='action' value='SELLER'>تحرير مستحقات البائع</button></form></article>{% else %}<p>لا توجد نزاعات مفتوحة.</p>{% endfor %}</section>""",rows=rows,csrf=session.get("csrf",""))
    return page("النزاعات",body)

@empire_bp.get("/admin/audit")
@login_required
@founder_required
def admin_audit():
    rows=db().execute("SELECT * FROM es_audit ORDER BY id DESC LIMIT 300").fetchall()
    body=render_template_string("""<section class='panel'><h1>سجل التدقيق</h1>{% for x in rows %}<p>{{x['created_at']}} · {{x['actor']}} · {{x['action']}} · {{x['object_type']}} {{x['object_ref']}} · {{x['details']}}</p>{% else %}<p>السجل فارغ.</p>{% endfor %}</section>""",rows=rows)
    return page("سجل التدقيق",body)

def register_empire_store(app):
    app.register_blueprint(empire_bp)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
