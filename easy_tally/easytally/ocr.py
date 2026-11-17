"""OCR for scanned bank statements (PDF scans and photos), fully offline.

Pipeline:  render page -> straighten (deskew) -> OCR -> word/line boxes -> `parse_ocr_pages` -> transactions.

OCR is never trusted blindly. Every transaction read this way is stored as *unverified* with a
confidence score, the straightened page image and the row's position on it, so a person can check each
row against the scan (see `Books.verify_bank_txns`). Rows whose running balance does not add up are flagged.

Engine: RapidOCR (ONNX models bundled in the pip package, no separate program to install).
"""
import io
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .accounting import parse_date
from .money import to_paise

MAX_PAGES = 60
MAX_PIXELS = 30_000_000          # downscale anything larger (phone photos can be huge)
OCR_DPI = 250                    # rendering resolution for PDF pages
VIEW_MAX_WIDTH = 1500            # width of the page image kept for the review screen
LOW_CONFIDENCE = 0.80


class OcrUnavailable(Exception):
    pass


@dataclass
class Box:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 1.0

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2


@dataclass
class OcrPage:
    boxes: list
    width: int
    height: int
    image: bytes = b""            # straightened page as JPEG, for the review screen


# ------------------------------------------------------------------------------------------ engine
_engine = None
_engine_lock = threading.Lock()      # model loading and inference are serialised
INSTALL_HINT = "OCR needs the 'rapidocr-onnxruntime' package:  pip install rapidocr-onnxruntime"


def engine_status() -> dict:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return {"available": True, "engine": "RapidOCR (offline)", "detail": ""}
    except Exception as e:           # ImportError, or a broken onnxruntime install
        return {"available": False, "engine": None, "detail": f"{INSTALL_HINT} ({e.__class__.__name__})"}


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        except Exception as e:
            raise OcrUnavailable(f"{INSTALL_HINT} ({e.__class__.__name__}: {e})")
    return _engine


def read_boxes(image) -> list:
    """Run OCR on a PIL image -> [Box]. Coordinates are pixels of that image."""
    import numpy as np
    with _engine_lock:
        result, _ = _get_engine()(np.asarray(image.convert("RGB"))[:, :, ::-1])      # RapidOCR expects BGR
    boxes = []
    for pts, text, conf in result or []:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if str(text).strip():
            boxes.append(Box(str(text).strip(), min(xs), min(ys), max(xs), max(ys), float(conf)))
    return boxes


# ------------------------------------------------------------------------------------------ images
def deskew(img):
    """Straighten a slightly tilted scan (+-3 degrees) so rows are horizontal. Returns (image, angle)."""
    import numpy as np
    from PIL import Image
    small = img.copy()
    small.thumbnail((900, 900))
    ink = (np.asarray(small) < 160).astype("uint8") * 255
    base = Image.fromarray(ink)
    best_angle, best_score = 0.0, -1.0
    for a in np.arange(-3.0, 3.01, 0.25):
        rows = np.asarray(base.rotate(float(a), resample=Image.BILINEAR)).sum(axis=1).astype(float)
        score = float(((rows[1:] - rows[:-1]) ** 2).sum())        # sharp row structure = straight text
        if score > best_score:
            best_angle, best_score = float(a), score
    if abs(best_angle) < 0.2:
        return img, 0.0
    return img.rotate(best_angle, resample=Image.BICUBIC, expand=True, fillcolor=255), best_angle


def _prepare(img):
    from PIL import ImageFilter, ImageOps
    img = ImageOps.exif_transpose(img).convert("L")
    if img.width * img.height > MAX_PIXELS:
        f = (MAX_PIXELS / (img.width * img.height)) ** 0.5
        img = img.resize((int(img.width * f), int(img.height * f)))
    # A 3x3 median filter removes scanner speckle without touching text strokes. On simulated scans it lifted
    # reading from 3-5 of 6 rows to 6 of 6 at every noise level tried (blurring or whitening the background
    # made it worse).
    img = img.filter(ImageFilter.MedianFilter(3))
    return ImageOps.autocontrast(img, cutoff=1)


def _jpeg(img) -> bytes:
    view = img.copy()
    if view.width > VIEW_MAX_WIDTH:
        view = view.resize((VIEW_MAX_WIDTH, int(view.height * VIEW_MAX_WIDTH / view.width)))
    buf = io.BytesIO()
    view.convert("L").save(buf, "JPEG", quality=72)
    return buf.getvalue()


