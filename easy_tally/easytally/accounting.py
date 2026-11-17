"""Core double-entry bookkeeping engine: masters, vouchers, invoices, bank staging."""
import hashlib
import uuid
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from . import db
from .gst import compute_gst, is_intra_state
from .money import fmt, to_paise

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

DATE_FORMATS = ["%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
                "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y",
                "%d %B %Y", "%d-%B-%Y", "%b %d %Y"]


def parse_date(text: str) -> str:
    """Parse many human date formats (day-first) into ISO yyyy-mm-dd, or raise ValueError."""
    s = re.sub(r"[\s,]+", " ", str(text).strip())
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {text!r}")


def display_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%Y")


def fy_start(today: date = None) -> str:
    """ISO date of 1 April of the current Indian financial year."""
    today = today or date.today()
    year = today.year if today.month >= 4 else today.year - 1
    return date(year, 4, 1).isoformat()


VOUCHER_TYPES = ["Payment", "Receipt", "Journal", "Contra"]
INVOICE_KINDS = ["Sales", "Purchase", "Credit Note", "Debit Note"]
SETTING_KEYS = ["name", "address", "phone", "email", "state", "gstin", "pan", "bank_details", "terms",
                "cash_payment_limit", "cash_receipt_limit", "cash_limit_mode"]
# Cash limits (India): Sec 40A(3) disallows cash payments above Rs 10,000 per person per day; Sec 269ST bars
# receiving Rs 2,00,000 or more in cash per person per day. A limit is "exceeded" when the amount is GREATER
# than it, so the receipt default is one paisa below 2 lakh. Both are editable; mode: warn | block | off.
DEFAULT_CASH_PAYMENT_LIMIT = 10_000 * 100
DEFAULT_CASH_RECEIPT_LIMIT = 19_999_999
CASH_MODES = ("warn", "block", "off")
CASH_GROUP = "Cash-in-Hand"
OWN_ACCOUNT_GROUPS = ("Bank Accounts", CASH_GROUP)


class CashLimitError(ValueError):
    """A voucher breaks the cash limit. `blocked` is True when the company is set to refuse such entries;
    otherwise the caller may repeat the call with confirmation."""
    def __init__(self, warnings, blocked):
        super().__init__("; ".join(warnings))
        self.warnings = list(warnings)
        self.blocked = blocked


