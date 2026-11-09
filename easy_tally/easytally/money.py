"""Money helpers. All amounts are stored as integer paise to avoid float errors."""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


def to_paise(value) -> int:
    """Convert rupees (str/int/float/Decimal) to integer paise, rounding half up."""
    try:
        d = Decimal(str(value).replace(",", "").strip() or "0")
    except InvalidOperation:
        raise ValueError(f"Invalid amount: {value!r}")
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_rupees(paise: int) -> Decimal:
    return Decimal(paise) / 100


def fmt(paise: int) -> str:
    """Format paise with Indian digit grouping, e.g. 12,34,567.80"""
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{sign}{s}.{p:02d}"


def fmt_dr_cr(paise: int) -> str:
    """Signed paise (Dr positive) -> '1,000.00 Dr' / '1,000.00 Cr' / ''."""
    if paise == 0:
        return ""
    return f"{fmt(abs(paise))} {'Dr' if paise > 0 else 'Cr'}"