def load_images(data: bytes) -> list:
    """PDF (every page rendered) or a JPEG/PNG photo -> [PIL grayscale images]."""
    from PIL import Image
    if data[:4] == b"%PDF":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        if len(pdf) > MAX_PAGES:
            raise OcrUnavailable(f"The PDF has {len(pdf)} pages; scanned statements are limited to {MAX_PAGES} pages.")
        return [_prepare(pdf[i].render(scale=OCR_DPI / 72).to_pil()) for i in range(len(pdf))]
    try:
        return [_prepare(Image.open(io.BytesIO(data)))]
    except Exception:
        raise OcrUnavailable("That file is not a PDF or a JPEG/PNG image.")


def scan_document(data: bytes) -> list:
    """Render, straighten and OCR every page -> [OcrPage]."""
    pages = []
    for img in load_images(data):
        straight, _ = deskew(img)
        pages.append(OcrPage(read_boxes(straight), straight.width, straight.height, _jpeg(straight)))
    return pages


# ------------------------------------------------------------------------------------------ text repair
# Amounts: Indian (12,34,567.89), western (1,234,567.89) or plain (1234.56). Strict separators on purpose:
# OCR often fuses neighbouring numbers ("412.001,52,894.30") and a loose pattern would swallow both.
_AMOUNT_CORE = re.compile(
    r"(?:\d{1,2}(?:,\d{2})*,\d{3}\.\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})(?:\s?(?:Cr|Dr|CR|DR)\b\.?)?")


def iter_amounts(text: str):
    """Yield every amount in `text`. A number may start at the beginning, after a non-digit, exactly where the
    previous amount ended ('412.001,52,894.30'), or after one separator character that follows an amount
    ('5,000.00,1,52,500.00'). It never starts in the middle of a number."""
    pos, last_end = 0, -1
    while pos < len(text):
        after_amount = pos == last_end or (pos - 1 == last_end and text[pos - 1] in ",:;")
        if pos != 0 and not after_amount and (text[pos - 1].isdigit() or text[pos - 1] == ","):
            pos += 1
            continue
        m = _AMOUNT_CORE.match(text, pos)
        if m:
            yield m
            pos = last_end = m.end()
        else:
            pos += 1


def first_amount(text: str):
    return next(iter_amounts(text), None)


DATE_PREFIX_RE = re.compile(
    r"^\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{1,2}[ \-][A-Za-z]{3,9}[ \-,]+\d{2,4}|\d{4}-\d{2}-\d{2})")
LOOSE_DATE_RE = re.compile(r"^\s*[\dOolI|]{1,2}\W{0,2}[\dOolI|]{1,2}\W{0,2}[\dOolI|]{2,4}")
NOISE_RE = re.compile(r"\bpage\b\W{0,3}\d+|statement of|opening balance|closing balance|total|account (no|number)|"
                      r"generated on|computer generated|brought forward|carried forward", re.I)
_DIGITISE = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})


def repair_date_prefix(text: str):
    """Fix common OCR damage at the start of a line and return (repaired text, was_changed).

    `05/0.4/2025` -> `05/04/2025`, `O1-04-2O25` -> `01-04-2025`. Only the leading date-like token is touched.
    """
    m = re.match(r"^(\s*)(\S{6,16}?)(?=\s|[A-Za-z]{3,}|$)", text)
    if not m:
        return text, False
    token = m.group(2)
    fixed = token
    if sum(token.count(c) for c in "/-") >= 2:
        fixed = re.sub(r"[.:,;'`]", "", fixed)              # stray specks inside a dd/mm/yyyy token
    if re.fullmatch(r"[\dOolI|/\-.]+", fixed):
        fixed = "".join(ch.translate(_DIGITISE) if ch.isalpha() or ch == "|" else ch for ch in fixed)
    if fixed != token:
        return text[:m.start(2)] + fixed + text[m.end(2):], True
    return text, False


def _amount(text: str):
    """'1,25,000.00 Cr' -> (paise, tag). Raises ValueError."""
    s = text.strip()
    tag = ""
    m = re.search(r"\b(Cr|Dr)\b\.?$", s, re.I)
    if m:
        tag, s = m.group(1).capitalize(), s[:m.start()].strip()
    try:
        return to_paise(Decimal(s.replace(",", ""))), tag
    except InvalidOperation:
        raise ValueError(text)


