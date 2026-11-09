"""Parse text-based bank statement PDFs into transactions.

Two strategies, tried in order:
  1. Table mode - pdfplumber finds ruled tables; columns are located from the header row
     (Date / Narration / Debit|Withdrawal / Credit|Deposit / Balance).
  2. Text mode  - each line that starts with a date is a transaction; the trailing amounts
     are the transaction amount and running balance. Debit vs credit is worked out from the
     change in running balance, which is far more reliable than guessing from the text.

Scanned (image-only) PDFs are detected and rejected with a clear message.
"""
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .accounting import parse_date
from .money import to_paise


@dataclass
class Txn:
    date: str            # ISO
    narration: str
    debit: int = 0       # paise
    credit: int = 0      # paise
    balance: int = None  # paise (signed; overdraft negative) or None
    ref: str = ""
    uncertain: bool = False   # direction had to be guessed


class StatementError(Exception):
    pass


DATE_PATTERN = (r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
                r"|\d{1,2}[ \-][A-Za-z]{3,9}[ \-,]+\d{2,4}"
                r"|\d{4}-\d{2}-\d{2}")
LINE_START_DATE = re.compile(rf"^\s*({DATE_PATTERN})(?=\s|$)")
AMOUNT = re.compile(r"(?<![\w/.-])\(?-?(?:Rs\.?|INR|₹)?\s?\d[\d,]*\.\d{1,2}\)?(?:\s?(?:Cr|Dr|CR|DR)\b\.?)?")
NOISE = re.compile(r"page\s+\d+|statement of|opening balance|closing balance|total|"
                   r"account (no|number)|generated on|computer generated", re.I)


def _to_decimal(text: str):
    """Parse '1,234.50', '(1,234.50)', '1,234.50 Dr' -> (Decimal, tag). Returns None if not a number."""
    if text is None:
        return None
    s = str(text).strip()
    if not s or s in ("-", "--", "NIL", "nil"):
        return None
    tag = ""
    m = re.search(r"\b(Cr|Dr)\b\.?$", s, re.I)
    if m:
        tag = m.group(1).capitalize()
        s = s[:m.start()].strip()
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"(?i)rs\.?|inr|₹|[(),\-\s]", "", s)
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return (-d if neg else d), tag


def amount_to_paise(text):
    r = _to_decimal(text)
    return None if r is None else to_paise(r[0])


def balance_to_paise(text):
    """Balance with optional Cr/Dr suffix; 'Dr' (overdrawn) is negative."""
    r = _to_decimal(text)
    if r is None:
        return None
    p = to_paise(r[0])
    return -abs(p) if r[1] == "Dr" else p


# --------------------------------------------------------------------------- table mode
def _locate_columns(header):
    cols = {}
    for i, raw in enumerate(header):
        h = (raw or "").lower().replace("\n", " ").strip()
        if not h:
            continue
        if "date" in h and "value" not in h and "date" not in cols:
            cols["date"] = i
        elif any(k in h for k in ("narration", "description", "particular", "details", "remark")):
            cols.setdefault("narration", i)
        elif any(k in h for k in ("chq", "cheque", "ref")):
            cols.setdefault("ref", i)
        elif any(k in h for k in ("debit", "withdrawal", "paid out")) or h in ("dr", "dr."):
            cols.setdefault("debit", i)
        elif any(k in h for k in ("credit", "deposit", "paid in")) or h in ("cr", "cr."):
            cols.setdefault("credit", i)
        elif "balance" in h:
            cols.setdefault("balance", i)
    if "date" in cols and "balance" in cols and "debit" in cols and "credit" in cols:
        if "narration" not in cols:
            others = [i for i in range(len(header)) if i not in cols.values()]
            if others:
                cols["narration"] = others[0]
        return cols
    return None


def _clean(cell):
    return re.sub(r"\s+", " ", (cell or "").replace("\n", " ")).strip()


def _parse_tables(pdf):
    txns, cols = [], None
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            for row in table:
                row = [_clean(c) for c in row]
                found = _locate_columns(row)
                if found:
                    cols = found
                    continue
                if not cols or len(row) <= max(cols.values()):
                    continue
                d = row[cols["date"]]
                try:
                    iso = parse_date(d.split(" ")[0] if not re.search(r"[A-Za-z]", d) else d)
                except ValueError:
                    # continuation of the previous narration
                    if txns and d == "" and row[cols.get("narration", 0)]:
                        txns[-1].narration += " " + row[cols["narration"]]
                    continue
                debit = amount_to_paise(row[cols["debit"]]) or 0
                credit = amount_to_paise(row[cols["credit"]]) or 0
                if debit < 0: debit = -debit
                if credit < 0: credit = -credit
                if debit == 0 and credit == 0:
                    continue
                txns.append(Txn(iso, row[cols.get("narration", 0)], debit, credit,
                                balance_to_paise(row[cols["balance"]]),
                                row[cols["ref"]] if "ref" in cols else ""))
    return txns


