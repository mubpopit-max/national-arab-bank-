"""Empire E-Story commerce module.

Uses a separate double-entry store ledger in the existing SQLite database.
No legacy bank balance is imported or treated as store money. External payment
verification is manual until a supported Binance/payment API is configured.
"""
import os, re, json, sqlite3, secrets, io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template_string, request, session, url_for, send_from_directory, jsonify
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

    CREATE TABLE IF NOT EXISTS es_transfer_details(
      reference TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
      recipient_account TEXT NOT NULL, currency TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_identity_status(
      username TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK(status IN ('UNVERIFIED','PENDING','VERIFIED','REJECTED')), updated_at TEXT NOT NULL);
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
      username TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency IN ('SDG','URC')),
      balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0),
      PRIMARY KEY(username,currency));
    CREATE TABLE IF NOT EXISTS es_ledger(
      id INTEGER PRIMARY KEY, reference TEXT NOT NULL, line_no INTEGER NOT NULL,
      account TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency IN ('SDG','URC')),
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
    CREATE TABLE IF NOT EXISTS es_withdrawals(
      id INTEGER PRIMARY KEY, reference TEXT UNIQUE NOT NULL, username TEXT NOT NULL,
      amount_sdg INTEGER NOT NULL CHECK(amount_sdg>0), payout_method TEXT NOT NULL,
      payout_details_cipher TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','PAID','REJECTED')),
      reviewer TEXT, created_at TEXT NOT NULL, reviewed_at TEXT);
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
    if "listing_type" not in listing_cols:
        db().execute("ALTER TABLE es_listings ADD COLUMN listing_type TEXT NOT NULL DEFAULT 'ACCOUNT'")
    if "category" not in listing_cols:
        db().execute("ALTER TABLE es_listings ADD COLUMN category TEXT NOT NULL DEFAULT 'حسابات ألعاب'")
    if "image_path" not in listing_cols:
        db().execute("ALTER TABLE es_listings ADD COLUMN image_path TEXT NOT NULL DEFAULT ''")
    product_cols={r["name"] for r in db().execute("PRAGMA table_info(es_products)").fetchall()}
    if "cost_sdg" not in product_cols:
        db().execute("ALTER TABLE es_products ADD COLUMN cost_sdg INTEGER NOT NULL DEFAULT 0")
    defaults = {
      "epc_price_sdg":"0", "epc_price_usdt_micros":"0", "epc_max_supply":"15000000",
      "fx_rate_mode":"manual", "fx_market_rate":"0", "fx_manual_rate":"0", "fx_buy_spread_bps":"0", "fx_sell_spread_bps":"0",
      "binance_id":os.getenv("EMPIRE_BINANCE_ID",""),
      "store_name":"الإمبراطورية إستوري", "market_fee_bps":"0",
      "topups_enabled":"0", "epc_sales_enabled":"0"
    }
    conn=db()
    # Safe schema migration: old URC ledger rows become URC; account numbers remain untouched.
    for table in ("es_wallets","es_ledger"):
        info=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
        if info and ("'URC'" not in (info["sql"] or "") or conn.execute(f"SELECT 1 FROM {table} WHERE currency='EPC' LIMIT 1").fetchone()):
            if table=="es_wallets":
                conn.execute("ALTER TABLE es_wallets RENAME TO es_wallets_legacy")
                conn.execute("""CREATE TABLE es_wallets(username TEXT NOT NULL,currency TEXT NOT NULL CHECK(currency IN ('SDG','URC')),balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0),PRIMARY KEY(username,currency))""")
                conn.execute("INSERT INTO es_wallets SELECT username,CASE currency WHEN 'EPC' THEN 'URC' WHEN 'URC' THEN 'URC' ELSE currency END,balance FROM es_wallets_legacy")
                conn.execute("DROP TABLE es_wallets_legacy")
            else:
                conn.execute("ALTER TABLE es_ledger RENAME TO es_ledger_legacy")
                conn.execute("""CREATE TABLE es_ledger(id INTEGER PRIMARY KEY,reference TEXT NOT NULL,line_no INTEGER NOT NULL,account TEXT NOT NULL,currency TEXT NOT NULL CHECK(currency IN ('SDG','URC')),delta INTEGER NOT NULL,memo TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(reference,line_no))""")
                conn.execute("INSERT INTO es_ledger SELECT id,reference,line_no,account,CASE currency WHEN 'EPC' THEN 'URC' WHEN 'URC' THEN 'URC' ELSE currency END,delta,memo,created_at FROM es_ledger_legacy")
                conn.execute("DROP TABLE es_ledger_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS es_ledger_account ON es_ledger(account,currency,id)")
    for k,v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO es_settings(key,value) VALUES(?,?)",(k,v))
    seed_owner_assets()

def user():
    username=session.get("username")
    if not username: return None
    return db().execute("SELECT username,full_name,account_number,role,active FROM users WHERE username=?",(username,)).fetchone()

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
    if currency not in ("SDG","URC"): raise ValueError("عملة غير مدعومة")
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

