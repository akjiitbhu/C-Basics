"""Printable GST invoice / credit note / debit note as a PDF (needs reportlab)."""
import io
from xml.sax.saxutils import escape

from .accounting import Books, display_date
from .gst import STATES
from .money import fmt

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven",
         "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + ((" " + _ONES[n % 10]) if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def amount_in_words(paise: int) -> str:
    """12345678 paise -> 'Rupees One Lakh Twenty Three Thousand Four Hundred Fifty Six and Seventy Eight Paise Only'."""
    rupees, p = divmod(abs(int(paise)), 100)
    if rupees == 0 and p == 0:
        return "Rupees Zero Only"
    parts = []
    for value, name in ((10 ** 7, "Crore"), (10 ** 5, "Lakh"), (1000, "Thousand")):
        if rupees >= value:
            parts.append(_below_1000(rupees // value) + " " + name)
            rupees %= value
    if rupees:
        parts.append(_below_1000(rupees))
    text = "Rupees " + " ".join(parts) if parts else "Rupees Zero"
    if p:
        text += f" and {_below_1000(p)} Paise"
    return text + " Only"


TITLES = {"Sales": "TAX INVOICE", "Credit Note": "CREDIT NOTE", "Debit Note": "DEBIT NOTE",
          "Purchase": "PURCHASE VOUCHER"}
SUBTITLES = {"Sales": "GST Sale Invoice", "Credit Note": "GST Credit Note (sales return)",
             "Debit Note": "GST Debit Note (purchase return)", "Purchase": ""}
DOC_NAMES = {"Sales": "Tax Invoice (GST Sale)", "Credit Note": "Credit Note", "Debit Note": "Debit Note",
             "Purchase": "Purchase"}


def _state_text(code: str) -> str:
    return f"{STATES.get(code, '')} ({code})" if code else "-"


def _collect(b: Books, voucher_id: int):
    """Everything an invoice shows: (voucher, invoice, party ledger, company settings, item lines)."""
    v = b.conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
    inv = b.conn.execute("SELECT * FROM invoices WHERE voucher_id=?", (voucher_id,)).fetchone()
    if not v or not inv:
        raise ValueError("Invoice not found")
    party = b.get_ledger(inv["party_id"])
    lines = b.conn.execute(
        """SELECT l.*, i.name, i.hsn, i.unit FROM invoice_lines l JOIN stock_items i ON i.id=l.item_id
           WHERE l.voucher_id=? ORDER BY l.id""", (voucher_id,)).fetchall()
    return v, inv, party, b.settings(), lines


HTML_STYLE = """
@page { size: A4; margin: 12mm; }
* { box-sizing: border-box; }
body { font: 12px/1.45 Arial, Helvetica, sans-serif; color: #000; background: #fff; margin: 0; padding: 14px 18px; }
h1 { font-size: 22px; text-align: center; margin: 0 0 2px; letter-spacing: .5px; }
.sub { text-align: center; color: #555; font-size: 13px; margin-bottom: 10px; }
table { border-collapse: collapse; width: 100%; }
.box { border: 1px solid #777; padding: 7px 9px; vertical-align: top; }
.items th, .items td { border: 1px solid #999; padding: 4px 6px; vertical-align: top; }
.items th { background: #eee; text-align: left; font-size: 11px; }
.r { text-align: right; font-variant-numeric: tabular-nums; }
.totals { width: 300px; margin: 8px 0 0 auto; }
.totals td { border: 1px solid #777; padding: 3px 8px; }
.totals tr.grand td { font-weight: bold; font-size: 13px; border-top: 2px solid #000; }
.words { margin: 8px 0; font-weight: bold; }
.small { font-size: 11px; color: #222; }
.sign { text-align: right; margin-top: 36px; }
@media print { body { padding: 0; } }
"""


def invoice_html(b: Books, voucher_id: int) -> str:
    """The invoice as a self-contained HTML page for the on-screen preview; prints exactly as shown."""
    from html import escape as e
    v, inv, party, co, lines = _collect(b, voucher_id)
    kind = inv["kind"]
    nl = lambda t: e(str(t or "")).replace("\n", "<br>")
    seller = [f"<b>{e(co['name'])}</b>"]
    for key, label in (("address", ""), ("phone", "Phone: "), ("email", "Email: ")):
        if co[key]:
            seller.append(label + nl(co[key]))
    seller += ["GSTIN: " + e(co["gstin"] or "-"), "State: " + e(_state_text(co["state"]))]
    meta = [f"<b>Invoice no.: {e(v['ref'] or str(v['number']))}</b>", "Date: " + display_date(v["date"]),
            "Document: " + e(DOC_NAMES.get(kind, kind)) + f" #{v['number']}",
            "Place of supply: " + e(_state_text(party["state"] or co["state"]))]
    bill = [f"<b>{'Supplier' if kind == 'Purchase' else 'Bill to'}</b>", f"<b>{e(party['name'])}</b>",
            "GSTIN: " + e(party["gstin"] or "Unregistered"), "State: " + e(_state_text(party["state"]))]
    rows = []
    for n, l in enumerate(lines, 1):
        tax = l["cgst"] + l["sgst"] + l["igst"]
        rows.append(f"<tr><td>{n}</td><td>{e(l['name'])}</td><td>{e(l['hsn'])}</td><td>{l['qty']:g} {e(l['unit'])}</td>"
                    f"<td class='r'>{fmt(l['rate'])}</td><td class='r'>{fmt(l['taxable'])}</td>"
                    f"<td class='r'>{l['gst_rate']}%</td><td class='r'>{fmt(tax)}</td><td class='r'>{fmt(l['taxable'] + tax)}</td></tr>")
    tot = [("Taxable value", fmt(inv["taxable"]))]
    if inv["cgst"] or inv["sgst"]:
        tot += [("CGST", fmt(inv["cgst"])), ("SGST", fmt(inv["sgst"]))]
    if inv["igst"]:
        tot.append(("IGST", fmt(inv["igst"])))
    totals = "".join(f"<tr><td>{a}</td><td class='r'>{c}</td></tr>" for a, c in tot)
    totals += f"<tr class='grand'><td>TOTAL (Rs.)</td><td class='r'>{fmt(inv['total'])}</td></tr>"
    extra = ""
    for head, text in (("Bank details", co["bank_details"]), ("Terms &amp; conditions", co["terms"]), ("Notes", v["narration"])):
        if text:
            extra += f"<p class='small'><b>{head}</b><br>{nl(text)}</p>"
    sub = f"<div class='sub'>{e(SUBTITLES[kind])}</div>" if SUBTITLES.get(kind) else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(DOC_NAMES.get(kind, kind))} {e(v['ref'] or str(v['number']))}</title>
<style>{HTML_STYLE}</style></head><body>
<h1>{e(TITLES.get(kind, kind.upper()))}</h1>{sub}
<table><tr><td class="box" style="width:58%">{"<br>".join(seller)}</td><td class="box">{"<br>".join(meta)}</td></tr></table>
<table style="margin-top:-1px"><tr><td class="box">{"<br>".join(bill)}</td></tr></table>
<table class="items" style="margin-top:12px"><thead><tr><th>#</th><th>Description</th><th>HSN/SAC</th><th>Qty</th><th class="r">Rate</th>
<th class="r">Taxable</th><th class="r">GST %</th><th class="r">GST Amt</th><th class="r">Amount</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<table class="totals">{totals}</table>
<div class="words">Amount in words: {e(amount_in_words(inv["total"]))}</div>{extra}
<div class="sign">For {e(co['name'])}<br><br><br>Authorised signatory</div>
<p class="small">This is a computer generated document.</p></body></html>"""


def invoice_pdf(b: Books, voucher_id: int) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    v, inv, party, co, lines = _collect(b, voucher_id)

    ss = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8.5, leading=11)
    bold = ParagraphStyle("bold", parent=small, fontName="Helvetica-Bold")
    big = ParagraphStyle("big", parent=ss["Title"], fontSize=15, spaceAfter=4)
    right = ParagraphStyle("right", parent=small, alignment=2)
    P = lambda t, st=small: Paragraph(escape(str(t)).replace("\n", "<br/>"), st)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"{inv['kind']} {v['ref'] or v['number']}")
    story = [Paragraph(TITLES.get(inv["kind"], inv["kind"].upper()), big)]
    if SUBTITLES.get(inv["kind"]):
        story.append(Paragraph(SUBTITLES[inv["kind"]], ParagraphStyle("sub", parent=small, alignment=1, fontSize=10,
                                                                      textColor=colors.HexColor("#555555"), spaceAfter=6)))

    seller = [P(co["name"], bold)]
    for key, label in (("address", ""), ("phone", "Phone: "), ("email", "Email: ")):
        if co[key]:
            seller.append(P(label + co[key]))
    seller.append(P("GSTIN: " + (co["gstin"] or "-")))
    seller.append(P("State: " + _state_text(co["state"])))
    meta = [P("Invoice no.: " + (v["ref"] or f"{v['number']}"), bold), P("Date: " + display_date(v["date"])),
            P("Document: " + DOC_NAMES.get(inv["kind"], inv["kind"]) + f" #{v['number']}"),
            P("Place of supply: " + _state_text(party["state"] or co["state"]))]
    top = Table([[seller, meta]], colWidths=[105 * mm, 77 * mm])
    top.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("LINEAFTER", (0, 0), (0, 0), 0.6, colors.black)]))
    story += [top]
    bill = [P("Bill to" if inv["kind"] != "Purchase" else "Supplier", bold), P(party["name"], bold),
            P("GSTIN: " + (party["gstin"] or "Unregistered")), P("State: " + _state_text(party["state"]))]
    t = Table([[bill]], colWidths=[182 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.black)]))
    story += [t, Spacer(1, 4 * mm)]

    head = ["#", "Description", "HSN/SAC", "Qty", "Rate", "Taxable", "GST %", "GST Amt", "Amount"]
    rows = [[P(h, bold) for h in head]]
    for n, l in enumerate(lines, 1):
        tax = l["cgst"] + l["sgst"] + l["igst"]
        rows.append([P(n), P(l["name"]), P(l["hsn"]), P(f"{l['qty']:g} {l['unit']}"), P(fmt(l["rate"]), right),
                     P(fmt(l["taxable"]), right), P(f"{l['gst_rate']}%", right), P(fmt(tax), right),
                     P(fmt(l["taxable"] + tax), right)])
    grid = Table(rows, colWidths=[8 * mm, 56 * mm, 20 * mm, 18 * mm, 20 * mm, 22 * mm, 12 * mm, 12 * mm + 4 * mm, 16 * mm],
                 repeatRows=1)
    grid.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                              ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [grid, Spacer(1, 3 * mm)]

    tot = [["Taxable value", fmt(inv["taxable"])]]
    if inv["cgst"] or inv["sgst"]:
        tot += [["CGST", fmt(inv["cgst"])], ["SGST", fmt(inv["sgst"])]]
    if inv["igst"]:
        tot.append(["IGST", fmt(inv["igst"])])
    tot.append(["TOTAL (Rs.)", fmt(inv["total"])])
    tt = Table([[P(a, bold if a.startswith("TOTAL") else small), P(c, bold if a.startswith("TOTAL") else right)]
                for a, c in tot], colWidths=[40 * mm, 32 * mm], hAlign="RIGHT")
    tt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.black),
                            ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black)]))
    story += [tt, Spacer(1, 3 * mm), P("Amount in words: " + amount_in_words(inv["total"]), bold)]

    foot = []
    if co["bank_details"]:
        foot += [P("Bank details", bold), P(co["bank_details"])]
    if co["terms"]:
        foot += [P("Terms & conditions", bold), P(co["terms"])]
    if v["narration"]:
        foot += [P("Notes", bold), P(v["narration"])]
    if foot:
        story += [Spacer(1, 4 * mm)] + foot
    story += [Spacer(1, 14 * mm), P("For " + co["name"], right), Spacer(1, 10 * mm), P("Authorised signatory", right),
              Spacer(1, 3 * mm), P("This is a computer generated document.")]
    doc.build(story)
    return buf.getvalue()