# --------------------------------------------------------------------------- text mode
def _direction_hint(narration: str, tag: str):
    """'Dr'/'Cr'/None from explicit tag or narration keywords (used only when balances can't decide)."""
    if tag:
        return tag
    n = narration.upper()
    if re.search(r"\b(CR|DEPOSIT|CREDIT|REFUND|INTEREST|SALARY CREDIT|BY (TRANSFER|CASH|CLG))\b|/CR/|\bNEFT CR|\bIMPS CR", n):
        return "Cr"
    if re.search(r"\b(DR|WDL|WITHDRAWAL|DEBIT|CHARGES?|FEE|PURCHASE|ATM|POS|EMI|TO (TRANSFER|CASH))\b|/DR/", n):
        return "Dr"
    return None


def _parse_text(pdf):
    raw = []   # dicts: date, narration, amounts [(Decimal, tag)]
    opening = None
    for page in pdf.pages:
        for line in (page.extract_text() or "").splitlines():
            m = LINE_START_DATE.match(line)
            if not m:
                om = re.search(r"opening balance\D*?(\(?-?[\d,]+\.\d{2}\)?(?:\s?(?:Cr|Dr))?)", line, re.I)
                if om and opening is None:
                    opening = balance_to_paise(om.group(1))
                elif raw and not NOISE.search(line) and not AMOUNT.search(line) and 0 < len(line.strip()) < 80:
                    raw[-1]["narration"] += " " + line.strip()
                continue
            try:
                iso = parse_date(m.group(1))
            except ValueError:
                continue
            rest = line[m.end():]
            vd = LINE_START_DATE.match(rest)           # skip a second (value) date
            if vd:
                rest = rest[vd.end():]
            amounts = list(AMOUNT.finditer(rest))
            if not amounts:
                continue
            narration = rest[:amounts[0].start()].strip()
            raw.append(dict(date=iso, narration=narration,
                            amounts=[_to_decimal(a.group(0).strip()) for a in amounts]))
    txns, prev_bal = [], opening
    for r in raw:
        amounts = [a for a in r["amounts"] if a]
        if not amounts:
            continue
        t = Txn(r["date"], re.sub(r"\s+", " ", r["narration"]).strip())
        if len(amounts) >= 2:
            amt = to_paise(abs(amounts[-2][0]))
            bal = balance_to_paise(f"{amounts[-1][0]} {amounts[-1][1]}".strip())
            t.balance = bal
            if prev_bal is not None:
                delta = bal - prev_bal
                if abs(delta) == amt:
                    is_credit = delta > 0
                else:  # chain broken (missing row etc.): fall back to hints
                    hint = _direction_hint(t.narration, amounts[-2][1])
                    is_credit = hint == "Cr"
                    t.uncertain = True
            else:
                hint = _direction_hint(t.narration, amounts[-2][1])
                is_credit = hint == "Cr"
                t.uncertain = hint is None
        else:
            amt = to_paise(abs(amounts[0][0]))
            hint = _direction_hint(t.narration, amounts[0][1])
            is_credit = hint == "Cr"
            t.uncertain = hint is None
        if amt == 0:
            continue
        t.credit, t.debit = (amt, 0) if is_credit else (0, amt)
        if t.balance is not None:
            prev_bal = t.balance
        txns.append(t)
    # The first row may be ambiguous; if the second row's balance chain pins it down, fix it.
    if len(txns) >= 2 and txns[0].uncertain and txns[0].balance is not None and txns[1].balance is not None:
        amt1 = txns[1].credit or txns[1].debit
        implied = txns[1].balance - amt1 if txns[1].credit else txns[1].balance + amt1
        if implied == txns[0].balance:
            txns[0].uncertain = False
    return txns


# --------------------------------------------------------------------------- public API
def check_consistency(txns):
    """Return a list of warning strings (running-balance breaks, guessed directions)."""
    warnings = []
    prev = None
    for i, t in enumerate(txns, 1):
        if t.balance is not None and prev is not None:
            expected = prev + t.credit - t.debit
            if expected != t.balance:
                warnings.append(f"Row {i} ({t.date}): balance does not follow from the previous row "
                                f"- a transaction may be missing or misread.")
        if t.balance is not None:
            prev = t.balance
        if t.uncertain:
            warnings.append(f"Row {i} ({t.date}): debit/credit direction was guessed - please verify.")
    return warnings


def parse_statement(path):
    """Parse a bank statement PDF into [Txn]. Raises StatementError with a friendly message."""
    try:
        import pdfplumber
    except ImportError:
        raise StatementError("The 'pdfplumber' package is required: pip install pdfplumber")
    try:
        with pdfplumber.open(str(path)) as pdf:
            if not pdf.pages:
                raise StatementError("The PDF has no pages.")
            if not any((p.extract_text() or "").strip() for p in pdf.pages):
                raise StatementError("This PDF has no selectable text (it looks scanned). "
                                     "Download the statement PDF directly from net banking, "
                                     "or run OCR on it first.")
            txns = _parse_tables(pdf)
            if not txns:
                txns = _parse_text(pdf)
    except StatementError:
        raise
    except Exception as e:  # password-protected, corrupt, etc.
        raise StatementError(f"Could not read the PDF: {e}")
    if not txns:
        raise StatementError("No transactions were recognised. The statement layout may not be "
                             "supported yet - please share a sample so a parser can be added.")
    return txns