def seed_owner_assets():
    """Create the founder's opening URC allocation once; never alter bank account IDs."""
    tables={r["name"] for r in db().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "users" not in tables:
        return False
    founder=db().execute("SELECT username FROM users WHERE role='founder' ORDER BY id LIMIT 1").fetchone()
    if not founder:
        return False
    owner=founder["username"]
    ensure_wallet(owner,"SDG")
    ensure_wallet(owner,"URC")
    if setting("owner_urc_seeded","0")!="1":
        if balance(owner,"URC")==0:
            d=db()
            d.execute("BEGIN IMMEDIATE")
            try:
                post("OWNER-URC-OPENING",[("user:"+owner,"URC",15000000),("issuer:urc","URC",-15000000)],"الرصيد الافتتاحي لأصول المالك")
                d.commit()
            except Exception:
                d.rollback()
                raise
        db().execute("INSERT INTO es_settings(key,value) VALUES('owner_urc_seeded','1') ON CONFLICT(key) DO UPDATE SET value='1'")
    return True

def assert_epc_capacity(quantity):
    maximum=int(setting("epc_max_supply","15000000"))
    if maximum<=0: raise ValueError("يجب على المالك تحديد الحد الأقصى لإصدار URC قبل البيع")
    issued=-db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='issuer:urc' AND currency='URC'").fetchone()["n"]
    reserved=db().execute("SELECT COALESCE(SUM(quantity_epc),0) n FROM es_payments WHERE purpose='EPC_PURCHASE' AND status='PENDING'").fetchone()["n"]
    if issued+reserved+quantity>maximum: raise ValueError("الكمية تتجاوز المعروض المتاح من URC")

def encryption():
    key=os.getenv("EMPIRE_DATA_ENCRYPTION_KEY","")
    if not key: raise RuntimeError("تسليم بيانات الحسابات متوقف حتى ضبط EMPIRE_DATA_ENCRYPTION_KEY")
    return Fernet(key.encode())

def page(title,body,**ctx):
    shell="""<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>
    <meta name='theme-color' content='#101326'><link rel='manifest' href='/store/manifest.webmanifest'><link rel='apple-touch-icon' href='/store/icon.svg'>
    <meta name='description' content='الإمبراطورية إستوري — وجهتك لشحن الألعاب والتجارة الرقمية.'>
    <title>{{title}} | الإمبراطورية إستوري</title>
    
    <style>
    *{-webkit-tap-highlight-color:transparent}html{scroll-behavior:auto}body{background:#f3f5f9!important}header:after{display:none!important}.panel{box-shadow:0 2px 8px rgba(22,35,58,.035)!important}.app-bottom{backdrop-filter:none!important;box-shadow:0 -2px 8px rgba(20,30,55,.06)!important}.service-card,.btn,button{transition:none!important}.service-card:hover,.btn:hover,button:hover{transform:none!important}
    
    :root{color-scheme:dark;--bg:#0c1020;--surface:#151b30;--surface2:#1b2340;--line:rgba(164,180,255,.13);--text:#f6f7ff;--muted:#aab2cc;--purple:#8b70ff;--cyan:#48d9e8;--pink:#f17acb;--green:#52d6a2;--radius:22px}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 90% -10%,rgba(116,83,255,.22),transparent 38%),radial-gradient(ellipse at 0% 35%,rgba(38,190,211,.10),transparent 32%),var(--bg);color:var(--text);font-family:system-ui,-apple-system,'Segoe UI',Tahoma,Arial,sans-serif;font-size:15px;line-height:1.75}
    a{color:#b9adff;text-decoration:none} .metric{padding:20px;border-radius:20px;background:linear-gradient(135deg,rgba(139,112,255,.18),rgba(72,217,232,.07));border:1px solid var(--line)} .metric strong{display:block;font-size:clamp(24px,4vw,34px);font-weight:900;direction:ltr;text-align:right} .admin-links a{min-height:82px;display:flex;align-items:center;justify-content:center;text-align:center;padding:18px;border-radius:17px;background:linear-gradient(135deg,rgba(139,112,255,.2),rgba(72,217,232,.08));border:1px solid var(--line);font-weight:800} .table-wrap{overflow-x:auto}a:hover{color:#fff}header{position:relative;overflow:hidden;padding:22px max(18px,calc((100% - 1120px)/2));background:linear-gradient(110deg,#171d38,#22234c 58%,#182d45);border-bottom:1px solid var(--line)}
    header:after{content:'';position:absolute;width:260px;height:260px;border-radius:50%;background:rgba(116,91,255,.18);filter:blur(55px);left:5%;top:-170px;pointer-events:none}
    .brand{position:relative;z-index:1;font-size:25px;font-weight:900;letter-spacing:-.5px;color:#fff}.brand:before{content:'✦';display:inline-grid;place-items:center;width:38px;height:38px;margin-left:10px;border-radius:13px;background:linear-gradient(135deg,var(--purple),var(--cyan));color:#101326}
    header small{position:relative;z-index:1;display:block;margin-right:49px;color:#c4cbea;font-size:12px;letter-spacing:1.2px}
    main{max-width:1120px;margin:24px auto 50px;padding:0 18px}.nav{position:sticky;top:8px;z-index:10;display:flex;gap:7px;flex-wrap:wrap;padding:9px;margin:0 0 22px;background:rgba(18,24,44,.88);backdrop-filter:blur(18px);border:1px solid var(--line);border-radius:18px;box-shadow:0 12px 35px rgba(0,0,0,.18)}
    .nav a{padding:8px 13px;border-radius:12px;color:#cbd2ec;font-weight:700;font-size:13px;transition:.2s}.nav a:hover{background:rgba(139,112,255,.18);color:#fff}
    .panel{background:linear-gradient(145deg,rgba(27,35,64,.96),rgba(19,25,46,.96));border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin:14px 0;box-shadow:0 18px 45px rgba(0,0,0,.16)}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr));gap:15px}.grid>.panel,.grid article{margin:0}
    h1,h2,h3{color:#fff;line-height:1.35;margin-top:0}h1{font-size:clamp(25px,4vw,38px);font-weight:900}h2{font-size:22px}h3{font-size:18px}p{color:#d0d6eb}.muted,small{color:var(--muted)}
    .btn,button{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:45px;padding:10px 18px;border:0;border-radius:13px;background:linear-gradient(110deg,var(--purple),#6c8dff);color:#fff;font:700 14px system-ui,sans-serif;text-decoration:none;cursor:pointer;box-shadow:0 8px 22px rgba(112,101,255,.22);transition:transform .18s,filter .18s}
    .btn:hover,button:hover{color:#fff;filter:brightness(1.1);transform:translateY(-2px)}.btn.secondary{background:rgba(255,255,255,.07);border:1px solid var(--line);box-shadow:none}
    input,textarea,select{display:block;width:100%;padding:13px 14px;margin:7px 0 15px;background:#0e1428;border:1px solid rgba(164,180,255,.2);border-radius:13px;color:#fff;font:inherit;outline:none}
    input:focus,textarea:focus,select:focus{border-color:var(--purple);box-shadow:0 0 0 3px rgba(139,112,255,.14)}label{display:block;color:#dce2fa;font-weight:700;font-size:13px}
    .tag{display:inline-flex;border:1px solid rgba(72,217,232,.25);border-radius:30px;padding:4px 11px;color:var(--cyan);background:rgba(72,217,232,.07);font-size:12px;font-weight:700}
    .hero{position:relative;overflow:hidden;padding:clamp(24px,5vw,48px);border-radius:28px;background:linear-gradient(115deg,#29245d 0%,#242d5d 48%,#125064 100%);border:1px solid rgba(181,190,255,.2);box-shadow:0 22px 55px rgba(28,27,86,.35)}
    .hero:after{content:'✦';position:absolute;left:5%;top:5%;font-size:150px;line-height:1;color:rgba(255,255,255,.06);transform:rotate(-15deg)}.hero p{max-width:580px;color:#e0e5ff;font-size:16px}
    .service-card{display:flex;flex-direction:column;gap:8px;min-height:170px;padding:21px;border:1px solid var(--line);border-radius:20px;background:linear-gradient(145deg,rgba(30,39,71,.95),rgba(19,27,49,.95));transition:.2s}
    .service-card:hover{transform:translateY(-4px);border-color:rgba(139,112,255,.55)}.service-icon{display:grid;place-items:center;width:48px;height:48px;border-radius:15px;background:linear-gradient(135deg,rgba(139,112,255,.25),rgba(72,217,232,.18));font-size:23px}
    .service-card strong{font-size:17px;color:#fff}.service-card span{font-size:13px;color:var(--muted)}
    .empty-state{padding:24px;text-align:center;border:1px dashed rgba(164,180,255,.24);border-radius:18px;color:var(--muted)}
    .receipt-card{max-width:720px;margin:28px auto;border-color:rgba(82,214,162,.3);background:radial-gradient(circle at 50% 0,rgba(82,214,162,.13),transparent 38%),linear-gradient(145deg,#1a2940,#141b30)}
    .receipt-head{text-align:center;padding:12px 0 18px}.receipt-mark{display:grid;place-items:center;width:62px;height:62px;margin:0 auto 12px;border-radius:50%;background:rgba(82,214,162,.14);border:1px solid rgba(82,214,162,.5);color:#75edbb;font-size:34px;font-weight:900}
    .receipt-head h1{margin:12px 0 4px}.receipt-amount{text-align:center;padding:20px;border:1px solid var(--line);border-radius:18px;background:rgba(8,13,27,.36)}
    .receipt-amount strong{display:block;margin-top:5px;color:#fff;font-size:clamp(28px,6vw,42px);font-weight:900;direction:ltr}
    .receipt-details{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:18px 0}
    .receipt-details>div{display:flex;flex-direction:column;gap:4px;padding:13px;border-radius:13px;background:rgba(255,255,255,.045);border:1px solid var(--line);min-width:0}
    .receipt-details b{overflow-wrap:anywhere;color:#f6f7ff}.receipt-success{color:#75edbb!important}.receipt-note{text-align:center;font-size:12px}
    .receipt-actions{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;margin-top:20px}
    @media(max-width:520px){.receipt-details{grid-template-columns:1fr}.receipt-card{padding:16px}}
    @media print{body{background:#fff!important;color:#111!important}.receipt-card,.receipt-amount,.receipt-details>div{background:#fff!important;color:#111!important;box-shadow:none!important;border-color:#bbb!important}.receipt-card *{color:#111!important}.receipt-actions,header,.nav,footer{display:none!important}main{margin:0!important;padding:0!important}.receipt-success{color:#087a45!important}}
    form{max-width:100%}button{font-family:inherit}.flash{padding:12px 15px;border:1px solid rgba(82,214,162,.25);border-radius:13px;background:rgba(82,214,162,.08);color:#baf4dc}
    footer{margin-top:28px;padding:20px 4px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
    .hero{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(220px,.85fr);align-items:center;gap:18px}.hero-copy{position:relative;z-index:2}.hero-character{position:relative;min-height:280px;display:grid;place-items:center;isolation:isolate}.hero-character:before{content:'';position:absolute;width:230px;height:230px;border-radius:50%;background:radial-gradient(circle,rgba(255,35,83,.42),rgba(125,53,255,.18) 55%,transparent 72%);filter:blur(8px);z-index:-1}.hero-character svg{width:min(100%,330px);height:auto;filter:drop-shadow(0 12px 28px rgba(255,22,75,.24))}.hero-kicker{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid rgba(255,86,117,.35);border-radius:99px;background:rgba(125,22,64,.2);color:#ffb2c1;font-size:12px;font-weight:800}.hero h1 span{color:#ff7899;text-shadow:0 0 25px rgba(255,40,95,.22)}.hero p{position:relative;z-index:2}.hero .grid{position:relative;z-index:2}@media(max-width:640px){.hero{grid-template-columns:1fr}.hero-character{grid-row:1;min-height:190px}.hero-character svg{width:220px}.hero-copy{text-align:center}.hero .grid{margin-right:auto;margin-left:auto}}\n    @media(max-width:640px){header{padding:18px}.brand{font-size:21px}main{margin-top:14px;padding:0 12px}.nav{position:static;gap:4px}.nav a{padding:7px 9px;font-size:12px}.panel{padding:17px;border-radius:18px}.hero{border-radius:22px}.btn,button{width:100%}}
    
    /* Native-wallet app shell inspired by Bravo's information hierarchy */
    body{padding-bottom:calc(82px + env(safe-area-inset-bottom));background:#f3f5f9;color:#182033;line-height:1.55}
    header{padding:15px 18px;background:#fff;color:#172033;border-bottom:1px solid #e7eaf0;box-shadow:0 2px 12px rgba(20,30,55,.04)}
    .brand{font-size:19px;color:#172033}.brand:before{background:linear-gradient(135deg,#185a9d,#2479c8);color:#fff}
    header small{color:#7b8496}
    main{max-width:760px;margin:14px auto 28px;padding:0 14px}
    .nav{display:none}
    .wallet-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 14px;padding:18px 19px;border-radius:22px;background:linear-gradient(125deg,#07518d,#087db5 65%,#13a3b0);color:#fff;box-shadow:0 12px 28px rgba(7,81,141,.18)}
    .wallet-top .eyebrow{font-size:12px;color:#d8efff}.wallet-top .amount{font-size:27px;font-weight:900;direction:ltr;text-align:right;line-height:1.2}.wallet-top .currency{font-size:12px;color:#d8efff}
    .wallet-top .urc-line{margin-top:5px;font-size:12px;color:#d8efff}.wallet-top .wallet-link{padding:9px 13px;border:1px solid rgba(255,255,255,.35);border-radius:12px;color:#fff;font-size:12px;font-weight:800;white-space:nowrap}
    .app-bottom{position:fixed;z-index:100;bottom:0;left:0;right:0;display:grid;grid-template-columns:repeat(5,1fr);gap:2px;padding:8px 8px calc(8px + env(safe-area-inset-bottom));background:rgba(255,255,255,.97);border-top:1px solid #e6eaf0;box-shadow:0 -5px 24px rgba(20,30,55,.08);backdrop-filter:blur(16px)}
    .app-bottom a{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;min-height:48px;color:#7b8496;font-size:10px;font-weight:800}
    .app-bottom a .ico{font-size:20px;line-height:1.25}.app-bottom a.active{color:#0865a4}
    .panel{background:#fff;color:#182033;border:1px solid #e9edf3;border-radius:18px;padding:17px;margin:12px 0;box-shadow:0 4px 16px rgba(22,35,58,.045)}
    .panel p{color:#647084}.muted,small{color:#7b8496}
    h1,h2,h3{color:#182033}h1{font-size:23px}h2{font-size:18px}h3{font-size:16px}
    .service-card{min-height:112px;padding:14px;background:#fff;border:1px solid #e9edf3;border-radius:17px;box-shadow:0 3px 12px rgba(22,35,58,.035);color:#182033}
    .service-card strong{color:#182033;font-size:14px}.service-card span{font-size:11px;color:#7b8496}
    .service-icon{width:42px;height:42px;border-radius:13px;background:#edf5fc;font-size:21px}
    .metric{background:#f4f8fc;border-color:#e5edf5}.metric strong{color:#155f99}
    .btn,button{background:#0874b5;box-shadow:0 5px 14px rgba(8,116,181,.14);border-radius:12px}
    .btn.secondary{background:#eef4f9;color:#175d8e;border-color:#e1ebf4}
    input,textarea,select{background:#f8fafc;color:#182033;border-color:#dce4ed}
    label{color:#3c485a}.tag{color:#0874b5;background:#edf6fc;border-color:#d9eaf7}
    .empty-state{color:#7b8496;border-color:#dfe6ee}
    .hero{display:none}
    footer{display:none}
    @media(min-width:760px){main{margin-top:22px}.app-bottom{left:50%;right:auto;transform:translateX(-50%);width:min(100%,760px);border:1px solid #e6eaf0;border-bottom:0;border-radius:18px 18px 0 0}}

    
    /* Sudan national wallet visual system */
    :root{color-scheme:light;--bg:#f3f7fb;--surface:#fff;--surface2:#edf5fb;--line:#dce8f1;--text:#172b3a;--muted:#687e8d;--blue:#0875b9;--green:#138a52;--red:#c83d43;--radius:20px}
    html,body{background:#f3f7fb!important;color:#172b3a!important;font-family:system-ui,-apple-system,'Segoe UI',Tahoma,Arial,sans-serif!important}
    body{padding-bottom:calc(84px + env(safe-area-inset-bottom));line-height:1.55}
    header{background:#fff!important;color:#16344a!important;border-bottom:1px solid #dce8f1!important;box-shadow:0 2px 10px rgba(20,60,85,.05)!important;padding:13px 18px!important}
    .brand,.brand:before{color:#fff!important}.brand{color:#153a55!important;font-size:19px!important}.brand:before{background:linear-gradient(135deg,#0875b9,#138a52)!important}
    header small{color:#6a8494!important}
    main{max-width:760px!important;margin:14px auto 24px!important;padding:0 14px!important}
    .nav{display:none!important}
    .panel{background:#fff!important;color:#172b3a!important;border:1px solid #e1eaf1!important;border-radius:19px!important;padding:17px!important;margin:12px 0!important;box-shadow:0 3px 12px rgba(20,60,85,.045)!important}
    h1,h2,h3{color:#173b55!important}p{color:#536b7c!important}.muted,small{color:#6c8190!important}
    a{color:#0875b9!important}
    .btn,button{background:#0875b9!important;color:#fff!important;border-radius:13px!important;box-shadow:none!important;transition:none!important}
    .btn.secondary{background:#edf5fa!important;color:#12618e!important;border:1px solid #d8e7f0!important}
    input,textarea,select{background:#f8fbfd!important;color:#172b3a!important;border:1px solid #d7e4ed!important;border-radius:12px!important}
    label{color:#36566c!important}
    .tag{background:#eaf6ef!important;color:#137344!important;border-color:#cde8d7!important}
    .hero{display:none!important}
    .service-card{display:flex;flex-direction:column;align-items:flex-start;justify-content:center;gap:6px;min-height:108px;padding:14px!important;background:#fff!important;border:1px solid #dfebf2!important;border-radius:17px!important;box-shadow:0 3px 10px rgba(22,65,90,.035)!important;color:#183b52!important}
    .service-card strong{font-size:13px;color:#183b52!important}.service-card span:last-child{font-size:11px;color:#718796!important}
    .service-icon{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:#eaf4fb!important;color:#0875b9;font-size:22px;font-style:normal}
    .app-bottom{position:fixed;z-index:100;bottom:0;left:0;right:0;display:grid;grid-template-columns:repeat(5,1fr);gap:2px;padding:7px 8px calc(7px + env(safe-area-inset-bottom));background:#fff!important;border-top:1px solid #dce8f1;box-shadow:0 -3px 14px rgba(20,60,85,.07)!important;backdrop-filter:none!important}
    .app-bottom a{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;min-height:48px;color:#6d8290!important;font-size:10px;font-weight:800}
    .app-bottom a .ico{font-size:20px;line-height:1.2}.app-bottom a.active{color:#0875b9!important}
    .wallet-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 14px;padding:19px;border-radius:21px;background:linear-gradient(125deg,#07558a,#087bb2 68%,#12865c)!important;color:#fff;box-shadow:0 8px 20px rgba(7,85,138,.14)}
    .wallet-top .eyebrow,.wallet-top .currency{font-size:12px;color:#e3f4ff!important}.wallet-top .amount{font-size:27px;font-weight:900;direction:ltr;text-align:right;line-height:1.2;color:#fff}.wallet-top .wallet-link{padding:9px 12px;border:1px solid rgba(255,255,255,.45);border-radius:11px;color:#fff!important;font-size:12px;font-weight:800;white-space:nowrap}
    .transfer-receipt{border-top:6px solid #16844f!important;background:linear-gradient(180deg,#fff 0%,#f8fbf9 100%)!important}
    .receipt-flag{display:flex;height:7px;border-radius:20px;overflow:hidden;margin:0 0 15px}.receipt-flag i{flex:1}.receipt-flag i:nth-child(1){background:#d52e36}.receipt-flag i:nth-child(2){background:#fff;border:1px solid #ddd}.receipt-flag i:nth-child(3){background:#111}.receipt-flag i:nth-child(4){background:#168447}
    footer{display:none!important}
    /* Approved Empire E-Story visual system */
    .app-header{height:82px;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 18px!important;background:linear-gradient(180deg,#fff,#f7fbff)!important}
    .header-brand{flex:1;text-align:center;min-width:0}.header-brand .brand{font-size:21px!important;font-weight:950;white-space:nowrap;color:#123d68!important}.header-brand .brand:before{display:none!important}.header-brand small{margin:0!important;font-size:11px!important;letter-spacing:.2px!important;text-align:center;color:#315a7b!important}
    .header-menu,.header-alert,.header-avatar{display:grid;place-items:center;width:43px;height:43px;border-radius:50%;background:#eaf3fb;color:#075b9b!important;font-size:22px;font-weight:900}.header-alert{background:#fff1f0;color:#c43d42!important}.header-avatar{background:#e7f5ee;color:#13834c!important}
    .wallet-top{position:relative;display:grid!important;grid-template-columns:58px 1fr 40px;align-items:center;gap:12px;min-height:166px;margin:12px 0 18px!important;padding:22px 17px!important;border:1px solid rgba(255,255,255,.35)!important;border-radius:27px!important;background:linear-gradient(118deg,#064a83 0%,#086bb0 58%,#087f78 100%)!important;color:#fff!important;box-shadow:0 14px 30px rgba(5,75,126,.22)!important;overflow:hidden}
    .wallet-top:after{content:'';position:absolute;width:190px;height:190px;left:-55px;top:-85px;border-radius:50%;border:1px solid rgba(255,255,255,.13);box-shadow:0 0 0 22px rgba(255,255,255,.035),0 0 0 44px rgba(255,255,255,.025);pointer-events:none}
    .wallet-card-icon{display:grid;place-items:center;width:58px;height:58px;border-radius:19px;background:rgba(255,255,255,.16);font-size:34px;color:#fff}.wallet-balance{min-width:0}
    .wallet-top .eyebrow{font-size:15px!important;font-weight:800;color:#e4f4ff!important;margin-bottom:7px}.wallet-top .amount{font-size:clamp(25px,7vw,38px)!important;font-weight:950!important;direction:ltr;text-align:right;line-height:1.12;color:#fff!important;white-space:nowrap}.wallet-top .amount span{font-size:16px;color:#ffcf37!important}.wallet-top .currency{font-size:11px;color:#d5edfa!important;margin-top:6px}
    .balance-toggle{width:40px!important;height:40px!important;min-height:40px!important;padding:0!important;border-radius:50%!important;background:rgba(255,255,255,.16)!important;color:#fff!important;font-size:21px!important;box-shadow:none!important}
    .wallet-secure{position:absolute;bottom:12px;left:17px;display:flex;align-items:center;gap:6px;color:#e4f8ec}.wallet-secure span{display:grid;place-items:center;width:19px;height:19px;border-radius:50%;background:#16a05b;color:white;font-weight:900}.wallet-secure small{color:#e4f8ec!important;font-size:10px}
    .home-services{margin:0 0 14px}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 2px 13px}.section-heading h2{margin:2px 0 0;font-size:18px!important}.section-kicker{font-size:10px;font-weight:900;color:#16834f;letter-spacing:.2px}.sudan-mark{font-size:24px}
    .service-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px}.service-grid .service-card{align-items:center!important;justify-content:flex-start!important;text-align:center;min-width:0;min-height:115px!important;padding:12px 5px!important;gap:8px!important;border-radius:19px!important;box-shadow:0 5px 15px rgba(23,64,92,.055)!important}.service-grid .service-card strong{font-size:11px!important;line-height:1.35!important;overflow-wrap:anywhere}.service-grid .service-icon{width:55px!important;height:55px!important;border-radius:18px!important;font-size:26px!important;font-style:normal!important}
    .service-icon.store{background:#e6f5fc!important;color:#0878b9}.service-icon.accounts{background:#e6f6ed!important;color:#16854e}.service-icon.transfer{background:#e8f0ff!important;color:#1765cf}.service-icon.coins{background:#fff3d9!important;color:#bd7b08}.service-icon.funding{background:#e7f6ef!important;color:#16854e}.service-icon.profile{background:#eaf1ff!important;color:#245eb5}.service-icon.games{background:#f0eaff!important;color:#7445bd}.service-icon.exchange{background:#e5f5f5!important;color:#087f80}.service-icon.withdraw{background:#fff0ec!important;color:#c25a38}.service-icon.agents{background:#edf3f8!important;color:#43647e}
    .national-banner{position:relative;display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:105px;margin:15px 0;padding:18px 20px;overflow:hidden;border-radius:23px;background:linear-gradient(110deg,#e8f5ff,#fff 48%,#e8f7ee);border:1px solid #dceaf3}.banner-copy{position:relative;z-index:2;display:flex;flex-direction:column;gap:5px}.banner-copy span{font-size:clamp(17px,4.5vw,23px);font-weight:950;color:#123f71}.banner-copy strong{font-size:12px;color:#39718b}.banner-flag{position:relative;width:60px;height:40px;display:flex;flex-direction:column;overflow:hidden;border-radius:5px;transform:rotate(-8deg);box-shadow:0 5px 13px rgba(20,45,65,.15)}.banner-flag i{flex:1}.banner-flag i:nth-child(1){background:#d92732}.banner-flag i:nth-child(2){background:#fff}.banner-flag i:nth-child(3){background:#111}.banner-flag b{position:absolute;left:0;top:0;width:0;height:0;border-top:20px solid transparent;border-bottom:20px solid transparent;border-left:27px solid #16864b}.banner-crown{position:absolute;left:28%;bottom:-20px;font-size:95px;color:rgba(8,103,161,.07);pointer-events:none}
    .recent-panel{margin-top:13px!important}.view-all{font-size:12px;font-weight:850;white-space:nowrap}.transaction-row{display:flex;align-items:center;gap:10px;padding:12px 0;border-bottom:1px solid #edf1f5}.transaction-row:last-child{border-bottom:0}.transaction-symbol{display:grid;place-items:center;flex:0 0 42px;height:42px;border-radius:50%;background:#e6f6ed;color:#13864e;font-size:23px;font-weight:900}.transaction-info{display:flex;flex:1;min-width:0;flex-direction:column;gap:3px}.transaction-info b{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#1b3e56}.transaction-info small{font-size:10px}.transaction-value{font-size:11px;white-space:nowrap}.transaction-value.in{color:#13864e}.transaction-value.out{color:#c04b4b}.founder-shortcut p{font-size:12px}
    .app-bottom{grid-template-columns:repeat(5,minmax(0,1fr))!important;min-height:72px}.app-bottom a{font-size:10px!important}.app-bottom a .ico{font-size:22px!important}
    @media(max-width:430px){.app-header{height:72px;padding:8px 12px!important}.header-brand .brand{font-size:17px!important}.header-brand small{font-size:9px!important}.header-menu,.header-alert,.header-avatar{width:37px;height:37px;font-size:19px}.wallet-top{grid-template-columns:48px 1fr 34px;gap:8px;padding:20px 12px!important;min-height:150px}.wallet-card-icon{width:48px;height:48px;font-size:28px}.wallet-top .amount{font-size:clamp(22px,6.1vw,30px)!important}.wallet-top .amount span{font-size:12px}.balance-toggle{width:34px!important;height:34px!important;min-height:34px!important}.service-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.service-grid .service-card{min-height:104px!important}.service-grid .service-icon{width:48px!important;height:48px!important}.service-grid .service-card strong{font-size:11px!important}.national-banner{min-height:96px;padding:14px}.banner-flag{width:47px;height:32px}.banner-flag b{border-top-width:16px;border-bottom-width:16px;border-left-width:22px}}
    @media(min-width:431px) and (max-width:650px){.service-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
    @media(min-width:760px){main{max-width:900px!important}.service-grid{grid-template-columns:repeat(5,minmax(0,1fr))}.app-bottom{width:min(100%,900px)!important}}
    @media(prefers-reduced-motion:reduce){*,*:before,*:after{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
    @media(min-width:760px){.app-bottom{left:50%;right:auto;transform:translateX(-50%);width:min(100%,760px);border:1px solid #dce8f1;border-bottom:0;border-radius:18px 18px 0 0}}

</style></head><body><header class='app-header'><a class='header-menu' href='/store/'>☰</a><div class='header-brand'><div class='brand'>♛ Empire E-Story</div><small>Digital Life .. Greater Sudan</small></div><a class='header-alert' href='/store/notifications' aria-label='الإشعارات'>♧</a><a class='header-avatar' href='/account_details' aria-label='الملف الشخصي'>♙</a></header><main>{% if request.endpoint == 'empire.home' %}<section class='wallet-top'><div class='wallet-card-icon'>▣</div><div class='wallet-balance'><div class='eyebrow'>رصيد المحفظة</div><div class='amount' id='sdgBalance' data-balance='{{wallet_sdg}}'>{{'{:,}'.format(wallet_sdg)}} <span>SDG</span></div><div class='currency'>جنيه سوداني</div></div><button class='balance-toggle' id='balanceToggle' type='button' aria-label='إخفاء الرصيد' aria-pressed='false'><span id='balanceEye'>◉</span></button><div class='wallet-secure'><span>✓</span><small>محفظتك بأمان</small></div></section>{% endif %}<nav class='nav'>
    {% with ms=get_flashed_messages() %}{% for m in ms %}<p class='flash'>{{m}}</p>{% endfor %}{% endwith %}
    {{body|safe}}<footer><a href='/account'>حسابي</a> · <a href='/logout'>تسجيل الخروج</a><span style='float:left'>الإمبراطورية إستوري ©</span></footer></main><nav class='app-bottom'><a href='/store/'><span class='ico'>⌂</span>الرئيسية</a><a href='/store/marketplace'><span class='ico'>▦</span>المتجر</a><a href='/store/transfer-epc'><span class='ico'>⇄</span>التحويلات</a><a href='/store/wallet'><span class='ico'>▣</span>عملاتي</a><a href='/account_details'><span class='ico'>♙</span>حسابي</a></nav><script>
(function(){
  const balance=document.getElementById('sdgBalance'),toggle=document.getElementById('balanceToggle');
  if(balance&&toggle){
    const formatted=Number(balance.dataset.balance||'0').toLocaleString('en-US');
    let hidden=false;try{hidden=localStorage.getItem('empire.balance.hidden')==='1'}catch(_){}
    function paint(){balance.innerHTML=hidden?'••••••••':formatted+' <span>SDG</span>';toggle.setAttribute('aria-pressed',hidden?'true':'false');toggle.setAttribute('aria-label',hidden?'إظهار الرصيد':'إخفاء الرصيد');document.getElementById('balanceEye').textContent=hidden?'◎':'◉'}
    toggle.addEventListener('click',function(){hidden=!hidden;try{localStorage.setItem('empire.balance.hidden',hidden?'1':'0')}catch(_){}paint()});paint();
  }
  const bottom=document.querySelector('.app-bottom');
  if(bottom){const path=location.pathname;bottom.querySelectorAll('a').forEach(function(a){try{const p=new URL(a.href,location.href).pathname;a.classList.toggle('active',p===path||(p==='/store/'&&path==='/store/'))}catch(_){}})}
})();
</script></body></html>"""
    current=user()
    ctx.setdefault("is_founder", bool(current and current["role"]=="founder"))
    ctx.setdefault("wallet_sdg", balance(current["username"],"SDG") if current else 0)
    ctx.setdefault("wallet_urc", balance(current["username"],"URC") if current else 0)
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
    rows=db().execute("SELECT * FROM es_ledger WHERE account=? ORDER BY id DESC LIMIT 4",("user:"+u["username"],)).fetchall()
    body=render_template_string("""<section class="home-services"><div class="section-heading"><div><span class="section-kicker">خدمات الإمبراطورية</span><h2>ماذا تريد أن تفعل اليوم؟</h2></div><span class="sudan-mark">🇸🇩</span></div>
    <div class="service-grid">
    <a class="service-card" href="/store/marketplace"><i class="service-icon store">▦</i><strong>المتجر</strong></a>
    <a class="service-card" href="/store/accounts"><i class="service-icon accounts">🛒</i><strong>بيع وشراء الحسابات</strong></a>
    <a class="service-card" href="/store/transfer-epc"><i class="service-icon transfer">⇄</i><strong>التحويلات</strong></a>
    <a class="service-card" href="/store/wallet"><i class="service-icon coins">◉</i><strong>عملاتي</strong></a>
    <a class="service-card" href="/account/funding"><i class="service-icon funding">↥</i><strong>تغذية حسابي</strong></a>
    <a class="service-card" href="/account_details"><i class="service-icon profile">♙</i><strong>ملفي الشخصي</strong></a>
    <a class="service-card" href="/store/topups"><i class="service-icon games">🎮</i><strong>الألعاب</strong></a>
    <a class="service-card" href="/store/exchange"><i class="service-icon exchange">⇄</i><strong>صرف العملات</strong></a>
    <a class="service-card" href="/store/withdrawal"><i class="service-icon withdraw">↧</i><strong>السحب</strong></a>
    <a class="service-card" href="/store/agents"><i class="service-icon agents">♧</i><strong>الوكلاء</strong></a></div></section>
    <section class="national-banner"><div class="banner-copy"><span>معاً نبني السودان الرقمي</span><strong>خدمات متكاملة .. أمان وثقة</strong></div><div class="banner-flag" aria-label="علم السودان"><i></i><i></i><i></i><b></b></div><div class="banner-crown">♛</div></section>
    <section class="panel recent-panel"><div class="section-heading"><div><span class="section-kicker">حسابك أولاً</span><h2>آخر العمليات</h2></div><a class="view-all" href="/store/wallet">عرض الكل ←</a></div>
    {% for x in rows %}<div class="transaction-row"><span class="transaction-symbol">{{'↗' if x['delta'] < 0 else '↙'}}</span><div class="transaction-info"><b>{{x['memo']}}</b><small>{{x['created_at'][:16]}}</small></div><b class="transaction-value {{'out' if x['delta'] < 0 else 'in'}}" dir="ltr">{{'+' if x['delta'] > 0 else ''}}{{'{:,}'.format(x['delta'])}} {{x['currency']}}</b></div>{% else %}<div class="empty-state">ستظهر هنا آخر حركات محفظتك.</div>{% endfor %}</section>
    {% if is_founder %}<section class="panel founder-shortcut"><h2>مركز المؤسس</h2><p>إدارة شاملة للمنصة والحسابات والأصول.</p><a class="btn" href="/store/admin">فتح لوحة تحكم المؤسس</a></section>{% endif %}""",rows=rows,is_founder=u["role"]=="founder")
    return page("الرئيسية",body)


@empire_bp.get("/wallet")
@login_required
def wallet():
    u=user(); ensure_wallet(u["username"],"SDG"); ensure_wallet(u["username"],"URC")
    body=render_template_string("""<section class='panel'><h1>المحفظة التجارية</h1>
    <div class='grid'><article class='panel'><small>رصيد SDG</small><h2>{{'{:,}'.format(sdg)}}</h2></article>
    <article class='panel'><small>رصيد URC</small><h2>{{'{:,}'.format(epc)}}</h2></article></div>
    <a class='btn' href='/store/deposit'>طلب إيداع SDG</a> <a class='btn' href='/store/withdrawal'>طلب سحب SDG</a> <a class='btn' href='/store/buy-epc'>شراء URC</a> <a class='btn' href='/store/transfer-epc'>تحويل URC</a> <a class='btn' href='/store/payment/reference'>ربط تحويل Binance</a>
    </section>
    <section class='panel'><h2>آخر القيود</h2>{% for x in rows %}<p>{{x['created_at']}} · {{x['memo']}} · {{x['delta']}} {{x['currency']}}</p>{% else %}<p>لا توجد عمليات.</p>{% endfor %}</section>""",
      sdg=balance(u["username"],"SDG"),epc=balance(u["username"],"URC"),
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

@empire_bp.route("/withdrawal",methods=["GET","POST"])
@login_required
def withdrawal():
    u=user()
    if request.method=="POST":
        csrf(); amount=money_int(request.form.get("amount"))
        method=request.form.get("payout_method","").strip()
        details=request.form.get("payout_details","").strip()
        if method not in ("BANK_TRANSFER","MOBILE_WALLET","OTHER") or len(details)<5 or len(details)>500: abort(400)
        encrypted=encryption().encrypt(details.encode()).decode()
        r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            post(r,[("user:"+u["username"],"SDG",-amount),("payout:pending","SDG",amount)],"حجز طلب سحب مستحقات")
            d.execute("INSERT INTO es_withdrawals(reference,username,amount_sdg,payout_method,payout_details_cipher,created_at) VALUES(?,?,?,?,?,?)",
                      (r,u["username"],amount,method,encrypted,now()))
            audit(u["username"],"WITHDRAWAL_REQUESTED","withdrawal",r)
            d.commit()
        except Exception: d.rollback(); raise
        flash("تم حجز المبلغ ورفع طلب السحب للمراجعة. لا يعتبر مدفوعاً قبل تأكيد المالك.")
        return redirect(url_for("empire.wallet"))
    body=render_template_string("""<section class='panel'><h1>طلب سحب SDG</h1><p>سيتم حجز المبلغ من محفظتك حتى يراجع المالك الطلب. أدخل بيانات الاستلام بدقة.</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>المبلغ SDG</label><input name='amount' type='number' min='1' required>
    <label>وسيلة الاستلام</label><select name='payout_method'><option value='BANK_TRANSFER'>تحويل بنكي</option><option value='MOBILE_WALLET'>محفظة هاتف</option><option value='OTHER'>أخرى</option></select>
    <label>بيانات الاستلام</label><textarea name='payout_details' maxlength='500' required></textarea><button>إرسال طلب السحب</button></form></section>""",csrf=session.get("csrf",""))
    return page("سحب SDG",body)

@empire_bp.get("/topups")
@login_required
def topups():
    products=db().execute("SELECT * FROM es_products WHERE active=1 ORDER BY game,title").fetchall()
    body=render_template_string("""<section class='panel'><h1>شحن الألعاب</h1><p>طلبات الشحن متاحة فقط للمنتجات التي يضيفها المالك، والتنفيذ يتطلب مورداً أو وكيلاً معتمداً.</p></section>
    <div class='grid'>{% for p in products %}<article class='panel'><span class='tag'>{{p['game']}}</span><h3>{{p['title']}}</h3><p>{{p['instructions']}}</p><b>{{'{:,}'.format(p['price_sdg'])}} SDG</b>
    <form method='post' action='/store/topups/new'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='product_id' value='{{p['id']}}'>
    <label>معرف اللاعب</label><input name='player_id' maxlength='100' required><button>طلب الشحن</button></form></article>{% else %}<div class='empty-state'>نعمل على تجهيز باقات الشحن. تفقد الصفحة قريباً.</div>{% endfor %}</div>""",products=products,csrf=session.get("csrf",""))
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
    rows=db().execute("SELECT * FROM es_listings WHERE status='PUBLISHED' AND listing_type='PRODUCT' ORDER BY id DESC LIMIT 100").fetchall()
    body=render_template_string("""<section class='panel'><span class='tag'>المتجر السوداني الرقمي</span><h1>المتجر الشامل</h1>
    <p>تسوق الخدمات والمنتجات الرقمية، أو اعرض منتجك للبيع.</p><a class='btn' href='/store/listing/new'>＋ أضف منتجاً للبيع</a> <a class='btn secondary' href='/store/accounts'>سوق الحسابات</a></section>
    <div class='grid'>{% for x in rows %}<article class='panel'><span class='tag'>{{x['category']}}</span><h3>{{x['title']}}</h3><p>{{x['description']}}</p><b>{{'{:,}'.format(x['price_sdg'])}} SDG</b><p><small>البائع: {{x['seller']}}</small></p>
    <form method='post' action='/store/marketplace/buy'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='listing_id' value='{{x['id']}}'><button>شراء مع ضمان</button></form></article>{% else %}<p>لا توجد منتجات معروضة حالياً.</p>{% endfor %}</div>""",rows=rows,csrf=session.get("csrf",""))
    return page("المتجر الشامل",body)

@empire_bp.get("/accounts")
@login_required
def accounts_market():
    rows=db().execute("SELECT * FROM es_listings WHERE status='PUBLISHED' AND listing_type='ACCOUNT' ORDER BY id DESC LIMIT 100").fetchall()
    body=render_template_string("""<section class='panel'><span class='tag'>بيع وشراء الحسابات</span><h1>سوق الحسابات</h1>
    <p>تصفح الحسابات المعروضة أو اعرض حسابك للبيع. رسوم نشر إعلان الحساب 1,000 SDG تخصم من محفظتك عند النشر.</p>
    <a class='btn' href='/store/listing/new'>＋ اعرض حسابك للبيع</a></section>
    <div class='grid'>{% for x in rows %}<article class='panel'><span class='tag'>{{x['category']}}</span><h3>{{x['title']}}</h3><p>{{x['description']}}</p><b>{{'{:,}'.format(x['price_sdg'])}} SDG</b><p><small>البائع: {{x['seller']}}</small></p>
    <form method='post' action='/store/marketplace/buy'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='listing_id' value='{{x['id']}}'><button>شراء مع ضمان</button></form></article>{% else %}<p>لا توجد حسابات معروضة حالياً.</p>{% endfor %}</div>""",rows=rows,csrf=session.get("csrf",""))
    return page("سوق الحسابات",body)

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
        listing_type=request.form.get("listing_type","ACCOUNT").strip()
        category=request.form.get("category","").strip()[:60]
        if listing_type not in ("ACCOUNT","PRODUCT") or not category or not title or len(title)>100 or len(desc)<10 or len(desc)>1500: abort(400)
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
            except Exception: abort(400,"ملف الصورة غير صالح")
        r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            if listing_type=="ACCOUNT":
                post(r+"F",[("user:"+u["username"],"SDG",-1000),("platform:revenue","SDG",1000)],"رسوم نشر حساب للبيع")
            d.execute("INSERT INTO es_listings(public_id,seller,game,title,description,price_sdg,image_path,status,created_at,listing_type,category) VALUES(?,?,?,?,?,?,?,'PUBLISHED',?,?,?)",
                      (r,u["username"],category,title,desc,price,image_path,now(),listing_type,category))
            audit(u["username"],"LISTING_CREATED","listing",r,"type="+listing_type)
            d.commit()
        except Exception:
            d.rollback()
            if image_path:
                try: os.remove(os.path.join(IMAGE_DIR,image_path))
                except OSError: pass
            raise
        flash("تم نشر إعلانك بنجاح." if listing_type=="PRODUCT" else "تم نشر حسابك وخصم رسوم النشر 1,000 SDG.")
        return redirect(url_for("empire.accounts_market" if listing_type=="ACCOUNT" else "empire.marketplace"))
    body=render_template_string("""<section class='panel'><h1>إضافة إعلان جديد</h1>
    <p>رسوم نشر حساب للبيع 1,000 SDG تخصم عند تأكيد النشر. نشر المنتجات في المتجر لا يخصم هذه الرسوم.</p>
    <form method='post' enctype='multipart/form-data'><input type='hidden' name='csrf' value='{{csrf}}'>
    <label>نوع الإعلان</label><select name='listing_type'><option value='ACCOUNT'>حساب للبيع</option><option value='PRODUCT'>منتج / خدمة في المتجر</option></select>
    <label>التصنيف</label><input name='category' maxlength='60' placeholder='مثال: PUBG Mobile، خدمات رقمية، منتجات' required>
    <label>صورة الإعلان (اختياري)</label><input type='file' name='image' accept='image/jpeg,image/png,image/webp'>
    <label>عنوان الإعلان</label><input name='title' maxlength='100' required>
    <label>الوصف دون بيانات دخول أو كلمات مرور</label><textarea name='description' minlength='10' maxlength='1500' required></textarea>
    <label>السعر SDG</label><input name='price_sdg' type='number' min='1' required><button>نشر الإعلان</button></form></section>""",csrf=session.get("csrf",""))
    return page("إضافة إعلان",body)

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
    rows=db().execute("SELECT reference,amount,currency,quantity_epc,operation_id,created_at FROM es_payments WHERE username=? AND status='PENDING' AND method='BINANCE_USDT' ORDER BY id DESC",(u["username"],)).fetchall()
    body=render_template_string("""<section class='panel'><h1>ربط تحويل Binance</h1><p>حوّل إلى Binance ID: <b>{{bid or 'لم يضبطه المالك بعد'}}</b>. أدخل رقم التحويل الحقيقي بعد إتمامه.</p>
    {% for x in rows %}<article class='panel'><p>المرجع {{x['reference']}} · {{x['amount']/1000000}} USDT · URC المطلوبة {{x['quantity_epc']}}</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'>
    <label>رقم التحويل من Binance</label><input name='operation_id' maxlength='100' required><button>إرسال للمراجعة</button></form>{% if not x['operation_id'] %}<form method='post' action='/store/payment/cancel'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>إلغاء الطلب</button></form>{% endif %}</article>{% else %}<p>لا توجد طلبات Binance معلقة.</p>{% endfor %}</section>""",
      bid=setting("binance_id"),rows=rows,csrf=session.get("csrf",""))
    return page("تحويل Binance",body)

@empire_bp.post("/payment/cancel")
@login_required
def cancel_payment():
    csrf(); u=user(); r=request.form.get("reference","")
    changed=db().execute("UPDATE es_payments SET status='REJECTED',reviewer=?,reviewed_at=? WHERE reference=? AND username=? AND status='PENDING' AND operation_id IS NULL",
                         (u["username"],now(),r,u["username"])).rowcount
    if changed!=1: abort(409,"لا يمكن إلغاء هذا الطلب بعد إرسال رقم التحويل أو مراجعته")
    audit(u["username"],"PAYMENT_CANCELLED","payment",r)
    flash("تم إلغاء طلب الشراء وإتاحة الكمية المحجوزة من URC.")
    return redirect(url_for("empire.wallet"))

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
        if order["cost_sdg"]<=0: raise ValueError("يجب على المالك إدخال تكلفة المورد الحقيقية قبل إغلاق الطلب")
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

@empire_bp.get("/transfer/lookup")
@login_required
def transfer_lookup():
    account=request.args.get("account_number","").strip()
    if not re.fullmatch(r"[0-9]{5,20}",account):
        return jsonify({"ok":False,"message":"أدخل رقم حساب صحيحاً"}),400
    target=db().execute("SELECT username,full_name,account_number,active FROM users WHERE account_number=?",(account,)).fetchone()
    if not target or not target["active"] or target["username"]==user()["username"]:
        return jsonify({"ok":False,"message":"لم يتم العثور على حساب مستلم صالح"})
    status=db().execute("SELECT status FROM es_identity_status WHERE username=?",(target["username"],)).fetchone()
    verified=bool(status and status["status"]=="VERIFIED")
    return jsonify({"ok":True,"name":target["full_name"],"account_number":target["account_number"],
                    "verification":"موثق" if verified else "غير موثق","verified":verified})

@empire_bp.route("/transfer-epc",methods=["GET","POST"])
@login_required
def transfer_epc():
    u=user()
    if request.method=="POST":
        csrf()
        account=request.form.get("recipient_account","").strip()
        currency=request.form.get("currency","URC").strip()
        qty=money_int(request.form.get("quantity"))
        note=request.form.get("note","").strip()[:250]
        if currency not in ("SDG","URC") or not re.fullmatch(r"[0-9]{5,20}",account): abort(400,"بيانات التحويل غير صالحة")
        target=db().execute("SELECT username,full_name,account_number,active FROM users WHERE account_number=?",(account,)).fetchone()
        if not target or not target["active"] or target["username"]==u["username"]: abort(400,"المستلم غير صالح")
        r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            post(r,[("user:"+u["username"],currency,-qty),("user:"+target["username"],currency,qty)],"تحويل "+currency+" إلى "+target["full_name"])
            d.execute("INSERT INTO es_transfer_details(reference,sender,recipient,recipient_account,currency,note,created_at) VALUES(?,?,?,?,?,?,?)",
                      (r,u["username"],target["username"],account,currency,note,now()))
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                      (target["username"],"وصلك تحويل جديد","استلمت تحويلًا بقيمة "+format(qty,",")+" "+currency+" من "+u["full_name"]+" · رقم العملية "+r,r,now()))
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                      (u["username"],"تم التحويل بنجاح","تم إرسال "+format(qty,",")+" "+currency+" إلى "+target["full_name"]+" · رقم العملية "+r,r,now()))
            audit(u["username"],"WALLET_TRANSFER","wallet",r,"to="+target["username"]+";currency="+currency)
            d.commit()
        except Exception:
            d.rollback(); raise
        return redirect(url_for("empire.transfer_receipt",reference=r))
    body=render_template_string("""<section class='panel'><span class='tag'>تحويل آمن</span><h1>إرسال تحويل</h1>
      <p>أدخل رقم حساب المستلم أولاً، وسنعرض اسمه وحالة التوثيق قبل المتابعة.</p>
      <form method='post' id='transferForm'><input type='hidden' name='csrf' value='{{csrf}}'>
      <label>رقم حساب المستلم</label><input id='recipientAccount' name='recipient_account' inputmode='numeric' pattern='[0-9]{5,20}' maxlength='20' required>
      <div id='recipientInfo' class='panel' style='display:none;background:#f2f8fc!important;margin:8px 0'></div>
      <label>عملة التحويل</label><select name='currency'><option value='SDG'>جنيه سوداني SDG</option><option value='URC'>عملة URC</option></select>
      <label>المبلغ</label><input name='quantity' type='number' min='1' step='1' required>
      <label>ملاحظات (اختياري)</label><textarea name='note' maxlength='250' placeholder='أضف ملاحظة للمستلم'></textarea>
      <button id='sendTransfer' disabled>إرسال التحويل</button></form></section>
      <script>
      (function(){const input=document.getElementById('recipientAccount'),box=document.getElementById('recipientInfo'),btn=document.getElementById('sendTransfer');let timer=null,last='';
      input.addEventListener('input',()=>{clearTimeout(timer);btn.disabled=true;box.style.display='none';const value=input.value.trim();if(!value)return;timer=setTimeout(async()=>{if(value===last)return;last=value;try{const r=await fetch('/store/transfer/lookup?account_number='+encodeURIComponent(value),{credentials:'same-origin'});const j=await r.json();box.style.display='block';if(j.ok){box.replaceChildren();const name=document.createElement('b');name.textContent='المستلم: '+j.name;const acc=document.createElement('div');acc.textContent='رقم الحساب: '+j.account_number;const ver=document.createElement('div');ver.textContent='حالة التوثيق: '+j.verification;box.append(name,acc,ver);btn.disabled=false}else{box.textContent=j.message||'تعذر العثور على الحساب';}}catch(e){box.style.display='block';box.textContent='تعذر التحقق من الحساب. حاول مجدداً.';}},350);});
      document.getElementById('transferForm').addEventListener('submit',e=>{if(btn.disabled){e.preventDefault();alert('تحقق من بيانات المستلم أولاً');}});
      })();</script>""",csrf=session.get("csrf",""))
    return page("التحويلات",body)

