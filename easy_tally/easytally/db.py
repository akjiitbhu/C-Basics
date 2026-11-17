"""SQLite schema and company (customer) file management.

Each customer company lives in its own SQLite file, so one customer's data is
physically separate from another's: simple backup, copy and privacy.
"""
import os
import re
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    parent_id INTEGER REFERENCES groups(id),
    nature TEXT NOT NULL CHECK (nature IN ('Asset','Liability','Income','Expense'))
);

CREATE TABLE IF NOT EXISTS ledgers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    opening INTEGER NOT NULL DEFAULT 0,        -- paise, Dr positive / Cr negative
    gstin TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT ''             -- 2-digit GST state code
);

CREATE TABLE IF NOT EXISTS vouchers (
    id INTEGER PRIMARY KEY,
    vtype TEXT NOT NULL,
    number INTEGER NOT NULL,
    date TEXT NOT NULL,                        -- ISO yyyy-mm-dd
    narration TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '',
    UNIQUE (vtype, number)
);
CREATE INDEX IF NOT EXISTS idx_vouchers_date ON vouchers(date);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    voucher_id INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id),
    amount INTEGER NOT NULL                    -- paise, Dr positive / Cr negative
);
CREATE INDEX IF NOT EXISTS idx_entries_voucher ON entries(voucher_id);
CREATE INDEX IF NOT EXISTS idx_entries_ledger ON entries(ledger_id);

CREATE TABLE IF NOT EXISTS stock_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    unit TEXT NOT NULL DEFAULT 'Nos',
    hsn TEXT NOT NULL DEFAULT '',
    gst_rate INTEGER NOT NULL DEFAULT 18,
    opening_qty REAL NOT NULL DEFAULT 0,
    opening_value INTEGER NOT NULL DEFAULT 0   -- paise
);

