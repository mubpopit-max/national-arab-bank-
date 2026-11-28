"""Empire E-Story commerce module.

Uses a separate double-entry store ledger in the existing SQLite database.
No legacy bank balance is imported or treated as store money. External payment
verification is manual until a supported Binance/payment API is configured.
"""
import os, re, json, sqlite3, secrets, io, hashlib, bcrypt
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
      title TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT 'سيتم تسليم كود الباقة بعد تأكيد الدفع.',
      price_sdg INTEGER NOT NULL CHECK(price_sdg>0), cost_sdg INTEGER NOT NULL DEFAULT 0 CHECK(cost_sdg>=0), active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_topup_orders(
      id INTEGER PRIMARY KEY, reference TEXT UNIQUE NOT NULL, buyer TEXT NOT NULL,
      product_id INTEGER NOT NULL REFERENCES es_products(id), player_id TEXT NOT NULL DEFAULT '',
      amount_sdg INTEGER NOT NULL CHECK(amount_sdg>0),
      status TEXT NOT NULL DEFAULT 'PENDING_PAYMENT'
        CHECK(status IN ('PENDING_PAYMENT','PAID','ASSIGNED','PROCESSING','COMPLETED','REJECTED','REFUNDED')),
      agent TEXT, note TEXT NOT NULL DEFAULT '', delivery_cipher TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_game_code_inventory(
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES es_products(id),
      code_cipher TEXT NOT NULL, code_hash TEXT NOT NULL UNIQUE, code_tail TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK(status IN ('AVAILABLE','USED','REVOKED')),
      created_by TEXT NOT NULL, created_at TEXT NOT NULL, used_order_ref TEXT, used_at TEXT);
    CREATE INDEX IF NOT EXISTS es_game_inventory_available ON es_game_code_inventory(product_id,status,id);

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
    CREATE TABLE IF NOT EXISTS es_ice_code_reveals(
      token_hash TEXT PRIMARY KEY, codes_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_ice_codes(
      id INTEGER PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, code_tail TEXT NOT NULL,
      code_cipher TEXT NOT NULL DEFAULT '', copied_at TEXT,
      points INTEGER NOT NULL CHECK(points>=0), status TEXT NOT NULL DEFAULT 'AVAILABLE'
        CHECK(status IN ('AVAILABLE','REDEEMED','REVOKED')),
      created_by TEXT NOT NULL, used_by TEXT, created_at TEXT NOT NULL, used_at TEXT);
    CREATE INDEX IF NOT EXISTS es_ice_codes_status ON es_ice_codes(status,created_at);
    CREATE TABLE IF NOT EXISTS es_loyalty_members(
      username TEXT PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0 CHECK(points>=0),
      badge TEXT NOT NULL DEFAULT 'ICE_PREMIUM', source TEXT NOT NULL DEFAULT 'ICE_STORY',
      joined_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_loyalty_ledger(
      id INTEGER PRIMARY KEY, username TEXT NOT NULL, reference TEXT UNIQUE NOT NULL,
      points_delta INTEGER NOT NULL, memo TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS es_manager_permissions(
      username TEXT NOT NULL, permission TEXT NOT NULL, granted_by TEXT NOT NULL,
      created_at TEXT NOT NULL, PRIMARY KEY(username,permission));
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

    CREATE TABLE IF NOT EXISTS es_fx_quotes(
      quote_id TEXT PRIMARY KEY, username TEXT NOT NULL,
      direction TEXT NOT NULL CHECK(direction IN ('URC_TO_SDG','SDG_TO_URC')),
      amount INTEGER NOT NULL CHECK(amount>0), rate INTEGER NOT NULL CHECK(rate>0),
      fee INTEGER NOT NULL CHECK(fee>=0), result INTEGER NOT NULL CHECK(result>0),
      expires_at INTEGER NOT NULL, used_at TEXT, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS es_fx_quotes_expiry ON es_fx_quotes(expires_at,used_at);
    """)
    tables={r["name"] for r in db().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "users" in tables:
        user_cols={r["name"] for r in db().execute("PRAGMA table_info(users)").fetchall()}
        if "email" not in user_cols:
            db().execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "phone" not in user_cols:
            db().execute("ALTER TABLE users ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
    ice_cols={r["name"] for r in db().execute("PRAGMA table_info(es_ice_codes)").fetchall()}
    if "code_cipher" not in ice_cols:
        db().execute("ALTER TABLE es_ice_codes ADD COLUMN code_cipher TEXT NOT NULL DEFAULT ''")
    if "copied_at" not in ice_cols:
        db().execute("ALTER TABLE es_ice_codes ADD COLUMN copied_at TEXT")
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
    topup_cols={r["name"] for r in db().execute("PRAGMA table_info(es_topup_orders)").fetchall()}
    if "delivery_cipher" not in topup_cols:
        db().execute("ALTER TABLE es_topup_orders ADD COLUMN delivery_cipher TEXT NOT NULL DEFAULT ''")
    # Game orders deliver redeem codes; no player ID is collected or retained.
    db().execute("UPDATE es_topup_orders SET player_id='' WHERE player_id IS NOT NULL AND player_id!=''")
    db().execute("UPDATE es_products SET instructions='سيتم تسليم كود الباقة داخل التطبيق بعد تأكيد الدفع.' WHERE instructions='أدخل معرف اللاعب بدقة.'")
    defaults = {
      "epc_price_sdg":"0", "epc_price_usdt_micros":"0", "epc_max_supply":"15000000",
      "fx_rate_mode":"manual", "fx_market_rate":"5600", "fx_manual_rate":"5600", "fx_buy_spread_bps":"0", "fx_sell_spread_bps":"0",
      "platform_fee_bps":"300",
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
    # Keep exchange liquidity separate from platform revenue/protected funds.
    conn.execute("UPDATE es_ledger SET account='protection:fund' WHERE account='platform:revenue'")
    conn.execute("UPDATE es_ledger SET account='fx:reserve' WHERE account='platform:fx_pool'")
    # Recover legacy FX reserve movements that an earlier migration placed in
    # protection:fund. Exchange posting line 2 is source-currency liquidity in,
    # line 3 is target-currency liquidity out; line 6 is the separate fee and
    # intentionally remains in protection:fund. Audit references identify the
    # exact exchange transactions, so no balances are estimated.
    conn.execute("""
        UPDATE es_ledger SET account='fx:reserve'
        WHERE account='protection:fund' AND line_no IN (2,3)
          AND reference IN (
            SELECT object_ref FROM es_audit WHERE action='CURRENCY_EXCHANGE'
          )
    """)
    conn.execute("""
        UPDATE es_ledger SET account='fx:reserve'
        WHERE account='protection:fund' AND line_no=2
          AND memo IN (
            'تمويل احتياطي الصرف من محفظة المالك',
            'إيداع احتياطي SDG خارجي مؤكد من المالك'
          )
    """)
    # Separate confirmed physical SDG cash from owner-funded digital FX inventory.
    conn.execute("""
        UPDATE es_ledger SET account='cash:sdg_reserve'
        WHERE account='fx:reserve' AND currency='SDG' AND line_no=2
          AND memo='إيداع احتياطي SDG خارجي مؤكد من المالك'
          AND reference LIKE 'FXEXT%'
    """)
    # Apply the initial 5,600 SDG/URC rate only where no positive rate was configured.
    for key in ("fx_manual_rate","fx_market_rate"):
        current=conn.execute("SELECT value FROM es_settings WHERE key=?",(key,)).fetchone()
        if not current or not str(current["value"]).isdigit() or int(current["value"])<=0:
            conn.execute("INSERT INTO es_settings(key,value) VALUES(?, '5600') ON CONFLICT(key) DO UPDATE SET value='5600'",(key,))
    reconcile_wallet_balances()
    seed_owner_assets()
    seed_game_products()

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

def admin_permission(permission):
    """Founder has full access; managers receive only explicitly granted modules."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            current = user()
            if not current or not current["active"]:
                return redirect("/login")
            if current["role"] == "founder":
                return fn(*args, **kwargs)
            if current["role"] != "manager":
                abort(403)
            allowed = db().execute(
                "SELECT 1 FROM es_manager_permissions WHERE username=? AND permission=?",
                (current["username"], permission)
            ).fetchone()
            if not allowed:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorate

def manager_dashboard_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        current = user()
        if not current or not current["active"]:
            return redirect("/login")
        if current["role"] == "founder":
            return fn(*args, **kwargs)
        if current["role"] != "manager":
            abort(403)
        if not db().execute("SELECT 1 FROM es_manager_permissions WHERE username=? LIMIT 1",(current["username"],)).fetchone():
            abort(403)
        return fn(*args, **kwargs)
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

def platform_fee(amount):
    """Three percent platform fee, rounded up to the smallest whole currency unit."""
    amount=int(amount)
    return (amount*300+9999)//10000 if amount>0 else 0

def notify_protection_income(reference,currency,amount,source):
    """Notify the founder when a real fee or platform margin is posted to the protected fund."""
    amount=int(amount)
    if amount<=0:
        return
    founder=db().execute("SELECT username FROM users WHERE role='founder' ORDER BY id LIMIT 1").fetchone()
    if not founder:
        return
    db().execute(
        "INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
        (founder["username"],"إيراد جديد لصندوق حماية المستخدمين",
         "تم تسجيل "+format(amount,",")+" "+currency+" من "+source+" في صندوق حماية المستخدمين. مرجع العملية: "+reference,
         reference,now())
    )

def ref():
    return secrets.token_hex(8).upper()

def ensure_wallet(username,currency):
    if currency not in ("SDG","URC"): raise ValueError("عملة غير مدعومة")
    db().execute("INSERT OR IGNORE INTO es_wallets(username,currency,balance) VALUES(?,?,0)",(username,currency))

def post(reference, entries, memo):
    """Post balanced signed lines atomically; the double-entry ledger is authoritative."""
    totals={}
    for account,currency,delta in entries:
        if currency not in ("SDG","URC") or not isinstance(delta,int):
            raise ValueError("قيد مالي غير صالح")
        totals[currency]=totals.get(currency,0)+delta
    if any(v!=0 for v in totals.values()): raise ValueError("القيد المحاسبي غير متوازن")
    for line_no,(account,currency,delta) in enumerate(entries, start=1):
        if account.startswith("user:"):
            name=account[5:]
            ensure_wallet(name,currency)
            row=db().execute(
                "SELECT COALESCE(SUM(delta),0) balance FROM es_ledger WHERE account=? AND currency=?",
                (account,currency)
            ).fetchone()
            new=int(row["balance"])+delta
            if new<0: raise ValueError("الرصيد غير كافٍ")
            db().execute("UPDATE es_wallets SET balance=? WHERE username=? AND currency=?",(new,name,currency))
        db().execute("INSERT INTO es_ledger(reference,line_no,account,currency,delta,memo,created_at) VALUES(?,?,?,?,?,?,?)",
                     (reference,line_no,account,currency,delta,memo,now()))

def balance(username,currency):
    if currency not in ("SDG","URC"):
        raise ValueError("عملة المحفظة غير مدعومة")
    row=db().execute(
        "SELECT COALESCE(SUM(delta),0) balance FROM es_ledger WHERE account=? AND currency=?",
        ("user:"+username,currency)
    ).fetchone()
    return int(row["balance"])

def reconcile_wallet_balances():
    """Repair cached wallet totals from ledger history and preserve legacy-only balances once."""
    d=db()
    wallets=d.execute("SELECT username,currency,balance FROM es_wallets").fetchall()
    if not wallets:
        return
    d.execute("BEGIN IMMEDIATE")
    try:
        for wallet in wallets:
            account="user:"+wallet["username"]
            history=d.execute(
                "SELECT COUNT(*) count,COALESCE(SUM(delta),0) total FROM es_ledger WHERE account=? AND currency=?",
                (account,wallet["currency"])
            ).fetchone()
            if int(history["count"])==0 and int(wallet["balance"])>0:
                # Preserve a pre-ledger wallet exactly once; this is a migration, not new issuance.
                legacy_ref="WALLET-OPENING-"+hashlib.sha256(
                    (wallet["username"]+"|"+wallet["currency"]).encode("utf-8")
                ).hexdigest()[:20].upper()
                stamp=now(); amount=int(wallet["balance"]); currency=wallet["currency"]
                d.execute("INSERT OR IGNORE INTO es_ledger(reference,line_no,account,currency,delta,memo,created_at) VALUES(?,1,?,?,?,?,?)",
                          (legacy_ref,account,currency,amount,"ترحيل رصيد محفظة سابق إلى السجل المحاسبي",stamp))
                d.execute("INSERT OR IGNORE INTO es_ledger(reference,line_no,account,currency,delta,memo,created_at) VALUES(?,2,'external:legacy_wallet_opening',?,?,?,?)",
                          (legacy_ref,currency,-amount,"مقابل ترحيل رصيد محفظة سابق",stamp))
                history=d.execute(
                    "SELECT COALESCE(SUM(delta),0) total FROM es_ledger WHERE account=? AND currency=?",
                    (account,currency)
                ).fetchone()
            d.execute("UPDATE es_wallets SET balance=? WHERE username=? AND currency=?",
                      (int(history["total"]),wallet["username"],wallet["currency"]))
        d.commit()
    except Exception:
        d.rollback()
        raise

def effective_fx_rate(direction):
    """Read the current owner-approved rate for every quote and execution."""
    if direction not in ("URC_TO_SDG","SDG_TO_URC"):
        raise ValueError("اتجاه التحويل غير صالح")
    mode=setting("fx_rate_mode","manual")
    if mode not in ("manual","market"):
        raise ValueError("طريقة التسعير غير صالحة")
    base=int(setting("fx_market_rate" if mode=="market" else "fx_manual_rate","5600"))
    buy=int(setting("fx_buy_spread_bps","0"))
    sell=int(setting("fx_sell_spread_bps","0"))
    if base<=0 or not 0<=buy<=10000 or not 0<=sell<=10000:
        raise ValueError("سعر الصرف أو هامش التسعير غير صالح")
    rate=(base*(10000-sell))//10000 if direction=="URC_TO_SDG" else (base*(10000+buy)+9999)//10000
    if rate<=0:
        raise ValueError("سعر الصرف الفعلي يجب أن يكون أكبر من صفر")
    return rate

def fx_reserve_balance(currency):
    if currency not in ("SDG","URC"):
        raise ValueError("عملة احتياطي الصرف غير مدعومة")
    row=db().execute(
        "SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='fx:reserve' AND currency=?",
        (currency,)
    ).fetchone()
    amount=int(row["n"])
    if amount < 0:
        raise ValueError("رصيد احتياطي الصرف غير متوازن؛ أوقف النظام وراجع القيود.")
    return amount

def cash_sdg_balance():
    """Confirmed physical SDG cash available for external withdrawals."""
    row=db().execute(
        "SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='cash:sdg_reserve' AND currency='SDG'"
    ).fetchone()
    amount=int(row["n"])
    if amount<0:
        raise ValueError("رصيد السيولة النقدية للسحب غير متوازن؛ أوقف السحب وراجع القيود.")
    return amount

def pending_sdg_withdrawals():
    row=db().execute(
        "SELECT COALESCE(SUM(amount_sdg),0) n FROM es_withdrawals WHERE status='PENDING'"
    ).fetchone()
    return int(row["n"])

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

def seed_game_products():
    """Install requested packages once; keep them hidden until priced and activated by the founder."""
    catalogue = {
      "PUBG Mobile": ["10 شدات","60 شدة","325 شدة","385 شدة","660 شدة","720 شدة","985 شدة","1800 شدة","3850 شدة","8100 شدة","16200 شدة","16800 شدة","24300 شدة","32400 شدة"],
      "Free Fire": ["110 جوهرة","231 جوهرة","341 جوهرة","583 جوهرة","650 جوهرة","1180 جوهرة","2420 جوهرة","4500 جوهرة"],
      "Mobile Legends": ["278 ماسة","571 ماسة","1192 ماسة","Starlight Member Plus"]
    }
    for game, titles in catalogue.items():
        for title in titles:
            exists=db().execute("SELECT 1 FROM es_products WHERE game=? AND title=? LIMIT 1",(game,title)).fetchone()
            if exists:
                continue
            code="CAT-"+secrets.token_hex(8).upper()
            db().execute("""INSERT INTO es_products
              (code,game,title,instructions,price_sdg,cost_sdg,active,created_at)
              VALUES(?,?,?,'سيتم تسليم كود الباقة بعد تأكيد الدفع.',1,0,0,?)""",
              (code,game,title,now()))

def remaining_epc_supply():
    maximum=int(setting("epc_max_supply","15000000"))
    if maximum<=0:
        raise ValueError("يجب على المالك تحديد الحد الأقصى لإصدار URC قبل البيع")
    issued=-int(db().execute(
        "SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='issuer:urc' AND currency='URC'"
    ).fetchone()["n"])
    burned=int(db().execute(
        "SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='burn:urc' AND currency='URC'"
    ).fetchone()["n"])
    reserved=int(db().execute(
        "SELECT COALESCE(SUM(quantity_epc),0) n FROM es_payments WHERE purpose='EPC_PURCHASE' AND status='PENDING'"
    ).fetchone()["n"])
    return max(0, maximum-max(0,issued-burned)-reserved)

def assert_epc_capacity(quantity):
    if int(quantity)<=0:
        raise ValueError("كمية URC غير صالحة")
    if int(quantity)>remaining_epc_supply():
        raise ValueError("الكمية تتجاوز المعروض المتاح من URC")

def encryption():
    key=os.getenv("EMPIRE_DATA_ENCRYPTION_KEY","").strip()
    if key:
        try:
            return Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("EMPIRE_DATA_ENCRYPTION_KEY is invalid; configure a valid persistent Fernet key") from exc
    # Fallback for managed hosts missing the dedicated secret. Keep FLASK_SECRET_KEY stable.
    from flask import current_app
    app_secret=current_app.secret_key
    if not app_secret:
        raise RuntimeError("A persistent FLASK_SECRET_KEY is required for encrypted data")
    current_app.logger.warning("EMPIRE_DATA_ENCRYPTION_KEY is unset; using a key derived from FLASK_SECRET_KEY. Configure a dedicated persistent key.")
    material=hashlib.sha256(("empire-estory-data-encryption-v1:"+str(app_secret)).encode("utf-8")).digest()
    import base64
    return Fernet(base64.urlsafe_b64encode(material))

def page(title,body,**ctx):
    shell="""<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>
    <meta name='theme-color' content='#f4f8fb'><link rel='manifest' href='/store/manifest.webmanifest'><link rel='apple-touch-icon' href='/store/icon.svg?v=3'>
    <meta name='description' content='الإمبراطورية إستوري — وجهتك لشحن الألعاب والتجارة الرقمية.'>
    <title>{{title}} | الإمبراطورية إستوري</title>
    
    <style>
:root{color-scheme:light;--ink:#18364b;--muted:#718496;--blue:#0876b9;--green:#16834f;--line:#e3ebf1}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}html{background:#f4f8fb;scroll-behavior:auto}
body{margin:0;min-height:100vh;padding-bottom:24px;background:#f4f8fb!important;color:var(--ink)!important;font:15px/1.55 system-ui,-apple-system,"Segoe UI",Tahoma,Arial,sans-serif}
a{color:var(--blue);text-decoration:none}header.app-header{position:sticky;top:0;z-index:50;height:66px;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 13px!important;background:rgba(255,255,255,.97)!important;border-bottom:1px solid var(--line);backdrop-filter:blur(14px)}
.header-brand{flex:1;min-width:0;text-align:center}.header-brand .brand{font-size:17px!important;font-weight:950;color:#153d5b!important;white-space:nowrap}.header-brand .brand:before{display:none!important}.header-brand small{display:block;margin:0!important;font-size:9px!important;color:#7890a1!important}
.header-menu,.header-alert,.header-avatar{display:grid;place-items:center;flex:0 0 40px;width:40px;height:40px;border-radius:14px;background:#edf5fa;color:#075b91!important;font-size:0;font-weight:900}.header-menu svg,.header-alert svg,.header-avatar svg{width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}.header-alert{background:#fff0ef;color:#c34245!important}.header-avatar{background:#eaf6ef;color:#16834f!important}
main{width:100%;max-width:520px!important;margin:12px auto 22px!important;padding:0 13px!important}.nav{display:none!important}
h1,h2,h3{color:#183d56!important;line-height:1.3;margin-top:0}h1{font-size:23px}h2{font-size:18px}h3{font-size:16px}p{color:#5e7485}small,.muted{color:var(--muted)}
.panel{margin:12px 0!important;padding:16px!important;border:1px solid var(--line)!important;border-radius:18px!important;background:#fff!important;color:var(--ink)!important;box-shadow:0 3px 12px rgba(24,60,85,.045)!important}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.grid>.panel,.grid article{min-width:0;margin:0}
.btn,button{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:10px 16px;border:0;border-radius:12px;background:var(--blue)!important;color:#fff!important;font:750 14px system-ui,sans-serif;box-shadow:none!important;cursor:pointer}
.btn.secondary{background:#edf4f8!important;color:#175d85!important;border:1px solid #dce8ef}
input,textarea,select{display:block;width:100%;min-height:46px;margin:7px 0 14px;padding:12px 13px;border:1px solid #d8e4ec;border-radius:12px;background:#f8fbfd;color:#18364b;font:inherit}textarea{min-height:100px;resize:vertical}label{display:block;color:#36576c;font-size:13px;font-weight:750}
.tag{display:inline-flex;padding:4px 10px;border:1px solid #cde8d7;border-radius:30px;background:#eaf6ef;color:#137344;font-size:11px;font-weight:800}
.metric{padding:17px;border:1px solid var(--line);border-radius:16px;background:#f5f9fc}.metric strong{display:block;color:#155f99;font-size:25px;font-weight:900}
.wallet-top{position:relative;isolation:isolate;display:grid!important;grid-template-columns:54px minmax(0,1fr) 42px;align-items:center;gap:13px;min-height:174px;margin:2px 0 22px!important;padding:24px 18px 30px!important;overflow:hidden;border:1px solid rgba(255,255,255,.22)!important;border-radius:27px!important;background:linear-gradient(125deg,#073b69 0%,#086da5 56%,#13845c 100%)!important;color:#fff;box-shadow:0 15px 34px rgba(5,63,103,.20)!important}
.wallet-top:after{content:"";position:absolute;left:-70px;top:-90px;width:190px;height:190px;border:1px solid rgba(255,255,255,.14);border-radius:50%;box-shadow:0 0 0 22px rgba(255,255,255,.035),0 0 0 44px rgba(255,255,255,.025);pointer-events:none}
.wallet-card-icon{position:relative;z-index:1;display:grid;place-items:center;width:54px;height:54px;border:1px solid rgba(255,255,255,.18);border-radius:18px;background:rgba(255,255,255,.15);color:#fff}.wallet-card-icon svg{width:29px;height:29px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.wallet-balance{position:relative;z-index:1;min-width:0}.wallet-top .eyebrow{margin-bottom:5px;color:#e3f3fc!important;font-size:12px;font-weight:800}.wallet-top .amount{color:#fff!important;font-size:clamp(24px,6.4vw,34px)!important;font-weight:950!important;line-height:1.2;direction:ltr;text-align:right;white-space:nowrap;letter-spacing:.2px;font-variant-numeric:tabular-nums}.wallet-top .amount span{color:#ffda68!important;font-size:13px}.wallet-top .currency{margin-top:5px;color:#d7ecf8!important;font-size:10px}
.balance-toggle{position:relative;z-index:3;display:grid!important;place-items:center;width:42px!important;min-width:42px!important;min-height:42px!important;height:42px;padding:0!important;border:1px solid rgba(255,255,255,.28)!important;border-radius:15px!important;background:rgba(255,255,255,.16)!important;color:#fff!important}.balance-toggle svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.wallet-secure{position:absolute;z-index:1;bottom:11px;left:17px;display:flex;align-items:center;gap:6px;color:#e5f8ed}.wallet-secure span{display:grid;place-items:center;width:16px;height:16px;border-radius:50%;background:#19a15c;color:#fff;font-size:10px}.wallet-secure small{color:#e5f8ed!important;font-size:9px}
.home-services{margin:0 0 14px}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 2px 12px}.section-heading h2{margin:2px 0 0;font-size:17px!important}.section-kicker{color:var(--green);font-size:10px;font-weight:900}.sudan-mark{font-size:23px}
.service-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px}.service-grid .service-card{display:flex;min-width:0;min-height:116px!important;flex-direction:column;align-items:center!important;justify-content:center!important;gap:10px!important;padding:13px 6px!important;border:1px solid #e4edf3!important;border-radius:20px!important;background:#fff!important;box-shadow:0 5px 16px rgba(24,60,85,.055)!important;text-align:center;color:#183d56!important;transition:transform .16s ease,box-shadow .16s ease}.service-grid .service-card:active{transform:scale(.97);box-shadow:0 2px 7px rgba(24,60,85,.08)!important}
.service-grid .service-icon{display:grid;place-items:center;width:51px!important;height:51px!important;margin:0;border-radius:17px!important;background:#eaf4fb;color:#0875b9;font-size:0!important;font-style:normal}.service-grid .service-icon svg{display:block;width:25px;height:25px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}.service-grid .service-card strong{display:block;max-width:100%;color:#183d56!important;font-size:11px!important;font-weight:850;line-height:1.4!important;overflow-wrap:anywhere}
.service-icon.store{background:#e6f5fc!important;color:#0878b9}.service-icon.accounts{background:#e6f6ed!important;color:#16854e}.service-icon.transfer{background:#e8f0ff!important;color:#1765cf}.service-icon.coins{background:#fff3d9!important;color:#bd7b08}.service-icon.funding{background:#e7f6ef!important;color:#16854e}.service-icon.profile{background:#eaf1ff!important;color:#245eb5}.service-icon.games{background:#f0eaff!important;color:#7445bd}.service-icon.exchange{background:#e5f5f5!important;color:#087f80}.service-icon.withdraw{background:#fff0ec!important;color:#c25a38}.service-icon.agents{background:#edf3f8!important;color:#43647e}
.national-banner{position:relative;display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:94px;margin:14px 0;padding:14px 16px;overflow:hidden;border:1px solid #dceaf3;border-radius:19px;background:linear-gradient(110deg,#e8f5ff,#fff 48%,#e8f7ee)}
.banner-copy{position:relative;z-index:2;display:flex;flex-direction:column;gap:4px}.banner-copy span{color:#123f71;font-size:16px;font-weight:950}.banner-copy strong{color:#39718b;font-size:10px}
.banner-flag{position:relative;display:flex;width:48px;height:32px;flex-direction:column;overflow:hidden;border-radius:4px;transform:rotate(-7deg);box-shadow:0 4px 10px rgba(20,45,65,.12)}.banner-flag i{flex:1}.banner-flag i:nth-child(1){background:#d92732}.banner-flag i:nth-child(2){background:#fff}.banner-flag i:nth-child(3){background:#111}.banner-flag b{position:absolute;left:0;top:0;width:0;height:0;border-top:16px solid transparent;border-bottom:16px solid transparent;border-left:22px solid #16864b}.banner-crown{position:absolute;left:28%;bottom:-20px;color:rgba(8,103,161,.07);font-size:80px;pointer-events:none}
.recent-panel{margin-top:12px!important}.view-all{font-size:11px;font-weight:850;white-space:nowrap}.transaction-row{display:flex;align-items:center;gap:9px;padding:11px 0;border-bottom:1px solid #edf1f5}.transaction-row:last-child{border-bottom:0}.transaction-symbol{display:grid;place-items:center;flex:0 0 38px;height:38px;border-radius:50%;background:#e6f6ed;color:#13864e;font-size:21px;font-weight:900}.transaction-info{display:flex;flex:1;min-width:0;flex-direction:column;gap:3px}.transaction-info b{overflow:hidden;color:#1b3e56;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.transaction-info small{font-size:9px}.transaction-value{font-size:10px;white-space:nowrap}.transaction-value.in{color:#13864e}.transaction-value.out{color:#c04b4b}
.app-bottom{position:sticky;z-index:60;right:0;bottom:0;left:0;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:3px;margin:16px 0 0;padding:8px 8px calc(10px + env(safe-area-inset-bottom,0px));border-top:1px solid #e0eaf1;background:rgba(255,255,255,.97);box-shadow:0 -5px 20px rgba(20,60,85,.08);backdrop-filter:blur(14px);padding-left:max(8px,env(safe-area-inset-left,0px));padding-right:max(8px,env(safe-area-inset-right,0px))}
.app-bottom a{display:flex;min-width:0;min-height:54px;flex-direction:column;align-items:center;justify-content:center;gap:4px;padding:4px 1px;color:#718496!important;font-size:10px;font-weight:850;line-height:1.1}.app-bottom a .ico{display:grid;place-items:center;width:26px;height:25px;font-size:0;line-height:1}.app-bottom a .ico svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}.app-bottom a.active{color:#0875b9!important}.app-bottom a.active .ico{background:#e9f4fb;border-radius:9px;width:34px;height:29px}.app-bottom a:focus-visible{outline:2px solid #0875b9;outline-offset:2px;border-radius:10px}
.admin-section{margin:16px 0 20px;padding:15px;border:1px solid #e1eaf0;border-radius:20px;background:linear-gradient(150deg,#fff,#f7fafc);box-shadow:0 5px 18px rgba(20,60,85,.04)}.admin-section-head{margin:0 2px 12px;padding-bottom:10px;border-bottom:1px solid #e8eef3}.admin-section-head span{color:#16834f;font-size:10px;font-weight:900;letter-spacing:.4px}.admin-section-head h2{margin:4px 0 0;font-size:17px}.admin-links a{display:flex;min-width:0;min-height:76px;flex-direction:column;justify-content:center;gap:5px;padding:13px;border:1px solid #e1ebf1;border-radius:15px;background:linear-gradient(145deg,#fff,#f7fbfd);color:#164d70!important;font-weight:850;transition:transform .16s ease,border-color .16s ease}.admin-links a:active{transform:scale(.985)}.admin-links a small{font-size:11px;color:#16834f}.admin-filters{display:flex;gap:7px;overflow:auto;padding:5px 0 12px}.admin-filters a{flex:0 0 auto;padding:7px 11px;border:1px solid #dce8ef;border-radius:30px;background:#f7fafc;color:#175d85;font-size:11px;font-weight:800}.order-card{padding:15px!important}.order-head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}.order-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.order-grid>div{min-width:0;padding:9px;border-radius:11px;background:#f7fafc}.order-grid small{display:block;color:#718496;font-size:10px}.order-grid b{display:block;overflow-wrap:anywhere;font-size:12px;color:#183d56}@media(max-width:380px){.order-grid{grid-template-columns:1fr}}\n.transfer-step{padding:20px!important}.transfer-step>p{line-height:1.8}.transfer-step form{margin-top:18px}.recipient-confirm{display:grid;gap:6px;margin:16px 0;padding:17px;border:1px solid #dcebe3;border-radius:17px;background:linear-gradient(135deg,#f1faf5,#f3f8fc)}.recipient-confirm small{color:#678}.recipient-confirm strong{font-size:20px;color:#173e58}.recipient-confirm span{font-weight:850;letter-spacing:1px}.recipient-confirm em{justify-self:start;padding:4px 10px;border-radius:20px;font-size:11px;font-style:normal;font-weight:800}.recipient-confirm .verified{background:#e1f5e9;color:#137343}.recipient-confirm .unverified{background:#fff3df;color:#98640d}.transfer-submit{width:100%;min-height:50px;margin:10px 0 4px;background:linear-gradient(110deg,#0876b9,#16834f)!important;box-shadow:0 7px 16px rgba(8,118,185,.16)!important}.fulfill-code-form{margin-top:14px;padding:14px;border:1px solid #dcebe3;border-radius:15px;background:#f5fbf7}.fulfill-code-form button{width:100%}.empty-state{padding:20px;border:1px dashed #dce6ed;border-radius:15px;color:#718496;text-align:center}.flash{padding:12px 14px;border:1px solid #cce7d6;border-radius:12px;background:#eff9f2;color:#17633d}
/* Final owner-console and mobile refinements: retain every existing admin action. */
.admin-section{position:relative;overflow:hidden;padding:17px;margin:18px 0;border:1px solid #e2e6ef;border-radius:22px;background:linear-gradient(145deg,#fff 0%,#f8f6fc 100%);box-shadow:0 9px 26px rgba(43,30,75,.055)}
.admin-section:before{content:"";position:absolute;inset:0 0 auto;height:3px;background:linear-gradient(90deg,#6b48aa,#d5b46c,#16834f)}
.admin-section-head{display:flex;align-items:center;gap:11px;margin:0 0 14px;padding:0 0 12px;border-bottom:1px solid #ece6f3}
.admin-section-head span{display:grid;place-items:center;flex:0 0 39px;width:39px;height:39px;border-radius:13px;background:#eee8f7;color:#604094;font-size:10px;font-weight:950;letter-spacing:0}
.admin-section-head h2{margin:0;font-size:17px;font-weight:950;color:#2d2048!important}
.admin-links{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.admin-links a{min-height:88px;padding:13px;border:1px solid #e8e1ef;border-radius:16px;background:linear-gradient(145deg,#fff,#faf8fd);color:#352452!important;box-shadow:0 3px 10px rgba(40,25,65,.035)}
.admin-links a b{font-size:13px;line-height:1.5}.admin-links a small{color:#7a6a91;font-size:10px;line-height:1.5}.admin-links a{position:relative;z-index:2;pointer-events:auto;cursor:pointer}.admin-section:before{pointer-events:none}
@media(max-width:360px){.admin-section{padding:13px;margin:13px 0;border-radius:17px}.admin-links{grid-template-columns:1fr}.admin-links a{min-height:68px}}
.transfer-step{border:1px solid #e5e0ee!important;box-shadow:0 12px 34px rgba(36,24,61,.07)!important}.transfer-submit{border-radius:14px!important}
footer{display:none!important}.receipt-card,.transfer-receipt{max-width:520px;margin:18px auto!important;border-top:5px solid #16844f!important;background:#fff!important}.receipt-flag{display:flex;height:7px;margin-bottom:14px;overflow:hidden;border-radius:20px}.receipt-flag i{flex:1}.receipt-flag i:nth-child(1){background:#d52e36}.receipt-flag i:nth-child(2){background:#fff;border:1px solid #ddd}.receipt-flag i:nth-child(3){background:#111}.receipt-flag i:nth-child(4){background:#168447}
.game-intro{background:linear-gradient(145deg,#fff,#f1f8fc)!important}.game-products{align-items:stretch}.game-product{display:flex;flex-direction:column;min-width:0}.game-price{display:block;margin:2px 0 12px;color:#146e49;font-size:20px}.game-buy{width:100%;margin-top:auto}.redeem-code{display:grid;gap:8px;margin:12px 0;padding:14px;border:1px dashed #16834f;border-radius:15px;background:#eff9f3}.redeem-code small{color:#17633d;font-weight:800}.redeem-code strong{padding:10px;border-radius:10px;background:#fff;color:#153d56;font-size:19px;overflow-wrap:anywhere;text-align:center;letter-spacing:.5px}.game-order-card{border-right:4px solid #16834f!important}.game-catalog-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:16px 0}.game-catalog-card{display:flex;min-height:116px;flex-direction:column;align-items:center;justify-content:center;gap:7px;padding:14px 8px;border:1px solid var(--line);border-radius:18px;background:#f8fbfd;color:#18364b!important;text-align:center}.game-catalog-card .game-catalog-icon{display:grid;place-items:center;width:48px;height:48px;border-radius:15px;background:#e8f2fa;color:#0875b9}.game-catalog-card svg{width:27px;height:27px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.game-catalog-card small{font-size:10px}.game-catalog-card.pubg .game-catalog-icon{background:#fff1db;color:#a66b17}.game-catalog-card.ml .game-catalog-icon{background:#e9efff;color:#3d62b4}.game-catalog-card.freefire .game-catalog-icon{background:#fff0e8;color:#d65b24}.game-catalog-card.fortnite .game-catalog-icon{background:#e9f5ef;color:#188253}.profile-hero{display:flex;align-items:center;gap:10px}.profile-hero h1{margin:0}.profile-hero p{margin:0}.profile-mark{display:grid;place-items:center;width:48px;height:48px;border-radius:15px;background:#eaf4ef;font-size:26px}.profile-info{display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}.profile-info span{color:var(--muted)}.profile-setting{display:grid;grid-template-columns:38px 1fr auto 16px;align-items:center;gap:8px;padding:13px 0;color:var(--ink)!important}.profile-setting span{font-size:22px}.profile-setting small{font-size:11px}.profile-setting strong{font-size:22px;color:var(--muted)}
.table-wrap{max-width:100%;overflow-x:auto}
@media(min-width:420px){main{padding-right:16px!important;padding-left:16px!important}.service-grid{grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.service-grid .service-card{min-height:112px!important}.service-grid .service-icon{width:49px!important;height:49px!important}}
@media(min-width:620px){main{max-width:760px!important}.service-grid{grid-template-columns:repeat(5,minmax(0,1fr))}.app-bottom{right:auto;left:auto;width:100%;transform:none;border:1px solid #dce8f1;border-bottom:0;border-radius:17px 17px 0 0}}
@media(max-width:360px){main{padding:0 10px!important}.wallet-top{grid-template-columns:44px minmax(0,1fr) 38px;gap:8px;padding:20px 11px 30px!important}.wallet-card-icon{width:44px;height:44px}.wallet-top .amount{font-size:22px!important}.service-grid{gap:8px}.service-grid .service-card{min-height:108px!important}.service-grid .service-icon{width:46px!important;height:46px!important}.app-bottom{gap:0;padding-left:3px;padding-right:3px}.app-bottom a{font-size:9px;min-height:52px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
@media print{body{background:#fff!important;padding:0}.app-header,.app-bottom,.nav,footer{display:none!important}.panel{box-shadow:none!important}}

/* Sudan digital-finance visual system and small-screen layout safeguards */
:root{color-scheme:light;--ink:#183b2c;--muted:#718477;--blue:#087a50;--green:#16834f;--line:#dfeae2;--gold:#c7a45a;--surface:#fff;--canvas:#f3f7f3}
html,body{max-width:100%;overflow-x:hidden;background:var(--canvas)!important;color:var(--ink)!important}
body{padding-bottom:calc(28px + env(safe-area-inset-bottom));font-family:system-ui,-apple-system,"Segoe UI",Tahoma,Arial,sans-serif}
header.app-header{background:rgba(255,255,255,.97)!important;border-bottom:1px solid #dce8df}
.header-brand .brand{color:#123e2c!important}
main{width:100%;max-width:680px!important;padding:0 clamp(10px,3.5vw,20px)!important;margin:14px auto 30px!important}
.panel,.metric,.admin-section,.admin-links a,.service-card{min-width:0;max-width:100%;overflow-wrap:anywhere}
.panel{border-color:#dfe9e2!important;box-shadow:0 5px 18px rgba(25,66,43,.045)!important}
h1,h2,h3{color:#17432f!important;overflow-wrap:anywhere}
p{overflow-wrap:anywhere}
.btn,button{max-width:100%;white-space:normal;line-height:1.35;background:linear-gradient(115deg,#08764b,#15945e)!important}
.btn.secondary{background:#edf5ef!important;color:#175b3c!important;border-color:#d8e8dc!important}
input,textarea,select{min-width:0;max-width:100%;border-color:#d6e4da;background:#fbfdfb}
input:focus,textarea:focus,select:focus{outline:3px solid rgba(20,132,78,.13);border-color:#16834f}
.tag{border-color:#d8c18a;background:#fbf5e7;color:#795b1e}
.wallet-top{background:linear-gradient(128deg,#073d2b 0%,#08744c 57%,#b38b3e 140%)!important}
.service-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.service-grid .service-card{min-height:108px!important;border-color:#e0eae2!important;box-shadow:0 5px 15px rgba(18,62,39,.055)!important}
.service-grid .service-icon{background:#eaf4ec;color:#147247}
.service-icon.store,.service-icon.accounts,.service-icon.transfer,.service-icon.coins,.service-icon.funding,.service-icon.profile,.service-icon.games,.service-icon.exchange,.service-icon.withdraw,.service-icon.agents{background:#eaf4ec!important;color:#147247!important}
.national-banner{border-color:#d7e5d8;background:linear-gradient(110deg,#e4f2e7,#fffdf7 54%,#f7f0dc)}
.admin-section{border-color:#dce8de;background:linear-gradient(145deg,#fff,#f7faf7)}
.admin-section:before{background:linear-gradient(90deg,#08764b,#d1b16a,#08764b)}
.admin-section-head span{background:#eaf3e9;color:#17613f}
.admin-section-head h2{color:#17432f!important}
.admin-links{grid-template-columns:repeat(2,minmax(0,1fr))}
.admin-links a{display:flex;flex-direction:column;justify-content:center;gap:6px;border-color:#e0e9e1;background:#fff;color:#17432f!important}
.transfer-step,.recipient-confirm{border-color:#dce9df!important}
.transfer-submit{background:linear-gradient(110deg,#08764b,#c39d4f)!important}
.ice-hero{position:relative;overflow:hidden;padding:clamp(18px,5vw,28px);border-radius:24px;background:radial-gradient(circle at 90% 8%,rgba(255,255,255,.35),transparent 28%),linear-gradient(135deg,#0b3550,#176c86 55%,#d4eaf0);color:#fff;box-shadow:0 14px 30px rgba(11,66,85,.15)}
.ice-hero h1,.ice-hero p{color:#fff!important}.ice-brand{color:#e8f8ff;font-weight:900;font-size:12px}
.empire-slogans{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:18px}
.empire-slogans article{min-width:0;padding:13px;border:1px solid rgba(255,255,255,.25);border-radius:15px;background:rgba(255,255,255,.12);backdrop-filter:blur(8px)}
.empire-slogans b,.empire-slogans strong{display:block;color:#fff}.empire-slogans b{font-size:12px}.empire-slogans strong{margin-top:5px;font-size:14px}
.premium-banner{margin:13px 0;padding:17px;border:1px solid #cfe4d8;border-radius:19px;background:linear-gradient(145deg,#f2faf5,#fffaf0)}
.ice-badge{display:inline-flex;align-items:center;gap:7px;padding:7px 12px;border:1px solid #a9d9e9;border-radius:30px;background:linear-gradient(110deg,#e7f8ff,#fff);color:#176d91;font-weight:950;box-shadow:0 4px 12px rgba(34,125,157,.12)}
.loyalty-points{display:block;color:#16724a;font-size:34px;font-weight:950;line-height:1.2}
.premium-list{display:grid;gap:8px}.premium-customer{display:flex;align-items:center;gap:9px;min-width:0;padding:10px;border:1px solid #e3eee6;border-radius:13px;background:#fbfdfb}
.premium-customer b{flex:1;min-width:0;overflow-wrap:anywhere}.ice-mini{display:grid;place-items:center;flex:0 0 32px;width:32px;height:32px;border-radius:11px;background:#e6f7fb;color:#2484a3}
.loyalty-service .service-icon{background:linear-gradient(145deg,#e5f8ff,#f7fdff)!important;color:#2a8aa9!important;border:1px solid #b9e0eb}
.ice-code-record{display:grid;gap:10px}.ice-code-copy{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:8px;padding:10px;border:1px solid #e2eee5;border-radius:13px;background:#f8fcf9}.ice-code-copy code{min-width:0;overflow-wrap:anywhere;font-size:12px;font-weight:900;color:#17432f}.ice-code-copy button{width:auto;min-height:38px;padding:7px 11px}.ice-code-copy small{grid-column:1/-1;color:#16834f;font-weight:800}
.history-strip{display:flex;align-items:center;gap:10px;margin:9px 0;padding:12px;border:1px solid #e0eae2;border-radius:14px;background:#fbfdfb;color:var(--ink)!important}.history-mark{display:grid;place-items:center;flex:0 0 36px;height:36px;border-radius:12px;background:#e8f5ec;color:#16834f;font-size:19px;font-weight:900}.history-main{display:flex;flex:1;min-width:0;flex-direction:column;gap:3px}.history-main b{font-size:12px;overflow-wrap:anywhere}.history-main small{font-size:9px;overflow-wrap:anywhere}.history-strip>strong{font-size:11px;white-space:nowrap;color:#176b45}.history-detail-hero{display:grid;gap:6px;margin:14px 0;padding:15px;border-radius:15px;background:#f0f7f2}.history-detail-hero b{overflow-wrap:anywhere;font-size:13px}
.permission-choice{display:flex;align-items:center;gap:9px;margin:8px 0;padding:9px;border:1px solid #e3ece5;border-radius:11px;background:#fbfdfb}
.permission-choice input{width:18px;min-height:18px;height:18px;margin:0;flex:0 0 18px;accent-color:#16834f}
.code-line{padding:9px 12px;border:1px dashed #9bcbb0;border-radius:10px;background:#fff;font-weight:900;letter-spacing:1px}
@media(max-width:420px){
  main{padding:0 10px!important;margin-top:10px!important}
  header.app-header{height:auto;min-height:58px;padding:7px 9px!important;gap:7px}
  .header-menu,.header-alert,.header-avatar{flex-basis:36px;width:36px;height:36px}
  .header-brand .brand{font-size:15px!important;white-space:normal;line-height:1.2}
  .header-brand small{font-size:8px!important}
  .wallet-top{grid-template-columns:42px minmax(0,1fr) 36px!important;gap:8px!important;padding:18px 12px 29px!important;min-height:150px!important;border-radius:21px!important}
  .wallet-card-icon{width:42px;height:42px;border-radius:13px}
  .balance-toggle{width:36px!important;min-width:36px!important;height:36px!important;min-height:36px!important}
  .wallet-top .amount{font-size:clamp(20px,6vw,28px)!important;white-space:normal;overflow-wrap:anywhere}
  .service-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
  .service-grid .service-card{min-height:96px!important;padding:10px 6px!important;border-radius:16px!important}
  .service-grid .service-icon{width:43px!important;height:43px!important;border-radius:14px!important}
  .grid,.admin-links{grid-template-columns:repeat(2,minmax(0,1fr))}
  .admin-section{padding:13px!important;margin:13px 0!important}
  .admin-section-head{align-items:flex-start}
  .admin-section-head h2{font-size:15px!important}
  .admin-links a{min-height:78px;padding:10px!important}
  .empire-slogans{grid-template-columns:1fr}
  .panel{padding:13px!important;border-radius:16px!important}
  .btn,button{width:100%;min-height:46px}
  form .btn,form button{width:100%}
  .premium-customer{flex-wrap:wrap}
}
@media(max-width:340px){
  .grid,.admin-links{grid-template-columns:1fr}
  .wallet-top{grid-template-columns:36px minmax(0,1fr) 34px!important}
  .wallet-card-icon{width:36px;height:36px}
}
@media(min-width:700px){
  main{max-width:920px!important}
  .service-grid{grid-template-columns:repeat(4,minmax(0,1fr))}
}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
/* Transaction receipt and notification typography: strict RTL layout, no overlap. */
.transfer-receipt-clean{max-width:560px;margin:12px auto;padding:clamp(16px,4vw,24px);border:1px solid #dce9df;border-radius:22px;background:#fff;box-shadow:0 12px 34px rgba(24,65,42,.08);direction:rtl;overflow:hidden}
.trc-header{text-align:center;padding:6px 4px 16px}
.trc-check{display:grid;place-items:center;width:54px;height:54px;margin:0 auto 10px;border-radius:50%;background:#e8f6ed;color:#137a48;font-size:30px;font-weight:900}
.trc-label{display:inline-flex;padding:5px 12px;border-radius:30px;background:#f2f7f3;color:#387052;font-size:11px;font-weight:850}
.trc-header h1{margin:10px 0 5px;font-size:22px;line-height:1.4}
.trc-header p{margin:0;font-size:12px;line-height:1.8;color:#718477}
.trc-amount{display:grid;gap:7px;justify-items:center;margin:8px 0 16px;padding:20px 12px;border:1px solid #d9ecdf;border-radius:17px;background:#f3faf5;text-align:center}
.trc-amount small,.trc-person small,.trc-field small,.trc-meta small{color:#718477;font-size:11px;font-weight:750}
.trc-amount strong{display:block;max-width:100%;color:#145c3a;font-size:clamp(25px,7vw,34px);font-weight:950;line-height:1.35;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.trc-amount strong span{font-size:15px}
.trc-status{padding:4px 11px;border-radius:30px;background:#e4f4e9;color:#176e43;font-size:11px;font-weight:850}
.trc-parties{display:grid;grid-template-columns:minmax(0,1fr) 28px minmax(0,1fr);align-items:center;gap:8px;margin:12px 0}
.trc-person{display:grid;gap:5px;min-width:0;padding:12px;border:1px solid #e5ede7;border-radius:14px;background:#fbfdfb}
.trc-person b,.trc-person span{min-width:0;overflow-wrap:anywhere}
.trc-person b{font-size:13px;line-height:1.55}
.trc-person span{font-size:11px;color:#54705f;font-variant-numeric:tabular-nums}
.trc-arrow{display:grid;place-items:center;color:#16834f;font-size:20px}
.trc-field,.trc-meta>div{display:grid;gap:5px;min-width:0;padding:12px 0;border-bottom:1px solid #edf2ee}
.trc-field b,.trc-meta b{min-width:0;overflow-wrap:anywhere;line-height:1.6;font-size:12px}
.trc-meta{display:grid;gap:2px;margin-top:8px}
.trc-meta b[dir=ltr]{text-align:left;unicode-bidi:plaintext;font-variant-numeric:tabular-nums}
.trc-foot{margin:14px 0;color:#718477;font-size:11px;line-height:1.8}
.trc-actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:14px}
.trc-actions>*{width:100%;min-width:0;min-height:44px;padding:9px 7px;font-size:11px;text-align:center}
.notification-page{display:grid;gap:9px}
.notification-card{display:flex;align-items:center;gap:10px;min-width:0;padding:13px;border:1px solid #e0eae2;border-radius:15px;background:#fbfdfb;color:#183b2c!important;text-decoration:none!important}
.notification-card:active{background:#f0f7f1}
.notification-symbol{display:grid;place-items:center;flex:0 0 36px;width:36px;height:36px;border-radius:12px;background:#e7f5eb;color:#16834f;font-weight:900}
.notification-copy{display:grid;flex:1;min-width:0;gap:4px}
.notification-copy b,.notification-copy span,.notification-copy small{min-width:0;overflow-wrap:anywhere;line-height:1.6}
.notification-copy b{font-size:13px}.notification-copy span{font-size:12px;color:#506c5b}.notification-copy small{font-size:10px;color:#819187}
.notification-arrow{flex:0 0 auto;color:#16834f;font-size:18px}
.admin-open{display:block;margin-top:5px;color:#16834f;font-size:11px;font-weight:850;pointer-events:none}
.admin-links a{display:flex!important;position:relative;z-index:3;min-width:0;gap:7px;cursor:pointer;pointer-events:auto;touch-action:manipulation;text-decoration:none!important}
.admin-links a>*{pointer-events:none}
.admin-links a:focus-visible{outline:3px solid rgba(22,131,79,.28);outline-offset:3px}
@media(max-width:420px){.trc-parties{grid-template-columns:1fr;gap:7px}.trc-arrow{transform:rotate(-90deg)}.trc-actions{grid-template-columns:1fr}.trc-actions>*{min-height:46px;font-size:13px}}
@media print{.trc-actions{display:none!important}.transfer-receipt-clean{max-width:none;border:0;box-shadow:none}}
</style></head><body><svg xmlns='http://www.w3.org/2000/svg' aria-hidden='true' style='position:absolute;width:0;height:0;overflow:hidden'><symbol id='i-wallet' viewBox='0 0 24 24'><rect x='3' y='5' width='18' height='15' rx='3'/><path d='M3 9h18M16 14h2'/><path d='M6 5V4a2 2 0 0 1 2-2h10'/></symbol><symbol id='i-home' viewBox='0 0 24 24'><path d='m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-6v-7h-4v7H4a1 1 0 0 1-1-1z'/></symbol><symbol id='i-store' viewBox='0 0 24 24'><path d='M3 9h18l-1.5-5h-15zM5 9v11h14V9M9 20v-6h6v6'/><path d='M3 9a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0'/></symbol><symbol id='i-transfer' viewBox='0 0 24 24'><path d='M4 7h15m-4-4 4 4-4 4M20 17H5m4-4-4 4 4 4'/></symbol><symbol id='i-coins' viewBox='0 0 24 24'><ellipse cx='9' cy='6' rx='6' ry='3'/><path d='M3 6v5c0 1.7 2.7 3 6 3s6-1.3 6-3V6M3 11v5c0 1.7 2.7 3 6 3 1.2 0 2.3-.2 3.3-.5M15 13c3.3 0 6 1.3 6 3s-2.7 3-6 3-6-1.3-6-3 2.7-3 6-3z'/></symbol><symbol id='i-user' viewBox='0 0 24 24'><circle cx='12' cy='8' r='4'/><path d='M4 21a8 8 0 0 1 16 0'/></symbol><symbol id='i-bell' viewBox='0 0 24 24'><path d='M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4'/></symbol><symbol id='i-game' viewBox='0 0 24 24'><path d='M6 8h12a4 4 0 0 1 3.8 5l-1.2 4a2 2 0 0 1-3.3 1l-2.3-2H9l-2.3 2a2 2 0 0 1-3.3-1l-1.2-4A4 4 0 0 1 6 8z'/><path d='M7 11v4m-2-2h4m7-1h.01M18 14h.01'/></symbol><symbol id='i-exchange' viewBox='0 0 24 24'><path d='M7 7h13m-4-4 4 4-4 4M17 17H4m4-4-4 4 4 4'/></symbol><symbol id='i-arrow-down' viewBox='0 0 24 24'><path d='M12 3v13m-5-5 5 5 5-5M4 21h16'/></symbol><symbol id='i-eye' viewBox='0 0 24 24'><path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/></symbol><symbol id='i-eye-off' viewBox='0 0 24 24'><path d='m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8'/><path d='M9.9 5.2A10 10 0 0 1 12 5c6.5 0 10 7 10 7a15 15 0 0 1-3 3.8M6.2 6.2C3.5 8 2 12 2 12s3.5 7 10 7a9 9 0 0 0 3-.5'/></symbol><symbol id='i-pubg' viewBox='0 0 24 24'><path d='M4 5h16v14H4zM8 9h8M8 12h5M8 15h8'/><path d='M2 8V4h4M18 4h4v4M2 16v4h4M18 20h4v-4'/></symbol><symbol id='i-freefire' viewBox='0 0 24 24'><path d='M13 2c1 5-4 6-3 11 1-2 3-3 4-5 4 4 5 7 3 11a7 7 0 0 1-13-3c0-4 3-7 5-9 0 4 1 5 2 6 0-5 1-8 2-11z'/></symbol><symbol id='i-ml' viewBox='0 0 24 24'><path d='M3 18 8 6l4 7 4-7 5 12M6 18h12'/><path d='m8 6 4 2 4-2'/></symbol><symbol id='i-fortnite' viewBox='0 0 24 24'><path d='M5 3h14v18H5zM9 7h7M9 11h5M9 15h7'/><path d='M8 3v18'/></symbol></svg><header class='app-header'><a class='header-menu' href='/store/' aria-label='الرئيسية'><svg><use href='#i-home'/></svg></a><div class='header-brand'><div class='brand'>Empire E-Story</div><small>Digital Life .. Greater Sudan</small></div><a class='header-alert' href='/store/notifications' aria-label='الإشعارات'><svg><use href='#i-bell'/></svg></a><a class='header-avatar' href='/store/profile' aria-label='الملف الشخصي'><svg><use href='#i-user'/></svg></a></header><main>{% if request.endpoint == 'empire.home' %}<section class='wallet-top'><div class='wallet-card-icon'><svg aria-hidden='true'><use href='#i-wallet'/></svg></div><div class='wallet-balance'><div class='eyebrow'>رصيد المحفظة</div><div class='amount' id='sdgBalance' data-balance='{{wallet_sdg}}'>{{'{:,}'.format(wallet_sdg)}} <span>SDG</span></div><div class='currency'>الجنيه السوداني · أصولك الرقمية: {{'{:,}'.format(wallet_urc)}} URC</div></div><button class='balance-toggle' id='balanceToggle' type='button' aria-label='إخفاء الرصيد' aria-pressed='false'><svg aria-hidden='true'><use id='balanceEye' href='#i-eye'/></svg></button><div class='wallet-secure'><span>✓</span><small>محفظتك بأمان</small></div></section>{% endif %}<div class='page-content'>
    {% with ms=get_flashed_messages() %}{% for m in ms %}<p class='flash'>{{m}}</p>{% endfor %}{% endwith %}
    {{body|safe}}<footer><a href='/account'>حسابي</a> · <a href='/logout'>تسجيل الخروج</a><span style='float:left'>الإمبراطورية إستوري ©</span></footer></div></main><nav class='app-bottom' aria-label='التنقل الرئيسي'><a href='/store/'><span class='ico'><svg><use href='#i-home'/></svg></span>الرئيسية</a><a href='/store/marketplace'><span class='ico'><svg><use href='#i-store'/></svg></span>المتجر</a><a href='/store/transfer-epc'><span class='ico'><svg><use href='#i-transfer'/></svg></span>التحويلات</a><a href='/store/history'><span class='ico'><svg><use href='#i-bell'/></svg></span>المعاملات</a><a href='/store/wallet'><span class='ico'><svg><use href='#i-coins'/></svg></span>محفظتي</a><a href='/store/profile'><span class='ico'><svg><use href='#i-user'/></svg></span>حسابي</a></nav><script>
(function(){
  const balance=document.getElementById('sdgBalance'),toggle=document.getElementById('balanceToggle');
  if(balance&&toggle){
    const formatted=Number(balance.dataset.balance||'0').toLocaleString('en-US');
    let hidden=false;try{hidden=localStorage.getItem('empire.balance.hidden')==='1'}catch(_){}
    function paint(){balance.innerHTML=hidden?'••••••••':formatted+' <span>SDG</span>';toggle.setAttribute('aria-pressed',hidden?'true':'false');toggle.setAttribute('aria-label',hidden?'إظهار الرصيد':'إخفاء الرصيد');document.getElementById('balanceEye').setAttribute('href',hidden?'#i-eye-off':'#i-eye')}
    toggle.addEventListener('click',function(){hidden=!hidden;try{localStorage.setItem('empire.balance.hidden',hidden?'1':'0')}catch(_){}paint()});paint();
  }
  const bottom=document.querySelector('.app-bottom');
  if(bottom){const path=location.pathname;bottom.querySelectorAll('a').forEach(function(a){try{const p=new URL(a.href,location.href).pathname;a.classList.toggle('active',p===path||(p==='/store/'&&path==='/store/'))}catch(_){}})}
})();
</script></body></html>"""
    current=user()
    if current and "csrf" not in session:
        session["csrf"]=secrets.token_urlsafe(32)
    ctx.setdefault("is_founder", bool(current and current["role"]=="founder"))
    ctx.setdefault("wallet_sdg", balance(current["username"],"SDG") if current else 0)
    ctx.setdefault("wallet_urc", balance(current["username"],"URC") if current else 0)
    return render_template_string(shell,title=title,body=body,**ctx)

@empire_bp.route("/profile",methods=["GET","POST"])
@login_required
def profile():
    u=user()
    row=db().execute("SELECT username,full_name,account_number,role,active,email,phone FROM users WHERE username=?",(u["username"],)).fetchone()
    identity=db().execute("SELECT status,updated_at FROM es_identity_status WHERE username=?",(u["username"],)).fetchone()
    if not identity:
        db().execute("INSERT OR IGNORE INTO es_identity_status(username,status,updated_at) VALUES(?,'UNVERIFIED',?)",(u["username"],now()))
        identity=db().execute("SELECT status,updated_at FROM es_identity_status WHERE username=?",(u["username"],)).fetchone()
    loyalty=db().execute("SELECT points,badge,joined_at FROM es_loyalty_members WHERE username=?",(u["username"],)).fetchone()
    if request.method=="POST":
        csrf()
        email=(request.form.get("email") or "").strip().lower()
        phone=(request.form.get("phone") or "").strip()
        if len(email)>254 or (email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email)): abort(400,"البريد الإلكتروني غير صالح")
        if len(phone)>30 or (phone and not re.fullmatch(r"[+0-9 ()-]{6,30}",phone)): abort(400,"رقم الهاتف غير صالح")
        if email and db().execute("SELECT 1 FROM users WHERE lower(email)=? AND username<>?",(email,u["username"])).fetchone(): abort(409,"البريد الإلكتروني مرتبط بحساب آخر")
        db().execute("UPDATE users SET email=?,phone=? WHERE username=?",(email,phone,u["username"]))
        new_status="PENDING" if email and phone else "UNVERIFIED"
        db().execute("INSERT INTO es_identity_status(username,status,updated_at) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at",(u["username"],new_status,now()))
        audit(u["username"],"PROFILE_CONTACT_UPDATED","profile",u["username"],"identity_status="+new_status)
        nref="PROFILE-REVIEW-"+u["username"]+"-"+hashlib.sha256((email+"|"+phone).encode()).hexdigest()[:12]
        title="تم استلام بيانات التوثيق" if new_status=="PENDING" else "أكمل بيانات التوثيق"
        message="تم استلام بريدك ورقم هاتفك. ستراجع الإدارة البيانات، وستظهر علامة التوثيق بعد الاعتماد." if new_status=="PENDING" else "أدخل البريد الإلكتروني ورقم الهاتف معاً ثم احفظهما لإرسال طلب المراجعة."
        db().execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",(u["username"],title,message,nref,now()))
        flash("تم إرسال بيانات الاتصال للمراجعة. ستظهر علامة التوثيق بعد الاعتماد." if new_status=="PENDING" else "أضف البريد ورقم الهاتف معاً لإرسال طلب التوثيق.")
        return redirect(url_for("empire.profile"))
    body=render_template_string("""<section class='panel profile-hero'><span class='profile-mark'>🇸🇩</span><div><h1>الملف الشخصي</h1><p>إدارة معلومات حسابك وإعداداتك.</p></div></section>
    <section class='panel'><h2>حالة التوثيق</h2>{% if identity['status']=='VERIFIED' %}<span class='tag'>✓ حساب موثّق</span>{% elif identity['status']=='PENDING' %}<span class='tag' style='background:#fff5df;color:#94620a'>قيد مراجعة التوثيق</span>{% else %}<span class='tag'>حساب غير موثّق</span>{% endif %}<p class='muted'>أضف البريد ورقم الهاتف معاً. لن تظهر علامة ✓ إلا بعد مراجعة الإدارة واعتماد البيانات.</p></section>
    {% if loyalty %}<section class='premium-banner'><span class='ice-badge'>❄ عميل آيس ستوري مميز</span><h2>عضوية مميزة</h2><b class='loyalty-points'>{{'{:,}'.format(loyalty['points'])}}</b><small>نقاط الولاء</small></section>{% endif %}
    <section class='panel'><h2>الضبط</h2><a class='profile-setting' href='/store/loyalty'><span>★</span><b>نقاط الولاء</b><small>رصيد النقاط وسجل المكافآت</small><strong>‹</strong></a><a class='profile-setting' href='/store/notifications'><span>🔔</span><b>الإشعارات</b><small>عرض تنبيهات حسابك</small><strong>‹</strong></a></section>
    <section class='panel'><h2>معلومات الحساب</h2><div class='profile-info'><span>الاسم</span><b>{{row['full_name']}}</b></div><div class='profile-info'><span>رقم الحساب</span><b dir='ltr'>{{row['account_number']}}</b></div><div class='profile-info'><span>الحالة</span><b><span class='tag'>{{'نشط' if row['active'] else 'موقوف'}}</span></b></div>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>البريد الإلكتروني المرتبط</label><input type='email' name='email' maxlength='254' value='{{row['email']}}' placeholder='name@example.com' required><label>رقم الهاتف</label><input type='tel' name='phone' maxlength='30' value='{{row['phone']}}' placeholder='+249...' required><button>حفظ وإرسال للمراجعة</button></form></section>
    <section class='panel'><h2>الوصول إلى الحساب</h2><a class='btn secondary' href='/logout'>تسجيل الخروج</a> <a class='btn secondary' href='/register'>إنشاء حساب جديد</a></section>""",row=row,csrf=session.get("csrf",""),identity=identity)
    return page("الملف الشخصي",body,loyalty=loyalty)

@empire_bp.get("/loyalty")
@login_required
def loyalty():
    u=user()
    member=db().execute("SELECT points,badge,joined_at FROM es_loyalty_members WHERE username=?",(u["username"],)).fetchone()
    points=int(member["points"]) if member else 0
    ledger=db().execute("SELECT reference,points_delta,memo,created_at FROM es_loyalty_ledger WHERE username=? ORDER BY id DESC LIMIT 100",(u["username"],)).fetchall()
    body=render_template_string("""<section class='premium-banner'><span class='ice-badge'>★ برنامج الولاء</span><h1>نقاط الولاء</h1><strong class='loyalty-points'>{{'{:,}'.format(points)}}</strong><p>إجمالي نقاطك المتاحة</p>{% if member %}<span class='tag'>❄ عميل آيس ستوري مميز</span>{% else %}<p>يمكنك تفعيل عضوية آيس ستوري باستخدام الكود الحصري الذي تحصل عليه من آيس ستوري.</p><a class='btn' href='/store/ice-empire'>تفعيل عضوية آيس ستوري</a>{% endif %}</section>
      <section class='panel'><h2>سجل النقاط</h2>{% for x in ledger %}<article class='transaction-row'><div class='transaction-info'><b>{{x['memo']}}</b><small>{{x['created_at'][:16]}} · {{x['reference']}}</small></div><b class='transaction-value {{'in' if x['points_delta']>0 else 'out'}}'>{{'+' if x['points_delta']>0 else ''}}{{x['points_delta']}} نقطة</b></article>{% else %}<div class='empty-state'>لم تُسجل نقاط في حسابك بعد.</div>{% endfor %}</section>
      <section class='panel'><h2>استبدال النقاط</h2><p>رصيد النقاط وسجل اكتسابها ظاهران هنا. لم يُفعّل الاستبدال بعد حتى نعتمد المكافآت وقيمة كل مكافأة وعدد النقاط المطلوب لها.</p></section>""",points=points,member=member,ledger=ledger)
    return page("نقاط الولاء",body)

@empire_bp.route("/admin/identity",methods=["GET","POST"])
@login_required
@founder_required
def admin_identity():
    if request.method=="POST":
        csrf()
        username=(request.form.get("username") or "").strip()
        action=request.form.get("action","")
        row=db().execute("SELECT username,email,phone FROM users WHERE username=?",(username,)).fetchone()
        if not row or not row["email"] or not row["phone"] or action not in ("VERIFY","REJECT"):
            flash("تعذر مراجعة الطلب: تحقق من بيانات الحساب.")
            return redirect(url_for("empire.admin_identity"))
        status="VERIFIED" if action=="VERIFY" else "REJECTED"
        db().execute("INSERT INTO es_identity_status(username,status,updated_at) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at",(username,status,now()))
        nref="IDENTITY-"+status+"-"+username+"-"+secrets.token_hex(4)
        title="تم توثيق حسابك ✓" if status=="VERIFIED" else "تعذر اعتماد بيانات التوثيق"
        message="تمت مراجعة بيانات الاتصال واعتماد حسابك. ستظهر علامة ✓ بجانب حالة حسابك." if status=="VERIFIED" else "لم يتم اعتماد بيانات الاتصال. يرجى مراجعة البريد ورقم الهاتف والتواصل مع الدعم."
        db().execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",(username,title,message,nref,now()))
        audit(user()["username"],"IDENTITY_"+status,"identity",username)
        flash("تم اعتماد الحساب وارسال إشعار للعميل." if status=="VERIFIED" else "تم رفض طلب التوثيق وإشعار العميل.")
        return redirect(url_for("empire.admin_identity"))
    rows=db().execute("""SELECT u.username,u.full_name,u.account_number,u.email,u.phone,s.status,s.updated_at
      FROM es_identity_status s JOIN users u ON u.username=s.username
      WHERE s.status IN ('PENDING','REJECTED') ORDER BY CASE s.status WHEN 'PENDING' THEN 0 ELSE 1 END,s.updated_at""").fetchall()
    body=render_template_string("""<section class='panel'><h1>مراجعة توثيق الحسابات</h1><p>راجع بيانات الاتصال وتحقق منها خارجياً قبل الاعتماد. لا تعتمد بيانات لم يتم التحقق منها.</p>
      {% for x in rows %}<article class='panel'><b>{{x['full_name']}} · {{x['account_number']}}</b><p dir='ltr'>{{x['email']}}</p><p dir='ltr'>{{x['phone']}}</p><span class='tag'>{{'قيد المراجعة' if x['status']=='PENDING' else 'مرفوض سابقاً'}}</span>
      <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='username' value='{{x['username']}}'><button name='action' value='VERIFY'>اعتماد التوثيق ✓</button><button class='btn secondary' name='action' value='REJECT'>رفض وإشعار العميل</button></form></article>{% else %}<div class='empty-state'>لا توجد طلبات توثيق معلقة.</div>{% endfor %}</section>""",rows=rows,csrf=session.get("csrf",""))
    return page("توثيق الحسابات",body)

@empire_bp.route("/notifications")
@login_required
def store_notifications():
    u=user()
    rows=db().execute("SELECT * FROM notifications WHERE username=? ORDER BY id DESC LIMIT 100",(u["username"],)).fetchall()
    items=[]
    for row in rows:
        item=dict(row)
        transfer=db().execute("SELECT 1 FROM es_transfer_details WHERE reference=? AND (sender=? OR recipient=?)",
                              (row["reference"],u["username"],u["username"])).fetchone() if row["reference"] else None
        fund_entry=db().execute("SELECT 1 FROM es_ledger WHERE reference=? AND account='protection:fund' LIMIT 1",(row["reference"],)).fetchone() if row["reference"] else None
        if u["role"]=="founder" and fund_entry:
            item["target"]="/store/admin/protection-fund"
        else:
            item["target"]="/store/transfer/receipt/"+row["reference"] if transfer else ("/store/history/"+row["reference"] if row["reference"] and db().execute("SELECT 1 FROM es_ledger WHERE reference=? AND account=?",(row["reference"],"user:"+u["username"])).fetchone() else "")
        items.append(item)
    db().execute("UPDATE notifications SET is_read=1 WHERE username=?",(u["username"],))
    body=render_template_string("""<section class='panel notification-page'><span class='tag'>مركز التنبيهات</span><h1>إشعاراتك</h1>
      {% for n in items %}{% if n['target'] %}<a class='notification-card' href='{{n['target']}}'><span class='notification-symbol' aria-hidden='true'>✓</span><span class='notification-copy'><b>{{n['title']}}</b><span>{{n['message']}}</span><small>{{n['created_at']}}</small></span><span class='notification-arrow' aria-hidden='true'>←</span></a>
      {% else %}<article class='notification-card'><span class='notification-symbol' aria-hidden='true'>i</span><span class='notification-copy'><b>{{n['title']}}</b><span>{{n['message']}}</span><small>{{n['created_at']}}</small></span></article>{% endif %}{% else %}<div class='empty-state'>لا توجد إشعارات حالياً.</div>{% endfor %}
      </section>""",items=items)
    return page("الإشعارات",body)


@empire_bp.route("/admin/managers",methods=["GET","POST"])
@login_required
@founder_required
def admin_managers():
    session.setdefault("csrf", secrets.token_urlsafe(32))
    allowed_permissions = {
        "products":"إدارة أسعار وباقات المنتجات",
        "orders":"متابعة الطلبات والتسليم",
        "payments":"مراجعة طلبات الدفع",
        "withdrawals":"مراجعة طلبات السحب",
        "exchange":"إدارة تحويل العملة والسيولة",
        "catalog":"إدارة إعلانات السوق",
        "agents":"إدارة الوكلاء",
        "settings":"إعدادات المنصة",
        "audit":"سجل التدقيق"
    }
    if request.method=="POST":
        csrf()
        action=(request.form.get("action") or "").strip()
        if action=="create":
            full_name=(request.form.get("full_name") or "").strip()
            password=request.form.get("password") or ""
            if len(full_name)<3 or len(full_name)>120:
                flash("أدخل الاسم الكامل للمدير.")
                return redirect(url_for("empire.admin_managers"))
            if len(password)<10 or len(password.encode("utf-8"))>72:
                flash("كلمة مرور المدير يجب أن تكون 10 أحرف على الأقل.")
                return redirect(url_for("empire.admin_managers"))
            permissions=[p for p in request.form.getlist("permissions") if p in allowed_permissions]
            if not permissions:
                flash("حدد صلاحية واحدة على الأقل للمدير.")
                return redirect(url_for("empire.admin_managers"))
            requested_account=(request.form.get("account_number") or "").strip()
            if requested_account and not re.fullmatch(r"[1-9][0-9]{6}",requested_account):
                flash("رقم حساب المدير يجب أن يتكون من 7 أرقام ويبدأ برقم غير صفر.")
                return redirect(url_for("empire.admin_managers"))
            account=requested_account
            if account and db().execute("SELECT 1 FROM users WHERE account_number=?",(account,)).fetchone():
                flash("رقم الحساب مستخدم بالفعل. استخدم خيار تغيير كلمة المرور إذا كان الحساب موجوداً.")
                return redirect(url_for("empire.admin_managers"))
            for _ in range(30) if not account else ():

                candidate=str(secrets.randbelow(9000000)+1000000)
                if not db().execute("SELECT 1 FROM users WHERE account_number=?",(candidate,)).fetchone():
                    account=candidate
                    break
            if not account:
                raise RuntimeError("تعذر إنشاء رقم حساب فريد للمدير.")
            username="manager_"+account
            password_hash=bcrypt.hashpw(password.encode("utf-8"),bcrypt.gensalt()).decode("utf-8")
            pin_hash=bcrypt.hashpw(secrets.token_hex(8).encode("utf-8"),bcrypt.gensalt()).decode("utf-8")
            db().execute("""INSERT INTO users(username,full_name,account_number,password_hash,pin_hash,balance,currency,role,active,created_at)
              VALUES(?,?,?,?,?,0,'SDG','manager',1,?)""",(username,full_name,account,password_hash,pin_hash,now()))
            for permission in permissions:
                db().execute("INSERT INTO es_manager_permissions(username,permission,granted_by,created_at) VALUES(?,?,?,?)",
                             (username,permission,user()["username"],now()))
            audit(user()["username"],"MANAGER_CREATED","manager",account,"permissions="+",".join(permissions))
            flash("تم إنشاء حساب المدير بنجاح. رقم الحساب: "+account+" · كلمة المرور هي التي أدخلتها عند الإنشاء.")
            return redirect(url_for("empire.admin_managers"))
        elif action=="permissions":
            account=(request.form.get("account_number") or "").strip()
            target=db().execute("SELECT username FROM users WHERE account_number=? AND role='manager'",(account,)).fetchone()
            if not target: abort(404)
            permissions=[p for p in request.form.getlist("permissions") if p in allowed_permissions]
            db().execute("DELETE FROM es_manager_permissions WHERE username=?",(target["username"],))
            for permission in permissions:
                db().execute("INSERT INTO es_manager_permissions(username,permission,granted_by,created_at) VALUES(?,?,?,?)",
                             (target["username"],permission,user()["username"],now()))
            audit(user()["username"],"MANAGER_PERMISSIONS_UPDATED","manager",account,"permissions="+",".join(permissions))
            flash("تم تحديث صلاحيات المدير.")
        elif action=="toggle":
            account=(request.form.get("account_number") or "").strip()
            target=db().execute("SELECT username,active FROM users WHERE account_number=? AND role='manager'",(account,)).fetchone()
            if not target: abort(404)
            active=0 if target["active"] else 1
            db().execute("UPDATE users SET active=? WHERE username=?",(active,target["username"]))
            audit(user()["username"],"MANAGER_STATUS_CHANGED","manager",account,"active="+str(active))
            flash("تم تحديث حالة حساب المدير.")
        elif action=="reset_password":
            account=(request.form.get("account_number") or "").strip()
            password=request.form.get("password") or ""
            target=db().execute("SELECT username FROM users WHERE account_number=? AND role='manager'",(account,)).fetchone()
            if not target: abort(404)
            if len(password)<10 or len(password.encode("utf-8"))>72:
                flash("كلمة المرور الجديدة يجب أن تكون 10 أحرف على الأقل.")
                return redirect(url_for("empire.admin_managers"))
            password_hash=bcrypt.hashpw(password.encode("utf-8"),bcrypt.gensalt()).decode("utf-8")
            db().execute("UPDATE users SET password_hash=? WHERE username=?",(password_hash,target["username"]))
            audit(user()["username"],"MANAGER_PASSWORD_RESET","manager",account)
            flash("تم تغيير كلمة مرور المدير.")
        else:
            abort(400)
        return redirect(url_for("empire.admin_managers"))
    rows=db().execute("SELECT username,full_name,account_number,active FROM users WHERE role='manager' ORDER BY id DESC").fetchall()
    managers=[]
    for row in rows:
        item=dict(row)
        item["permissions"]=[x["permission"] for x in db().execute("SELECT permission FROM es_manager_permissions WHERE username=? ORDER BY permission",(row["username"],)).fetchall()]
        managers.append(item)
    body=render_template_string("""<section class='panel'><span class='tag'>صلاحيات تحت سيطرة المالك</span><h1>إدارة المدير العام</h1>
      <p>أنشئ حساباً مستقلاً، ثم اختر بدقة الوحدات التي يستطيع إدارتها. لا يستطيع المدير إنشاء مديرين أو تعديل صلاحياته.</p>
      <form method='post' class='panel'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='create'>
      <h2>إنشاء حساب المدير العام</h2><label>رقم الحساب</label><input name='account_number' inputmode='numeric' pattern='[1-9][0-9]{6}' maxlength='7' value='9274618' required><label>الاسم الكامل</label><input name='full_name' maxlength='120' value='المدير العام للإمبراطورية' required>
      <label>كلمة مرور أولية (10 أحرف على الأقل)</label><input name='password' type='password' minlength='10' maxlength='72' autocomplete='new-password' required>
      <h3>الصلاحيات الممنوحة</h3>{% for key,label in permissions %}<label class='permission-choice'><input type='checkbox' name='permissions' value='{{key}}'>{{label}}</label>{% endfor %}
      <button>إنشاء حساب المدير</button></form>
      <h2>المديرون الحاليون</h2>{% for x in managers %}<article class='panel'><b>{{x['full_name']}}</b><p>رقم الحساب: <strong dir='ltr'>{{x['account_number']}}</strong> · {{'نشط' if x['active'] else 'موقوف'}}</p>
      <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='permissions'><input type='hidden' name='account_number' value='{{x['account_number']}}'>
      <b>تعديل الصلاحيات</b>{% for key,label in permissions %}<label class='permission-choice'><input type='checkbox' name='permissions' value='{{key}}' {% if key in x['permissions'] %}checked{% endif %}>{{label}}</label>{% endfor %}<button>حفظ الصلاحيات</button></form>
      <form method='post' class='panel'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='reset_password'><input type='hidden' name='account_number' value='{{x['account_number']}}'><label>تعيين كلمة مرور جديدة</label><input type='password' name='password' minlength='10' maxlength='72' required><button>تغيير كلمة المرور</button></form>
      <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='toggle'><input type='hidden' name='account_number' value='{{x['account_number']}}'><button class='btn secondary'>{{'إيقاف الحساب' if x['active'] else 'إعادة تفعيل الحساب'}}</button></form></article>{% else %}<p>لم يتم إنشاء مديرين بعد.</p>{% endfor %}</section>""",
      managers=managers,permissions=list(allowed_permissions.items()),csrf=session.get("csrf",""))
    return page("إدارة المدير العام",body)

@empire_bp.route("/ice-empire",methods=["GET","POST"])
@login_required
def ice_empire():
    session.setdefault("csrf", secrets.token_urlsafe(32))
    u=user()
    if request.method=="POST":
        csrf()
        submitted=(request.form.get("code") or "").strip().upper().replace(" ","")
        if not re.fullmatch(r"ICE-[A-Z0-9]{12}",submitted):
            flash("صيغة الكود غير صحيحة. راجع الكود الذي استلمته من آيس ستوري.")
            return redirect(url_for("empire.ice_empire"))
        code_hash=hashlib.sha256(submitted.encode("utf-8")).hexdigest()
        d=db()
        d.execute("BEGIN IMMEDIATE")
        try:
            code=d.execute("SELECT * FROM es_ice_codes WHERE code_hash=? AND status='AVAILABLE'",(code_hash,)).fetchone()
            if not code:
                raise ValueError("الكود غير متاح أو سبق استخدامه. تواصل مع آيس ستوري للمساعدة.")
            existing=d.execute("SELECT username FROM es_loyalty_members WHERE username=?",(u["username"],)).fetchone()
            if existing:
                raise ValueError("حسابك مسجل بالفعل ضمن عملاء آيس ستوري المميزين.")
            stamp=now()
            d.execute("UPDATE es_ice_codes SET status='REDEEMED',used_by=?,used_at=? WHERE id=? AND status='AVAILABLE'",(u["username"],stamp,code["id"]))
            if d.execute("SELECT changes() n").fetchone()["n"]!=1:
                raise ValueError("تم استخدام الكود في عملية أخرى. حاول مجدداً.")
            points=int(code["points"])
            d.execute("INSERT INTO es_loyalty_members(username,points,badge,source,joined_at,updated_at) VALUES(?,?,'ICE_PREMIUM','ICE_STORY',?,?)",
                      (u["username"],points,stamp,stamp))
            d.execute("INSERT INTO es_loyalty_ledger(username,reference,points_delta,memo,created_at) VALUES(?,?,?,?,?)",
                      (u["username"],"ICE-"+secrets.token_hex(8).upper(),points,"مكافأة تفعيل عضوية آيس ستوري المميزة",stamp))
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                      (u["username"],"تهانينا! أصبحت عميلاً مميزاً","مبارك لك الانضمام إلى عملاء آيس ستوري المميزين في الإمبراطورية إستوري. حصل حسابك على الشارة الثلجية ونقاط الولاء. امبراطورية النار: هنا تصنع مجدك · امبراطورية الثلج: متجرك وأكثر.", "ICE-"+secrets.token_hex(6).upper(),stamp))
            audit(u["username"],"ICE_PREMIUM_REDEEMED","loyalty",code["code_tail"],"points="+str(points))
            d.commit()
            flash("مبارك! تمت ترقيتك إلى عميل مميز. ظهرت الشارة الثلجية وأضيفت نقاط الولاء إلى حسابك.")
        except Exception as exc:
            d.rollback()
            flash(str(exc))
        return redirect(url_for("empire.ice_empire"))
    try:
        member=db().execute("SELECT points,badge,joined_at FROM es_loyalty_members WHERE username=?",(u["username"],)).fetchone()
        customers=db().execute("""SELECT u.full_name,m.points,m.joined_at,COALESCE(s.status,'UNVERIFIED') identity_status FROM es_loyalty_members m
          JOIN users u ON u.username=m.username LEFT JOIN es_identity_status s ON s.username=u.username
          ORDER BY m.joined_at DESC LIMIT 100""").fetchall()
        body=render_template_string("""<section class='ice-hero'><span class='ice-brand'>آيس ستوري × الإمبراطورية إستوري</span><h1>مرحباً بك في إمبراطورية الثلج</h1><p>مساحة خاصة لعملاء آيس ستوري المميزين.</p><div class='empire-slogans'><article><b>إمبراطورية النار</b><strong>هنا تصنع مجدك</strong></article><article><b>إمبراطورية الثلج</b><strong>متجرك وأكثر</strong></article></div></section>
          {% if member %}<section class='premium-banner'><span class='ice-badge'>❄ عميل مميز</span><h2>أهلاً بك في نادي العملاء المميزين</h2><p>نقاط الولاء</p><strong class='loyalty-points'>{{'{:,}'.format(member['points'])}}</strong><small>عضوية مفعلة منذ {{member['joined_at'][:10]}}</small></section>
          {% else %}<section class='panel'><h2>فعّل شارتك الثلجية</h2><p>أدخل الكود الخاص الذي حصلت عليه من آيس ستوري على واتساب، ثم اضغط تحقق.</p><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>كود عميل آيس ستوري</label><input name='code' maxlength='16' placeholder='ICE-XXXXXXXXXXXX' autocapitalize='characters' autocomplete='off' required><button>تحقق وتفعيل العضوية</button></form></section>{% endif %}
          <section class='panel'><h2>عملاء آيس ستوري المميزون</h2><p>مجتمع العملاء الذين فعّلوا عضويتهم بكود صحيح.</p><div class='premium-list'>{% for x in customers %}<div class='premium-customer'><span class='ice-mini'>❄</span><b>{{x['full_name']}}</b><span class='tag'>عميل مميز</span>{% if x['identity_status']=='VERIFIED' %}<span class='tag'>✓ موثّق</span>{% endif %}</div>{% else %}<p>كن أول المنضمين إلى النادي.</p>{% endfor %}</div></section>
          <section class='panel ice-footer-note'><h2>آيس ستوري × الإمبراطورية إستوري</h2><p>امبراطورية النار</p><strong>هنا تصنع مجدك</strong><p>امبراطورية الثلج</p><strong>متجرك وأكثر</strong></section>""",
          member=member,customers=customers,csrf=session.get("csrf",""))
        return page("آيس ستوري · إمبراطورية الثلج",body)
    except Exception:
        from flask import current_app
        current_app.logger.exception("Ice Story customer page failed for user %s",u["username"])
        return render_template_string("""<!doctype html><html lang='ar' dir='rtl'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>آيس ستوري</title><body style='font-family:system-ui;background:#f3f7f3;padding:24px;line-height:1.8'><main style='max-width:520px;margin:30px auto;background:#fff;border:1px solid #dfeae2;border-radius:18px;padding:22px'><h2>تعذر فتح قسم آيس ستوري مؤقتاً</h2><p>لم يتم تنفيذ أي عملية أو تغيير في رصيدك. تم تسجيل الخطأ الفني لمراجعته. حاول لاحقاً.</p><a href='/store/'>العودة للرئيسية</a></main></body></html>"""),503

@empire_bp.route("/admin/ice-codes",methods=["GET","POST"])
@login_required
@founder_required
def admin_ice_codes():
    session.setdefault("csrf", secrets.token_urlsafe(32))
    # First-time owner visit provisions the requested exclusive batch exactly once.
    # Plain codes are revealed only in this authenticated owner session, then erased.
    existing_codes=db().execute("SELECT COUNT(*) n FROM es_ice_codes").fetchone()["n"]
    if existing_codes < 200 and request.method == "GET":
        created=[]
        stamp=now()
        for _ in range(200-existing_codes):
            plain="ICE-"+secrets.token_hex(6).upper()
            digest=hashlib.sha256(plain.encode("utf-8")).hexdigest()
            cipher=encryption().encrypt(plain.encode("utf-8")).decode("utf-8")
            db().execute("INSERT INTO es_ice_codes(code_hash,code_tail,code_cipher,points,status,created_by,created_at) VALUES(?,?,?,100,'AVAILABLE',?,?)",
                         (digest,plain[-4:],cipher,user()["username"],stamp))
            created.append(plain)
        audit(user()["username"],"ICE_CODES_INITIAL_BATCH_CREATED","loyalty",str(200-existing_codes),"points_each=100")
        reveal_token=secrets.token_urlsafe(24)
        token_hash=hashlib.sha256(reveal_token.encode("utf-8")).hexdigest()
        db().execute("INSERT INTO es_ice_code_reveals(token_hash,codes_json,created_at) VALUES(?,?,?)",
                     (token_hash,json.dumps(created),stamp))
        session["ice_codes_reveal_token"]=reveal_token
        flash("تم استكمال الدفعة الحصرية إلى 200 كود، 100 نقطة لكل كود.")
    if request.method=="POST":
        csrf()
        count_raw=(request.form.get("count") or "1").strip()
        points_raw=(request.form.get("points") or "100").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,2}",count_raw) or int(count_raw)>100:
            abort(400,"عدد الأكواد من 1 إلى 100")
        if not re.fullmatch(r"[0-9]{1,7}",points_raw):
            abort(400,"عدد نقاط الولاء غير صالح")
        count,points=int(count_raw),int(points_raw)
        created=[]
        for _ in range(count):
            plain="ICE-"+secrets.token_hex(6).upper()
            digest=hashlib.sha256(plain.encode("utf-8")).hexdigest()
            cipher=encryption().encrypt(plain.encode("utf-8")).decode("utf-8")
            db().execute("INSERT INTO es_ice_codes(code_hash,code_tail,code_cipher,points,status,created_by,created_at) VALUES(?,?,?,?,'AVAILABLE',?,?)",
                         (digest,plain[-4:],cipher,points,user()["username"],now()))
            created.append(plain)
        audit(user()["username"],"ICE_CODES_CREATED","loyalty",str(count),"points_each="+str(points))
        reveal_token=secrets.token_urlsafe(24)
        token_hash=hashlib.sha256(reveal_token.encode("utf-8")).hexdigest()
        db().execute("INSERT INTO es_ice_code_reveals(token_hash,codes_json,created_at) VALUES(?,?,?)",
                     (token_hash,json.dumps(created),now()))
        session["ice_codes_reveal_token"]=reveal_token
        flash("تم إنشاء الأكواد. انسخها ووزعها على العملاء؛ لن تُعرض مرة أخرى.")
        return redirect(url_for("empire.admin_ice_codes"))
    generated=[]
    reveal_token=session.pop("ice_codes_reveal_token",None)
    if reveal_token:
        token_hash=hashlib.sha256(reveal_token.encode("utf-8")).hexdigest()
        reveal=db().execute("SELECT codes_json FROM es_ice_code_reveals WHERE token_hash=?",(token_hash,)).fetchone()
        if reveal:
            generated=json.loads(reveal["codes_json"])
            db().execute("DELETE FROM es_ice_code_reveals WHERE token_hash=?",(token_hash,))
    raw_rows=db().execute("SELECT id,code_tail,code_cipher,copied_at,points,status,created_at,used_by,used_at FROM es_ice_codes ORDER BY id DESC LIMIT 200").fetchall()
    rows=[]
    for raw in raw_rows:
        item=dict(raw); item["plain_code"]=""
        if item["status"]=="AVAILABLE" and item["code_cipher"]:
            try: item["plain_code"]=encryption().decrypt(item["code_cipher"].encode("utf-8")).decode("utf-8")
            except Exception: item["plain_code"]=""
        rows.append(item)
    body=render_template_string("""<section class='panel'><span class='tag'>آيس ستوري × الإمبراطورية إستوري</span><h1>أكواد العملاء المميزين</h1>
      <p>كل كود فريد ويستخدم مرة واحدة فقط. تحفظ المنصة بصمة الكود ولا تحتفظ بنصه الصريح بعد إنشائه.</p>
      {% if generated %}<div class='premium-banner'><h2>الأكواد الجديدة — انسخها الآن</h2>{% for code in generated %}<p class='code-line' dir='ltr'>{{code}}</p>{% endfor %}<button type='button' onclick='navigator.clipboard.writeText({{generated|tojson}}.join("\\n"))'>نسخ جميع الأكواد</button></div>{% endif %}
      <form method='post' class='panel'><input type='hidden' name='csrf' value='{{csrf}}'><label>عدد الأكواد (1–100)</label><input type='number' name='count' min='1' max='100' value='1' required><label>نقاط الولاء لكل كود</label><input type='number' name='points' min='0' max='9999999' value='100' required><button>إنشاء أكواد جديدة</button></form>
      <h2>سجل الأكواد</h2><p class='muted'>الأكواد المتاحة مشفرة ويمكن نسخ كل كود مرة أخرى. عند الاسترداد يُقفل نهائياً ولا يعود قابلاً للنسخ.</p>
      {% for x in rows %}<article class='panel ice-code-record'><div><b dir='ltr'>••••{{x['code_tail']}}</b> · {{x['points']}} نقطة · <span class='tag'>{{'متاح' if x['status']=='AVAILABLE' else ('تم الاسترداد — غير صالح' if x['status']=='REDEEMED' else 'ملغي')}}</span></div>
      {% if x['status']=='AVAILABLE' and x['plain_code'] %}<div class='ice-code-copy'><code dir='ltr'>{{x['plain_code']}}</code><button type='button' class='ice-copy-button' data-code='{{x['plain_code']}}' data-code-id='{{x['id']}}' data-csrf='{{csrf}}'>{{'نسخ مرة أخرى' if x['copied_at'] else 'نسخ الكود'}}</button><small class='copy-status'>{{'تم النسخ سابقاً' if x['copied_at'] else 'لم يُنسخ بعد'}}</small></div>
      {% elif x['status']=='AVAILABLE' %}<p class='muted'>الكود قديم ولم تُحفظ نسخة مشفرة قابلة للاستعادة. يمكن استخدامه إن كان العميل يحتفظ به، لكن لا يمكن إعادة نسخه من اللوحة.</p>{% endif %}
      {% if x['used_by'] %}<p>استُرد بواسطة: {{x['used_by']}} · {{x['used_at'][:16] if x['used_at'] else ''}}</p>{% endif %}<small>{{x['created_at'][:16]}}</small></article>{% else %}<p>لم تُنشأ أكواد بعد.</p>{% endfor %}
      <script>(function(){document.querySelectorAll('.ice-copy-button').forEach(function(btn){btn.addEventListener('click',async function(){var code=btn.dataset.code;try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(code)}else{var area=document.createElement('textarea');area.value=code;document.body.appendChild(area);area.select();if(!document.execCommand('copy'))throw new Error('copy failed');area.remove()}var response=await fetch('/store/admin/ice-codes/copied/'+btn.dataset.codeId,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'csrf='+encodeURIComponent(btn.dataset.csrf)});if(!response.ok)throw new Error('save failed');btn.textContent='نسخ مرة أخرى';btn.parentElement.querySelector('.copy-status').textContent='تم النسخ ✓';}catch(e){alert('تعذر النسخ أو حفظ حالته. حاول مرة أخرى.')}})})})();</script></section>""",
      generated=generated,rows=rows,csrf=session.get("csrf",""))
    return page("أكواد آيس ستوري",body)

@empire_bp.get("/history")
@login_required
def transaction_history():
    u=user(); account="user:"+u["username"]
    refs=db().execute("""SELECT reference,MAX(created_at) created_at,MAX(memo) memo,MAX(id) last_id
      FROM es_ledger WHERE account=? GROUP BY reference ORDER BY last_id DESC LIMIT 100""",(account,)).fetchall()
    items=[]
    for row in refs:
        movements=db().execute("SELECT currency,delta FROM es_ledger WHERE reference=? AND account=? ORDER BY line_no",(row["reference"],account)).fetchall()
        principal=max(movements,key=lambda x:abs(int(x["delta"]))) if movements else None
        items.append({"reference":row["reference"],"created_at":row["created_at"],"memo":row["memo"],
                      "amount":abs(int(principal["delta"])) if principal else 0,
                      "currency":principal["currency"] if principal else ""})
    body=render_template_string("""<section class='panel'><span class='tag'>سجل حسابك</span><h1>المعاملات السابقة</h1><p>كل شريط يعرض قيمة العملية ووقتها ورقمها والخدمة. اضغط على أي عملية لفتح إشعارها الكامل.</p>
      {% for x in items %}<a class='history-strip' href='/store/history/{{x["reference"]}}'><span class='history-mark'>↗</span><span class='history-main'><b>{{x['memo']}}</b><small>{{x['created_at'][:19].replace('T',' ')}} UTC</small><small dir='ltr'>{{x['reference']}}</small></span><strong>{{'{:,}'.format(x['amount'])}} {{x['currency']}}</strong></a>{% else %}<div class='empty-state'>لا توجد معاملات مسجلة في محفظتك بعد.</div>{% endfor %}</section>""",items=items)
    return page("المعاملات السابقة",body)

@empire_bp.get("/history/<reference>")
@login_required
def transaction_history_detail(reference):
    u=user()
    account="user:"+u["username"]
    own=db().execute("SELECT currency,delta,memo,created_at FROM es_ledger WHERE reference=? AND account=? ORDER BY line_no",(reference,account)).fetchall()
    if not own:
        abort(404)
    transfer=db().execute("SELECT sender,recipient,recipient_account,note FROM es_transfer_details WHERE reference=? AND (sender=? OR recipient=?)",
                          (reference,u["username"],u["username"])).fetchone()
    fx_audit=db().execute("SELECT details FROM es_audit WHERE object_ref=? AND action='CURRENCY_EXCHANGE' LIMIT 1",(reference,)).fetchone()
    movements=[]
    counterparty=""
    memo=own[0]["memo"]
    if transfer:
        is_sender=transfer["sender"]==u["username"]
        other=transfer["recipient"] if is_sender else transfer["sender"]
        person=db().execute("SELECT full_name,account_number FROM users WHERE username=?",(other,)).fetchone()
        if person:
            counterparty=person["full_name"]+" · "+person["account_number"]
        principal=[x for x in own if (int(x["delta"])<0 if is_sender else int(x["delta"])>0)]
        if not principal:
            abort(404)
        main=max(principal,key=lambda x:abs(int(x["delta"])))
        movements=[{"currency":main["currency"],"delta":int(main["delta"]),"label":"قيمة التحويل"}]
        memo="تحويل صادر" if is_sender else "تحويل وارد"
    elif fx_audit:
        debits=[x for x in own if int(x["delta"])<0]
        credits=[x for x in own if int(x["delta"])>0]
        if not debits or not credits:
            abort(404)
        principal=max(debits,key=lambda x:abs(int(x["delta"])))
        target=next((x for x in credits if x["currency"]!=principal["currency"]),None)
        if not target:
            abort(404)
        fee_rows=[x for x in debits if x["currency"]==principal["currency"] and x is not principal]
        movements=[
            {"currency":principal["currency"],"delta":abs(int(principal["delta"])),"label":"المبلغ المحوّل من محفظتك"},
            {"currency":target["currency"],"delta":int(target["delta"]),"label":"المبلغ الذي أُضيف إلى محفظتك"},
        ]
        if fee_rows:
            movements.append({"currency":principal["currency"],"delta":sum(abs(int(x["delta"])) for x in fee_rows),"label":"رسوم العملية (3%)"})
        memo="إشعار تحويل العملات"
    else:
        movements=[{"currency":x["currency"],"delta":int(x["delta"]),"label":"إضافة إلى المحفظة" if int(x["delta"])>0 else "خصم من المحفظة"} for x in own]
    body=render_template_string("""<section class='panel notification-detail'><span class='tag'>إشعار معاملة</span><h1>{{memo}}</h1>
      <div class='history-detail-hero'><small>رقم العملية</small><b dir='ltr'>{{reference}}</b><small>التاريخ والوقت (UTC)</small><b dir='ltr'>{{created_at}}</b></div>
      <h2>تفاصيل العملية</h2>{% for x in movements %}<div class='profile-info'><span>{{x['label']}} · {{x['currency']}}</span><b>{{'+' if x['delta']>0 else ''}}{{'{:,}'.format(x['delta'])}} {{x['currency']}}</b></div>{% endfor %}
      {% if counterparty %}<div class='profile-info'><span>الطرف الآخر</span><b>{{counterparty}}</b></div>{% endif %}
      {% if note %}<div class='profile-info'><span>ملاحظة</span><b>{{note}}</b></div>{% endif %}
      <p class='muted'>تعرض هذه الصفحة تفاصيل العملية الخاصة بحسابك فقط، دون أرصدة الحسابات الأخرى أو البيانات الداخلية.</p>
      <a class='btn secondary' href='/store/history'>العودة إلى سجل المعاملات</a></section>""",
      memo=memo,reference=reference,created_at=own[0]["created_at"],movements=movements,
      counterparty=counterparty,note=(transfer["note"] or "").strip() if transfer else "")
    return page("إشعار المعاملة",body)


@empire_bp.post("/admin/ice-codes/copied/<int:code_id>")
@login_required
@founder_required
def mark_ice_code_copied(code_id):
    csrf()
    changed=db().execute("UPDATE es_ice_codes SET copied_at=? WHERE id=? AND status='AVAILABLE' AND code_cipher<>''",(now(),code_id)).rowcount
    if changed!=1:
        return jsonify({"ok":False,"message":"الكود غير متاح للنسخ أو تم استرداده."}),409
    audit(user()["username"],"ICE_CODE_COPIED","loyalty_code",str(code_id))
    return jsonify({"ok":True,"copied":True})

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
            "icons":[{"src":"/store/icon.svg?v=3","sizes":"any","type":"image/svg+xml","purpose":"any maskable"}]},200,{"Content-Type":"application/manifest+json"}

@empire_bp.get("/icon.svg")
def app_icon():
    svg="""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs>
 <linearGradient id="gold" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#fff0a5"/><stop offset=".35" stop-color="#f6c64b"/><stop offset=".72" stop-color="#b87517"/><stop offset="1" stop-color="#ffe99a"/></linearGradient>
 <linearGradient id="fire" x1="0" y1="1" x2="0" y2="0"><stop stop-color="#8b0808"/><stop offset=".45" stop-color="#ed3217"/><stop offset="1" stop-color="#ffca45"/></linearGradient>
 <radialGradient id="bg"><stop stop-color="#3a0907"/><stop offset=".62" stop-color="#130808"/><stop offset="1" stop-color="#050506"/></radialGradient>
 <filter id="glow"><feGaussianBlur stdDeviation="7" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<rect width="512" height="512" rx="112" fill="url(#bg)"/>
<path d="M40 345C-2 250 54 165 106 132c-14 57 7 83 38 99-6-58 24-115 71-157-8 60 14 88 41 109 24-57 72-92 113-107-20 60-5 100 35 130 24-18 41-43 50-77 43 90 17 174-20 220 7 44-8 86-31 120H112c-28-37-45-77-40-119Z" fill="url(#fire)" opacity=".92"/>
<path d="M72 321c26-62 68-104 124-125l-34 54 65-20-41 49 68-16-53 52 81-14-66 48 71 7-82 25-89-15-48-45Z" fill="url(#gold)" stroke="#9b4a0b" stroke-width="5"/>
<path d="M440 321c-26-62-68-104-124-125l34 54-65-20 41 49-68-16 53 52-81-14 66 48-71 7 82 25 89-15 48-45Z" fill="url(#gold)" stroke="#9b4a0b" stroke-width="5"/>
<path d="M123 285l93-22-54 44 83-9-59 43 70 4-62 30-71-18-42-40Z" fill="#e94b1b" opacity=".9"/>
<path d="M389 285l-93-22 54 44-83-9 59 43-70 4 62 30 71-18 42-40Z" fill="#e94b1b" opacity=".9"/>
<path d="M256 161c-20 31-39 52-44 91-6 45 17 84 44 111 27-27 50-66 44-111-5-39-24-60-44-91Z" fill="url(#gold)" stroke="#8b4b13" stroke-width="5"/>
<path d="M251 194c-25-14-54-7-68 8 25-4 42 4 53 18-27-2-43 9-55 27 29-8 51-3 66 13 10-17 27-24 50-21-8-20-25-37-46-45Z" fill="#ffdf76"/>
<path d="M265 191c27-14 55-7 69 8-25-4-42 4-53 18 27-2 43 9 55 27-29-8-51-3-66 13-10-17-27-24-50-21 8-20 25-37 45-45Z" fill="#ffb42f"/>
<path d="M272 217l47 8-34 18-13-26Z" fill="#f8d878"/>
<circle cx="280" cy="211" r="5" fill="#1b0806"/><circle cx="281" cy="210" r="2" fill="#fff"/>
<path d="M164 128l18-31 26 23 20-45 28 40 28-40 20 45 26-23 18 31-17 18H181Z" fill="url(#gold)" stroke="#6f3a0b" stroke-width="6"/>
<path d="M180 148h152v16H180z" fill="#a55b12" stroke="#ffe38a" stroke-width="4"/>
<path d="M256 66l10 20 22 3-16 15 4 22-20-11-20 11 4-22-16-15 22-3Z" fill="#fff0a0" filter="url(#glow)"/>
<path d="M99 373c46 31 96 42 157 42s111-11 157-42l-22 44c-40 26-84 39-135 39s-95-13-135-39Z" fill="#d61d22" stroke="url(#gold)" stroke-width="6"/>
<path d="M99 397c48 27 100 38 157 38s109-11 157-38v25c-45 28-97 40-157 40s-112-12-157-40Z" fill="#fff"/>
<path d="M99 422c48 27 100 38 157 38s109-11 157-38v24c-45 28-97 40-157 40s-112-12-157-40Z" fill="#101014"/>
<path d="M256 418l-42 43 42 24 42-24Z" fill="#16834b" stroke="url(#gold)" stroke-width="4"/>
<rect x="15" y="15" width="482" height="482" rx="100" fill="none" stroke="url(#gold)" stroke-width="9"/>
<rect x="27" y="27" width="458" height="458" rx="90" fill="none" stroke="#8e531b" stroke-width="2" opacity=".8"/>
</svg>"""
    return svg,200,{"Content-Type":"image/svg+xml","Cache-Control":"public, max-age=31536000"}

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
    contacts=db().execute("SELECT email,phone FROM users WHERE username=?",(u["username"],)).fetchone()
    if contacts and (not contacts["email"] or not contacts["phone"]):
        reminder_ref="PROFILE-CONTACT-"+u["username"]
        if not db().execute("SELECT 1 FROM notifications WHERE username=? AND reference=?",(u["username"],reminder_ref)).fetchone():
            db().execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                (u["username"],"أكمل بيانات ملفك الشخصي","أضف بريدك الإلكتروني ورقم هاتفك من الملف الشخصي، ثم أرسل البيانات للمراجعة حتى نتمكن من اعتماد حالة حسابك.",reminder_ref,now()))
    rows=db().execute("SELECT * FROM es_ledger WHERE account=? ORDER BY id DESC LIMIT 4",("user:"+u["username"],)).fetchall()
    body=render_template_string("""<section class="home-services"><div class="section-heading"><div><span class="section-kicker">خدمات الإمبراطورية</span><h2>ماذا تريد أن تفعل اليوم؟</h2></div><span class="sudan-mark">🇸🇩</span></div>
    <div class="service-grid">
    <a class="service-card" href="/store/marketplace"><i class="service-icon store"><svg><use href="#i-store"/></svg></i><strong>المتجر</strong></a>
    <a class="service-card" href="/store/accounts"><i class="service-icon accounts"><svg><use href="#i-user"/></svg></i><strong>بيع وشراء الحسابات</strong></a>
    <a class="service-card" href="/store/transfer-epc"><i class="service-icon transfer"><svg><use href="#i-transfer"/></svg></i><strong>التحويلات</strong></a>
    <a class="service-card" href="/store/wallet"><i class="service-icon coins"><svg><use href="#i-coins"/></svg></i><strong>عملاتي</strong></a>
    <a class="service-card" href="/account/funding"><i class="service-icon funding"><svg><use href="#i-arrow-down"/></svg></i><strong>تغذية حسابي</strong></a>
    <a class="service-card" href="/store/profile"><i class="service-icon profile"><svg><use href="#i-user"/></svg></i><strong>ملفي الشخصي</strong></a>
    <a class="service-card" href="/store/topups"><i class="service-icon games"><svg><use href="#i-game"/></svg></i><strong>الألعاب</strong></a>
    <a class="service-card" href="/store/exchange"><i class="service-icon exchange"><svg><use href="#i-exchange"/></svg></i><strong>تحويل العملة</strong></a>
    <a class="service-card" href="/store/history"><i class="service-icon funding"><svg><use href="#i-bell"/></svg></i><strong>المعاملات السابقة</strong></a>
    <a class="service-card" href="/store/withdrawal"><i class="service-icon withdraw"><svg><use href="#i-arrow-down"/></svg></i><strong>السحب</strong></a>
    <a class="service-card" href="/store/agents"><i class="service-icon agents"><svg><use href="#i-user"/></svg></i><strong>الوكلاء</strong></a>
    <a class="service-card loyalty-service" href="/store/loyalty"><i class="service-icon loyalty"><span>★</span></i><strong>نقاط الولاء</strong></a>
    <a class="service-card loyalty-service" href="/store/ice-empire"><i class="service-icon loyalty"><span>❄</span></i><strong>آيس ستوري · عميل مميز</strong></a></div></section>
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
            cash_available=cash_sdg_balance()-pending_sdg_withdrawals()
            if amount>cash_available:
                raise ValueError("السيولة النقدية المتاحة للسحب غير كافية حالياً.")
            fee=platform_fee(amount)
            post(r,[("user:"+u["username"],"SDG",-(amount+fee)),("payout:pending","SDG",amount),("protection:fund","SDG",fee)],"حجز طلب سحب مستحقات · عمولة التطبيق 3%")
            notify_protection_income(r,"SDG",fee,"رسوم طلب سحب")
            d.execute("INSERT INTO es_withdrawals(reference,username,amount_sdg,payout_method,payout_details_cipher,created_at) VALUES(?,?,?,?,?,?)",
                      (r,u["username"],amount,method,encrypted,now()))
            audit(u["username"],"WITHDRAWAL_REQUESTED","withdrawal",r)
            d.commit()
        except ValueError as exc:
            d.rollback(); flash(str(exc)); return redirect(url_for("empire.withdrawal"))
        except Exception:
            d.rollback()
            raise
        flash("تم حجز المبلغ ورفع طلب السحب للمراجعة. لا يعتبر مدفوعاً قبل تأكيد المالك.")
        return redirect(url_for("empire.wallet"))
    body=render_template_string("""<section class='panel'><h1>طلب سحب SDG</h1><p>أدخل المبلغ وبيانات الاستلام بدقة. سيحسب النظام الرسوم تلقائياً عند تنفيذ الطلب.</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>المبلغ SDG</label><input name='amount' type='number' min='1' required>
    <label>وسيلة الاستلام</label><select name='payout_method'><option value='BANK_TRANSFER'>تحويل بنكي</option><option value='MOBILE_WALLET'>محفظة هاتف</option><option value='OTHER'>أخرى</option></select>
    <label>بيانات الاستلام</label><textarea name='payout_details' maxlength='500' required></textarea><button>إرسال طلب السحب</button></form></section>""",csrf=session.get("csrf",""))
    return page("سحب SDG",body)

@empire_bp.get("/topups")
@login_required
def topups():
    games=[
      ("PUBG Mobile","ببجي موبايل","i-pubg","/store/topups?game=PUBG%20Mobile","pubg"),
      ("Mobile Legends","موبايل ليجند","i-ml","/store/topups?game=Mobile%20Legends","ml"),
      ("Free Fire","فري فاير","i-freefire","/store/topups?game=Free%20Fire","freefire"),
      ("Fortnite","فورتنايت","i-fortnite","/store/topups?game=Fortnite","fortnite")
    ]
    selected=request.args.get("game","").strip()
    allowed={x[0] for x in games}
    if selected and selected not in allowed: abort(404)
    if selected:
        products=db().execute("SELECT * FROM es_products WHERE game=? ORDER BY id",(selected,)).fetchall()
    else:
        products=db().execute("SELECT * FROM es_products ORDER BY CASE game WHEN 'PUBG Mobile' THEN 1 WHEN 'Free Fire' THEN 2 WHEN 'Mobile Legends' THEN 3 ELSE 4 END,id").fetchall()
    body=render_template_string("""<section class='panel game-intro'><span class='tag'>أكواد رقمية</span><h1>باقات الألعاب</h1><p>اختر اللعبة والباقة المناسبة. لا نطلب آيدي اللاعب ولا ندخل إلى حسابك؛ بعد تأكيد الدفع نسلّمك كود الباقة داخل التطبيق.</p>
    <div class='game-catalog-grid'>{% for g in games %}<a class='game-catalog-card {{g[4]}}' href='{{g[3]}}'><span class='game-catalog-icon'><svg><use href='#{{g[2]}}'/></svg></span><b>{{g[1]}}</b><small>{{g[0]}}</small></a>{% endfor %}</div>
    {% if selected %}<h2>باقات {{selected}}</h2>{% endif %}<a class='btn secondary' href='/store/topups'>كل الألعاب</a></section>
    <div class='grid game-products'>{% for p in products %}<article class='panel game-product'><span class='tag'>{{p['game']}}</span><h3>{{p['title']}}</h3><p>{{p['instructions']}}</p>
    {% if p['active'] and p['price_sdg'] > 1 %}<b class='game-price'>{{'{:,}'.format(p['price_sdg'])}} SDG</b>
    <form method='post' action='/store/topups/new'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='product_id' value='{{p['id']}}'><button class='game-buy'>شراء الكود</button></form>
    {% else %}<p class='muted'>الباقة مسجلة — غير متاحة للطلب حالياً.</p>{% endif %}
    </article>{% else %}<div class='empty-state'>لا توجد باقات مسجلة لهذه اللعبة حالياً.</div>{% endfor %}</div>""",products=products,csrf=session.get("csrf",""),games=games,selected=selected)
    return page("شحن الألعاب",body)

@empire_bp.post("/topups/new")
@login_required
def new_topup():
    csrf(); u=user()
    if setting("topups_enabled","0")!="1": abort(503,"طلبات الشحن متوقفة حالياً")
    p=db().execute("SELECT * FROM es_products WHERE id=? AND active=1 AND price_sdg>1",(request.form.get("product_id"),)).fetchone()
    if not p: abort(404)
    r=ref()
    db().execute("INSERT INTO es_topup_orders(reference,buyer,product_id,player_id,amount_sdg,status,created_at,updated_at) VALUES(?,?,?,'',?,'PENDING_PAYMENT',?,?)",
                 (r,u["username"],p["id"],p["price_sdg"],now(),now()))
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
                post(r+"F",[("user:"+u["username"],"SDG",-1000),("protection:fund","SDG",1000)],"رسوم نشر حساب للبيع")
                notify_protection_income(r+"F","SDG",1000,"رسوم نشر إعلان")
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
        fee=platform_fee(listing["price_sdg"])
        post(r,[("user:"+u["username"],"SDG",-(listing["price_sdg"]+fee)),("escrow:market","SDG",listing["price_sdg"]),("protection:fund","SDG",fee)],"حجز قيمة صفقة سوق · عمولة التطبيق 3%")
        notify_protection_income(r,"SDG",fee,"عمولة صفقة سوق")
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

def fulfill_topup_from_inventory(d, order, stock, actor, note="automatic inventory fulfillment"):
    """Consume one matching stock code and complete a paid top-up atomically."""
    if order["status"]!="PAID" or order["agent"] is not None:
        raise ValueError("الطلب ليس مؤهلاً للتسليم التلقائي.")
    cost=int(order["cost_sdg"])
    if cost<0 or cost>int(order["amount_sdg"]):
        raise ValueError("تكلفة المورد لا يمكن أن تكون سالبة أو أعلى من سعر البيع.")
    code=encryption().decrypt(stock["code_cipher"].encode("utf-8")).decode("utf-8")
    consumed=d.execute(
        "UPDATE es_game_code_inventory SET status='USED',used_order_ref=?,used_at=? WHERE id=? AND product_id=? AND status='AVAILABLE'",
        (order["reference"],now(),stock["id"],order["product_id"])
    ).rowcount
    if consumed!=1:
        raise ValueError("كود المخزون لم يعد متاحاً.")
    cipher=encryption().encrypt(code.encode("utf-8")).decode("utf-8")
    margin=int(order["amount_sdg"])-cost
    entries=[("clearing:topups","SDG",-int(order["amount_sdg"]))]
    if cost:
        entries.append(("supplier:topup_cost","SDG",cost))
    entries.append(("protection:fund","SDG",margin))
    post(order["reference"]+"T",entries,"تسوية وتسليم تلقائي لكود لعبة")
    notify_protection_income(order["reference"]+"T","SDG",margin,"هامش ربح تسليم تلقائي لكود لعبة")
    changed=d.execute(
        "UPDATE es_topup_orders SET status='COMPLETED',delivery_cipher=?,note=?,updated_at=? WHERE reference=? AND status='PAID' AND agent IS NULL",
        (cipher,note,now(),order["reference"])
    ).rowcount
    if changed!=1:
        raise ValueError("تغيرت حالة الطلب أثناء التسليم.")
    d.execute(
        "INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
        (order["buyer"],"تم تسليم كود لعبتك تلقائياً","تم التحقق من الدفع وتسليم كود الباقة. افتح طلباتي لعرضه بأمان.",order["reference"],now())
    )
    audit(actor,"TOPUP_AUTO_DELIVERED","topup",order["reference"],"inventory_code_id="+str(stock["id"])+(";supplier_cost_unset" if cost==0 else ";supplier_cost="+str(cost)))
    return code

@empire_bp.post("/topups/pay")
@login_required
def pay_topup():
    csrf(); u=user(); r=request.form.get("reference","")
    d=db(); d.execute("BEGIN IMMEDIATE")
    try:
        row=d.execute("""SELECT t.*,p.cost_sdg FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id
                           WHERE t.reference=? AND t.buyer=? AND t.status='PENDING_PAYMENT'""",(r,u["username"])).fetchone()
        if not row: raise ValueError("طلب الشحن غير متاح للدفع")
        fee=platform_fee(row["amount_sdg"])
        post(r,[("user:"+u["username"],"SDG",-(row["amount_sdg"]+fee)),("clearing:topups","SDG",row["amount_sdg"]),
                ("protection:fund","SDG",fee)],"دفع طلب شحن ألعاب من المحفظة · عمولة التطبيق 3%")
        notify_protection_income(r,"SDG",fee,"عمولة شحن ألعاب")
        d.execute("UPDATE es_topup_orders SET status='PAID',updated_at=? WHERE reference=? AND status='PENDING_PAYMENT'",(now(),r))
        row=d.execute("""SELECT t.*,p.cost_sdg FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id
                         WHERE t.reference=? AND t.status='PAID'""",(r,)).fetchone()
        stock=d.execute("SELECT * FROM es_game_code_inventory WHERE product_id=? AND status='AVAILABLE' ORDER BY id LIMIT 1",(row["product_id"],)).fetchone()
        if stock and 0<=int(row["cost_sdg"])<=int(row["amount_sdg"]):
            fulfill_topup_from_inventory(d,row,stock,u["username"])
            d.commit()
            delivered=True
        else:
            audit(u["username"],"TOPUP_PAID","topup",r,"awaiting matching game code inventory or valid supplier cost")
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                      (u["username"],"تم استلام دفع طلب الشحن","تم خصم المبلغ من محفظتك. سيُسلّم الكود تلقائياً فور توفر كود مطابق وتكلفة مورد صحيحة.",r,now()))
            d.commit()
            delivered=False
    except Exception: d.rollback(); raise
    flash("تم الدفع وتسليم كود الباقة تلقائياً." if delivered else "تم الدفع بنجاح. سيُسلّم الكود تلقائياً فور توفره في المخزون واعتماد تكلفة المورد.")
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
    if len(note)>300: abort(400)
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
                    ("user:"+u["username"],"SDG",commission),("protection:fund","SDG",platform_profit)],
             "تسوية شحن لعبة وتسجيل عمولة الوكيل")
        notify_protection_income(r+"T","SDG",platform_profit,"حصة المنصة من ربح شحن الألعاب")
        delivery_code=request.form.get("delivery_code","").strip()
        if len(delivery_code)<4 or len(delivery_code)>300:
            raise ValueError("أدخل كود الباقة الصحيح (4 إلى 300 حرف).")
        delivery_cipher=encryption().encrypt(delivery_code.encode("utf-8")).decode("utf-8")
        d.execute("UPDATE es_topup_orders SET status='COMPLETED',note=?,delivery_cipher=?,updated_at=? WHERE reference=? AND agent=? AND status='ASSIGNED'",
                  (note,delivery_cipher,now(),r,u["username"]))
        d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                  (order["buyer"],"كود لعبتك جاهز","تم تجهيز كود باقتك. افتح طلباتك لعرض الكود بأمان.",r,now()))
        audit(u["username"],"TOPUP_COMPLETED","topup",r,"commission="+str(commission)+"; code delivered")
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
    top_rows=db().execute("SELECT t.*,p.title,p.game FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id WHERE t.buyer=? OR t.agent=? ORDER BY t.id DESC LIMIT 100",(u["username"],u["username"])).fetchall()
    top=[]
    for row in top_rows:
        item=dict(row); item["delivery_code_text"]=""
        if item["buyer"]==u["username"] and item["status"]=="COMPLETED" and item.get("delivery_cipher"):
            try: item["delivery_code_text"]=encryption().decrypt(item["delivery_cipher"].encode()).decode()
            except Exception: item["delivery_code_text"]="تعذر عرض الكود حالياً؛ تواصل مع الدعم."
        top.append(item)
    agent=db().execute("SELECT status FROM es_agents WHERE username=?",(u["username"],)).fetchone()
    open_jobs=db().execute("SELECT t.*,p.title,p.game FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id WHERE t.status='PAID' AND t.agent IS NULL ORDER BY t.id LIMIT 50").fetchall() if agent and agent["status"]=="APPROVED" else []
    topup_labels={"PENDING_PAYMENT":"بانتظار الدفع","PAID":"مدفوع — بانتظار التنفيذ","ASSIGNED":"قيد التنفيذ لدى الوكيل","PROCESSING":"قيد التنفيذ","COMPLETED":"تم تسليم الكود","REJECTED":"مرفوض","REFUNDED":"مسترد"}
    wallet_sdg=balance(u["username"],"SDG")
    for item in top:
        item["fee_sdg"]=platform_fee(item["amount_sdg"])
        item["total_sdg"]=int(item["amount_sdg"])+item["fee_sdg"]
    body=render_template_string("""<section class='panel'><h1>طلباتي</h1><h2>صفقات الحسابات</h2>{% for x in markets %}<article class='panel'><b>{{x['reference']}}</b> · {{x['price_sdg']}} SDG · {{x['status']}}<p>المشتري: {{x['buyer']}} — البائع: {{x['seller']}}</p>
    {% if x['seller']==me and x['status']=='ESCROWED' %}<form method='post' action='/store/order/deliver'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><textarea name='delivery' minlength='10' maxlength='3000' placeholder='بيانات التسليم الآمنة' required></textarea><button>تسليم للمشتري</button></form>{% endif %}
    {% if x['buyer']==me and x['status']=='DELIVERED' %}<div class='panel'><b>بيانات التسليم</b><pre style='white-space:pre-wrap;overflow-wrap:anywhere'>{{x['delivery_text']}}</pre></div><form method='post' action='/store/order/confirm'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>تأكيد الاستلام وإطلاق مستحقات البائع</button></form>{% endif %}
    {% if x['status'] in ('ESCROWED','DELIVERED') and (x['buyer']==me or x['seller']==me) %}<form method='post' action='/store/order/dispute'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><input name='reason' maxlength='500' required placeholder='سبب النزاع'><button>فتح نزاع</button></form>{% endif %}</article>{% else %}<p>لا توجد صفقات.</p>{% endfor %}
    <h2>أكواد الألعاب وطلباتي</h2>{% for x in top %}<article class='panel game-order-card'><div class='order-head'><b>{{x['game']}} · {{x['title']}}</b><span class='tag'>{{topup_labels.get(x['status'],x['status'])}}</span></div><p>رقم الطلب: <b dir='ltr'>{{x['reference']}}</b> · قيمة الباقة {{'{:,}'.format(x['amount_sdg'])}} SDG</p>
    {% if x['buyer']==me and x['status']=='COMPLETED' and x['delivery_code_text'] %}<div class='redeem-code'><small>كود الباقة الخاص بك</small><strong dir='ltr'>{{x['delivery_code_text']}}</strong><button type='button' class='btn secondary' onclick='navigator.clipboard&&navigator.clipboard.writeText({{x['delivery_code_text']|tojson}}).then(()=>this.textContent="تم النسخ").catch(()=>{})'>نسخ الكود</button></div>{% endif %}
    {% if x['buyer']==me and x['status']=='PENDING_PAYMENT' %}<div class='panel'><b>طريقة الدفع</b><p>الدفع من محفظتك SDG — رصيدك الحالي: {{'{:,}'.format(wallet_sdg)}} SDG</p><form method='post' action='/store/topups/pay'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>تأكيد الدفع من المحفظة</button></form><small class='muted'>يمكنك تغذية المحفظة أولاً عبر حسابات الاستقبال المعتمدة إذا لم يكفِ الرصيد.</small></div>{% endif %}
    {% if agent and agent['status']=='APPROVED' and x['status']=='PAID' and not x['agent'] %}<form method='post' action='/store/agent/topup/claim'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>استلام الطلب كوكيل</button></form>{% endif %}
    {% if x['agent']==me and x['status']=='ASSIGNED' %}<form method='post' action='/store/agent/topup/complete'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><label>كود الباقة</label><input name='delivery_code' maxlength='300' required><label>مرجع المورد (اختياري)</label><input name='note' maxlength='300' placeholder='مرجع التنفيذ'><button>تسليم الكود للعميل</button></form>{% endif %}
    </article>{% else %}<p>لا توجد طلبات.</p>{% endfor %}
    {% if agent and agent['status']=='APPROVED' %}<h2>طلبات متاحة للوكلاء</h2>{% for x in open_jobs %}<article class='panel'><p>{{x['reference']}} · {{x['game']}} {{x['title']}} · {{'{:,}'.format(x['amount_sdg'])}} SDG</p>
    <form method='post' action='/store/agent/topup/claim'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='reference' value='{{x['reference']}}'><button>استلام الطلب</button></form></article>{% else %}<p>لا توجد طلبات متاحة.</p>{% endfor %}{% endif %}</section>""",markets=markets,top=top,agent=agent,open_jobs=open_jobs,topup_labels=topup_labels,wallet_sdg=wallet_sdg,me=u["username"],csrf=session.get("csrf",""))
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
        seller_amount=row["price_sdg"]
        post(r+"R",[("escrow:market","SDG",-row["price_sdg"]),("user:"+row["seller"],"SDG",seller_amount)],"تحرير صفقة بعد تأكيد المشتري — العمولة حُصّلت من المشتري")
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
    premium=db().execute("SELECT points FROM es_loyalty_members WHERE username=?",(target["username"],)).fetchone()
    return jsonify({"ok":True,"name":target["full_name"],"account_number":target["account_number"],
                    "verification":"موثق" if verified else "غير موثق","verified":verified,
                    "premium":bool(premium),"premium_badge":"❄ عميل آيس ستوري مميز" if premium else "",
                    })

@empire_bp.route("/transfer-epc",methods=["GET","POST"])
@login_required
def transfer_epc():
    u=user()
    if request.method=="POST":
        csrf()
        action=request.form.get("action","lookup").strip()
        account=request.form.get("recipient_account","").strip()
        target=db().execute("SELECT username,full_name,account_number,active FROM users WHERE account_number=?",(account,)).fetchone() if re.fullmatch(r"[0-9]{5,20}",account) else None
        if action=="lookup":
            if not target or not target["active"] or target["username"]==u["username"]:
                flash("لم نعثر على حساب مستلم صالح. راجع الرقم وحاول مجدداً.")
                return redirect(url_for("empire.transfer_epc"))
            status=db().execute("SELECT status FROM es_identity_status WHERE username=?",(target["username"],)).fetchone()
            verified=bool(status and status["status"]=="VERIFIED")
            premium=db().execute("SELECT points FROM es_loyalty_members WHERE username=?",(target["username"],)).fetchone()
            # Bind the confirmation step to this authenticated session; a forged execute POST is rejected.
            session["transfer_verified_account"]=target["account_number"]
            body=render_template_string("""<section class='panel transfer-step'><span class='tag'>الخطوة 2 من 2 · تم التحقق</span><h1>تأكيد بيانات المستلم</h1>
              <div class='recipient-confirm'><small>سيتم التحويل إلى</small><strong>{{target['full_name']}}</strong><span dir='ltr'>{{target['account_number']}}</span><em class='{{"verified" if verified else "unverified"}}'>{{"حساب موثق" if verified else "حساب غير موثق"}}</em>
              {% if premium %}<strong class='ice-badge'>❄ عميل آيس ستوري مميز</strong>{% endif %}</div>
              <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='execute'><input type='hidden' name='recipient_account' value='{{target['account_number']}}'>
              <label>عملة التحويل</label><select name='currency'><option value='SDG'>الجنيه السوداني · SDG</option><option value='URC'>URC</option></select>
              <label>المبلغ الذي سيصل للمستلم</label><input name='quantity' type='number' min='1' step='1' inputmode='numeric' placeholder='أدخل المبلغ' required>
              <label>ملاحظات للمستلم <span class='muted'>(اختياري)</span></label><textarea name='note' maxlength='250' placeholder='اكتب ملاحظة مختصرة'></textarea>
              <button class='transfer-submit'>تأكيد وإرسال التحويل</button></form>
              <a class='btn secondary' href='/store/transfer-epc'>تغيير رقم الحساب</a></section>""",target=target,verified=verified,csrf=session.get("csrf",""))
            return page("تأكيد المستلم",body,premium=premium)
        if action!="execute":
            abort(400,"طلب تحويل غير صالح")
        verified_account=session.pop("transfer_verified_account",None)
        if not verified_account or not secrets.compare_digest(str(verified_account),account):
            abort(400,"يجب التحقق من حساب المستلم أولاً. أعد خطوة التحقق.")
        if not target or not target["active"] or target["username"]==u["username"]:
            abort(400,"تعذر التحقق من المستلم. ابدأ العملية من جديد.")
        currency=request.form.get("currency","URC").strip()
        qty=money_int(request.form.get("quantity"))
        note=request.form.get("note","").strip()[:250]
        if currency not in ("SDG","URC"):
            abort(400,"عملة التحويل غير مدعومة")
        r=ref(); d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            current_target=d.execute("SELECT username,full_name,account_number,active FROM users WHERE account_number=?",(account,)).fetchone()
            if not current_target or not current_target["active"] or current_target["username"]==u["username"]:
                raise ValueError("حساب المستلم لم يعد صالحاً.")
            fee=platform_fee(qty)
            post(r,[("user:"+u["username"],currency,-qty),("user:"+current_target["username"],currency,qty),
                    ("user:"+u["username"],currency,-fee),("protection:fund",currency,fee)],
                 "تحويل "+currency+" إلى "+current_target["full_name"]+" · عمولة التطبيق 3%")
            notify_protection_income(r,currency,fee,"عمولة تحويل محفظة")
            d.execute("INSERT INTO es_transfer_details(reference,sender,recipient,recipient_account,currency,note,created_at) VALUES(?,?,?,?,?,?,?)",
                      (r,u["username"],current_target["username"],account,currency,note,now()))
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                      (current_target["username"],"وصلك تحويل جديد","استلمت تحويلاً بقيمة "+format(qty,",")+" "+currency+" من "+u["full_name"]+" · رقم العملية "+r,r,now()))
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                      (u["username"],"تم التحويل بنجاح","تم إرسال "+format(qty,",")+" "+currency+" إلى "+current_target["full_name"]+" · رقم العملية "+r,r,now()))
            audit(u["username"],"WALLET_TRANSFER","wallet",r,"to="+current_target["username"]+";currency="+currency)
            d.commit()
        except ValueError as exc:
            d.rollback()
            flash(str(exc))
            return redirect(url_for("empire.transfer_epc"))
        except Exception:
            d.rollback()
            raise
        session.pop("transfer_verified_account",None)
        return redirect(url_for("empire.transfer_receipt",reference=r))
    body=render_template_string("""<section class='panel transfer-step'><span class='tag'>تحويل آمن · الخطوة 1 من 2</span><h1>إلى من تريد التحويل؟</h1><p>أدخل رقم حساب المستلم واضغط تحقق. لن نعرض أي رصيد، ولن نطلب منك المبلغ إلا بعد التأكد من بيانات المستلم.</p>
      <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='lookup'>
      <label>رقم حساب المستلم</label><input name='recipient_account' inputmode='numeric' pattern='[0-9]{5,20}' maxlength='20' placeholder='رقم الحساب' required>
      <button class='transfer-submit'>تحقق من الحساب</button></form></section>""",csrf=session.get("csrf",""))
    return page("التحويلات",body)

@empire_bp.get("/transfer/receipt/<reference>")
@login_required
def transfer_receipt(reference):
    u=user()
    detail=db().execute("SELECT * FROM es_transfer_details WHERE reference=?",(reference,)).fetchone()
    if not detail:
        abort(404)
    sender=detail["sender"]
    recipient=detail["recipient"]
    if u["username"] not in (sender,recipient):
        abort(404)
    credit=db().execute(
        "SELECT currency,delta,created_at FROM es_ledger WHERE reference=? AND account=? AND delta>0 ORDER BY line_no LIMIT 1",
        (reference,"user:"+recipient)
    ).fetchone()
    if not credit:
        abort(404)
    sender_user=db().execute("SELECT full_name,account_number FROM users WHERE username=?",(sender,)).fetchone()
    recipient_user=db().execute("SELECT full_name,account_number FROM users WHERE username=?",(recipient,)).fetchone()
    if not sender_user or not recipient_user:
        abort(404)
    body=render_template_string("""<section class='transfer-receipt-clean'>
      <header class='trc-header'>
        <div class='trc-check' aria-hidden='true'>✓</div>
        <span class='trc-label'>إيصال تحويل</span>
        <h1>تم التحويل بنجاح</h1>
        <p>تم تسجيل العملية في محفظة الإمبراطورية إستوري</p>
      </header>
      <section class='trc-amount'>
        <small>المبلغ المحوّل</small>
        <strong dir='ltr'>{{'{:,}'.format(amount)}} <span>{{currency}}</span></strong>
        <span class='trc-status'>عملية مكتملة</span>
      </section>
      <section class='trc-parties'>
        <div class='trc-person'><small>المرسل</small><b>{{sender_name}}</b><span dir='ltr'>{{sender_account}}</span></div>
        <div class='trc-arrow' aria-hidden='true'>←</div>
        <div class='trc-person'><small>المستلم</small><b>{{recipient_name}}</b><span dir='ltr'>{{recipient_account}}</span></div>
      </section>
      {% if note %}<section class='trc-field'><small>ملاحظة</small><b>{{note}}</b></section>{% endif %}
      <section class='trc-meta'>
        <div><small>رقم العملية</small><b dir='ltr'>{{reference}}</b></div>
        <div><small>التاريخ والوقت (UTC)</small><b dir='ltr'>{{created_at}}</b></div>
      </section>
      <p class='trc-foot'>هذا الإيصال يثبت إتمام التحويل. لا يتضمن أرصدة الحسابات أو تفاصيل داخلية.</p>
      <div class='trc-actions'><button type='button' onclick='window.print()'>حفظ الإيصال</button><a class='btn secondary' href='/store/history'>سجل المعاملات</a><a class='btn secondary' href='/store/'>الرئيسية</a></div>
    </section>""",
      amount=int(credit["delta"]),currency=credit["currency"],reference=reference,
      sender_name=sender_user["full_name"],sender_account=sender_user["account_number"],
      recipient_name=recipient_user["full_name"],recipient_account=recipient_user["account_number"],
      note=(detail["note"] or "").strip(),created_at=credit["created_at"])
    return page("إيصال التحويل",body)
  
@empire_bp.get("/exchange/quote")
@login_required
def exchange_quote():
    direction=(request.args.get("direction") or "").strip()
    if direction not in ("URC_TO_SDG","SDG_TO_URC"):
        return jsonify({"ok":False,"message":"اختر اتجاه التحويل."}),400
    mode=setting("fx_rate_mode","manual")
    try:
        rate=effective_fx_rate(direction)
    except (TypeError,ValueError):
        return jsonify({"ok":False,"message":"السعر غير متاح حالياً."}),503
    source="URC" if direction=="URC_TO_SDG" else "SDG"; target="SDG" if source=="URC" else "URC"
    wallet=max(0,int(balance(user()["username"],source)))
    minimum=1 if source=="URC" else max(1,rate)
    lo,hi=0,wallet
    while lo<hi:
        mid=(lo+hi+1)//2
        if mid+platform_fee(mid)<=wallet: lo=mid
        else: hi=mid-1
    # Internal conversion uses digital balances only. Cash liquidity is not
    # consulted here; only SDG withdrawals reserve real payout liquidity.
    supply_limit=999_999_999_999
    if source=="SDG" and rate>0:
        supply_limit=max(0,(remaining_epc_supply()+1)*rate-1)
    maximum=min(supply_limit,lo,999_999_999_999)
    data={"ok":True,"direction":direction,"source_currency":source,"target_currency":target,
          "rate":rate,"minimum":minimum,"maximum":maximum,"max_convertible":maximum,
          "wallet_balance":wallet,"available":maximum>=minimum,
          "mode":"market" if mode=="market" else "manual"}
    raw=(request.args.get("amount") or "").strip()
    if raw:
        if not re.fullmatch(r"[0-9]{1,12}",raw):
            data.update({"valid":False,"message":"أدخل مبلغاً صحيحاً."})
        else:
            amount=int(raw)
            if rate<=0:
                data.update({"valid":False,"message":"سعر التحويل غير متاح حالياً."})
            else:
                converted=amount*rate if source=="URC" else amount//rate
                fee=platform_fee(amount)
                if converted<0 or converted>9_000_000_000_000_000_000:
                    data.update({"valid":False,"message":"قيمة التحويل غير صالحة."})
                elif amount<minimum or amount>maximum:
                    # Always return a live preview; availability controls confirmation, not calculation.
                    data.update({"valid":False,"amount":amount,"converted":converted,"fee":fee,
                                 "total_debit":amount+fee,
                                 "message":"تم حساب المقابل، لكن رصيدك غير كافٍ لإتمام التحويل."})
                else:
                    quote_id=secrets.token_urlsafe(24); issued=int(datetime.now(timezone.utc).timestamp()); d=db()
                    d.execute("DELETE FROM es_fx_quotes WHERE expires_at<?",(issued-86400,))
                    d.execute("""INSERT INTO es_fx_quotes
                      (quote_id,username,direction,amount,rate,fee,result,expires_at,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                      (quote_id,user()["username"],direction,amount,rate,fee,converted,issued+60,now()))
                    data.update({"valid":True,"amount":amount,"converted":converted,"fee":fee,
                                 "total_debit":amount+fee,"quote_id":quote_id,"expires_in":60})
    response=jsonify(data); response.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"]="no-cache"
    return response


@empire_bp.route("/exchange",methods=["GET","POST"])
@login_required
def exchange():
    u=user()
    def effective_rate(direction):
        return effective_fx_rate(direction)
    def affordable_limit(wallet_amount):
        lo,hi=0,max(0,int(wallet_amount))
        while lo<hi:
            mid=(lo+hi+1)//2
            if mid+platform_fee(mid)<=wallet_amount: lo=mid
            else: hi=mid-1
        return lo
    def calculate_limit(direction):
        rate=effective_rate(direction)
        if rate<=0: return {"rate":rate,"minimum":1,"maximum":0,"currency":"URC" if direction=="URC_TO_SDG" else "SDG"}
        if direction=="URC_TO_SDG":
            minimum=1; wallet_amount=balance(u["username"],"URC"); currency="URC"
            supply_limit=999_999_999_999
        else:
            minimum=rate; wallet_amount=balance(u["username"],"SDG"); currency="SDG"
            supply_limit=max(0,(remaining_epc_supply()+1)*rate-1)
        # The conversion itself is digital in both directions; never gate it
        # on the cash payout reserve.
        return {"rate":rate,"minimum":minimum,"maximum":min(supply_limit,affordable_limit(wallet_amount),999_999_999_999),"currency":currency}
    if request.method=="POST":
        csrf(); direction=(request.form.get("direction") or "").strip()
        quote_id=(request.form.get("quote_id") or "").strip()
        try: amount=money_int(request.form.get("amount"))
        except ValueError:
            flash("المبلغ غير صالح."); return redirect(url_for("empire.exchange"))
        if direction not in ("URC_TO_SDG","SDG_TO_URC") or not quote_id:
            flash("حدّث السعر ثم راجع التحويل قبل التأكيد."); return redirect(url_for("empire.exchange"))
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            quote=d.execute("SELECT * FROM es_fx_quotes WHERE quote_id=?",(quote_id,)).fetchone()
            epoch=int(datetime.now(timezone.utc).timestamp())
            if (not quote or quote["username"]!=u["username"] or quote["direction"]!=direction
                or int(quote["amount"])!=amount or quote["used_at"] is not None or int(quote["expires_at"])<epoch):
                raise ValueError("انتهى عرض السعر. حدّث السعر وحاول مجدداً.")
            # BEGIN IMMEDIATE serializes this read with owner price updates.
            # The same live helper is used by quote generation and execution.
            limits=calculate_limit(direction)
            if amount<limits["minimum"] or amount>limits["maximum"]:
                raise ValueError("تغيّر رصيدك أو المعروض الرقمي المتاح. حدّث السعر وحاول مجدداً.")
            rate=limits["rate"]; result=amount*rate if direction=="URC_TO_SDG" else amount//rate; fee=platform_fee(amount)
            if rate!=int(quote["rate"]) or result!=int(quote["result"]) or fee!=int(quote["fee"]):
                raise ValueError("تغيّر سعر التحويل. حدّث السعر لمعاينة عرض جديد.")
            if result<=0 or result>9_000_000_000_000_000_000: raise ValueError("قيمة التحويل غير صالحة.")
            r=ref()
            if direction=="URC_TO_SDG":
                # URC converted to digital SDG is burned from circulation;
                # the SDG credit is an internal digital issuance, not a cash payout.
                post(r,[("user:"+u["username"],"URC",-amount),("burn:urc","URC",amount),
                        ("user:"+u["username"],"SDG",result),("conversion:sdg","SDG",-result),
                        ("user:"+u["username"],"URC",-fee),("protection:fund","URC",fee)],
                     "تحويل رقمي URC إلى SDG · حرق URC · عمولة التطبيق 3%")
                source_currency,target_currency="URC","SDG"
            else:
                # SDG is a digital balance too. Its conversion does not use
                # the cash reserve; URC issuance remains bounded by the supply cap.
                assert_epc_capacity(result)
                post(r,[("user:"+u["username"],"SDG",-amount),("burn:sdg","SDG",amount),
                        ("user:"+u["username"],"URC",result),("issuer:urc","URC",-result),
                        ("user:"+u["username"],"SDG",-fee),("protection:fund","SDG",fee)],
                     "تحويل رقمي SDG إلى URC · عمولة التطبيق 3%")
                source_currency,target_currency="SDG","URC"
            audit(u["username"],"CURRENCY_EXCHANGE","exchange",r,
                  direction+" rate="+str(rate)+";source_amount="+str(amount)+";target_amount="+str(result)+";fee="+str(fee))
            msg=("اكتمل تحويل "+format(amount,",")+" "+source_currency+" إلى "+format(result,",")+" "+target_currency+
                 ". رسوم العملية: "+format(fee,",")+" "+source_currency+". رقم العملية: "+r)
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                      (u["username"],"تم تحويل العملات بنجاح",msg,r,now()))
            founder=d.execute("SELECT username FROM users WHERE role='founder' ORDER BY id LIMIT 1").fetchone()
            if founder and fee>0:
                d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                          (founder["username"],"إيراد جديد لصندوق حماية المستخدمين",
                           "تم تسجيل رسوم صرف بقيمة "+format(fee,",")+" "+source_currency+" في صندوق حماية المستخدمين. رقم العملية: "+r, r,now()))
            d.execute("UPDATE es_fx_quotes SET used_at=? WHERE quote_id=? AND used_at IS NULL",(now(),quote_id))
            if d.execute("SELECT changes()").fetchone()[0]!=1: raise ValueError("تم استخدام عرض السعر مسبقاً.")
            d.commit()
        except ValueError as exc:
            d.rollback(); flash(str(exc)); return redirect(url_for("empire.exchange"))
        except Exception:
            d.rollback()
            from flask import current_app
            current_app.logger.exception("Currency exchange failed for user %s",u["username"])
            flash("تعذر تنفيذ التحويل. لم يتم اعتماد العملية."); return redirect(url_for("empire.exchange"))
        flash("تم التحويل بنجاح."); return redirect(url_for("empire.transaction_history_detail",reference=r))
    user_sdg=balance(u["username"],"SDG"); user_urc=balance(u["username"],"URC")
    urc_limits=calculate_limit("URC_TO_SDG"); sdg_limits=calculate_limit("SDG_TO_URC")
    return page("تحويل العملة",render_template_string("""<style>
.fx-shell{max-width:560px;margin:0 auto;padding:8px 0 24px}.fx-head{text-align:center;padding:8px 8px 18px}
.fx-head h1{font-size:24px;margin:0 0 5px;color:var(--text,#171717)}.fx-head p{margin:0;color:var(--muted,#777);font-size:13px}
.fx-card{background:var(--surface,#fff);border:1px solid var(--border,#e8e8e8);border-radius:18px;padding:16px;margin:12px 0;box-shadow:0 8px 28px #1111}
.fx-pair{display:grid;grid-template-columns:1fr 42px 1fr;gap:9px;align-items:end}
.fx-label{display:block;font-size:12px;font-weight:700;margin:0 0 8px;color:var(--text,#222)}
.fx-select,.fx-input{width:100%;box-sizing:border-box;border:1px solid var(--border,#ddd);border-radius:12px;padding:13px;background:var(--surface,#fff);color:var(--text,#222);font-size:15px;outline:none}
.fx-input:focus,.fx-select:focus{border-color:#c59b44;box-shadow:0 0 0 3px #c59b4422}
.fx-swap{height:44px;width:42px;border:1px solid var(--border,#ddd);border-radius:12px;background:var(--surface,#fff);color:#9a762d;font-size:23px;cursor:pointer}
.fx-balance{display:flex;justify-content:space-between;gap:10px;margin-top:13px;font-size:12px;color:var(--muted,#777)}
.fx-result{margin-top:16px;padding:16px;border:1px solid #e8d7aa;border-radius:15px;background:linear-gradient(145deg,#fbf7eb,#fff);text-align:center;color:#222}
.fx-result small{display:block;color:#78663e;font-size:12px}.fx-result strong{display:block;font-size:25px;margin-top:6px;color:#8d6a24;direction:ltr}
.fx-rate{font-size:12px;text-align:center;color:var(--muted,#777);margin-top:10px}
.fx-message{min-height:18px;text-align:center;font-size:12px;color:var(--muted,#777);margin:10px 0 0}
.fx-submit{width:100%;border:0;border-radius:13px;padding:14px;margin-top:14px;background:linear-gradient(110deg,#d9b65f,#b68a31);color:#171717;font-size:16px;font-weight:800;cursor:pointer}
.fx-submit:disabled{opacity:.45;cursor:not-allowed}
@media(max-width:420px){.fx-card{padding:13px}.fx-pair{gap:6px}}
</style>
<section class="fx-shell"><header class="fx-head"><h1>تحويل العملة</h1><p>حوّل بين URC وSDG بالسعر المحدد</p></header>
<form method="post" id="fxForm" class="fx-card">
<input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="quote_id" id="fxQuoteId" value="">
<input type="hidden" name="direction" id="fxDirection" value="URC_TO_SDG">
<div class="fx-pair">
<div><label class="fx-label" for="fxFrom">من</label><select class="fx-select" id="fxFrom"><option value="URC">URC</option><option value="SDG">SDG</option></select></div>
<button class="fx-swap" type="button" id="fxSwap" aria-label="عكس اتجاه التحويل">⇄</button>
<div><label class="fx-label" for="fxTo">إلى</label><select class="fx-select" id="fxTo"><option value="SDG">SDG</option><option value="URC">URC</option></select></div>
</div>
<div class="fx-balance"><span>رصيدك المتاح</span><b id="fxBalance">{{'{:,}'.format(user_urc)}} URC</b></div>
<div style="height:16px"></div>
<label class="fx-label" for="fxAmount">المبلغ</label>
<input class="fx-input" name="amount" id="fxAmount" type="number" min="1" step="1" inputmode="numeric" placeholder="أدخل المبلغ" autocomplete="off" required>
<div class="fx-result" aria-live="polite"><small>المبلغ المقابل</small><strong id="fxResult">—</strong></div>
<div class="fx-rate" id="fxRate">سعر الصرف: —</div>
<p class="fx-message" id="fxMessage">أدخل المبلغ لعرض القيمة المقابلة.</p>
<button class="fx-submit" id="fxSubmit" type="submit" disabled>تحويل الآن</button>
</form></section>
<script>(function(){
const from=document.getElementById('fxFrom'),to=document.getElementById('fxTo'),swap=document.getElementById('fxSwap');
const direction=document.getElementById('fxDirection'),amount=document.getElementById('fxAmount'),submit=document.getElementById('fxSubmit');
const balanceOut=document.getElementById('fxBalance'),result=document.getElementById('fxResult'),rateOut=document.getElementById('fxRate');
const message=document.getElementById('fxMessage'),quoteId=document.getElementById('fxQuoteId');
const fmt=n=>{const v=String(n??'0');try{return /^-?\d+$/.test(v)?BigInt(v).toLocaleString('en-US'):Number(n).toLocaleString('en-US');}catch(_){return '—';}};
let requestNo=0,debounce=null;
function clearQuote(){quoteId.value='';submit.disabled=true;}
function setPair(){if(from.value===to.value)to.value=from.value==='URC'?'SDG':'URC';direction.value=from.value+'_TO_'+to.value;update();}
async function update(){
 const id=++requestNo,src=from.value,dst=to.value,raw=amount.value.trim();
 direction.value=src+'_TO_'+dst;clearQuote();result.textContent='—';rateOut.textContent='سعر الصرف: —';
 if(raw&&!/^\d{1,12}$/.test(raw)){message.textContent='أدخل مبلغاً صحيحاً.';return;}
 message.textContent=raw?'جارٍ حساب المبلغ المقابل…':'أدخل المبلغ لعرض القيمة المقابلة.';
 try{
  const q=new URLSearchParams({direction:direction.value});if(raw)q.set('amount',raw);
  const response=await fetch('/store/exchange/quote?'+q.toString(),{credentials:'same-origin',cache:'no-store',headers:{'Accept':'application/json'}});
  const data=await response.json();if(id!==requestNo)return;
  if(!response.ok||!data.ok)throw new Error(data.message||'تعذر حساب التحويل.');
  balanceOut.textContent=fmt(data.wallet_balance)+' '+src;
  rateOut.textContent='سعر الصرف: '+fmt(data.rate)+' SDG لكل URC';
  if(!raw)return;
  if(data.converted!==undefined)result.textContent=fmt(data.converted)+' '+dst;
  if(!data.valid){message.textContent=data.message||'الرصيد أو السيولة غير كافية.';return;}
  quoteId.value=data.quote_id;submit.disabled=false;message.textContent='الرصيد كافٍ، يمكنك تنفيذ التحويل.';
 }catch(error){if(id!==requestNo)return;message.textContent=error.message;clearQuote();}
}
from.addEventListener('change',setPair);to.addEventListener('change',setPair);
swap.addEventListener('click',()=>{const v=from.value;from.value=to.value;to.value=v;setPair();});
amount.addEventListener('input',()=>{clearTimeout(debounce);clearQuote();debounce=setTimeout(update,120);});
document.getElementById('fxForm').addEventListener('submit',e=>{if(!quoteId.value||submit.disabled){e.preventDefault();return;}submit.disabled=true;submit.textContent='جارٍ التحويل…';});
})();</script>""",user_sdg=user_sdg,user_urc=user_urc,csrf=session.get("csrf","")))