@empire_bp.get("/transfer/receipt/<reference>")
@login_required
def transfer_receipt(reference):
    u=user()
    rows=db().execute("SELECT account,currency,delta,memo,created_at FROM es_ledger WHERE reference=? ORDER BY line_no",(reference,)).fetchall()
    if len(rows)!=2 or any(not row["account"].startswith("user:") for row in rows): abort(404)
    debit=next((row for row in rows if row["delta"]<0),None)
    credit=next((row for row in rows if row["delta"]>0),None)
    if not debit or not credit or debit["currency"]!=credit["currency"]: abort(404)
    sender=debit["account"][5:]; recipient=credit["account"][5:]
    if u["username"] not in (sender,recipient): abort(404)
    detail=db().execute("SELECT * FROM es_transfer_details WHERE reference=?",(reference,)).fetchone()
    if not detail: abort(404)
    sender_user=db().execute("SELECT full_name,account_number FROM users WHERE username=?",(sender,)).fetchone()
    recipient_user=db().execute("SELECT full_name,account_number FROM users WHERE username=?",(recipient,)).fetchone()
    amount=credit["delta"]
    body=render_template_string("""<section class='panel transfer-receipt'>
      <div class='receipt-flag'><i></i><i></i><i></i><i></i></div>
      <div style='text-align:center'><span class='tag'>إشعار تحويل رسمي</span><div style='font-size:42px;color:#16844f'>✓</div>
      <h1>تم التحويل بنجاح</h1><p>الإمبراطورية إستوري · خدمة مالية سودانية</p></div>
      <div style='background:#edf7f1;border:1px solid #d1eadb;border-radius:16px;padding:17px;text-align:center;margin:14px 0'>
        <small>قيمة التحويل</small><div style='font-size:30px;font-weight:900;color:#145c3a'>{{'{:,}'.format(amount)}} {{currency}}</div><b style='color:#16844f'>عملية مكتملة</b>
      </div>
      <div style='display:grid;gap:11px'>
        <div><small>من المرسل</small><b>{{sender_name}} · {{sender_account}}</b></div>
        <div><small>إلى المستلم</small><b>{{recipient_name}} · {{recipient_account}}</b></div>
        <div><small>ملاحظات المرسل</small><b>{{note or 'لا توجد ملاحظات'}}</b></div>
        <div><small>رقم العملية</small><b dir='ltr'>{{reference}}</b></div>
        <div><small>تاريخ ووقت العملية (UTC)</small><b dir='ltr'>{{created_at}}</b></div>
      </div>
      <p class='muted' style='margin-top:15px'>هذا إشعار يثبت تسجيل التحويل داخل محفظة الإمبراطورية إستوري. لا يعرض أرصدة أطراف العملية.</p>
      <div style='display:flex;gap:8px;flex-wrap:wrap'><button type='button' onclick='window.print()'>حفظ الإشعار / PDF</button><a class='btn secondary' href='/store/'>الرئيسية</a></div>
    </section>""",
      amount=amount,currency=credit["currency"],reference=reference,
      sender_name=sender_user["full_name"],sender_account=sender_user["account_number"],
      recipient_name=recipient_user["full_name"],recipient_account=recipient_user["account_number"],
      note=detail["note"],created_at=credit["created_at"])
    return page("إشعار التحويل",body)