CREATE TABLE IF NOT EXISTS invoices (
    voucher_id INTEGER PRIMARY KEY REFERENCES vouchers(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                        -- Sales | Purchase | Credit Note | Debit Note
    party_id INTEGER NOT NULL REFERENCES ledgers(id),
    intra_state INTEGER NOT NULL,
    taxable INTEGER NOT NULL,
    cgst INTEGER NOT NULL, sgst INTEGER NOT NULL, igst INTEGER NOT NULL,
    total INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY,
    voucher_id INTEGER NOT NULL REFERENCES invoices(voucher_id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES stock_items(id),
    qty REAL NOT NULL,
    rate INTEGER NOT NULL,                     -- paise per unit
    gst_rate INTEGER NOT NULL,
    taxable INTEGER NOT NULL,
    cgst INTEGER NOT NULL, sgst INTEGER NOT NULL, igst INTEGER NOT NULL
);

-- Bank statement import: rows are staged here, reviewed, then posted as vouchers.
CREATE TABLE IF NOT EXISTS bank_imports (
    id INTEGER PRIMARY KEY,
    bank_ledger_id INTEGER NOT NULL REFERENCES ledgers(id),
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bank_txns (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES bank_imports(id) ON DELETE CASCADE,
    bank_ledger_id INTEGER NOT NULL REFERENCES ledgers(id),
    date TEXT NOT NULL,
    narration TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    debit INTEGER NOT NULL DEFAULT 0,          -- paise withdrawn
    credit INTEGER NOT NULL DEFAULT 0,         -- paise deposited
    balance INTEGER,                           -- paise, may be NULL
    uncertain INTEGER NOT NULL DEFAULT 0,      -- direction (Dr/Cr) was guessed
    counter_ledger_id INTEGER REFERENCES ledgers(id),
    voucher_id INTEGER REFERENCES vouchers(id) ON DELETE SET NULL,
    fingerprint TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

-- Vouchers brought in from Tally XML; the fingerprint stops a file being imported twice.
CREATE TABLE IF NOT EXISTS tally_imports (
    fingerprint TEXT PRIMARY KEY,
    voucher_id INTEGER REFERENCES vouchers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bank_rules (
    id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,              -- matched case-insensitively in narration
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE
);
"""

# (name, parent, nature)
DEFAULT_GROUPS = [
    ("Capital Account", None, "Liability"),
    ("Reserves & Surplus", "Capital Account", "Liability"),
    ("Loans (Liability)", None, "Liability"),
    ("Secured Loans", "Loans (Liability)", "Liability"),
    ("Unsecured Loans", "Loans (Liability)", "Liability"),
    ("Bank OD A/c", "Loans (Liability)", "Liability"),

    ("Current Liabilities", None, "Liability"),
    ("Duties & Taxes", "Current Liabilities", "Liability"),
    ("Sundry Creditors", "Current Liabilities", "Liability"),
    ("Provisions", "Current Liabilities", "Liability"),
    ("Fixed Assets", None, "Asset"),
    ("Investments", None, "Asset"),
    ("Current Assets", None, "Asset"),
    ("Bank Accounts", "Current Assets", "Asset"),
    ("Cash-in-Hand", "Current Assets", "Asset"),
    ("Sundry Debtors", "Current Assets", "Asset"),
    ("Deposits (Asset)", "Current Assets", "Asset"),
    ("Loans & Advances (Asset)", "Current Assets", "Asset"),
    ("Stock-in-Hand", "Current Assets", "Asset"),
    ("Misc. Expenses (ASSET)", None, "Asset"),
    ("Suspense A/c", None, "Asset"),
    ("Sales Accounts", None, "Income"),
    ("Direct Incomes", None, "Income"),
    ("Indirect Incomes", None, "Income"),
    ("Purchase Accounts", None, "Expense"),
    ("Direct Expenses", None, "Expense"),
    ("Indirect Expenses", None, "Expense"),
]

# (name, group)
DEFAULT_LEDGERS = [
    ("Cash", "Cash-in-Hand"),
    ("Sales", "Sales Accounts"),
    ("Purchase", "Purchase Accounts"),
    ("Output CGST", "Duties & Taxes"),
    ("Output SGST", "Duties & Taxes"),
    ("Output IGST", "Duties & Taxes"),
    ("Input CGST", "Duties & Taxes"),
    ("Input SGST", "Duties & Taxes"),
    ("Input IGST", "Duties & Taxes"),
    ("Bank Charges", "Indirect Expenses"),
    ("Interest Received", "Indirect Incomes"),
    ("Suspense A/c", "Suspense A/c"),
]

# Starter keyword rules for bank narrations.
DEFAULT_RULES = [
    ("SMS CHARGES", "Bank Charges"),
    ("BANK CHARGES", "Bank Charges"),
    ("SERVICE CHARGE", "Bank Charges"),
    ("INTEREST", "Interest Received"),
    ("ATM WDL", "Cash"),
    ("CASH WITHDRAWAL", "Cash"),
    ("CASH DEPOSIT", "Cash"),
]


def home_dir() -> Path:
    return Path(os.environ.get("EASYTALLY_HOME", Path.home() / "EasyTally"))


def companies_dir() -> Path:
    d = home_dir() / "companies"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "company"


def company_path(slug: str) -> Path:
    return companies_dir() / f"{slug}.db"


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)  # web server uses threads + a lock
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA_VERSION = 1


def migrate(conn: sqlite3.Connection):
    """Bring an older company file up to date. Safe to run on every open."""
    conn.executescript(SCHEMA)            # all CREATE ... IF NOT EXISTS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        # v1: invoices.kind accepts Credit Note / Debit Note (old files had a CHECK on it).
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name='invoices'").fetchone()
        if row and "CHECK" in row[0]:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("""CREATE TABLE invoices_new (
                voucher_id INTEGER PRIMARY KEY REFERENCES vouchers(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                party_id INTEGER NOT NULL REFERENCES ledgers(id),
                intra_state INTEGER NOT NULL,
                taxable INTEGER NOT NULL,
                cgst INTEGER NOT NULL, sgst INTEGER NOT NULL, igst INTEGER NOT NULL,
                total INTEGER NOT NULL)""")
            conn.execute("INSERT INTO invoices_new SELECT * FROM invoices")
            conn.execute("DROP TABLE invoices")
            conn.execute("ALTER TABLE invoices_new RENAME TO invoices")
            conn.execute("PRAGMA foreign_keys = ON")
    _ensure_groups(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _ensure_groups(conn):
    """Add any default groups missing from older files (does not touch existing ones)."""
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM groups")}
    for gname, parent, nature in DEFAULT_GROUPS:
        if gname not in ids:
            cur = conn.execute("INSERT INTO groups(name, parent_id, nature) VALUES (?,?,?)",
                               (gname, ids.get(parent), nature))
            ids[gname] = cur.lastrowid


def init_company(conn: sqlite3.Connection, name: str, state: str = "", gstin: str = ""):
    conn.executescript(SCHEMA)
    conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                     [("name", name), ("state", state), ("gstin", gstin)])
    ids = {}
    for gname, parent, nature in DEFAULT_GROUPS:
        cur = conn.execute("INSERT INTO groups(name, parent_id, nature) VALUES (?,?,?)",
                           (gname, ids.get(parent), nature))
        ids[gname] = cur.lastrowid
    lids = {}
    for lname, gname in DEFAULT_LEDGERS:
        cur = conn.execute("INSERT INTO ledgers(name, group_id) VALUES (?,?)", (lname, ids[gname]))
        lids[lname] = cur.lastrowid
    for kw, lname in DEFAULT_RULES:
        conn.execute("INSERT INTO bank_rules(keyword, ledger_id) VALUES (?,?)", (kw, lids[lname]))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