class Books:
    """All accounting operations for one company database."""

    def __init__(self, path):
        self.path = Path(path)
        self.conn = db.connect(self.path)
        db.migrate(self.conn)

    # ---- company lifecycle -------------------------------------------------
    @classmethod
    def create(cls, name: str, state: str = "", gstin: str = "") -> "Books":
        name = name.strip()
        if not name:
            raise ValueError("Company name is required")
        path = db.company_path(db.slugify(name))
        if path.exists():
            raise ValueError(f"A company named '{name}' already exists")
        conn = db.connect(path)
        try:
            db.init_company(conn, name, state, gstin)
        finally:
            conn.close()
        return cls(path)

    @staticmethod
    def list_companies():
        """[(slug, display name)] of every customer company on this machine."""
        out = []
        for p in sorted(db.companies_dir().glob("*.db")):
            try:
                conn = db.connect(p)
                row = conn.execute("SELECT value FROM settings WHERE key='name'").fetchone()
                conn.close()
                out.append((p.stem, row["value"] if row else p.stem))
            except sqlite3.Error:
                continue
        return out

    @classmethod
    def open(cls, slug: str) -> "Books":
        return cls(db.company_path(slug))

    def close(self):
        self.conn.close()

    def setting(self, key: str) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

    @property
    def company_name(self) -> str:
        return self.setting("name")

    def settings(self) -> dict:
        return {k: self.setting(k) for k in SETTING_KEYS}

    def set_settings(self, values: dict):
        """Update company details (printed on invoices and used for GST returns)."""
        clean = {}
        for k in SETTING_KEYS:
            if k in values:
                clean[k] = str(values[k]).strip()
        if "name" in clean and not clean["name"]:
            raise ValueError("Company name cannot be empty")
        gstin = clean.get("gstin", "").upper()
        if gstin and not GSTIN_RE.match(gstin):
            raise ValueError("GSTIN looks wrong: it should be 15 characters, e.g. 27ABCDE1234F1Z5")
        if "gstin" in clean:
            clean["gstin"] = gstin
        for k in ("cash_payment_limit", "cash_receipt_limit"):
            if clean.get(k):
                if to_paise(clean[k]) < 0:
                    raise ValueError("Cash limits cannot be negative")
        if clean.get("cash_limit_mode") and clean["cash_limit_mode"] not in CASH_MODES:
            raise ValueError("Cash limit mode must be warn, block or off")
        for k, v in clean.items():
            self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, v))
        self.conn.commit()

    def audit(self, user: str, action: str, detail: str = ""):
        self.conn.execute("INSERT INTO audit_log(user, action, detail) VALUES (?,?,?)",
                          (user or "", action, detail[:500]))
        self.conn.commit()

    def audit_log(self, limit: int = 300):
        return self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---- cash limits ---------------------------------------------------------
    def cash_limits(self):
        """(payment limit, receipt limit, mode); amounts in paise. Missing settings use the legal defaults."""
        cfg = self.settings()

        def amt(key, default):
            v = (cfg.get(key) or "").strip()
            return to_paise(v) if v else default
        mode = cfg.get("cash_limit_mode") or "warn"
        return (amt("cash_payment_limit", DEFAULT_CASH_PAYMENT_LIMIT),
                amt("cash_receipt_limit", DEFAULT_CASH_RECEIPT_LIMIT), mode if mode in CASH_MODES else "warn")

    def _ledger_groups(self):
        return {r["id"]: (r["name"], r["gname"]) for r in self.conn.execute(
            "SELECT l.id, l.name, g.name gname FROM ledgers l JOIN groups g ON g.id=l.group_id")}

    def _cash_split(self, signed):
        """Split [(ledger_id, paise)] into (cash lines, other lines, group map), or None when the voucher is
        a plain transfer between the company's own cash/bank accounts (not a payment or receipt to a person)."""
        info = self._ledger_groups()
        cash = [(l, a) for l, a in signed if info[l][1] == CASH_GROUP]
        others = [(l, a) for l, a in signed if info[l][1] != CASH_GROUP]
        if not cash or all(info[l][1] in OWN_ACCOUNT_GROUPS for l, _ in others):
            return None
        return cash, others, info

    def cash_single_excess(self, signed):
        """[(ledger_id, paise, kind, limit)] for each cash line above its limit. kind: 'payment' or 'receipt'."""
        pay, recv, mode = self.cash_limits()
        split = self._cash_split(signed)
        if mode == "off" or not split:
            return []
        out = []
        for lid, a in split[0]:
            kind, limit = ("receipt", recv) if a > 0 else ("payment", pay)
            if limit > 0 and abs(a) > limit:
                out.append((lid, abs(a), kind, limit))
        return out

    def cash_warnings(self, signed, iso_date, exclude_voucher=None):
        """Human-readable cash-limit problems for a voucher about to be saved (or already saved)."""
        pay, recv, mode = self.cash_limits()
        split = self._cash_split(signed)
        if mode == "off" or not split:
            return []
        cash, others, info = split
        who = ", ".join(info[l][0] for l, _ in others)
        warnings = [f"Cash {kind} of {fmt(amt)} {'from' if kind == 'receipt' else 'to'} {who} is above the limit of "
                    f"{fmt(limit)}" for _, amt, kind, limit in self.cash_single_excess(signed)]
        # Day total with the same person (splitting one payment into several does not avoid the limit).
        if len(others) == 1:
            party = others[0][0]
            for sign, kind, limit in ((-1, "payment", pay), (1, "receipt", recv)):
                mine = sum(abs(a) for _, a in cash if (a > 0) == (sign > 0))
                if not mine or limit <= 0 or mine > limit:
                    continue                                  # nothing to add up, or already reported above
                other_day = self.conn.execute(
                    """SELECT COALESCE(SUM(ABS(c.amount)), 0) FROM entries c
                       JOIN vouchers v ON v.id = c.voucher_id
                       JOIN ledgers cl ON cl.id = c.ledger_id JOIN groups cg ON cg.id = cl.group_id
                       WHERE cg.name = ? AND v.date = ? AND v.id != ? AND (c.amount > 0) = ?
                         AND EXISTS (SELECT 1 FROM entries p WHERE p.voucher_id = v.id AND p.ledger_id = ?)""",
                    (CASH_GROUP, iso_date, exclude_voucher or -1, sign > 0, party)).fetchone()[0]
                if other_day and mine + other_day > limit:
                    warnings.append(f"Cash {kind}s {'from' if kind == 'receipt' else 'to'} {info[party][0]} on "
                                    f"{display_date(iso_date)} total {fmt(mine + other_day)}, above the limit of {fmt(limit)}")
        return warnings

    def _check_cash(self, signed, iso_date, mode, exclude_voucher=None):
        """mode: 'ask' (default), 'confirmed' (user already agreed) or 'skip' (historic imports)."""
        if mode == "skip":
            return
        warnings = self.cash_warnings(signed, iso_date, exclude_voucher)
        if not warnings:
            return
        blocked = self.cash_limits()[2] == "block"
        if blocked or mode != "confirmed":
            raise CashLimitError(warnings, blocked)

    def voucher_cash_warnings(self, voucher_id: int):
        """Cash-limit problems of a saved voucher (used to colour lists red and explain why)."""
        v = self.conn.execute("SELECT date FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
        if not v:
            return []
        signed = [(r["ledger_id"], r["amount"]) for r in
                  self.conn.execute("SELECT ledger_id, amount FROM entries WHERE voucher_id=?", (voucher_id,))]
        return self.cash_warnings(signed, v["date"], exclude_voucher=voucher_id)

    def voucher_cash_over(self, voucher_id: int) -> bool:
        return bool(self.voucher_cash_warnings(voucher_id))

    # ---- masters -----------------------------------------------------------
    def groups(self):
        return self.conn.execute("SELECT * FROM groups ORDER BY name").fetchall()

    def group_id(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone()
        if not row:
            raise ValueError(f"Unknown group: {name}")
        return row["id"]

    def add_ledger(self, name, group, opening="0", side="Dr", gstin="", state="") -> int:
        name = name.strip()
        if not name:
            raise ValueError("Ledger name is required")
        amount = to_paise(opening)
        if amount < 0:
            raise ValueError("Opening balance must not be negative; choose Dr or Cr")
        signed = amount if side == "Dr" else -amount
        gstin = gstin.strip().upper()
        if gstin and not GSTIN_RE.match(gstin):
            raise ValueError("GSTIN looks wrong: it should be 15 characters, e.g. 27ABCDE1234F1Z5")
        if gstin and not state:
            state = gstin[:2]            # first two digits of a GSTIN are the state code
        try:
            cur = self.conn.execute(
                "INSERT INTO ledgers(name, group_id, opening, gstin, state) VALUES (?,?,?,?,?)",
                (name, self.group_id(group), signed, gstin, state))
        except sqlite3.IntegrityError:
            raise ValueError(f"Ledger '{name}' already exists")
        self.conn.commit()
        return cur.lastrowid

    def ledgers(self, group: str = None):
        q = ("SELECT l.*, g.name AS group_name, g.nature FROM ledgers l "
             "JOIN groups g ON g.id = l.group_id")
        args = ()
        if group:
            q += " WHERE g.name = ?"
            args = (group,)
        return self.conn.execute(q + " ORDER BY l.name", args).fetchall()

    def ledger_id(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM ledgers WHERE name=?", (name,)).fetchone()
        if not row:
            raise ValueError(f"Unknown ledger: {name}")
        return row["id"]

    def get_ledger(self, ledger_id: int):
        return self.conn.execute(
            "SELECT l.*, g.name AS group_name FROM ledgers l JOIN groups g ON g.id=l.group_id "
            "WHERE l.id=?", (ledger_id,)).fetchone()

    def delete_ledger(self, ledger_id: int):
        used = self.conn.execute("SELECT 1 FROM entries WHERE ledger_id=? LIMIT 1", (ledger_id,)).fetchone()
        if used:
            raise ValueError("This ledger has transactions and cannot be deleted")
        self.conn.execute("DELETE FROM ledgers WHERE id=?", (ledger_id,))
        self.conn.commit()

    def add_stock_item(self, name, unit="Nos", hsn="", gst_rate=18, opening_qty=0, opening_value="0") -> int:
        name = name.strip()
        if not name:
            raise ValueError("Item name is required")
        try:
            cur = self.conn.execute(
                "INSERT INTO stock_items(name, unit, hsn, gst_rate, opening_qty, opening_value) "
                "VALUES (?,?,?,?,?,?)",
                (name, unit.strip() or "Nos", hsn.strip(), int(gst_rate), float(opening_qty),
                 to_paise(opening_value)))
        except sqlite3.IntegrityError:
            raise ValueError(f"Item '{name}' already exists")
        self.conn.commit()
        return cur.lastrowid

    def stock_items(self):
        return self.conn.execute("SELECT * FROM stock_items ORDER BY name").fetchall()

    def delete_stock_item(self, item_id: int):
        used = self.conn.execute("SELECT 1 FROM invoice_lines WHERE item_id=? LIMIT 1", (item_id,)).fetchone()
        if used:
            raise ValueError("This item is used in invoices and cannot be deleted")
        self.conn.execute("DELETE FROM stock_items WHERE id=?", (item_id,))
        self.conn.commit()

    # ---- vouchers ----------------------------------------------------------
    def _next_number(self, vtype: str) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(number),0)+1 AS n FROM vouchers WHERE vtype=?",
                                (vtype,)).fetchone()
        return row["n"]

    def _signed_entries(self, entries):
        signed = []
        for ledger_id, side, amount in entries:
            p = to_paise(amount)
            if p <= 0:
                raise ValueError("Every amount must be greater than zero")
            if side not in ("Dr", "Cr"):
                raise ValueError("Side must be Dr or Cr")
            if not self.get_ledger(ledger_id):
                raise ValueError(f"Unknown ledger id {ledger_id}")
            signed.append((ledger_id, p if side == "Dr" else -p))
        if len(signed) < 2:
            raise ValueError("A voucher needs at least two entries")
        diff = sum(a for _, a in signed)
        if diff != 0:
            raise ValueError(f"Voucher does not balance (difference {abs(diff) / 100:.2f})")
        return signed

    def add_voucher(self, vtype, vdate, entries, narration="", ref="", _commit=True, cash_check="ask") -> int:
        """entries: [(ledger_id, 'Dr'|'Cr', amount_in_rupees), ...]. Dr total must equal Cr total.

        cash_check: 'ask' raises CashLimitError when a cash limit is broken (the caller can confirm and retry),
        'confirmed' accepts warnings unless the company blocks them, 'skip' ignores limits (historic imports).
        """
        iso = parse_date(vdate)
        signed = self._signed_entries(entries)
        self._check_cash(signed, iso, cash_check)
        number = self._next_number(vtype)
        cur = self.conn.execute(
            "INSERT INTO vouchers(vtype, number, date, narration, ref) VALUES (?,?,?,?,?)",
            (vtype, number, iso, narration.strip(), ref.strip()))
        vid = cur.lastrowid
        self.conn.executemany("INSERT INTO entries(voucher_id, ledger_id, amount) VALUES (?,?,?)",
                              [(vid, lid, a) for lid, a in signed])
        if _commit:
            self.conn.commit()
        return vid

    def _is_invoice(self, voucher_id: int) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM invoices WHERE voucher_id=?", (voucher_id,)).fetchone())

    def update_voucher(self, voucher_id, vdate, entries, narration="", ref="", cash_check="ask"):
        """Edit a plain voucher in place (type and number are kept). Invoices use update_invoice."""
        v = self.conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
        if not v:
            raise ValueError("Voucher not found")
        if self._is_invoice(voucher_id):
            raise ValueError("This is an invoice; edit it from the Invoices screen")
        iso = parse_date(vdate)
        signed = self._signed_entries(entries)
        self._check_cash(signed, iso, cash_check, exclude_voucher=voucher_id)
        try:
            self.conn.execute("UPDATE vouchers SET date=?, narration=?, ref=? WHERE id=?",
                              (iso, narration.strip(), ref.strip(), voucher_id))
            self.conn.execute("DELETE FROM entries WHERE voucher_id=?", (voucher_id,))
            self.conn.executemany("INSERT INTO entries(voucher_id, ledger_id, amount) VALUES (?,?,?)",
                                  [(voucher_id, lid, a) for lid, a in signed])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_voucher(self, voucher_id: int):
        """Everything needed to show an edit form. Invoices include their item lines."""
        v = self.conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
        if not v:
            raise ValueError("Voucher not found")
        out = {"id": v["id"], "vtype": v["vtype"], "number": v["number"], "date": v["date"],
               "narration": v["narration"], "ref": v["ref"], "invoice": None,
               "entries": [{"ledger_id": e["ledger_id"], "name": e["name"],
                            "side": "Dr" if e["amount"] > 0 else "Cr", "amount": abs(e["amount"]) / 100}
                           for e in self.conn.execute(
                               "SELECT e.ledger_id, e.amount, l.name FROM entries e JOIN ledgers l "
                               "ON l.id=e.ledger_id WHERE e.voucher_id=? ORDER BY e.id", (voucher_id,))]}
        inv = self.conn.execute("SELECT * FROM invoices WHERE voucher_id=?", (voucher_id,)).fetchone()
        if inv:
            out["invoice"] = {"kind": inv["kind"], "party_id": inv["party_id"],
                              "lines": [{"item_id": l["item_id"], "qty": l["qty"], "rate": l["rate"] / 100}
                                        for l in self.conn.execute(
                                            "SELECT * FROM invoice_lines WHERE voucher_id=? ORDER BY id",
                                            (voucher_id,))]}
        return out

    def delete_voucher(self, voucher_id: int):
        # Any staged bank row pointing at this voucher is released (ON DELETE SET NULL).
        self.conn.execute("DELETE FROM vouchers WHERE id=?", (voucher_id,))
        self.conn.commit()

    def vouchers(self, start=None, end=None, vtype=None):
        q = "SELECT * FROM vouchers WHERE 1=1"
        args = []
        if start:
            q += " AND date >= ?"; args.append(start)
        if end:
            q += " AND date <= ?"; args.append(end)
        if vtype:
            q += " AND vtype = ?"; args.append(vtype)
        return self.conn.execute(q + " ORDER BY date, id", args).fetchall()

    def voucher_entries(self, voucher_id: int):
        return self.conn.execute(
            "SELECT e.amount, l.name FROM entries e JOIN ledgers l ON l.id=e.ledger_id "
            "WHERE e.voucher_id=? ORDER BY e.amount DESC, e.id", (voucher_id,)).fetchall()

    # ---- balances ----------------------------------------------------------
    def balances(self, upto=None, start=None, with_opening=True):
        """{ledger_id: signed paise}. Dr positive. `start`/`upto` are inclusive ISO dates."""
        rows = self.conn.execute(
            """SELECT l.id, l.opening, COALESCE(SUM(x.amount), 0) AS movement
               FROM ledgers l LEFT JOIN (
                   SELECT e.ledger_id, e.amount FROM entries e JOIN vouchers v ON v.id = e.voucher_id
                   WHERE (:start IS NULL OR v.date >= :start) AND (:upto IS NULL OR v.date <= :upto)
               ) x ON x.ledger_id = l.id GROUP BY l.id""",
            {"start": start, "upto": upto}).fetchall()
        return {r["id"]: (r["opening"] if with_opening else 0) + r["movement"] for r in rows}

    # ---- invoices (GST) ----------------------------------------------------
    @staticmethod
    def _invoice_plan(kind):
        """(main ledger, tax ledger prefix, party side, other side) for each document kind."""
        plans = {"Sales": ("Sales", "Output", "Dr", "Cr"),
                 "Purchase": ("Purchase", "Input", "Cr", "Dr"),
                 "Credit Note": ("Sales", "Output", "Cr", "Dr"),      # sales return
                 "Debit Note": ("Purchase", "Input", "Dr", "Cr")}     # purchase return
        if kind not in plans:
            raise ValueError("Invoice kind must be one of: " + ", ".join(INVOICE_KINDS))
        return plans[kind]

    def preview_invoice(self, kind, party_id, lines):
        """lines: [(item_id, qty, rate_rupees)]. Returns dict with per-line and total tax figures."""
        self._invoice_plan(kind)
        if not lines:
            raise ValueError("Add at least one item")
        party = self.get_ledger(party_id)
        if not party:
            raise ValueError("Select a party")
        intra = is_intra_state(self.setting("state"), party["state"])
        out_lines, totals = [], dict(taxable=0, cgst=0, sgst=0, igst=0)
        for item_id, qty, rate in lines:
            item = self.conn.execute("SELECT * FROM stock_items WHERE id=?", (item_id,)).fetchone()
            if not item:
                raise ValueError("Unknown stock item")
            qty = float(qty)
            if qty <= 0:
                raise ValueError("Quantity must be greater than zero")
            rate_p = to_paise(rate)
            if rate_p < 0:
                raise ValueError("Rate cannot be negative")
            taxable = int(round(qty * rate_p))
            cg, sg, ig = compute_gst(taxable, item["gst_rate"], intra)
            out_lines.append(dict(item_id=item_id, qty=qty, rate=rate_p, gst_rate=item["gst_rate"],
                                  taxable=taxable, cgst=cg, sgst=sg, igst=ig))
            totals["taxable"] += taxable; totals["cgst"] += cg
            totals["sgst"] += sg; totals["igst"] += ig
        totals["total"] = sum(totals[k] for k in ("taxable", "cgst", "sgst", "igst"))
        return dict(intra_state=intra, lines=out_lines, **totals)

    def _invoice_entries(self, kind, party_id, inv):
        main_name, prefix, party_side, other_side = self._invoice_plan(kind)
        entries = [(party_id, party_side, inv["total"] / 100),
                   (self.ledger_id(main_name), other_side, inv["taxable"] / 100)]
        for tax in ("cgst", "sgst", "igst"):
            if inv[tax]:
                entries.append((self.ledger_id(f"{prefix} {tax.upper()}"), other_side, inv[tax] / 100))
        return entries

    def _write_invoice_rows(self, vid, kind, party_id, inv):
        self.conn.execute(
            "INSERT INTO invoices(voucher_id, kind, party_id, intra_state, taxable, cgst, sgst, igst, total)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (vid, kind, party_id, int(inv["intra_state"]), inv["taxable"], inv["cgst"],
             inv["sgst"], inv["igst"], inv["total"]))
        self.conn.executemany(
            "INSERT INTO invoice_lines(voucher_id, item_id, qty, rate, gst_rate, taxable, cgst, sgst, igst)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(vid, l["item_id"], l["qty"], l["rate"], l["gst_rate"], l["taxable"],
              l["cgst"], l["sgst"], l["igst"]) for l in inv["lines"]])

    def add_invoice(self, kind, vdate, party_id, lines, narration="", ref="") -> int:
        """Post a Sales / Purchase invoice or a Credit / Debit Note as a balanced voucher + stock lines."""
        inv = self.preview_invoice(kind, party_id, lines)
        try:
            vid = self.add_voucher(kind, vdate, self._invoice_entries(kind, party_id, inv),
                                   narration, ref, _commit=False, cash_check="skip")
            self._write_invoice_rows(vid, kind, party_id, inv)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return vid

    def update_invoice(self, voucher_id, vdate, party_id, lines, narration="", ref=""):
        """Replace an invoice's party, items, date and reference. Type and number are kept."""
        row = self.conn.execute("SELECT kind FROM invoices WHERE voucher_id=?", (voucher_id,)).fetchone()
        if not row:
            raise ValueError("Invoice not found")
        kind = row["kind"]
        inv = self.preview_invoice(kind, party_id, lines)
        iso = parse_date(vdate)
        signed = self._signed_entries(self._invoice_entries(kind, party_id, inv))
        try:
            self.conn.execute("UPDATE vouchers SET date=?, narration=?, ref=? WHERE id=?",
                              (iso, narration.strip(), ref.strip(), voucher_id))
            self.conn.execute("DELETE FROM entries WHERE voucher_id=?", (voucher_id,))
            self.conn.executemany("INSERT INTO entries(voucher_id, ledger_id, amount) VALUES (?,?,?)",
                                  [(voucher_id, lid, a) for lid, a in signed])
            self.conn.execute("DELETE FROM invoice_lines WHERE voucher_id=?", (voucher_id,))
            self.conn.execute("DELETE FROM invoices WHERE voucher_id=?", (voucher_id,))
            self._write_invoice_rows(voucher_id, kind, party_id, inv)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- bank statement staging -------------------------------------------
    def suggest_ledger(self, narration: str):
        """Return ledger id of the first matching keyword rule (longest keyword wins), else None."""
        text = narration.upper()
        best = None
        for r in self.conn.execute("SELECT keyword, ledger_id FROM bank_rules"):
            kw = r["keyword"].upper()
            if kw in text and (best is None or len(kw) > len(best[0])):
                best = (kw, r["ledger_id"])
        return best[1] if best else None

    def add_rule(self, keyword: str, ledger_id: int):
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("Keyword is required")
        self.conn.execute("INSERT OR REPLACE INTO bank_rules(keyword, ledger_id) VALUES (?,?)",
                          (keyword, ledger_id))
        self.conn.commit()

    def stage_bank_transactions(self, bank_ledger_id: int, filename: str, txns, pages=None, engine: str = "",
                                opening=None):
        """Store parsed statement rows. Returns (new_count, duplicate_count).

        A fingerprint of (bank, date, narration, debit, credit, balance, occurrence-in-file)
        makes re-importing the same statement (or an overlapping one) safe.
        Rows read by OCR are stored *unverified*: they cannot be posted until a person has checked them.
        `pages` are JPEG images of the scanned pages, kept so rows can be checked against them.
        """
        source = "ocr" if any(t.source == "ocr" for t in txns) else "pdf"
        cur = self.conn.execute(
            "INSERT INTO bank_imports(bank_ledger_id, filename, source, pages, engine, opening_balance) VALUES (?,?,?,?,?,?)",
            (bank_ledger_id, Path(filename).name, source, len(pages or []), engine, opening))
        import_id = cur.lastrowid
        seen, new, dup = {}, 0, 0
        for t in txns:
            key = (bank_ledger_id, t.date, t.narration, t.debit, t.credit, t.balance)
            seen[key] = seen.get(key, 0) + 1
            fp = hashlib.sha1((repr(key) + str(seen[key])).encode()).hexdigest()
            counter = self.suggest_ledger(t.narration)
            try:
                c = self.conn.execute(
                    "INSERT INTO bank_txns(import_id, bank_ledger_id, date, narration, ref, debit, credit,"
                    " balance, uncertain, counter_ledger_id, fingerprint, source, verified, confidence, page,"
                    " y0, y1, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (import_id, bank_ledger_id, t.date, t.narration, t.ref, t.debit, t.credit,
                     t.balance, int(t.uncertain), counter, fp, t.source, 0 if t.source == "ocr" else 1,
                     t.confidence, t.page, t.y0, t.y1, t.note))
                self.conn.execute("UPDATE bank_txns SET ord = id WHERE id = ?", (c.lastrowid,))
                new += 1
            except sqlite3.IntegrityError:
                dup += 1
        if new == 0:
            self.conn.execute("DELETE FROM bank_imports WHERE id=?", (import_id,))
        else:
            for n, image in enumerate(pages or [], 1):
                self.conn.execute("INSERT INTO bank_pages(import_id, page, image) VALUES (?,?,?)", (import_id, n, image))
        self.conn.commit()
        return new, dup

    def bank_txns(self, bank_ledger_id=None, pending_only=False, import_id=None):
        q = ("SELECT t.*, l.name AS counter_name FROM bank_txns t "
             "LEFT JOIN ledgers l ON l.id = t.counter_ledger_id WHERE 1=1")
        args = []
        if bank_ledger_id:
            q += " AND t.bank_ledger_id = ?"; args.append(bank_ledger_id)
        if import_id:
            q += " AND t.import_id = ?"; args.append(import_id)
        if pending_only:
            q += " AND t.voucher_id IS NULL"
        return self.conn.execute(q + (" ORDER BY t.ord, t.id" if import_id else " ORDER BY t.date, t.id"), args).fetchall()

    # ---- verifying scanned statements --------------------------------------
    def bank_imports(self, bank_ledger_id: int):
        """One row per imported statement, newest first, with counts for the review list."""
        return self.conn.execute(
            """SELECT i.id, i.filename, i.source, i.pages, i.engine, i.imported_at,
                      i.opening_balance, i.closing_balance,
                      COUNT(t.id) AS rows,
                      SUM(CASE WHEN t.verified = 0 AND t.voucher_id IS NULL THEN 1 ELSE 0 END) AS unverified,
                      SUM(CASE WHEN t.voucher_id IS NOT NULL THEN 1 ELSE 0 END) AS posted
               FROM bank_imports i LEFT JOIN bank_txns t ON t.import_id = i.id
               WHERE i.bank_ledger_id = ? GROUP BY i.id ORDER BY i.id DESC""", (bank_ledger_id,)).fetchall()

    def bank_page(self, import_id: int, page: int):
        r = self.conn.execute("SELECT mime, image FROM bank_pages WHERE import_id=? AND page=?",
                              (import_id, page)).fetchone()
        return (r["mime"], bytes(r["image"])) if r else None

    def _editable_txn(self, txn_id: int):
        r = self.conn.execute("SELECT * FROM bank_txns WHERE id=?", (txn_id,)).fetchone()
        if not r:
            raise ValueError("Bank row not found")
        if r["voucher_id"]:
            raise ValueError("This row is already posted. Delete its voucher first if it must change.")
        return r

    @staticmethod
    def _txn_amounts(debit, credit, balance):
        d, c = to_paise(debit or "0"), to_paise(credit or "0")
        if d < 0 or c < 0:
            raise ValueError("Amounts cannot be negative")
        if (d > 0) == (c > 0):
            raise ValueError("Enter either a debit or a credit amount (not both, not neither)")
        bal = None if balance in (None, "") else to_paise(balance)
        return d, c, bal

    def update_bank_txn(self, txn_id, date, narration, debit, credit, balance=None, verify=True):
        """Correct a staged row after looking at the scan. Saving marks it verified unless verify=False."""
        self._editable_txn(txn_id)
        iso = parse_date(date)
        narration = (narration or "").strip()
        if not narration:
            raise ValueError("Narration is required")
        d, c, bal = self._txn_amounts(debit, credit, balance)
        self.conn.execute(
            "UPDATE bank_txns SET date=?, narration=?, debit=?, credit=?, balance=?, verified=?, uncertain=0 WHERE id=?",
            (iso, narration, d, c, bal, int(bool(verify)), txn_id))
        self.conn.commit()

    def add_bank_txn(self, import_id, after_txn_id, date, narration, debit, credit, balance=None, verify=True):
        """Add a row the OCR missed. It is placed right after `after_txn_id` (or first if None).
        verify=False leaves it for a person to fill in and check (used for placeholder rows)."""
        imp = self.conn.execute("SELECT * FROM bank_imports WHERE id=?", (import_id,)).fetchone()
        if not imp:
            raise ValueError("Statement not found")
        iso = parse_date(date)
        narration = (narration or "").strip()
        if not narration:
            raise ValueError("Narration is required")
        d, c, bal = self._txn_amounts(debit, credit, balance)
        page, y = 1, 0.0
        if after_txn_id:
            a = self.conn.execute("SELECT * FROM bank_txns WHERE id=? AND import_id=?", (after_txn_id, import_id)).fetchone()
            if not a:
                raise ValueError("The row to insert after was not found")
            nxt = self.conn.execute("SELECT MIN(ord) FROM bank_txns WHERE import_id=? AND ord > ?",
                                    (import_id, a["ord"])).fetchone()[0]
            order = (a["ord"] + nxt) / 2 if nxt is not None else a["ord"] + 1
            page, y = a["page"], a["y1"]
        else:
            first = self.conn.execute("SELECT MIN(ord) FROM bank_txns WHERE import_id=?", (import_id,)).fetchone()[0]
            order = (first - 1) if first is not None else 1
        fp = hashlib.sha1(f"manual|{uuid.uuid4()}".encode()).hexdigest()
        cur = self.conn.execute(
            "INSERT INTO bank_txns(import_id, bank_ledger_id, date, narration, debit, credit, balance, counter_ledger_id,"
            " fingerprint, source, verified, page, y0, y1, ord, note) VALUES (?,?,?,?,?,?,?,?,?,'manual',?,?,?,?,?,?)",
            (import_id, imp["bank_ledger_id"], iso, narration, d, c, bal, self.suggest_ledger(narration), fp,
             int(bool(verify)), page, y, y, order, "added by hand"))
        self.conn.commit()
        return cur.lastrowid

    def delete_bank_txns(self, txn_ids):
        for t in txn_ids:
            self._editable_txn(t)
        self.conn.executemany("DELETE FROM bank_txns WHERE id=?", [(t,) for t in txn_ids])
        self.conn.commit()

    def verify_bank_txns(self, txn_ids, verified=True):
        """Mark staged rows as checked by a person (or un-check them). Posted rows are left alone."""
        self.conn.executemany("UPDATE bank_txns SET verified=? WHERE id=? AND voucher_id IS NULL",
                              [(int(bool(verified)), t) for t in txn_ids])
        self.conn.commit()

    def set_import_balances(self, import_id, opening, closing):
        """Opening / closing balance as printed on the statement (blank clears)."""
        o = None if opening in (None, "") else to_paise(opening)
        c = None if closing in (None, "") else to_paise(closing)
        self.conn.execute("UPDATE bank_imports SET opening_balance=?, closing_balance=? WHERE id=?", (o, c, import_id))
        self.conn.commit()

    def reconcile(self, import_id: int):
        """Check a statement's running balance row by row, in statement order.

        A row is OK when previous balance + credit - debit equals its printed balance. For a row that is not
        OK we say what would fix it: its balance, its amount, or a missing row before it.
        """
        imp = self.conn.execute("SELECT * FROM bank_imports WHERE id=?", (import_id,)).fetchone()
        if not imp:
            raise ValueError("Statement not found")
        rows = self.conn.execute("SELECT * FROM bank_txns WHERE import_id=? ORDER BY ord, id", (import_id,)).fetchall()
        opening, inferred = imp["opening_balance"], False
        prev = opening
        out, ok_n, bad_n, unchecked, debits, credits = [], 0, 0, 0, 0, 0
        for r in rows:
            net = r["credit"] - r["debit"]
            debits += r["debit"]
            credits += r["credit"]
            item = {"id": r["id"], "ok": None}
            if r["balance"] is None:
                prev = None if prev is None else prev + net
                unchecked += 1
            elif prev is None:                       # first row and no opening balance: it becomes the anchor
                if opening is None:
                    opening, inferred = r["balance"] - net, True
                prev = r["balance"]
                unchecked += 1
            else:
                expected = prev + net
                if expected == r["balance"]:
                    item["ok"] = True
                    ok_n += 1
                else:
                    bad_n += 1
                    delta = r["balance"] - prev
                    missing = delta - net
                    item.update(ok=False, expected=expected,
                                fix_amount=({"side": "credit" if delta > 0 else "debit", "amount": abs(delta)}
                                            if delta else None),
                                missing_row=({"side": "credit" if missing > 0 else "debit", "amount": abs(missing)}
                                             if missing else None))
                prev = r["balance"]
            out.append(item)
        computed = None if opening is None else opening + credits - debits
        stated = imp["closing_balance"]
        return {"rows": out, "ok": ok_n, "bad": bad_n, "unchecked": unchecked, "debit_total": debits,
                "credit_total": credits, "opening": opening, "opening_inferred": inferred,
                "closing_computed": computed, "closing_stated": stated,
                "closing_diff": None if stated is None or computed is None else stated - computed}

    def verify_reconciled(self, import_id: int) -> int:
        """Mark every unverified row whose running balance adds up as verified. Returns how many."""
        rec = self.reconcile(import_id)
        ok_ids = {x["id"] for x in rec["rows"] if x["ok"] is True}
        todo = [r["id"] for r in self.conn.execute(
            "SELECT id FROM bank_txns WHERE import_id=? AND verified=0 AND voucher_id IS NULL", (import_id,))
            if r["id"] in ok_ids]
        self.verify_bank_txns(todo, True)
        return len(todo)

    def set_txn_ledger(self, txn_id: int, ledger_id, learn_keyword: str = ""):
        """Assign a counter ledger to a staged row. Optionally learn a keyword rule and apply it
        to other pending rows that have no ledger yet."""
        self.conn.execute("UPDATE bank_txns SET counter_ledger_id=? WHERE id=? AND voucher_id IS NULL",
                          (ledger_id, txn_id))
        if learn_keyword.strip() and ledger_id:
            self.add_rule(learn_keyword, ledger_id)
            self.conn.execute(
                "UPDATE bank_txns SET counter_ledger_id=? WHERE voucher_id IS NULL "
                "AND counter_ledger_id IS NULL AND UPPER(narration) LIKE ?",
                (ledger_id, f"%{learn_keyword.strip().upper()}%"))
        self.conn.commit()

    def post_bank_transactions(self, txn_ids, use_suspense: bool = False):
        """Create Payment/Receipt vouchers for staged rows. Returns (posted, skipped).

        Withdrawal -> Payment: Dr counter ledger, Cr bank.   Deposit -> Receipt: Dr bank, Cr counter.
        Rows with no ledger are skipped unless use_suspense, which books them to 'Suspense A/c'.
        """
        suspense = self.ledger_id("Suspense A/c")
        posted = skipped = 0
        try:
            for tid in txn_ids:
                t = self.conn.execute("SELECT * FROM bank_txns WHERE id=?", (tid,)).fetchone()
                if not t or t["voucher_id"] or not t["verified"]:      # scanned rows need a person's check first
                    skipped += 1
                    continue
                counter = t["counter_ledger_id"] or (suspense if use_suspense else None)
                if not counter:
                    skipped += 1
                    continue
                if t["debit"] > 0:
                    vtype, entries, amt = "Payment", [(counter, "Dr"), (t["bank_ledger_id"], "Cr")], t["debit"]
                else:
                    vtype, entries, amt = "Receipt", [(t["bank_ledger_id"], "Dr"), (counter, "Cr")], t["credit"]
                vid = self.add_voucher(vtype, t["date"], [(l, s, amt / 100) for l, s in entries],
                                       narration=t["narration"], ref=t["ref"], _commit=False, cash_check="confirmed")
                self.conn.execute("UPDATE bank_txns SET voucher_id=?, counter_ledger_id=? WHERE id=?",
                                  (vid, counter, tid))
                posted += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return posted, skipped
