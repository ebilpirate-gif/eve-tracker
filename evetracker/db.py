"""SQLite storage. One connection per thread, WAL mode so the web UI can read while the worker writes."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

-- ---------- static data (SDE) ----------
CREATE TABLE IF NOT EXISTS types (
    type_id INTEGER PRIMARY KEY, group_id INTEGER, name TEXT, volume REAL, packaged_volume REAL,
    portion_size INTEGER, base_price REAL, published INTEGER, market_group_id INTEGER, meta_level INTEGER
);
CREATE INDEX IF NOT EXISTS types_name ON types(name);
CREATE TABLE IF NOT EXISTS groups_ (group_id INTEGER PRIMARY KEY, category_id INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS categories (category_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS bp_activity (type_id INTEGER, activity_id INTEGER, time INTEGER, PRIMARY KEY (type_id, activity_id));
CREATE TABLE IF NOT EXISTS bp_materials (type_id INTEGER, activity_id INTEGER, material_type_id INTEGER, quantity INTEGER);
CREATE INDEX IF NOT EXISTS bp_materials_t ON bp_materials(type_id, activity_id);
CREATE TABLE IF NOT EXISTS bp_products (type_id INTEGER, activity_id INTEGER, product_type_id INTEGER, quantity INTEGER);
CREATE INDEX IF NOT EXISTS bp_products_p ON bp_products(product_type_id);
CREATE TABLE IF NOT EXISTS regions (region_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS systems (system_id INTEGER PRIMARY KEY, name TEXT, region_id INTEGER, security REAL);
CREATE INDEX IF NOT EXISTS systems_name ON systems(name);
CREATE TABLE IF NOT EXISTS stations (station_id INTEGER PRIMARY KEY, name TEXT, system_id INTEGER, region_id INTEGER);

-- NOTE: in the data tables below, `character_id` is the OWNER id: a character id or, for
-- corporation data, a corporation id (EVE ids are unique across both). The `owners` view
-- maps an owner id to a display name.

-- ---------- characters, corporations & auth ----------
CREATE TABLE IF NOT EXISTS characters (
    character_id INTEGER PRIMARY KEY, name TEXT, corporation_id INTEGER,
    refresh_token TEXT, access_token TEXT, access_expires REAL, scopes TEXT,
    added_at REAL, last_sync REAL, sync_error TEXT, needs_reauth INTEGER DEFAULT 0, roles TEXT
);
CREATE TABLE IF NOT EXISTS corporations (
    corporation_id INTEGER PRIMARY KEY, name TEXT, ticker TEXT, ceo_id INTEGER, enabled INTEGER DEFAULT 1,
    divisions TEXT, access TEXT, last_sync REAL, sync_error TEXT
);
CREATE TABLE IF NOT EXISTS corp_wallets (corporation_id INTEGER, division INTEGER, balance REAL,
    PRIMARY KEY (corporation_id, division));
CREATE VIEW IF NOT EXISTS owners AS
    SELECT character_id AS owner_id, name, 0 AS is_corp FROM characters
    UNION ALL
    SELECT corporation_id, '[' || COALESCE(ticker, '?') || '] ' || COALESCE(name, corporation_id), 1 FROM corporations;
CREATE TABLE IF NOT EXISTS sync_state (character_id INTEGER, step TEXT, last_run REAL, error TEXT,
    PRIMARY KEY (character_id, step));
CREATE TABLE IF NOT EXISTS esi_cache (key TEXT PRIMARY KEY, etag TEXT, expires REAL, body TEXT);

-- ---------- universe lookups ----------
CREATE TABLE IF NOT EXISTS structures (structure_id INTEGER PRIMARY KEY, name TEXT, system_id INTEGER,
    type_id INTEGER, fetched_at REAL, accessible INTEGER);

-- ---------- character data ----------
CREATE TABLE IF NOT EXISTS assets (
    item_id INTEGER PRIMARY KEY, character_id INTEGER, type_id INTEGER, location_id INTEGER,
    location_flag TEXT, location_type TEXT, quantity INTEGER, is_singleton INTEGER,
    is_blueprint_copy INTEGER, root_location_id INTEGER, item_name TEXT
);
CREATE INDEX IF NOT EXISTS assets_char ON assets(character_id);
CREATE INDEX IF NOT EXISTS assets_root ON assets(root_location_id);
CREATE TABLE IF NOT EXISTS wallet (character_id INTEGER PRIMARY KEY, balance REAL, updated REAL);
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER, character_id INTEGER, date TEXT, type_id INTEGER,
    quantity INTEGER, unit_price REAL, is_buy INTEGER, location_id INTEGER, client_id INTEGER,
    journal_ref_id INTEGER, is_personal INTEGER, division INTEGER,
    PRIMARY KEY (transaction_id, character_id)
);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER, character_id INTEGER, date TEXT, ref_type TEXT, amount REAL,
    balance REAL, description TEXT, division INTEGER DEFAULT 0,
    PRIMARY KEY (id, character_id, division)
);
CREATE INDEX IF NOT EXISTS journal_date ON journal(date);
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY, character_id INTEGER, type_id INTEGER, region_id INTEGER,
    location_id INTEGER, is_buy INTEGER, price REAL, volume_total INTEGER, volume_remain INTEGER,
    issued TEXT, duration INTEGER, escrow REAL, range TEXT,
    competitor_price REAL, undercut INTEGER, checked_at REAL, wallet_division INTEGER
);
CREATE TABLE IF NOT EXISTS industry_jobs (
    job_id INTEGER PRIMARY KEY, character_id INTEGER, activity_id INTEGER, blueprint_type_id INTEGER,
    product_type_id INTEGER, runs INTEGER, status TEXT, start_date TEXT, end_date TEXT,
    facility_id INTEGER, cost REAL
);
CREATE TABLE IF NOT EXISTS blueprints (
    item_id INTEGER PRIMARY KEY, character_id INTEGER, type_id INTEGER, location_id INTEGER,
    material_efficiency INTEGER, time_efficiency INTEGER, runs INTEGER, quantity INTEGER
);
CREATE TABLE IF NOT EXISTS skills (character_id INTEGER, skill_id INTEGER, level INTEGER,
    PRIMARY KEY (character_id, skill_id));
CREATE TABLE IF NOT EXISTS networth (
    ts REAL, character_id INTEGER, wallet REAL, assets REAL, sell_orders REAL, buy_escrow REAL,
    PRIMARY KEY (ts, character_id)
);

-- ---------- market data ----------
CREATE TABLE IF NOT EXISTS prices (type_id INTEGER PRIMARY KEY, adjusted REAL, average REAL);
CREATE TABLE IF NOT EXISTS hub_prices (
    station_id INTEGER, type_id INTEGER, best_buy REAL, best_sell REAL,
    buy_volume INTEGER, sell_volume INTEGER, buy_orders INTEGER, sell_orders INTEGER,
    PRIMARY KEY (station_id, type_id)
);
CREATE TABLE IF NOT EXISTS market_history (
    region_id INTEGER, type_id INTEGER, avg_volume REAL, avg_price REAL, fetched_at REAL,
    PRIMARY KEY (region_id, type_id)
);
CREATE TABLE IF NOT EXISTS cost_indices (system_id INTEGER, activity TEXT, cost_index REAL,
    PRIMARY KEY (system_id, activity));
CREATE TABLE IF NOT EXISTS job_log (job TEXT PRIMARY KEY, last_run REAL, duration REAL, ok INTEGER, message TEXT);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def conn() -> sqlite3.Connection:
    global _initialized
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
        with _init_lock:
            if not _initialized:
                c.executescript(SCHEMA)
                migrate(c)
                c.executescript(SCHEMA)  # recreate any indexes dropped by a table rebuild
                _initialized = True
    return c


def _columns(c, table: str) -> dict:
    return {r[1]: r for r in c.execute(f"PRAGMA table_info({table})")}


def _rebuild(c, table: str, keep_cols) -> None:
    """Recreate `table` with the current SCHEMA definition, copying `keep_cols` across."""
    create = SCHEMA[SCHEMA.index(f"CREATE TABLE IF NOT EXISTS {table} ("):]
    create = create[:create.index(");") + 2]
    cols = ", ".join(keep_cols)
    c.execute("BEGIN")
    c.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    c.execute(create)
    c.execute(f"INSERT OR IGNORE INTO {table} ({cols}) SELECT {cols} FROM {table}_old")
    c.execute(f"DROP TABLE {table}_old")
    c.execute("COMMIT")


def migrate(c) -> None:
    """Upgrade databases created by older versions in place."""
    for table, col, decl in (("characters", "roles", "TEXT"), ("orders", "wallet_division", "INTEGER")):
        if col not in _columns(c, table):
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # Corporation support: transactions/journal need composite keys (the same id can appear
    # on both a character's and a corporation's wallet).
    tx = _columns(c, "transactions")
    if "division" not in tx:
        _rebuild(c, "transactions", list(tx))
    jr = _columns(c, "journal")
    if "division" not in jr:
        _rebuild(c, "journal", list(jr))


def q(sql: str, params=()) -> list:
    return conn().execute(sql, params).fetchall()


def q1(sql: str, params=()):
    return conn().execute(sql, params).fetchone()


def val(sql: str, params=(), default=None):
    row = q1(sql, params)
    return default if row is None or row[0] is None else row[0]


def ex(sql: str, params=()):
    return conn().execute(sql, params)


def exmany(sql: str, rows):
    return conn().executemany(sql, rows)


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


# ---------- key/value settings ----------

DEFAULT_SETTINGS = {
    "sales_tax_pct": None,       # None = derive from best Accounting skill
    "broker_fee_pct": None,      # None = derive from best Broker Relations skill
    "valuation_hub": "jita",
    "valuation_side": "sell",    # "sell" (best sell order) or "buy" (best buy order)
    "hubs": ["jita", "amarr"],  # each extra hub costs bandwidth and CPU; enable more in Settings
    "hub_refresh_minutes": 30,
    "industry_hub": "jita",
    "industry_system": "Jita",
    "industry_facility_tax_pct": 0.25,
    "industry_structure_me_bonus_pct": 0.0,
    "industry_default_me": 10,
    "industry_material_side": "sell",
}


def get_setting(key: str):
    row = q1("SELECT value FROM kv WHERE key = ?", ("setting:" + key,))
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return json.loads(row[0])


def set_setting(key: str, value) -> None:
    ex("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", ("setting:" + key, json.dumps(value)))


def get_kv(key: str, default=None):
    row = q1("SELECT value FROM kv WHERE key = ?", (key,))
    return default if row is None else json.loads(row[0])


def set_kv(key: str, value) -> None:
    ex("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def log_job(job: str, started: float, ok: bool, message: str = "") -> None:
    ex(
        "INSERT OR REPLACE INTO job_log (job, last_run, duration, ok, message) VALUES (?, ?, ?, ?, ?)",
        (job, started, time.time() - started, int(ok), message[:500]),
    )
