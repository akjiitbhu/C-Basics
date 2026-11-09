"""Core double-entry bookkeeping engine: masters, vouchers, invoices, bank staging."""
import hashlib
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from . import db
from .gst import compute_gst, is_intra_state
from .money import to_paise

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


class Books:
    """All accounting operations for one company database."""

    def __init__(self, path):
        self.path = Path(path)
        self.conn = db.connect(self.path)

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
        try:
            cur = self.conn.execute(
                "INSERT INTO ledgers(name, group_id, opening, gstin, state) VALUES (?,?,?,?,?)",
                (name, self.group_id(group), signed, gstin.strip().upper(), state))
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

    def add_voucher(self, vtype, vdate, entries, narration="", ref="", _commit=True) -> int:
        """entries: [(ledger_id, 'Dr'|'Cr', amount_in_rupees), ...]. Dr total must equal Cr total."""
        iso = parse_date(vdate)
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
    def preview_invoice(self, kind, party_id, lines):
        """lines: [(item_id, qty, rate_rupees)]. Returns dict with per-line and total tax figures."""
        if kind not in ("Sales", "Purchase"):
            raise ValueError("Invoice kind must be Sales or Purchase")
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

    def add_invoice(self, kind, vdate, party_id, lines, narration="", ref="") -> int:
        """Post a Sales or Purchase GST invoice as a balanced voucher plus stock lines."""
        inv = self.preview_invoice(kind, party_id, lines)
        prefix = "Output" if kind == "Sales" else "Input"
        main = self.ledger_id("Sales" if kind == "Sales" else "Purchase")
        party_side, other_side = ("Dr", "Cr") if kind == "Sales" else ("Cr", "Dr")
        entries = [(party_id, party_side, inv["total"] / 100), (main, other_side, inv["taxable"] / 100)]
        for tax in ("cgst", "sgst", "igst"):
            if inv[tax]:
                entries.append((self.ledger_id(f"{prefix} {tax.upper()}"), other_side, inv[tax] / 100))
        try:
            vid = self.add_voucher(kind, vdate, entries, narration, ref, _commit=False)
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
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return vid

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

    def stage_bank_transactions(self, bank_ledger_id: int, filename: str, txns):
        """Store parsed statement rows. Returns (new_count, duplicate_count).

        A fingerprint of (bank, date, narration, debit, credit, balance, occurrence-in-file)
        makes re-importing the same statement (or an overlapping one) safe.
        """
        cur = self.conn.execute("INSERT INTO bank_imports(bank_ledger_id, filename) VALUES (?,?)",
                                (bank_ledger_id, Path(filename).name))
        import_id = cur.lastrowid
        seen, new, dup = {}, 0, 0
        for t in txns:
            key = (bank_ledger_id, t.date, t.narration, t.debit, t.credit, t.balance)
            seen[key] = seen.get(key, 0) + 1
            fp = hashlib.sha1((repr(key) + str(seen[key])).encode()).hexdigest()
            counter = self.suggest_ledger(t.narration)
            try:
                self.conn.execute(
                    "INSERT INTO bank_txns(import_id, bank_ledger_id, date, narration, ref, debit, credit,"
                    " balance, uncertain, counter_ledger_id, fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (import_id, bank_ledger_id, t.date, t.narration, t.ref, t.debit, t.credit,
                     t.balance, int(t.uncertain), counter, fp))
                new += 1
            except sqlite3.IntegrityError:
                dup += 1
        if new == 0:
            self.conn.execute("DELETE FROM bank_imports WHERE id=?", (import_id,))
        self.conn.commit()
        return new, dup

    def bank_txns(self, bank_ledger_id=None, pending_only=False):
        q = ("SELECT t.*, l.name AS counter_name FROM bank_txns t "
             "LEFT JOIN ledgers l ON l.id = t.counter_ledger_id WHERE 1=1")
        args = []
        if bank_ledger_id:
            q += " AND t.bank_ledger_id = ?"; args.append(bank_ledger_id)
        if pending_only:
            q += " AND t.voucher_id IS NULL"
        return self.conn.execute(q + " ORDER BY t.date, t.id", args).fetchall()

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
                if not t or t["voucher_id"]:
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
                                       narration=t["narration"], ref=t["ref"], _commit=False)
                self.conn.execute("UPDATE bank_txns SET voucher_id=?, counter_ledger_id=? WHERE id=?",
                                  (vid, counter, tid))
                posted += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return posted, skipped