@empire_bp.route("/admin/exchange",methods=["GET","POST"])
@login_required
@admin_permission("exchange")
def admin_exchange():
    seed_owner_assets()
    u=user()
    if request.method=="POST":
        csrf()
        action=request.form.get("action","settings")
        if u["role"] != "founder":
            abort(403)
        if action=="liquidity":
            currency=request.form.get("currency","")
            amount=money_int(request.form.get("amount"))
            if currency not in ("URC","SDG"): abort(400)
            d=db(); d.execute("BEGIN IMMEDIATE")
            try:
                post(ref(),[("user:"+u["username"],currency,-amount),("fx:reserve",currency,amount)],"تمويل احتياطي الصرف من محفظة المالك")
                audit(u["username"],"FX_LIQUIDITY_ADDED","fx_pool",currency+" "+str(amount))
                d.commit()
            except Exception:
                d.rollback()
                raise
            flash("تمت إضافة السيولة من محفظة المالك إلى احتياطي الصرف.")
            return redirect(url_for("empire.admin_exchange"))
        if action=="external_liquidity":
            amount=money_int(request.form.get("amount"))
            external_ref=(request.form.get("external_reference") or "").strip()
            confirmed=request.form.get("cash_confirmed")=="yes"
            if not confirmed or not 4<=len(external_ref)<=100:
                raise ValueError("أدخل مرجع الاستلام وأكد استلام مبلغ SDG فعلياً قبل تسجيل السيولة.")
            d=db(); d.execute("BEGIN IMMEDIATE")
            try:
                ledger_ref="FXEXT"+hashlib.sha256(external_ref.encode("utf-8")).hexdigest()[:24].upper()
                post(ledger_ref,[("external:fx_cash_reserve","SDG",-amount),("cash:sdg_reserve","SDG",amount)],
                     "إيداع احتياطي SDG خارجي مؤكد من المالك")
                audit(u["username"],"FX_EXTERNAL_SDG_LIQUIDITY","fx_pool",ledger_ref,
                      str(amount)+" SDG; external_reference="+external_ref)
                d.commit()
            except Exception:
                d.rollback()
                raise
            flash("تم تسجيل احتياطي SDG بعد تأكيد المالك استلام النقد فعلياً.")
            return redirect(url_for("empire.admin_exchange"))
        mode=request.form.get("fx_rate_mode","manual")
        market=request.form.get("fx_market_rate","0").strip()
        manual=request.form.get("fx_manual_rate","0").strip()
        buy=request.form.get("fx_buy_spread_bps","0").strip()
        sell=request.form.get("fx_sell_spread_bps","0").strip()
        if mode not in ("market","manual") or not all(re.fullmatch(r"[0-9]{1,12}",v) for v in (market,manual,buy,sell)) or (mode=="market" and int(market)<=0) or (mode=="manual" and int(manual)<=0) or int(buy)>10000 or int(sell)>10000: abort(400)
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            for k,v in (("fx_rate_mode",mode),("fx_market_rate",market),("fx_manual_rate",manual),("fx_buy_spread_bps",buy),("fx_sell_spread_bps",sell)):
                d.execute("INSERT INTO es_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,v))
            # A changed owner rate must never leave an old, still-confirmable quote.
            d.execute("DELETE FROM es_fx_quotes WHERE used_at IS NULL")
            audit(u["username"],"FX_RATES_UPDATED","settings",mode,
                  "market="+market+";manual="+manual+";buy_bps="+buy+";sell_bps="+sell)
            d.commit()
        except Exception:
            d.rollback()
            raise
        flash("تم حفظ سعر الصرف وإلغاء عروض الأسعار القديمة. السعر الجديد مطبق على العروض التالية.")
    is_founder=u["role"]=="founder"
    pools={c:fx_reserve_balance(c) for c in ("URC","SDG")} if is_founder else {"URC":0,"SDG":0}
    cash_total=cash_sdg_balance() if is_founder else 0
    cash_pending=pending_sdg_withdrawals() if is_founder else 0
    cash_available=max(0,cash_total-cash_pending)
    owner_row=db().execute("SELECT username FROM users WHERE role='founder' ORDER BY id LIMIT 1").fetchone()
    owner_username=owner_row["username"] if owner_row else u["username"]
    owner={c:balance(owner_username,c) for c in ("URC","SDG")} if is_founder else {"URC":0,"SDG":0}
    return page("إدارة الصرف",render_template_string("""<section class='panel'><h1>إدارة الصرف</h1><p class='muted'>احتياطي الصرف مستقل محاسبياً عن صندوق حماية المستخدمين؛ رسوم التطبيق وإيراداته لا تُستخدم لتغطية التحويلات.</p>
      {% if is_founder %}<div class='grid'><article class='panel'><small>أصول المالك URC</small><h2>{{'{:,}'.format(owner['URC'])}}</h2></article><article class='panel'><small>أصول المالك SDG</small><h2>{{'{:,}'.format(owner['SDG'])}}</h2></article>
      <article class='panel'><small>احتياطي الصرف URC</small><h2>{{'{:,}'.format(pools['URC'])}}</h2></article><article class='panel'><small>احتياطي الصرف SDG</small><h2>{{'{:,}'.format(pools['SDG'])}}</h2></article>
       <article class='panel'><small>السيولة النقدية للسحب SDG</small><h2>{{'{:,}'.format(cash_available)}}</h2><small>محجوز لطلبات معلقة: {{'{:,}'.format(cash_pending)}}</small></article></div>{% endif %}
      <h2>مصدر سعر الصرف</h2>{% if is_founder %}<form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>
      <label>طريقة التسعير</label><select name='fx_rate_mode'><option value='market' {% if values['fx_rate_mode']=='market' %}selected{% endif %}>سعر السوق المرجعي</option><option value='manual' {% if values['fx_rate_mode']=='manual' %}selected{% endif %}>سعر يدوي</option></select>
      <label>سعر السوق المرجعي (SDG لكل URC)</label><input name='fx_market_rate' type='number' min='1' value='{{values["fx_market_rate"]}}'>
      <label>السعر اليدوي (SDG لكل URC)</label><input name='fx_manual_rate' type='number' min='1' value='{{values["fx_manual_rate"]}}'>
      <label>هامش شراء URC بالنقاط الأساسية</label><input name='fx_buy_spread_bps' type='number' min='0' max='10000' value='{{values["fx_buy_spread_bps"]}}'>
      <label>هامش بيع URC بالنقاط الأساسية</label><input name='fx_sell_spread_bps' type='number' min='0' max='10000' value='{{values["fx_sell_spread_bps"]}}'><button>حفظ الأسعار</button></form>
      <p class='muted'>عند اختيار «سعر السوق» أدخل السعر المرجعي الحالي بنفسك وحدّثه وفق الطلب. لا يوجد ربط تلقائي بمزوّد أسعار خارجي حتى الآن.</p><div class='panel'><b>السعر الفعلي المستخدم الآن</b><h2>{{'{:,}'.format(active_rate)}} SDG لكل 1 URC</h2><small>يمكن التبديل بين السعر اليدوي والسوق المرجعي في أي وقت.</small></div>{% else %}<p class='muted'>عرض فقط: لا تملك صلاحية تعديل إعدادات الصرف أو الاطلاع على الصندوق المالي.</p>{% endif %}
      {% if is_founder %}<h2>تمويل احتياطي الصرف</h2><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='liquidity'>
      <label>العملة</label><select name='currency'><option>URC</option><option>SDG</option></select><label>المبلغ من محفظة المالك</label><input name='amount' type='number' min='1' required><button>إضافة إلى احتياطي الصرف</button></form>
      <h2>تسجيل احتياطي SDG نقدي مستلم فعلياً</h2><p class='muted'>استخدم هذا الخيار فقط بعد استلام النقد فعلياً والتحقق منه خارج المنصة. يسجل النظام مرجع العملية في سجل التدقيق ولا ينشئ رصيداً للعميل.</p>
      <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='external_liquidity'><label>المبلغ النقدي المستلم SDG</label><input name='amount' type='number' min='1' required><label>مرجع الإيداع أو الاستلام</label><input name='external_reference' minlength='4' maxlength='100' required><label><input type='checkbox' name='cash_confirmed' value='yes' required> أؤكد أن المبلغ استُلم نقداً وتم التحقق منه فعلياً</label><button>تسجيل الاحتياطي النقدي</button></form>{% endif %}</section>""",
      owner=owner,pools=pools,is_founder=is_founder,active_rate=int(setting("fx_market_rate" if setting("fx_rate_mode","manual")=="market" else "fx_manual_rate","5600")),values={k:setting(k,d) for k,d in (("fx_rate_mode","manual"),("fx_market_rate","5600"),("fx_manual_rate","5600"),("fx_buy_spread_bps","0"),("fx_sell_spread_bps","0"))},cash_available=cash_available,cash_pending=cash_pending,csrf=session.get("csrf","")))

@empire_bp.route("/admin/protection-fund",methods=["GET","POST"])
@login_required
@founder_required
def admin_protection_fund():
    u=user()
    if request.method=="POST":
        csrf()
        currency=(request.form.get("currency") or "").strip()
        amount=money_int(request.form.get("amount"))
        destination=(request.form.get("destination") or "").strip()
        note=(request.form.get("note") or "").strip()
        if currency not in ("SDG","URC") or len(note)<4 or len(note)>250:
            flash("راجع العملة والمبلغ وسبب التحويل.")
            return redirect(url_for("empire.admin_protection_fund"))
        target=db().execute("SELECT username,full_name,active FROM users WHERE account_number=?",(destination,)).fetchone()
        if not target or not target["active"]:
            flash("رقم الحساب المستلم غير صالح.")
            return redirect(url_for("empire.admin_protection_fund"))
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            fund=int(d.execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='protection:fund' AND currency=?",(currency,)).fetchone()["n"])
            if fund<amount: raise ValueError("المبلغ المطلوب أكبر من الرصيد المتاح في الصندوق.")
            r=ref()
            post(r,[("protection:fund",currency,-amount),("user:"+target["username"],currency,amount)],"تحويل معتمد من صندوق حماية المستخدمين إلى "+target["full_name"]+" · "+note)
            audit(u["username"],"PROTECTION_FUND_TRANSFER","protection_fund",r,"to="+target["username"]+";currency="+currency+";amount="+str(amount)+";reason="+note)
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",(target["username"],"إضافة رصيد من إدارة المنصة","تمت إضافة "+format(amount,",")+" "+currency+" إلى محفظتك. رقم العملية "+r,r,now()))
            d.commit()
            flash("تم تنفيذ التحويل وتسجيله في سجل التدقيق.")
        except ValueError as exc:
            d.rollback(); flash(str(exc))
        except Exception:
            d.rollback()
            from flask import current_app
            current_app.logger.exception("Protection fund transfer failed")
            flash("تعذر تنفيذ التحويل. لم يتم اعتماد العملية.")
        return redirect(url_for("empire.admin_protection_fund"))
    balances={c:int(db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='protection:fund' AND currency=?",(c,)).fetchone()["n"]) for c in ("SDG","URC")}
    rows=db().execute("SELECT reference,currency,delta,memo,created_at FROM es_ledger WHERE account='protection:fund' ORDER BY id DESC LIMIT 300").fetchall()
    body=render_template_string("""<section class='panel'><span class='tag'>خاص بالمالك فقط</span><h1>صندوق حماية المستخدمين</h1><p>تُسجل هنا عمولات التطبيق وإيراداته فقط. سيولة الصرف محفوظة في احتياطي مستقل ولا تُخلط بأموال هذا الصندوق.</p>
      <div class='grid'><article class='panel'><small>الرصيد المحاسبي SDG</small><h2>{{'{:,}'.format(balances['SDG'])}}</h2></article><article class='panel'><small>الرصيد المحاسبي URC</small><h2>{{'{:,}'.format(balances['URC'])}}</h2></article></div>
      <h2>تحويل من الصندوق</h2><form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>رقم حساب المستلم</label><input name='destination' inputmode='numeric' pattern='[0-9]{7}' maxlength='7' required><label>العملة</label><select name='currency'><option>SDG</option><option>URC</option></select><label>المبلغ</label><input name='amount' type='number' min='1' required><label>سبب التحويل</label><textarea name='note' minlength='4' maxlength='250' required></textarea><button>تأكيد التحويل من الصندوق</button></form>
      <h2>سجل الصندوق</h2>{% for x in rows %}<article class='transaction-row'><div class='transaction-info'><b>{{x['memo']}}</b><small dir='ltr'>{{x['reference']}}</small><small>{{x['created_at']}}</small></div><strong>{{'+' if x['delta']>0 else ''}}{{'{:,}'.format(x['delta'])}} {{x['currency']}}</strong></article>{% else %}<p class='empty-state'>لا توجد حركات بعد.</p>{% endfor %}</section>""",balances=balances,rows=rows,csrf=session.get("csrf",""))
    return page("صندوق حماية المستخدمين",body)

@empire_bp.get("/admin")
@login_required
@manager_dashboard_required
def admin():
    seed_owner_assets()
    owner=db().execute("SELECT username,account_number FROM users WHERE role='founder' ORDER BY id LIMIT 1").fetchone()
    owner_name=owner["username"] if owner else user()["username"]
    owner_account=owner["account_number"] if owner else "—"
    owner_urc=balance(owner_name,"URC")
    owner_sdg=balance(owner_name,"SDG")
    is_founder = user()["role"] == "founder"
    manager_permissions = [x["permission"] for x in db().execute(
        "SELECT permission FROM es_manager_permissions WHERE username=?",
        (user()["username"],)
    ).fetchall()] if user()["role"] == "manager" else []
    stats={
      "protection_sdg":int(db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='protection:fund' AND currency='SDG'").fetchone()["n"]),
      "protection_urc":int(db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='protection:fund' AND currency='URC'").fetchone()["n"]),
      "listings":db().execute("SELECT COUNT(*) n FROM es_listings WHERE status='PUBLISHED'").fetchone()["n"],
      "escrow":db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='escrow:market'").fetchone()["n"],
      "revenue":db().execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='protection:fund' AND currency='SDG'").fetchone()["n"],
      "pending":db().execute("SELECT COUNT(*) n FROM es_payments WHERE status='PENDING'").fetchone()["n"],
      "withdrawals":db().execute("SELECT COUNT(*) n FROM es_withdrawals WHERE status='PENDING'").fetchone()["n"],
      "agents":db().execute("SELECT COUNT(*) n FROM es_agents WHERE status='APPROVED'").fetchone()["n"],
      "topups":db().execute("SELECT COUNT(*) n FROM es_topup_orders WHERE status IN ('PAID','ASSIGNED','PROCESSING')").fetchone()["n"],
      "funding":db().execute("SELECT COUNT(*) n FROM funding_requests WHERE status='PENDING'").fetchone()["n"],
      "market_orders":db().execute("SELECT COUNT(*) n FROM es_market_orders WHERE status IN ('ESCROWED','DELIVERED','DISPUTED')").fetchone()["n"]
    }
    body=render_template_string("""{% if is_founder %}<section class='hero'><div><span class='hero-kicker'>SUDAN DIGITAL FINANCE · OWNER</span><h1>مركز قيادة الإمبراطورية</h1><p>إدارة الأصول والخدمات والعمليات المالية.</p><span class='tag'>رقم حساب المالك: {{owner_account}}</span></div><div class='hero-character'><div class='metric'><small>الأصول الرقمية للمالك</small><strong>{{'{:,}'.format(owner_urc)}} URC</strong><small>الأصول النقدية: {{'{:,}'.format(owner_sdg)}} SDG</small></div></div></section>{% else %}<section class='hero'><div><span class='hero-kicker'>AUTHORIZED MANAGEMENT</span><h1>مركز إدارة الإمبراطورية</h1><p>تظهر هنا الوحدات التي منحك المالك صلاحية الوصول إليها فقط.</p></div></section>{% endif %}
    <section class='panel'><h2>مؤشرات المنصة</h2><div class='grid'>
    {% if is_founder or 'catalog' in manager_permissions %}<article class='panel'><small>الإعلانات النشطة</small><h2>{{stats['listings']}}</h2></article>{% endif %}
    {% if is_founder or 'orders' in manager_permissions %}<article class='panel'><small>أموال صفقات السوق المحتجزة (SDG)</small><h2>{{'{:,}'.format(stats['escrow'])}}</h2></article><article class='panel'><small>طلبات الشحن قيد التنفيذ</small><h2>{{stats['topups']}}</h2></article>{% endif %}
    {% if is_founder %}<article class='panel'><small>صندوق حماية المستخدمين (SDG)</small><h2>{{'{:,}'.format(stats['revenue'])}}</h2></article>{% endif %}{% if is_founder or 'payments' in manager_permissions %}<article class='panel'><small>طلبات الدفع المعلقة</small><h2>{{stats['pending']}}</h2></article>{% endif %}
    {% if is_founder or 'withdrawals' in manager_permissions %}<article class='panel'><small>طلبات السحب المعلقة</small><h2>{{stats['withdrawals']}}</h2></article>{% endif %}
    {% if is_founder or 'agents' in manager_permissions %}<article class='panel'><small>الوكلاء المعتمدون</small><h2>{{stats['agents']}}</h2></article>{% endif %}
    </div></section>
    {% if is_founder or 'payments' in manager_permissions or 'withdrawals' in manager_permissions or 'exchange' in manager_permissions %}<section class='admin-section'><div class='admin-section-head'><span>01 · العمليات المالية</span><h2>إدارة الأموال والمدفوعات</h2></div><div class='grid admin-links'>
    {% if is_founder %}<a href='{{url_for('founder_panel')}}'><b>تغذية حسابات العملاء</b><small>{{stats['funding']}} طلب معلق</small><span class='admin-open'>فتح القسم ←</span></a><a href='{{url_for('empire.admin_protection_fund')}}'><b>صندوق حماية المستخدمين</b><small>إيرادات وعمولات التطبيق — المالك فقط</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder or 'payments' in manager_permissions %}<a href='{{url_for('empire.admin_payments')}}'><b>طلبات الدفع وشراء URC</b><small>{{stats['pending']}} طلب معلق</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder or 'withdrawals' in manager_permissions %}<a href='{{url_for('empire.admin_withdrawals')}}'><b>طلبات السحب</b><small>{{stats['withdrawals']}} طلب معلق</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder or 'exchange' in manager_permissions %}<a href='{{url_for('empire.admin_exchange')}}'><b>إدارة تحويل العملة</b><small>أسعار التحويل وأرصدة السيولة</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}</div></section>{% endif %}
    {% if is_founder or 'orders' in manager_permissions %}<section class='admin-section'><div class='admin-section-head'><span>02 · تنفيذ الخدمات</span><h2>الطلبات والتسليم</h2></div><div class='grid admin-links'>
    {% if is_founder or 'orders' in manager_permissions %}<a href='{{url_for('empire.admin_topups')}}'><b>أكواد شحن الألعاب</b><small>{{stats['topups']}} طلب قيد التنفيذ</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder or 'orders' in manager_permissions %}<a href='{{url_for('empire.admin_market_orders')}}'><b>طلبات المتجر والصفقات</b><small>{{stats['market_orders']}} صفقة مفتوحة</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder or 'orders' in manager_permissions %}<a href='{{url_for('empire.admin_disputes')}}'><b>النزاعات والشكاوى</b><small>مراجعة الحالات المفتوحة</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}</div></section>{% endif %}
    {% if is_founder or 'products' in manager_permissions or 'catalog' in manager_permissions or 'agents' in manager_permissions %}<section class='admin-section'><div class='admin-section-head'><span>03 · المنتجات والشركاء</span><h2>إدارة الكتالوج والوكلاء</h2></div><div class='grid admin-links'>
    {% if is_founder or 'products' in manager_permissions %}<a href='{{url_for('empire.admin_products')}}'><b>باقات الألعاب والأسعار</b><small>أسعار البيع وتكلفة المورد</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder or 'catalog' in manager_permissions %}<a href='{{url_for('empire.admin_listings')}}'><b>السوق والإعلانات</b><small>المنتجات والحسابات المعروضة</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder or 'agents' in manager_permissions %}<a href='{{url_for('empire.admin_agents')}}'><b>إدارة الوكلاء</b><small>الاعتماد والعمولات والخدمات</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}</div></section>{% endif %}
    {% if is_founder or 'settings' in manager_permissions or 'audit' in manager_permissions %}<section class='admin-section'><div class='admin-section-head'><span>04 · التحكم والرقابة</span><h2>الإعدادات وسجل النظام</h2></div><div class='grid admin-links'>
    {% if is_founder or 'settings' in manager_permissions %}<a href='{{url_for('empire.admin_settings')}}'><b>إعدادات المنصة</b><small>الأسعار وسياسات التشغيل</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder or 'audit' in manager_permissions %}<a href='{{url_for('empire.admin_audit')}}'><b>سجل التدقيق</b><small>تتبع إجراءات الإدارة</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}
    {% if is_founder %}<a href='{{url_for('empire.admin_managers')}}'><b>المدير العام</b><small>إنشاء حساب وتحديد صلاحياته</small><span class='admin-open'>فتح القسم ←</span></a><a href='{{url_for('empire.admin_code_inventory')}}'><b>مخزون أكواد الألعاب</b><small>إضافة الأكواد ومتابعة المتاح والمستخدم</small><span class='admin-open'>فتح القسم ←</span></a><a href='{{url_for('empire.admin_ice_codes')}}'><b>أكواد آيس ستوري</b><small>إنشاء أكواد العملاء ونقاط الولاء</small><span class='admin-open'>فتح القسم ←</span></a><a href='{{url_for('empire.admin_identity')}}'><b>توثيق حسابات العملاء</b><small>مراجعة البريد والهاتف واعتماد العلامة ✓</small><span class='admin-open'>فتح القسم ←</span></a><a href='{{url_for('founder_receiving_accounts')}}'><b>🏦 حسابات الدفع والتحويل الخارجي</b><small>إضافة · تعديل · حذف · إيقاف — فوري · بنكك · برافو</small><span class='admin-open'>فتح القسم ←</span></a>{% endif %}</div></section>{% endif %}""",stats=stats,owner_account=owner_account,owner_urc=owner_urc,owner_sdg=owner_sdg,
      is_founder=is_founder,manager_permissions=manager_permissions)
    dashboard_title="لوحة المدير العام" if user()["role"]=="manager" else "لوحة المالك"
    return page(dashboard_title,body,manager_permissions=manager_permissions,is_founder=is_founder)

@empire_bp.route("/admin/agents",methods=["GET","POST"])
@login_required
@admin_permission("agents")
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
@admin_permission("catalog")
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
@admin_permission("products")
def admin_products():
    # Create the session CSRF token before rendering any form on this page.
    # page() initializes it later, which was too late for this route's pre-rendered HTML.
    session.setdefault("csrf", secrets.token_urlsafe(32))
    if request.method == "POST":
        csrf()
        action = (request.form.get("action") or "").strip()
        pid = (request.form.get("product_id") or "").strip()
        try:
            if action == "toggle":
                row = db().execute("SELECT * FROM es_products WHERE id=?", (pid,)).fetchone()
                if not row:
                    flash("الباقة المطلوبة غير موجودة.")
                elif int(row["price_sdg"]) <= 1:
                    flash("أدخل سعر البيع أولاً ثم فعّل الباقة.")
                else:
                    active = 0 if int(row["active"]) else 1
                    db().execute("UPDATE es_products SET active=? WHERE id=?", (active, pid))
                    if active:
                        db().execute("INSERT INTO es_settings(key,value) VALUES('topups_enabled','1') ON CONFLICT(key) DO UPDATE SET value='1'")
                    audit(user()["username"], "PRODUCT_TOGGLED", "product", pid)
                    flash("تم تحديث حالة الباقة.")
            elif action == "update":
                row = db().execute("SELECT * FROM es_products WHERE id=?", (pid,)).fetchone()
                if not row:
                    flash("الباقة المطلوبة غير موجودة.")
                else:
                    raw_price = (request.form.get("price_sdg") or "").strip().replace(",", "").replace("٬", "")
                    raw_price = raw_price.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
                    if not re.fullmatch(r"[1-9][0-9]{0,11}", raw_price):
                        raise ValueError("أدخل سعر بيع صحيحاً بالأرقام.")
                    price = int(raw_price)
                    raw_cost = (request.form.get("cost_sdg") or str(row["cost_sdg"])).strip().replace(",", "").replace("٬", "")
                    raw_cost = raw_cost.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
                    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})", raw_cost):
                        raise ValueError("تكلفة المورد غير صحيحة.")
                    cost = int(raw_cost)
                    if cost > price:
                        raise ValueError("تكلفة المورد لا يمكن أن تتجاوز سعر البيع.")
                    db().execute("UPDATE es_products SET price_sdg=?, cost_sdg=? WHERE id=?", (price, cost, pid))
                    audit(user()["username"], "PRODUCT_PRICE_UPDATED", "product", pid, "price="+str(price)+";cost="+str(cost))
                    flash("تم حفظ السعر والتكلفة بنجاح.")
            elif action == "create":
                game = (request.form.get("game") or "").strip()
                title = (request.form.get("title") or "").strip()
                raw_price = (request.form.get("price_sdg") or "").strip().replace(",", "").replace("٬", "")
                raw_price = raw_price.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
                raw_cost = (request.form.get("cost_sdg") or "0").strip().replace(",", "").replace("٬", "")
                raw_cost = raw_cost.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
                if game not in ("PUBG Mobile", "Free Fire", "Mobile Legends", "Fortnite") or not title or len(title) > 120:
                    raise ValueError("بيانات الباقة غير مكتملة.")
                if not re.fullmatch(r"[1-9][0-9]{0,11}", raw_price) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})", raw_cost):
                    raise ValueError("أدخل السعر والتكلفة بشكل صحيح.")
                price, cost = int(raw_price), int(raw_cost)
                if cost > price:
                    raise ValueError("تكلفة المورد لا يمكن أن تتجاوز سعر البيع.")
                code = ref()
                db().execute("INSERT INTO es_products(code,game,title,price_sdg,cost_sdg,created_at) VALUES(?,?,?,?,?,?)", (code, game, title, price, cost, now()))
                audit(user()["username"], "PRODUCT_CREATED", "product", code)
                flash("تمت إضافة الباقة.")
            else:
                raise ValueError("طلب غير معروف. أعد تحميل الصفحة وحاول مجدداً.")
        except ValueError as exc:
            flash(str(exc))
        return redirect(url_for("empire.admin_products"))

    rows = db().execute("SELECT * FROM es_products ORDER BY CASE game WHEN 'PUBG Mobile' THEN 1 WHEN 'Free Fire' THEN 2 WHEN 'Mobile Legends' THEN 3 ELSE 4 END, id").fetchall()
    body = render_template_string("""<section class='panel'><h1>إدارة أسعار شحن الألعاب</h1>
    <p>كل الباقات المطلوبة موجودة هنا. أدخل سعر البيع الذي تحدده، ثم فعّل الباقة لتظهر للعملاء. لا تُعرض أي باقة بسعر افتراضي.</p>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='create'>
    <label>اللعبة</label><select name='game' required><option>PUBG Mobile</option><option>Free Fire</option><option>Mobile Legends</option><option>Fortnite</option></select>
    <label>اسم الباقة</label><input name='title' required><label>سعر البيع SDG</label><input name='price_sdg' type='text' inputmode='numeric' pattern='[0-9٠-٩٬,]+' required>
    <label>تكلفة المورد SDG</label><input name='cost_sdg' type='text' inputmode='numeric' pattern='[0-9٠-٩٬,]+' value='0' required><button>إضافة باقة جديدة</button></form>
    {% for x in rows %}<article class='panel'><b>{{x['game']}} · {{x['title']}}</b>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='product_id' value='{{x['id']}}'>
    <input type='hidden' name='action' value='update'><label>سعر البيع SDG</label><input name='price_sdg' type='text' inputmode='numeric' pattern='[0-9٠-٩٬,]+' value='{{'' if x['price_sdg'] <= 1 else x['price_sdg']}}' placeholder='أدخل السعر بالأرقام' autocomplete='off' required>
    <label>تكلفة المورد SDG</label><input name='cost_sdg' type='text' inputmode='numeric' pattern='[0-9٠-٩٬,]+' value='{{x['cost_sdg']}}' required>
    <button type='submit'>حفظ السعر</button></form>
    <form method='post'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='product_id' value='{{x['id']}}'><input type='hidden' name='action' value='toggle'>
    <button class='btn secondary' type='submit' {% if x['price_sdg'] <= 1 %}disabled{% endif %}>{{'إيقاف الباقة' if x['active'] else 'تفعيل الباقة'}}</button></form>
    <small>{{'فعالة' if x['active'] else ('بانتظار تحديد السعر' if x['price_sdg'] <= 1 else 'السعر محفوظ — الباقة متوقفة')}}</small></article>{% endfor %}
    </section>""", rows=rows, csrf=session.get("csrf",""))
    return page("باقات الشحن", body)

@empire_bp.route("/admin/payments",methods=["GET","POST"])
@login_required
@admin_permission("payments")
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
                fee=platform_fee(cost)
                post(r,[("user:"+u["username"],"SDG",-(cost+fee)),("platform:epc_sales","SDG",cost),("protection:fund","SDG",fee),("user:"+u["username"],"URC",qty),("issuer:urc","URC",-qty)],"شراء URC من محفظة SDG · عمولة التطبيق 3%")
                notify_protection_income(r,"SDG",fee,"عمولة شراء URC")
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
@admin_permission("settings")
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
@admin_permission("withdrawals")
def admin_withdrawals():
    u=user()
    if request.method=="POST":
        csrf(); wid=request.form.get("id",""); action=request.form.get("action","")
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            row=d.execute("SELECT * FROM es_withdrawals WHERE id=? AND status='PENDING'",(wid,)).fetchone()
            if not row: raise ValueError("طلب السحب غير متاح للمراجعة")
            if action=="PAID":
                payout_amount=int(row["amount_sdg"])
                if cash_sdg_balance()<payout_amount:
                    raise ValueError("السيولة النقدية الفعلية غير كافية لتنفيذ طلب السحب.")
                post(row["reference"]+"P",[
                    ("payout:pending","SDG",-payout_amount),
                    ("external:payout_settlements","SDG",payout_amount),
                    ("cash:sdg_reserve","SDG",-payout_amount),
                    ("external:cash_payouts","SDG",payout_amount)
                ],"تأكيد صرف طلب سحب من السيولة النقدية الفعلية")
                status="PAID"
            elif action=="REJECTED":
                post(row["reference"]+"X",[("payout:pending","SDG",-row["amount_sdg"]),("user:"+row["username"],"SDG",row["amount_sdg"])],"إعادة مبلغ طلب سحب مرفوض")
                status="REJECTED"
            else: raise ValueError("إجراء غير صالح")
            d.execute("UPDATE es_withdrawals SET status=?,reviewer=?,reviewed_at=? WHERE id=?",(status,u["username"],now(),wid))
            audit(u["username"],"WITHDRAWAL_"+status,"withdrawal",row["reference"])
            d.commit()
        except ValueError as exc:
            d.rollback(); flash(str(exc)); return redirect(url_for("empire.admin_withdrawals"))
        except Exception:
            d.rollback()
            raise
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
@admin_permission("orders")
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
                post(r+"S",[("escrow:market","SDG",-order["price_sdg"]),("user:"+order["seller"],"SDG",order["price_sdg"])],"تحرير مستحقات البائع بعد قرار النزاع — العمولة حُصّلت من المشتري")
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

@empire_bp.route("/admin/code-inventory",methods=["GET","POST"])
@login_required
@founder_required
def admin_code_inventory():
    session.setdefault("csrf", secrets.token_urlsafe(32))
    if request.method=="POST":
        csrf()
        product_id=request.form.get("product_id","").strip()
        raw_codes=request.form.get("codes","")
        codes=[line.strip() for line in raw_codes.splitlines() if line.strip()]
        if not product_id.isdigit() or not 1<=len(codes)<=500:
            flash("اختر الباقة وأدخل من 1 إلى 500 كود، كل كود في سطر مستقل.")
            return redirect(url_for("empire.admin_code_inventory"))
        product=db().execute("SELECT id,game,title FROM es_products WHERE id=?",(int(product_id),)).fetchone()
        if not product:
            flash("الباقة المحددة غير موجودة.")
            return redirect(url_for("empire.admin_code_inventory"))
        normalized=[re.sub(r"\s+","",code).upper() for code in codes]
        if any(not 4<=len(code)<=300 for code in normalized) or len(set(normalized))!=len(normalized):
            flash("تحقق من الأكواد: طول كل كود 4–300 حرف ولا يجوز تكرار كود داخل الدفعة.")
            return redirect(url_for("empire.admin_code_inventory"))
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            for code in normalized:
                digest=hashlib.sha256(code.encode("utf-8")).hexdigest()
                cipher=encryption().encrypt(code.encode("utf-8")).decode("utf-8")
                d.execute("INSERT INTO es_game_code_inventory(product_id,code_cipher,code_hash,code_tail,status,created_by,created_at) VALUES(?,?,?,?, 'AVAILABLE',?,?)",
                          (int(product_id),cipher,digest,code[-4:],user()["username"],now()))
            pending=d.execute("""SELECT t.*,p.cost_sdg FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id
                                  WHERE t.product_id=? AND t.status='PAID' AND t.agent IS NULL ORDER BY t.id""",(int(product_id),)).fetchall()
            auto_delivered=0
            for paid_order in pending:
                if int(paid_order["cost_sdg"])<0 or int(paid_order["cost_sdg"])>int(paid_order["amount_sdg"]):
                    continue
                stock=d.execute("SELECT * FROM es_game_code_inventory WHERE product_id=? AND status='AVAILABLE' ORDER BY id LIMIT 1",(int(product_id),)).fetchone()
                if not stock: break
                fulfill_topup_from_inventory(d,paid_order,stock,user()["username"],"automatic delivery after inventory import")
                auto_delivered+=1
            audit(user()["username"],"GAME_CODES_IMPORTED","game_code_inventory",product_id,"count="+str(len(normalized))+";auto_delivered="+str(auto_delivered))
            d.commit()
            flash("تمت إضافة "+str(len(normalized))+" كوداً. تم التسليم التلقائي لـ "+str(auto_delivered)+" طلب مدفوع عند توفر المخزون.")
        except sqlite3.IntegrityError:
            d.rollback()
            flash("توجد أكواد مكررة مسبقاً في المخزون؛ لم تُحفظ أي أكواد من هذه الدفعة.")
        except Exception as exc:
            d.rollback()
            flash(str(exc))
        return redirect(url_for("empire.admin_code_inventory"))
    products=db().execute("SELECT id,game,title FROM es_products ORDER BY game,title").fetchall()
    rows=db().execute("""SELECT p.game,p.title,
      SUM(CASE WHEN i.status='AVAILABLE' THEN 1 ELSE 0 END) available,
      SUM(CASE WHEN i.status='USED' THEN 1 ELSE 0 END) used,
      SUM(CASE WHEN i.status='REVOKED' THEN 1 ELSE 0 END) revoked
      FROM es_products p LEFT JOIN es_game_code_inventory i ON i.product_id=p.id
      GROUP BY p.id ORDER BY p.game,p.title""").fetchall()
    body=render_template_string("""<section class='panel'><h1>مخزون أكواد الألعاب</h1>
      <p>أدخل الأكواد المشتراة من المورد لكل لعبة وباقة. تحفظ مشفرة، ولا تظهر كاملة في لوحة الإدارة، ويُستهلك كل كود مرة واحدة عند التسليم.</p>
      <form method='post' class='panel'><input type='hidden' name='csrf' value='{{csrf}}'><label>اللعبة والباقة</label><select name='product_id' required>{% for p in products %}<option value='{{p['id']}}'>{{p['game']}} · {{p['title']}}</option>{% endfor %}</select>
      <label>الأكواد — كود واحد في كل سطر</label><textarea name='codes' rows='8' maxlength='160000' required placeholder='CODE-XXXX-XXXX'></textarea><button>إضافة الأكواد إلى المخزون</button></form>
      <h2>ملخص المخزون حسب الباقة</h2>{% for x in rows %}<article class='panel'><b>{{x['game']}} · {{x['title']}}</b><p>متاح: {{x['available'] or 0}} · مستخدم: {{x['used'] or 0}} · ملغي: {{x['revoked'] or 0}}</p></article>{% else %}<p>لا توجد باقات بعد.</p>{% endfor %}</section>""",products=products,rows=rows,csrf=session.get("csrf",""))
    return page("مخزون أكواد الألعاب",body)

@empire_bp.route("/admin/topups",methods=["GET","POST"])
@login_required
@admin_permission("orders")
def admin_topups():
    if request.method=="POST":
        csrf()
        action=request.form.get("action","")
        r=request.form.get("reference","").strip()
        if action!="fulfill":
            abort(400,"إجراء غير معروف")
        code=request.form.get("delivery_code","").strip()
        inventory_id=(request.form.get("inventory_id") or "").strip()
        note=request.form.get("note","").strip()[:300]
        if not inventory_id and not 4<=len(code)<=300:
            flash("اختر كوداً من المخزون أو أدخل كوداً يدوياً صحيحاً.")
            return redirect(url_for("empire.admin_topups"))
        d=db(); d.execute("BEGIN IMMEDIATE")
        try:
            order=d.execute("""SELECT t.*,p.cost_sdg FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id
                               WHERE t.reference=? AND t.status='PAID' AND t.agent IS NULL""",(r,)).fetchone()
            if not order: raise ValueError("الطلب لم يعد متاحاً للتسليم اليدوي.")
            if order["cost_sdg"]<=0 or order["cost_sdg"]>order["amount_sdg"]:
                raise ValueError("راجع تكلفة المورد وسعر البيع قبل تسليم الكود.")
            margin=order["amount_sdg"]-order["cost_sdg"]
            source_note="manual owner fulfillment"
            if inventory_id:
                if not inventory_id.isdigit():
                    raise ValueError("اختيار كود المخزون غير صالح.")
                stock=d.execute("SELECT * FROM es_game_code_inventory WHERE id=? AND product_id=? AND status='AVAILABLE'",
                                (int(inventory_id),order["product_id"])).fetchone()
                if not stock:
                    raise ValueError("الكود المختار لم يعد متاحاً لهذه الباقة.")
                code=encryption().decrypt(stock["code_cipher"].encode("utf-8")).decode("utf-8")
                consumed=d.execute("UPDATE es_game_code_inventory SET status='USED',used_order_ref=?,used_at=? WHERE id=? AND status='AVAILABLE'",
                                   (r,now(),stock["id"])).rowcount
                if consumed!=1:
                    raise ValueError("تم استهلاك الكود في طلب آخر؛ اختر كوداً جديداً.")
                source_note="inventory_code_id="+str(stock["id"])
            cipher=encryption().encrypt(code.encode("utf-8")).decode("utf-8")
            post(r+"T",[("clearing:topups","SDG",-order["amount_sdg"]),("supplier:topup_cost","SDG",order["cost_sdg"]),("protection:fund","SDG",margin)],
                 "تسوية كود لعبة وتسليم العميل")
            notify_protection_income(r+"T","SDG",margin,"هامش ربح تسليم كود لعبة")
            changed=d.execute("UPDATE es_topup_orders SET status='COMPLETED',delivery_cipher=?,note=?,updated_at=? WHERE reference=? AND status='PAID' AND agent IS NULL",
                              (cipher,note,now(),r)).rowcount
            if changed!=1: raise ValueError("تغيرت حالة الطلب أثناء المعالجة.")
            d.execute("INSERT INTO notifications(username,title,message,reference,is_read,created_at) VALUES(?,?,?,?,0,?)",
                      (order["buyer"],"كود لعبتك جاهز","تم تجهيز كود باقتك. افتح طلباتي لعرض الكود بأمان.",r,now()))
            audit(user()["username"],"TOPUP_CODE_DELIVERED","topup",r,source_note)
            d.commit()
            flash("تم تسليم الكود للعميل وإغلاق الطلب بنجاح.")
        except Exception as exc:
            d.rollback()
            flash(str(exc))
        return redirect(url_for("empire.admin_topups"))
    allowed={"ALL","PENDING_PAYMENT","PAID","ASSIGNED","PROCESSING","COMPLETED","REJECTED","REFUNDED"}
    status=request.args.get("status","ALL").upper()
    if status not in allowed: abort(400)
    query="SELECT t.*,p.title,p.game FROM es_topup_orders t JOIN es_products p ON p.id=t.product_id"
    params=()
    if status!="ALL": query+=" WHERE t.status=?"; params=(status,)
    query+=" ORDER BY t.id DESC LIMIT 300"
    raw_rows=db().execute(query,params).fetchall()
    rows=[]
    for row in raw_rows:
        item=dict(row)
        item["stock"]=db().execute("SELECT id,code_tail FROM es_game_code_inventory WHERE product_id=? AND status='AVAILABLE' ORDER BY id LIMIT 100",(item["product_id"],)).fetchall()
        rows.append(item)
    labels={"PENDING_PAYMENT":"بانتظار الدفع","PAID":"مدفوع — جاهز لتسليم الكود","ASSIGNED":"استلمه وكيل","PROCESSING":"قيد التنفيذ","COMPLETED":"تم تسليم الكود","REJECTED":"مرفوض","REFUNDED":"مسترد"}
    body=render_template_string("""<section class='panel'><span class='tag'>مركز تنفيذ الأكواد</span><h1>طلبات أكواد الألعاب</h1><p>لا يُطلب آيدي لاعب. بعد التحقق من الدفع، أدخل كود الباقة ليظهر للعميل داخل «طلباتي» بشكل آمن.</p>
      <div class='admin-filters'><a href='/store/admin/topups'>الكل</a>{% for k,v in labels.items() %}<a href='/store/admin/topups?status={{k}}'>{{v}}</a>{% endfor %}</div>
      {% for x in rows %}<article class='panel order-card'><div class='order-head'><b dir='ltr'>{{x['reference']}}</b><span class='tag'>{{labels.get(x['status'],x['status'])}}</span></div>
      <div class='order-grid'><div><small>العميل</small><b>{{x['buyer']}}</b></div><div><small>اللعبة / الباقة</small><b>{{x['game']}} · {{x['title']}}</b></div><div><small>المبلغ</small><b>{{'{:,}'.format(x['amount_sdg'])}} SDG</b></div><div><small>الوكيل</small><b>{{x['agent'] or 'تنفيذ المالك'}}</b></div><div><small>تاريخ الطلب</small><b>{{x['created_at']}}</b></div><div><small>آخر تحديث</small><b>{{x['updated_at']}}</b></div></div>
      {% if x['status']=='PAID' and not x['agent'] %}<form method='post' class='fulfill-code-form'><input type='hidden' name='csrf' value='{{csrf}}'><input type='hidden' name='action' value='fulfill'><input type='hidden' name='reference' value='{{x['reference']}}'><label>كود جاهز من المخزون (اختياري)</label><select name='inventory_id'><option value=''>إدخال كود يدوي بدلاً منه</option>{% for code in x['stock'] %}<option value='{{code['id']}}'>كود مخزون ••••{{code['code_tail']}}</option>{% endfor %}</select><label>كود يدوي عند عدم استخدام المخزون</label><input name='delivery_code' maxlength='300' autocomplete='off' placeholder='أدخل كود الشحن من المورد'><label>ملاحظة داخلية (اختياري)</label><input name='note' maxlength='300' placeholder='مرجع المورد أو ملاحظة تنفيذ'><button>تسليم الكود للعميل</button></form>{% endif %}
      {% if x['note'] %}<p class='muted'>{{x['note']}}</p>{% endif %}</article>
      {% else %}<div class='empty-state'>لا توجد طلبات في هذا القسم.</div>{% endfor %}</section>""",rows=rows,labels=labels,csrf=session.get("csrf",""))
    return page("طلبات أكواد الألعاب",body)

@empire_bp.get("/admin/market-orders")
@login_required
@admin_permission("orders")
def admin_market_orders():
    rows = db().execute("""SELECT o.*,l.title,l.game,l.category FROM es_market_orders o LEFT JOIN es_listings l ON l.id=o.listing_id ORDER BY o.id DESC LIMIT 300""").fetchall()
    labels = {"ESCROWED":"بانتظار تسليم البائع","DELIVERED":"تم التسليم — بانتظار تأكيد المشتري","RELEASED":"أغلقت لصالح البائع","REFUNDED":"تم رد المبلغ","DISPUTED":"نزاع مفتوح"}
    body = render_template_string("""<section class='panel'><h1>طلبات المتجر والصفقات</h1><p>هذا القسم خاص بمشتريات المتجر وصفقات الحسابات، ومنفصل عن تغذية الحساب وشحن الألعاب.</p>
      {% for x in rows %}<article class='panel order-card'><div class='order-head'><b>{{x['reference']}}</b><span class='tag'>{{labels.get(x['status'],x['status'])}}</span></div>
      <div class='order-grid'><div><small>العنصر</small><b>{{x['title'] or 'عنصر محذوف'}} · {{x['category'] or ''}}</b></div><div><small>المبلغ المحجوز</small><b>{{'{:,}'.format(x['price_sdg'])}} SDG</b></div><div><small>المشتري</small><b>{{x['buyer']}}</b></div><div><small>البائع</small><b>{{x['seller']}}</b></div><div><small>الحالة</small><b>{{labels.get(x['status'],x['status'])}}</b></div><div><small>التاريخ</small><b>{{x['updated_at']}}</b></div></div></article>
      {% else %}<div class='empty-state'>لا توجد صفقات مسجلة.</div>{% endfor %}</section>""",rows=rows,labels=labels)
    return page("طلبات المتجر والصفقات",body)

@empire_bp.get("/admin/audit")
@login_required
@admin_permission("audit")
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