# ------------------------------------------------------------------------------------------ layout
@dataclass
class Item:
    kind: str          # 'text' | 'amount'
    text: str
    x0: float
    x1: float
    conf: float
    paise: int = 0
    tag: str = ""

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2


def group_lines(boxes: list) -> list:
    """Cluster boxes into visual rows (top to bottom), each sorted left to right."""
    if not boxes:
        return []
    heights = sorted(b.y1 - b.y0 for b in boxes)
    tol = max(6.0, 0.55 * heights[len(heights) // 2])
    lines, mean = [], None
    for b in sorted(boxes, key=lambda b: b.cy):
        if lines and abs(b.cy - mean) <= tol:
            lines[-1].append(b)
            mean = sum(x.cy for x in lines[-1]) / len(lines[-1])
        else:
            lines.append([b])
            mean = b.cy
    return [sorted(l, key=lambda b: b.x0) for l in lines]


def _items(line: list) -> list:
    """Split each box into text pieces and amounts, keeping an x position for every piece."""
    out = []
    for b in line:
        text = re.sub(r"(?<=\d);(?=\d)", ",", b.text)               # ';' read instead of ','
        n, last = max(len(text), 1), 0
        span = b.x1 - b.x0

        def put(kind, s, a, z, **kw):
            out.append(Item(kind, s, b.x0 + span * a / n, b.x0 + span * z / n, b.conf, **kw))
        for m in iter_amounts(text):
            if m.start() > last and text[last:m.start()].strip(" :;,"):
                put("text", text[last:m.start()].strip(), last, m.start())
            try:
                paise, tag = _amount(m.group(0))
                put("amount", m.group(0), m.start(), m.end(), paise=paise, tag=tag)
            except ValueError:
                put("text", m.group(0), m.start(), m.end())
            last = m.end()
        if last < len(text) and text[last:].strip(" :;,"):
            put("text", text[last:].strip(), last, len(text))
    return out


COLUMN_WORDS = (("debit", ("debit", "withdraw", "paid out", "dr.")), ("credit", ("credit", "deposit", "paid in", "cr.")),
                ("balance", ("balance",)), ("ref", ("chq", "cheque", "ref")),
                ("narration", ("narration", "description", "particular", "details", "remark")))


def find_columns(line: list):
    """If this row is a table header, return {'debit':(x0,x1), 'credit':..., 'balance':...}; else None."""
    cols = {}
    for b in line:
        t = b.text.lower()
        for name, words in COLUMN_WORDS:
            if name not in cols and any(w in t for w in words):
                cols[name] = (b.x0, b.x1)
                break
        else:
            if "date" in t and "value" not in t:
                cols.setdefault("date", (b.x0, b.x1))
    return cols if {"debit", "credit", "balance"} <= set(cols) else None


def _column_of(item: Item, cols: dict):
    """Which of debit/credit/balance an amount sits under (by horizontal overlap, else nearest centre)."""
    best, dist = None, None
    for name in ("debit", "credit", "balance"):
        x0, x1 = cols[name]
        d = 0.0 if (item.x1 >= x0 and item.x0 <= x1) else min(abs(item.cx - x0), abs(item.cx - x1))
        if dist is None or d < dist:
            best, dist = name, d
    return best, dist


def _direction_hint(narration: str, tag: str):
    if tag:
        return tag
    n = narration.upper()
    if re.search(r"\b(CR|DEPOSIT|CREDIT|REFUND|INTEREST|SALARY)\b|/CR/|NEFT CR|IMPS CR", n):
        return "Cr"
    if re.search(r"\b(DR|WDL|WITHDRAWAL|DEBIT|CHARGES?|FEE|PURCHASE|ATM|POS|EMI)\b|/DR/", n):
        return "Dr"
    return None


def _clean_narration(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" :;,.-|")


def parse_ocr_pages(pages: list, info: dict = None):
    """OCR pages -> [Txn] (all unverified). If `info` is a dict it receives 'opening': the statement's printed
    opening balance in paise (or None). Imported lazily to avoid a circular import with bank_import."""
    from .bank_import import Txn
    txns, cols, prev_bal, opening = [], None, None, None
    last_date = None
    for page_no, page in enumerate(pages, 1):
        H = max(page.height, 1)
        for line in group_lines(page.boxes):
            found = find_columns(line)
            if found:
                cols = found
                continue
            items = _items(line)
            text = " ".join(i.text for i in items)
            y0, y1 = min(b.y0 for b in line) / H, max(b.y1 for b in line) / H
            om = re.search(r"opening\s*balance\W{0,4}(.*)$", text, re.I)
            if om and opening is None:
                am = first_amount(re.sub(r"(?<=\d);(?=\d)", ",", om.group(1)))
                if am:
                    p, tag = _amount(am.group(0))
                    opening = prev_bal = -p if tag == "Dr" else p
                continue
            first = next((i for i in items if i.kind == "text"), None)
            amounts = [i for i in items if i.kind == "amount"]
            repaired, changed = repair_date_prefix(first.text) if first else ("", False)
            has_date = bool(first and (DATE_PREFIX_RE.match(repaired) or LOOSE_DATE_RE.match(first.text)))
            if not (has_date and amounts and items[0] is first):
                # not a transaction row: maybe the wrapped second line of a narration
                if txns and not amounts and first and not NOISE_RE.search(text) and 0 < len(text) < 80 \
                        and txns[-1].page == page_no:
                    txns[-1].narration = _clean_narration(txns[-1].narration + " " + text)
                    txns[-1].y1 = y1
                continue

            note, uncertain = [], False
            # ---- date
            m = DATE_PREFIX_RE.match(repaired)
            iso = None
            if m:
                try:
                    iso = parse_date(m.group(1))
                except ValueError:
                    iso = None
            if changed:
                note.append("date text repaired")
            if iso is None:
                iso = last_date or "2000-01-01"
                note.append("date unreadable: check it")
                uncertain = True
            last_date = iso
            rest = repaired[m.end():] if m else re.sub(r"^\S+", "", first.text, count=1)
            rest = re.sub(DATE_PREFIX_RE, "", rest.strip(), count=1) if DATE_PREFIX_RE.match(rest.strip()) else rest   # value date
            texts = [rest] + [i.text for i in items[items.index(first) + 1:] if i.kind == "text"
                              and not DATE_PREFIX_RE.fullmatch(repair_date_prefix(i.text)[0].strip())]
            narration = _clean_narration(" ".join(texts))
            conf = min([first.conf] + [a.conf for a in amounts])

            # ---- amounts: by column position when the header was seen, otherwise by order
            debit = credit = 0
            balance = None
            if cols:
                for a in amounts:
                    name, dist = _column_of(a, cols)
                    if name == "balance":
                        balance = -a.paise if a.tag == "Dr" else a.paise
                    elif name == "debit":
                        debit = debit or a.paise
                    else:
                        credit = credit or a.paise
                if debit and credit:
                    uncertain = True
                    note.append("both debit and credit read: check")
                if not (debit or credit):
                    continue                                    # only a balance on this line: not a transaction
            else:
                if len(amounts) >= 2:
                    amt = amounts[-2].paise
                    balance = -amounts[-1].paise if amounts[-1].tag == "Dr" else amounts[-1].paise
                    tag = amounts[-2].tag
                else:
                    amt, tag = amounts[0].paise, amounts[0].tag
                is_credit = None
                if balance is not None and prev_bal is not None and abs(balance - prev_bal) == amt:
                    is_credit = balance > prev_bal
                else:
                    hint = _direction_hint(narration, tag)
                    is_credit = hint == "Cr"
                    if hint is None or (balance is not None and prev_bal is not None):
                        uncertain = True
                        note.append("debit/credit direction guessed")
                credit, debit = (amt, 0) if is_credit else (0, amt)

            # ---- running-balance check
            if balance is not None and prev_bal is not None and prev_bal + credit - debit != balance:
                uncertain = True
                note.append("balance does not follow from the previous row")
            if balance is not None:
                prev_bal = balance
            if conf < LOW_CONFIDENCE:
                note.append("low OCR confidence")
            txns.append(Txn(iso, narration, debit, credit, balance, "", uncertain, page=page_no, y0=y0, y1=y1,
                            confidence=round(conf, 3), source="ocr", note="; ".join(dict.fromkeys(note))))
    if info is not None:
        info["opening"] = opening
    return txns
