"""Move accounting data between EasyTally and Tally (Prime / ERP 9) using Tally's XML format.

Scope: groups, ledgers (with opening balances and GSTIN) and accounting vouchers. Stock items,
inventory allocations and bill references are not carried over. In Tally XML a *debit* amount is
negative and a credit amount is positive.
"""
import hashlib
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from . import db
from .accounting import Books, GSTIN_RE
from .gst import STATES
from .money import to_paise

MAX_WARNINGS = 60
SYNONYMS = {"Expenses (Direct)": "Direct Expenses", "Expenses (Indirect)": "Indirect Expenses",
            "Income (Direct)": "Direct Incomes", "Income (Indirect)": "Indirect Incomes",
            "Branch / Divisions": "Current Liabilities", "Retained Earnings": "Reserves & Surplus",
            "Primary": ""}
STATE_BY_NAME = {n.lower(): c for c, n in STATES.items()}


def _num(text) -> Decimal:
    try:
        return Decimal(str(text).replace(",", "").strip() or "0")
    except InvalidOperation:
        raise ValueError(f"Bad number {text!r}")


# ------------------------------------------------------------------------------------ export
def _envelope(company: str, report: str):
    env = ET.Element("ENVELOPE")
    h = ET.SubElement(env, "HEADER")
    ET.SubElement(h, "TALLYREQUEST").text = "Import Data"
    body = ET.SubElement(env, "BODY")
    imp = ET.SubElement(body, "IMPORTDATA")
    desc = ET.SubElement(imp, "REQUESTDESC")
    ET.SubElement(desc, "REPORTNAME").text = report
    sv = ET.SubElement(desc, "STATICVARIABLES")
    ET.SubElement(sv, "SVCURRENTCOMPANY").text = company
    return env, ET.SubElement(imp, "REQUESTDATA")


def _dump(env) -> bytes:
    ET.indent(env)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(env, encoding="utf-8")