@empire_bp.route("/exchange",methods=["GET","POST"])
@login_required
def exchange():
    u=user()
    if request.method=="POST":
        csrf()
        direction=request.form.get("direction","")
        amount=money_int(request.form.get("amount"))
        mode=setting("fx_rate_mode","manual")
        base=int(setting("fx_market_rate" if mode=="market" else "fx_manual_rate","0"))
        if base<=0: abort(503,"سعر الصرف غير مضبوط من إدارة المنصة")
        buy_spread=int(setting("fx_buy_spread_bps","0")); sell_spread=int(setting("fx_sell_spread_bps","0"))
        if direction=="URC_TO_SDG":
            rate=(base*(10000-sell_spread))//10000
            result=amount*rate
            source="URC"
        elif direction=="SDG_TO_URC":
            rate=(base*(10000+buy_spread)+9999)//10000
            result=amount//rate
            source="SDG"
        else: abort(400)
        if result<=0: abort(400,"المبلغ أقل من الحد الأدنى للتحويل")
        r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            if direction=="URC_TO_SDG":
                pool=int(d.execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='platform:fx_pool' AND currency='SDG'").fetchone()["n"])
                if pool<result: raise ValueError("سيولة SDG المتاحة لدى المنصة لا تكفي لإتمام التحويل")
                post(r,[("user:"+u["username"],"URC",-amount),("platform:fx_pool","URC",amount),("platform:fx_pool","SDG",-result),("user:"+u["username"],"SDG",result)],"تحويل URC إلى SDG")
            else:
                pool=int(d.execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='platform:fx_pool' AND currency='URC'").fetchone()["n"])
                if pool<result: raise ValueError("سيولة URC المتاحة لدى المنصة لا تكفي لإتمام التحويل")
                post(r,[("user:"+u["username"],"SDG",-amount),("platform:fx_pool","SDG",amount),("platform:fx_pool","URC",-result),("user:"+u["username"],"URC",result)],"تحويل SDG إلى URC")
            audit(u["username"],"CURRENCY_EXCHANGE","exchange",r,direction+" rate="+str(rate))
            d.commit()
        except Exception:
            d.rollback()
            raise
        flash("تم تنفيذ تحويل العملات بنجاح.")
        return redirect(url_for("empire.wallet"))
    mode=setting("fx_rate_mode","manual")
    base=int(setting("fx_market_rate" if mode=="market" else "fx_manual_rate","0"))
    return page("تحويل العملات",render_template_string("""<section class='panel'><span class='tag'>تحويل العملات</span><h1>URC ↔ SDG</h1>
      <p>السعر المعتمد حالياً: {{rate}} SDG لكل URC · المصدر: {{mode}}</p>
      <p class='muted'>تُنفّذ العملية فقط عند توفر سيولة كافية لدى المنصة بالعملة المقابلة. راجع الناتج قبل التأكيد.</p>
      <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>
      <label>اتجاه التحويل</label><select name='direction'><option value='URC_TO_SDG'>بيع URC واستلام SDG</option><option value='SDG_TO_URC'>شراء URC باستخدام SDG</option></select>
      <label>المبلغ</label><input name='amount' type='number' min='1' required><button>تنفيذ التحويل</button></form>
      <a class='btn secondary' href='/store/wallet'>العودة للمحفظة</a></section>""",rate=base,mode="سعر السوق المرجعي" if mode=="market" else "السعر اليدوي",csrf=session.get("csrf","")))
    
@empire_bp.route("/admin/exchange",methods=["GET","POST"])
@login_required
@founder_required
def admin_exchange():
    seed_owner_assets()
    u=user()
    if request.method=="POST":
        csrf()
        action=request.form.get("action","settings")
        if action=="liquidity":
            currency=request.form.get("currency","")
            amount=money_int(request.form.get("amount"))
            if currency not in ("URC","SDG"): abort(400)
            d=db(); d.execute("BEGIN IMMEDIATE")
            try:
                post(ref(),[("user:"+u["username"],currency,-amount),("platform:fx_pool",currency,amount)],"تمويل احتياطي الصرف من محفظة المالك")
                audit(u["username"],"FX_LIQUIDITY_ADDED","fx_pool",currency+" "+str(amount))
                d.commit()
            except Exception:
                d.rollback()
                raise
            flash("تمت إضافة السيولة إلى احتياطي الصرف.")
            return redirect(url_for("empire.admin_exchange"))
        mode=request.form.get("fx_rate_mode","manual")
        market=request.form.get("fx_market_rate","0").strip()
        manual=request.form.get("fx_manual_rate","0").strip()
        buy=request.form.get("fx_buy_spread_bps","0").strip()
        sell=request.form.get("fx_sell_spread_bps","0").strip()
        if mode not in ("market","manual") or not all(re.fullmatch(r"[0-9]{1,12}",v) for v in (market,manual,buy,sell)) or (mode=="market" and int(market)<=0) or (mode=="manual" and int(manual)<=0) or int(buy)>10000 or int(sell)>10000: abort(400)
        for k,v in (("fx_rate_mode",mode),("fx_market_rate",market),("fx_manual_rate",manual),("fx_buy_spread_bps",buy),("fx_sell_spread_bps",sell)):
            db().execute("INSERT INTO es_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,v))
        audit(u["username"],"FX_RATES_UPDATED","settings",mode)
        flash("تم حفظ إعدادات سعر الصرف.")
    pools={c:int(db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='platform:fx_pool' AND currency=?",(c,)).fetchone()["n"]) for c in ("URC","SDG")}
    owner={c:balance(u["username"],c) for c in ("URC","SDG")}
    return page("إدارة الصرف",render_template_string("""<section class='panel'><h1>إدارة الصرف والسيولة</h1>
      <div class='grid'><article class='panel'><small>أصول المالك URC</small><h2>{{'{:,}'.format(owner['URC'])}}</h2></article><article class='panel'><small>أصول المالك SDG</small><h2>{{'{:,}'.format(owner['SDG'])}}</h2></article>
      <article class='panel'><small>احتياطي URC</small><h2>{{'{:,}'.format(pools['URC'])}}</h2></article><article class='panel'><small>احتياطي SDG</small><h2>{{'{:,}'.format(pools['SDG'])}}</h2></article></div>
      <h2>مصدر سعر الصرف</h2><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>
      <label>طريقة التسعير</label><select name='fx_rate_mode'><option value='market' {% if values['fx_rate_mode']=='market' %}selected{% endif %}>سعر السوق المرجعي</option><option value='manual' {% if values['fx_rate_mode']=='manual' %}selected{% endif %}>سعر يدوي</option></select>
      <label>سعر السوق المرجعي (SDG لكل URC)</label><input name='fx_market_rate' type='number' min='1' value='{{values["fx_market_rate"]}}'>
      <label>السعر اليدوي (SDG لكل URC)</label><input name='fx_manual_rate' type='number' min='1' value='{{values["fx_manual_rate"]}}'>
      <label>هامش شراء URC بالنقاط الأساسية</label><input name='fx_buy_spread_bps' type='number' min='0' max='10000' value='{{values["fx_buy_spread_bps"]}}'>
      <label>هامش بيع URC بالنقاط الأساسية</label><input name='fx_sell_spread_bps' type='number' min='0' max='10000' value='{{values["fx_sell_spread_bps"]}}'><button>حفظ الأسعار</button></form>
      <p class='muted'>سعر السوق هنا سعر مرجعي يدخله المالك أو يحدّثه؛ لا يوجد حالياً اتصال تلقائي بمزوّد أسعار خارجي.</p>
      <h2>تمويل احتياطي الصرف</h2><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='liquidity'>
      <label>العملة</label><select name='currency'><option>URC</option><option>SDG</option></select><label>المبلغ من محفظة المالك</label><input name='amount' type='number' min='1' required><button>إضافة إلى احتياطي الصرف</button></form></section>""",
      owner=owner,pools=pools,values={k:setting(k,d) for k,d in (("fx_rate_mode","manual"),("fx_market_rate","0"),("fx_manual_rate","0"),("fx_buy_spread_bps","0"),("fx_sell_spread_bps","0"))},csrf=session.get("csrf","")))

@empire_bp.get("/admin")
@login_required
@founder_required
def admin():
    seed_owner_assets()
    owner=db().execute("SELECT username,account_number FROM users WHERE role='founder' ORDER BY id LIMIT 1").fetchone()
    owner_name=owner["username"] if owner else user()["username"]
    owner_account=owner["account_number"] if owner else "—"
    owner_urc=balance(owner_name,"URC")
    owner_sdg=balance(owner_name,"SDG")
    stats={
      "listings":db().execute("SELECT COUNT(*) n FROM es_listings WHERE status='PUBLISHED'").fetchone()["n"],
      "escrow":db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='escrow:market'").fetchone()["n"],
      "revenue":db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='platform:revenue'").fetchone()["n"],
      "pending":db().execute("SELECT COUNT(*) n FROM es_payments WHERE status='PENDING'").fetchone()["n"],
      "withdrawals":db().execute("SELECT COUNT(*) n FROM es_withdrawals WHERE status='PENDING'").fetchone()["n"],
      "agents":db().execute("SELECT COUNT(*) n FROM es_agents WHERE status='APPROVED'").fetchone()["n"],
      "topups":db().execute("SELECT COUNT(*) n FROM es_topup_orders WHERE status IN ('PAID','ASSIGNED')").fetchone()["n"]
    }
    body=render_template_string("""<section class='hero'><div><span class='hero-kicker'>OWNER CONTROL CENTER</span><h1>مركز قيادة الإمبراطورية</h1><p>لوحة المالك الشاملة لإدارة الأصول والخدمات والعمليات المالية.</p><span class='tag'>رقم حساب المالك: {{owner_account}}</span></div><div class='hero-character'><div class='metric'><small>الأصول الرقمية للمالك</small><strong>{{'{:,}'.format(owner_urc)}} URC</strong><small>الأصول النقدية: {{'{:,}'.format(owner_sdg)}} SDG</small></div></div></section>
    <section class='panel'><h2>مؤشرات المنصة</h2><div class='grid'>
    <article class='panel'><small>الإعلانات النشطة</small><h2>{{stats['listings']}}</h2></article>
    <article class='panel'><small>أموال صفقات السوق المحتجزة (SDG)</small><h2>{{'{:,}'.format(stats['escrow'])}}</h2></article>
    <article class='panel'><small>صافي إيراد المتجر المسجل (SDG)</small><h2>{{'{:,}'.format(stats['revenue'])}}</h2></article>
    <article class='panel'><small>طلبات الدفع المعلقة</small><h2>{{stats['pending']}}</h2></article>
    <article class='panel'><small>طلبات السحب المعلقة</small><h2>{{stats['withdrawals']}}</h2></article>
    <article class='panel'><small>الوكلاء المعتمدون</small><h2>{{stats['agents']}}</h2></article>
    <article class='panel'><small>طلبات الشحن قيد التنفيذ</small><h2>{{stats['topups']}}</h2></article></div></section>
    <section class='panel'><h2>إدارة شاملة</h2><div class='grid admin-links'>
    <a href='/store/admin/exchange'>إدارة الصرف والسيولة</a><a href='/store/admin/settings'>إعدادات المنصة والأسعار</a><a href='/store/admin/agents'>إدارة الوكلاء</a><a href='/store/admin/listings'>إدارة السوق والإعلانات</a><a href='/store/admin/products'>باقات الشحن والتكلفة</a><a href='/store/admin/payments'>طلبات الدفع</a><a href='/store/admin/withdrawals'>طلبات السحب</a><a href='/store/admin/disputes'>النزاعات</a><a href='/store/admin/audit'>سجل التدقيق</a></div></section>""",stats=stats,owner_account=owner_account,owner_urc=owner_urc,owner_sdg=owner_sdg)
    return page("لوحة المالك",body)

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

@empire_bp.route("/admin/listings",methods=["GET","POST"])
@login_required
@founder_required
def admin_listings():
    if request.method=="POST":
        csrf(); listing_id=request.form.get("listing_id",""); action=request.form.get("action","")
        target={"PAUSE":"PAUSED","PUBLISH":"PUBLISHED","REMOVE":"REMOVED"}.get(action)
        if not target: abort(400)
        changed=db().execute("UPDATE es_listings SET status=? WHERE id=? AND status!='SOLD'",(target,listing_id)).rowcount
        if changed!=1: abort(409,"الإعلان غير قابل للتعديل")
        audit(user()["username"],"LISTING_"+action,"listing",listing_id)
    rows=db().execute("SELECT * FROM es_listings ORDER BY id DESC LIMIT 300").fetchall()
    body=render_template_string("""<section class='panel'><h1>إدارة إعلانات السوق</h1>{% for x in rows %}<article class='panel'>
    <b>{{x['title']}}</b> · {{x['seller']}} · {{x['price_sdg']}} SDG · {{x['status']}}
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='listing_id' value='{{x['id']}}'>
    <button name='action' value='PAUSE'>إيقاف</button><button name='action' value='PUBLISH'>نشر</button><button name='action' value='REMOVE'>إزالة</button></form></article>{% else %}<p>لا توجد إعلانات.</p>{% endfor %}</section>""",rows=rows,csrf=session.get("csrf",""))
    return page("إدارة الإعلانات",body)

@empire_bp.route("/admin/products",methods=["GET","POST"])
@login_required
@founder_required
def admin_products():
    if request.method=="POST":
        csrf()
        if request.form.get("action")=="toggle":
            pid=request.form.get("product_id","")
            row=db().execute("SELECT * FROM es_products WHERE id=?",(pid,)).fetchone()
            if not row: abort(404)
            db().execute("UPDATE es_products SET active=? WHERE id=?",(0 if row["active"] else 1,pid))
            audit(user()["username"],"PRODUCT_TOGGLED","product",pid)
        else:
            game=request.form.get("game","").strip(); title=request.form.get("title","").strip()
            price=money_int(request.form.get("price_sdg")); cost=money_int(request.form.get("cost_sdg")); code=ref()
            if not game or not title or len(title)>120 or cost>price: abort(400)
            db().execute("INSERT INTO es_products(code,game,title,price_sdg,cost_sdg,created_at) VALUES(?,?,?,?,?,?)",(code,game,title,price,cost,now()))
            audit(user()["username"],"PRODUCT_CREATED","product",code)
    rows=db().execute("SELECT * FROM es_products ORDER BY id DESC").fetchall()
    body=render_template_string("""<section class='panel'><h1>باقات شحن الألعاب</h1><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>
    <input name='game' placeholder='اللعبة' required><input name='title' placeholder='اسم الباقة' required><input name='price_sdg' type='number' min='1' placeholder='سعر البيع SDG' required><input name='cost_sdg' type='number' min='0' placeholder='تكلفة المورد SDG' required><button>إضافة باقة</button></form>
    {% for x in rows %}<article class='panel'><p>{{x['game']}} · {{x['title']}} · البيع {{x['price_sdg']}} SDG · التكلفة {{x['cost_sdg']}} SDG · {{'فعال' if x['active'] else 'متوقف'}}</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='product_id' value='{{x['id']}}'><button name='action' value='toggle'>{{'إيقاف' if x['active'] else 'تفعيل'}}</button></form></article>{% endfor %}</section>""",rows=rows,csrf=session.get("csrf",""))
    return page("باقات الشحن",body)

@empire_bp.route("/admin/payments",methods=["GET","POST"])
@login_required
@founder_required
def admin_payments():
    u=user()
    if request.method=="POST":
        csrf(); pid=request.form.get("id",""); action=request.form.get("action","")
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            row=d.execute("SELECT * FROM es_payments WHERE id=? AND status='PENDING'",(pid,)).fetchone()
            if not row: raise ValueError("طلب الدفع لم يعد معلقاً")
            if action=="CONFIRMED":
                if row["currency"]=="USDT" and not row["operation_id"]:
                    raise ValueError("لا يمكن اعتماد تحويل Binance دون رقم عملية")
                if row["purpose"]=="WALLET_DEPOSIT":
                    post(row["reference"],[("user:"+row["username"],"SDG",row["amount"]),("external:verified_deposits","SDG",-row["amount"])],"إيداع SDG مؤكد يدوياً")
                elif row["purpose"]=="EPC_PURCHASE":
                    epc=int(row["quantity_epc"])
                    if epc<=0 or row["currency"]!="USDT": raise ValueError("بيانات طلب URC غير صالحة")
                    post(row["reference"],[("asset:binance_usdt","USDT",row["amount"]),("external:usdt_receipts","USDT",-row["amount"]),
                                          ("user:"+row["username"],"URC",epc),("issuer:urc","URC",-epc)],
                         "تأكيد استلام USDT وإصدار URC")
                else:
                    raise ValueError("نوع طلب الدفع غير مدعوم للاعتماد")
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
        if setting("epc_sales_enabled","0")!="1": abort(403,"شراء URC متوقف من الإدارة حالياً")
        if method=="SDG_WALLET":
            rate=int(setting("epc_price_sdg","0"))
            if rate<=0: abort(503,"يجب على المالك ضبط سعر URC مقابل SDG أولاً")
            cost=qty*rate
            r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
            try:
                assert_epc_capacity(qty)
                post(r,[("user:"+u["username"],"SDG",-cost),("platform:epc_sales","SDG",cost),("user:"+u["username"],"URC",qty),("issuer:urc","URC",-qty)],"شراء URC من محفظة SDG")
                audit(u["username"],"EPC_PURCHASE","wallet",r); d.commit()
            except Exception: d.rollback(); raise
            flash("تم شراء URC وإضافتها إلى محفظتك.")
        elif method=="BINANCE_USDT":
            bid=setting("binance_id","")
            if not bid: abort(503,"لم يضبط المالك Binance ID بعد")
            micros=int(setting("epc_price_usdt_micros","0"))
            if micros<=0: abort(503,"يجب على المالك ضبط سعر URC مقابل USDT أولاً")
            amount=qty*micros
            r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
            try:
                assert_epc_capacity(qty)
                d.execute("INSERT INTO es_payments(reference,username,method,currency,amount,quantity_epc,purpose,proof_note,created_at) VALUES(?,?,'BINANCE_USDT','USDT',?,?,'EPC_PURCHASE',?,?)",
                          (r,u["username"],amount,qty,"Binance ID: "+bid,now()))
                audit(u["username"],"URC_BINANCE_ORDER_CREATED","payment",r)
                d.commit()
            except Exception: d.rollback(); raise
            flash("تم إنشاء طلب شراء. حوّل USDT إلى Binance ID المعروض، ثم أرسل رقم التحويل في نموذج ربط إثبات الدفع. لن تصدر URC قبل التحقق اليدوي.")
            session["last_epc_payment_ref"]=r
        else: abort(400)
        return redirect(url_for("empire.wallet"))
    body=render_template_string("""<section class='panel'><h1>شراء Empire Coin (URC)</h1><p>السعر الحالي: {{price}} SDG للوحدة. البيع {{enabled}}.</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>الكمية URC</label><input name='quantity' type='number' min='1' required>
    <label>طريقة الدفع</label><select name='method'><option value='SDG_WALLET'>محفظة SDG</option><option value='BINANCE_USDT'>USDT عبر Binance ID</option></select><button>إنشاء الشراء</button></form>
    <p class='muted'>لا يمكن سحب URC أو تداولها خارج المتجر حالياً. مدفوعات Binance تخضع للتحقق اليدوي.</p></section>""",
      price=setting("epc_price_sdg","0"),enabled="مفعل" if setting("epc_sales_enabled")=="1" else "متوقف",csrf=session.get("csrf",""))
    return page("شراء URC",body)

@empire_bp.route("/admin/settings",methods=["GET","POST"])
@login_required
@founder_required
def admin_settings():
    if request.method=="POST":
        csrf()
        keys=("epc_price_sdg","epc_price_usdt_micros","epc_max_supply","binance_id","market_fee_bps","epc_sales_enabled","topups_enabled")
        for k in keys:
            v=request.form.get(k,"").strip()
            if k in ("epc_price_sdg","epc_price_usdt_micros","epc_max_supply","market_fee_bps") and not re.fullmatch(r"[0-9]{1,12}",v): abort(400)
            if k in ("epc_price_sdg","epc_price_usdt_micros") and int(v)<1: abort(400)
            if k=="epc_max_supply":
                issued=-db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='issuer:urc' AND currency='URC'").fetchone()["n"]
                reserved=db().execute("SELECT COALESCE(SUM(quantity_epc),0) n FROM es_payments WHERE purpose='EPC_PURCHASE' AND status='PENDING'").fetchone()["n"]
                if int(v)<issued+reserved: abort(400,"لا يمكن خفض الحد الأقصى دون إجمالي الإصدار والطلبات المعلقة")
            if k=="market_fee_bps" and int(v)>10000: abort(400)
            if k in ("epc_sales_enabled","topups_enabled") and v not in ("0","1"): abort(400)
            db().execute("INSERT INTO es_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,v))
        audit(user()["username"],"SETTINGS_UPDATED","settings","global")
        flash("تم حفظ الإعدادات.")
    body=render_template_string("""<section class='panel'><h1>إعدادات المنصة</h1><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>
    {% for key,label in fields %}<label>{{label}}</label><input name='{{key}}' value='{{values[key]}}'>{% endfor %}<button>حفظ الإعدادات</button></form></section>""",
      fields=[("epc_price_sdg","سعر URC بالجنيه SDG"),("epc_price_usdt_micros","سعر URC بالـ USDT بوحدة مليونية"),("epc_max_supply","الحد الأقصى لإصدار URC (وحدات صحيحة)"),("binance_id","Binance ID للاستقبال"),("market_fee_bps","عمولة السوق بالنقاط الأساسية"),("epc_sales_enabled","تفعيل بيع URC: 1 أو 0"),("topups_enabled","تفعيل طلبات الشحن: 1 أو 0")],
      values={k:setting(k) for k in ("epc_price_sdg","epc_price_usdt_micros","epc_max_supply","binance_id","market_fee_bps","epc_sales_enabled","topups_enabled")},csrf=session.get("csrf",""))
    return page("الإعدادات",body)

@empire_bp.route("/admin/withdrawals",methods=["GET","POST"])
@login_required
@founder_required
def admin_withdrawals():
    u=user()
    if request.method=="POST":
        csrf(); wid=request.form.get("id",""); action=request.form.get("action","")
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            row=d.execute("SELECT * FROM es_withdrawals WHERE id=? AND status='PENDING'",(wid,)).fetchone()
            if not row: raise ValueError("طلب السحب غير متاح للمراجعة")
            if action=="PAID":
                post(row["reference"]+"P",[("payout:pending","SDG",-row["amount_sdg"]),("external:payout_settlements","SDG",row["amount_sdg"])],"تأكيد صرف طلب سحب")
                status="PAID"
            elif action=="REJECTED":
                post(row["reference"]+"X",[("payout:pending","SDG",-row["amount_sdg"]),("user:"+row["username"],"SDG",row["amount_sdg"])],"إعادة مبلغ طلب سحب مرفوض")
                status="REJECTED"
            else: raise ValueError("إجراء غير صالح")
            d.execute("UPDATE es_withdrawals SET status=?,reviewer=?,reviewed_at=? WHERE id=?",(status,u["username"],now(),wid))
            audit(u["username"],"WITHDRAWAL_"+status,"withdrawal",row["reference"])
            d.commit()
        except Exception: d.rollback(); raise
    rows=[]
    for row in db().execute("SELECT * FROM es_withdrawals ORDER BY id DESC LIMIT 200").fetchall():
        item=dict(row)
        try: item["payout_details"]=encryption().decrypt(item["payout_details_cipher"].encode()).decode()
        except Exception: item["payout_details"]="تعذر فك بيانات الاستلام"
        rows.append(item)
    body=render_template_string("""<section class='panel'><h1>طلبات السحب</h1>{% for x in rows %}<article class='panel'><b>{{x['reference']}}</b> · {{x['username']}} · {{x['amount_sdg']}} SDG · {{x['payout_method']}} · {{x['status']}}<p>{{x['payout_details']}}</p>
    {% if x['status']=='PENDING' %}<form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='id' value='{{x['id']}}'><button name='action' value='PAID'>تأكيد الصرف بعد الدفع الفعلي</button><button name='action' value='REJECTED'>رفض وإعادة الرصيد</button></form>{% endif %}</article>{% else %}<p>لا توجد طلبات.</p>{% endfor %}</section>""",rows=rows,csrf=session.get("csrf",""))
    return page("طلبات السحب",body)

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
    app.config.setdefault("MAX_CONTENT_LENGTH", 6 * 1024 * 1024)
    app.register_blueprint(empire_bp)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
