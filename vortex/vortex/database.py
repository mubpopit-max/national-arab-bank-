import sqlite3
from datetime import datetime, timezone

VORTEX_DATABASE = "vortex.db"


def now():
    return datetime.now(timezone.utc).isoformat()


def get_vortex_db():
    db = sqlite3.connect(
        VORTEX_DATABASE,
        timeout=30,
        isolation_level=None,
    )

    db.row_factory = sqlite3.Row

    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")

    return db


def init_vortex_db():

    db = get_vortex_db()

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS vortex_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_type TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            address TEXT UNIQUE NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vortex_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_hash TEXT UNIQUE NOT NULL,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            fee REAL NOT NULL DEFAULT 0,
            timestamp TEXT NOT NULL,
            block_height INTEGER,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vortex_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_height INTEGER UNIQUE NOT NULL,
            block_hash TEXT UNIQUE NOT NULL,
            previous_hash TEXT NOT NULL,
            merkle_root TEXT NOT NULL,
            nonce INTEGER NOT NULL,
            difficulty INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vortex_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address TEXT NOT NULL,
            side TEXT NOT NULL,
            price_usd REAL NOT NULL CHECK(price_usd > 0),
            amount REAL NOT NULL CHECK(amount > 0),
            remaining REAL NOT NULL CHECK(remaining >= 0),
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vortex_supply (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            max_supply REAL NOT NULL,
            founder REAL NOT NULL,
            trading REAL NOT NULL,
            reserve REAL NOT NULL,
            mining REAL NOT NULL,
            incentives REAL NOT NULL,
            circulating REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS vortex_market (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            price_usd REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    existing = db.execute(
        "SELECT id FROM vortex_supply WHERE id = 1"
    ).fetchone()

    if not existing:
        db.execute(
            """
            INSERT INTO vortex_supply (
                id,
                max_supply,
                founder,
                trading,
                reserve,
                mining,
                incentives,
                circulating
            )
            VALUES (
                1,
                25000000,
                2000000,
                10000000,
                10000000,
                2000000,
                1000000,
                0
            )
            """
        )

    market = db.execute(
        "SELECT id FROM vortex_market WHERE id = 1"
    ).fetchone()

    if not market:
        db.execute(
            """
            INSERT INTO vortex_market (
                id,
                price_usd,
                updated_at
            )
            VALUES (1, 70.0, ?)
            """,
            (now(),)
        )

    db.close()
