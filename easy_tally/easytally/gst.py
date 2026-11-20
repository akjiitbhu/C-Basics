"""GST calculation helpers (India)."""
from decimal import Decimal, ROUND_HALF_UP

# Current slabs. The 12% and 28% slabs were withdrawn from 22 Sep 2025 (items and old invoices that still carry
# them keep working, but new choices are limited to these).
GST_RATES = [0, 5, 18]
RATE_ERROR = "GST rate must be 0, 5 or 18"

STATES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan",
    "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand",
    "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman & Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh",
}


def _round_div(numerator: int, denominator: int) -> int:
    return int((Decimal(numerator) / Decimal(denominator)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def compute_gst(taxable: int, rate: int, intra_state: bool):
    """Return (cgst, sgst, igst) in paise for a taxable value in paise.

    Intra-state supply -> CGST + SGST (each half the rate); inter-state -> IGST.
    """
    if intra_state:
        half = _round_div(taxable * rate, 200)
        return half, half, 0
    return 0, 0, _round_div(taxable * rate, 100)


def is_intra_state(company_state: str, party_state: str) -> bool:
    """States are 2-digit codes. Unknown party state is treated as intra-state."""
    if not party_state or not company_state:
        return True
    return company_state == party_state