def export_masters(b: Books) -> bytes:
    """Ledgers as a Tally 'All Masters' import file. Import this first."""
    env, data = _envelope(b.company_name, "All Masters")
    for led in b.ledgers():
        msg = ET.SubElement(data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        e = ET.SubElement(msg, "LEDGER", {"NAME": led["name"], "ACTION": "Create"})
        ET.SubElement(e, "NAME").text = led["name"]
        ET.SubElement(e, "PARENT").text = led["group_name"]
        if led["opening"]:
            ET.SubElement(e, "OPENINGBALANCE").text = f"{-led['opening'] / 100:.2f}"      # Dr is negative
        if led["gstin"]:
            ET.SubElement(e, "PARTYGSTIN").text = led["gstin"]
        if led["state"] in STATES:
            ET.SubElement(e, "LEDSTATENAME").text = STATES[led["state"]]
        if led["group_name"] in ("Sundry Debtors", "Sundry Creditors"):
            ET.SubElement(e, "ISBILLWISEON").text = "Yes"
    return _dump(env)


def export_vouchers(b: Books, start: str = None, end: str = None) -> bytes:
    """Accounting vouchers as a Tally 'Vouchers' import file. Import after the masters."""
    env, data = _envelope(b.company_name, "Vouchers")
    for v in b.vouchers(start, end):
        msg = ET.SubElement(data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        e = ET.SubElement(msg, "VOUCHER", {"VCHTYPE": v["vtype"], "ACTION": "Create",
                                          "OBJVIEW": "Accounting Voucher View"})
        ET.SubElement(e, "DATE").text = v["date"].replace("-", "")
        ET.SubElement(e, "VOUCHERTYPENAME").text = v["vtype"]
        ET.SubElement(e, "VOUCHERNUMBER").text = str(v["number"])
        if v["ref"]:
            ET.SubElement(e, "REFERENCE").text = v["ref"]
        if v["narration"]:
            ET.SubElement(e, "NARRATION").text = v["narration"]
        ET.SubElement(e, "ISINVOICE").text = "No"
        entries = b.conn.execute(
            "SELECT e.amount, l.name FROM entries e JOIN ledgers l ON l.id=e.ledger_id "
            "WHERE e.voucher_id=? ORDER BY e.id", (v["id"],)).fetchall()
        for x in entries:
            le = ET.SubElement(e, "ALLLEDGERENTRIES.LIST")
            ET.SubElement(le, "LEDGERNAME").text = x["name"]
            ET.SubElement(le, "ISDEEMEDPOSITIVE").text = "Yes" if x["amount"] > 0 else "No"
            ET.SubElement(le, "AMOUNT").text = f"{-x['amount'] / 100:.2f}"                 # Dr negative
    return _dump(env)


# ------------------------------------------------------------------------------------ import
def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    # Tally writes raw control characters as references (e.g. &#4;) which XML parsers reject.
    text = re.sub(r"&#(?:x([0-9a-fA-F]+)|(\d+));",
                  lambda m: "" if (int(m.group(1), 16) if m.group(1) else int(m.group(2))) < 32
                  and (int(m.group(1), 16) if m.group(1) else int(m.group(2))) not in (9, 10, 13) else m.group(0), text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text)


def _text(el, *tags):
    for t in tags:
        found = el.find(t)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _name(el) -> str:
    return (el.get("NAME") or _text(el, "NAME", "NAME.LIST/NAME")).strip()


class _Report:
    def __init__(self):
        self.counts = dict(groups=0, ledgers=0, ledgers_existing=0, vouchers=0, duplicates=0, skipped=0)
        self.warnings = []

    def warn(self, msg):
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(msg)
        elif len(self.warnings) == MAX_WARNINGS:
            self.warnings.append("... more warnings omitted")

    def result(self):
        return {**self.counts, "warnings": self.warnings}


def _resolve_group(b: Books, name: str, rep: _Report, known: dict):
    """Return a group id for `name`, mapping Tally synonyms; None if it cannot be placed."""
    name = SYNONYMS.get(name, name)
    if name in known:
        return known[name]
    for gname, parent, nature in db.DEFAULT_GROUPS:         # a standard group missing from this file
        if gname == name:
            pid = _resolve_group(b, parent, rep, known) if parent else None
            cur = b.conn.execute("INSERT INTO groups(name, parent_id, nature) VALUES (?,?,?)", (name, pid, nature))
            known[name] = cur.lastrowid
            return known[name]
    return None


def import_xml(b: Books, data: bytes) -> dict:
    """Import groups, ledgers and vouchers from a Tally XML file. Safe to run twice (vouchers are de-duplicated)."""
    text = _decode(data)
    if re.search(r"<!(doctype|entity)", text, re.I):
        raise ValueError("This XML file uses a DOCTYPE/ENTITY declaration, which is not accepted for safety.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"Could not read the XML file: {e}")
    rep = _Report()
    known = {r["name"]: r["id"] for r in b.conn.execute("SELECT id, name FROM groups")}

    try:
        # ---- groups (parents may appear after children: loop until nothing more can be placed)
        pending = [(_name(g), (g.findtext("PARENT") or "").strip()) for g in root.iter("GROUP")]
        progress = True
        while pending and progress:
            progress, rest = False, []
            for name, parent in pending:
                if not name or name in known:
                    continue
                parent = SYNONYMS.get(parent, parent)
                pid = _resolve_group(b, parent, rep, known) if parent else None
                if parent and pid is None:
                    rest.append((name, parent))
                    continue
                nature = b.conn.execute("SELECT nature FROM groups WHERE id=?", (pid,)).fetchone()["nature"] if pid else None
                if nature is None:
                    rep.warn(f"Group '{name}' skipped: its parent could not be determined.")
                    continue
                cur = b.conn.execute("INSERT INTO groups(name, parent_id, nature) VALUES (?,?,?)", (name, pid, nature))
                known[name] = cur.lastrowid
                rep.counts["groups"] += 1
                progress = True
            pending = rest
        for name, parent in pending:
            rep.warn(f"Group '{name}' skipped: unknown parent '{parent}'.")

        # ---- ledgers
        existing = {r["name"] for r in b.conn.execute("SELECT name FROM ledgers")}
        suspense_gid = known["Suspense A/c"]
        for el in root.iter("LEDGER"):
            name = _name(el)
            if not name:
                continue
            if name in existing:
                rep.counts["ledgers_existing"] += 1
                continue
            parent = (el.findtext("PARENT") or "").strip()
            gid = _resolve_group(b, parent, rep, known) if parent else None
            if gid is None:
                rep.warn(f"Ledger '{name}': group '{parent}' is unknown, placed under Suspense A/c.")
                gid = suspense_gid
            try:
                opening = -int(to_paise(_num(_text(el, "OPENINGBALANCE") or "0")))       # Tally: Dr negative
            except ValueError:
                rep.warn(f"Ledger '{name}': opening balance unreadable, set to 0.")
                opening = 0
            gstin = (_text(el, "PARTYGSTIN", "GSTIN", "LEDGSTREGDETAILS.LIST/GSTIN") or "").upper()
            if gstin and not GSTIN_RE.match(gstin):
                rep.warn(f"Ledger '{name}': GSTIN '{gstin}' is not valid and was left out.")
                gstin = ""
            state = STATE_BY_NAME.get(_text(el, "LEDSTATENAME").lower(), gstin[:2])
            b.conn.execute("INSERT INTO ledgers(name, group_id, opening, gstin, state) VALUES (?,?,?,?,?)",
                           (name, gid, opening, gstin, state))
            existing.add(name)
            rep.counts["ledgers"] += 1

        # ---- vouchers
        ledger_ids = {r["name"]: r["id"] for r in b.conn.execute("SELECT id, name FROM ledgers")}
        for v in root.iter("VOUCHER"):
            vtype = (v.get("VCHTYPE") or _text(v, "VOUCHERTYPENAME")).strip()
            number = _text(v, "VOUCHERNUMBER")
            label = f"{vtype} {number}".strip()
            if _text(v, "ISCANCELLED").lower() == "yes" or _text(v, "ISOPTIONAL").lower() == "yes":
                rep.counts["skipped"] += 1
                continue
            raw_date = _text(v, "DATE")
            if not re.fullmatch(r"\d{8}", raw_date) or not vtype:
                rep.warn(f"Voucher {label or '?'} skipped: missing type or date.")
                rep.counts["skipped"] += 1
                continue
            iso = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
            entries, total = [], 0
            for ent in list(v.findall("ALLLEDGERENTRIES.LIST")) + list(v.findall("LEDGERENTRIES.LIST")):
                lname = _text(ent, "LEDGERNAME")
                try:
                    paise = -to_paise(_num(_text(ent, "AMOUNT") or "0"))                 # Tally: Dr negative -> +Dr
                except ValueError:
                    paise = None
                if not lname or paise is None:
                    entries = None
                    break
                if paise == 0:
                    continue
                entries.append((lname, paise))
                total += paise
            if not entries or total != 0 or len(entries) < 2:
                rep.warn(f"Voucher {label} dated {iso} skipped: entries missing or not balanced.")
                rep.counts["skipped"] += 1
                continue
            narration = _text(v, "NARRATION")
            fp = hashlib.sha1(repr((vtype, iso, number, narration, sorted(entries))).encode()).hexdigest()
            if b.conn.execute("SELECT 1 FROM tally_imports WHERE fingerprint=?", (fp,)).fetchone():
                rep.counts["duplicates"] += 1
                continue
            lines = []
            for lname, paise in entries:
                if lname not in ledger_ids:
                    cur = b.conn.execute("INSERT INTO ledgers(name, group_id) VALUES (?,?)", (lname, suspense_gid))
                    ledger_ids[lname] = cur.lastrowid
                    rep.warn(f"Ledger '{lname}' was not in the file; created under Suspense A/c.")
                lines.append((ledger_ids[lname], "Dr" if paise > 0 else "Cr", abs(paise) / 100))
            vid = b.add_voucher(vtype, iso, lines, narration, _text(v, "REFERENCE") or (f"Tally #{number}" if number else ""),
                                _commit=False)
            b.conn.execute("INSERT INTO tally_imports(fingerprint, voucher_id) VALUES (?,?)", (fp, vid))
            rep.counts["vouchers"] += 1
        b.conn.commit()
    except Exception:
        b.conn.rollback()
        raise
    return rep.result()
