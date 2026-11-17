"""GSTR-1 and GSTR-3B preparation from the books.

Produces (a) JSON laid out like the GST offline-tool / portal upload files, and (b) an Excel workbook
you can read and check line by line. Always review the warnings, and validate the JSON in the GST
offline tool or portal before filing: the portal's schema changes from time to time.
"""
import io
import re
from calendar import monthrange
from collections import defaultdict

from .accounting import GSTIN_RE, Books, display_date
from .gst import STATES

B2CL_LIMIT = 250000 * 100            # inter-state unregistered invoices above Rs 2.5 lakh
DOC_NUM_RE = re.compile(r"^[A-Za-z0-9/-]{1,16}$")
UQC = {"nos": "NOS", "pcs": "PCS", "piece": "PCS", "pieces": "PCS", "kg": "KGS", "kgs": "KGS", "gm": "GMS",
       "gms": "GMS", "ltr": "LTR", "l": "LTR", "litre": "LTR", "mtr": "MTR", "m": "MTR", "box": "BOX",
       "set": "SET", "pair": "PRS", "dozen": "DOZ", "bag": "BAG", "ton": "TON", "tonne": "TON", "sqm": "SQM",
       "sqft": "SQF", "bottle": "BTL", "pack": "PAC", "unit": "UNT", "roll": "ROL", "can": "CAN", "carton": "CTN"}


def _r(paise) -> float:
    return round(paise / 100 + 0.0, 2)


def period(year: int, month: int):
    if not (1 <= month <= 12):
        raise ValueError("Month must be 1-12")
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"


def collect(b: Books, start: str, end: str):
    """All invoice-type documents in the period with their tax lines. Returns (docs, company, warnings)."""
    co = b.settings()
    warnings = []
    if not co["gstin"]:
        warnings.append("Company GSTIN is not set (Admin > Company details). The file will have an empty GSTIN.")
    elif not GSTIN_RE.match(co["gstin"]):
        warnings.append("Company GSTIN is not valid.")
    if not co["state"]:
        warnings.append("Company state is not set; place of supply cannot be worked out reliably.")
    docs = []
    rows = b.conn.execute(
        """SELECT v.id, v.date, v.number, v.ref, i.kind, i.party_id, i.intra_state, i.total
           FROM invoices i JOIN vouchers v ON v.id=i.voucher_id
           WHERE v.date BETWEEN ? AND ? ORDER BY v.date, v.id""", (start, end)).fetchall()
    seen = set()
    for r in rows:
        party = b.get_ledger(r["party_id"])
        gstin = party["gstin"] if GSTIN_RE.match(party["gstin"] or "") else ""
        if party["gstin"] and not gstin:
            warnings.append(f"Party '{party['name']}' has an invalid GSTIN; treated as unregistered.")
        prefix = {"Sales": "INV", "Credit Note": "CN", "Debit Note": "DN", "Purchase": "PUR"}[r["kind"]]
        label = r["ref"] or f"{prefix}{r['number']}"
        if r["kind"] in ("Sales", "Credit Note"):
            if not DOC_NUM_RE.match(label):
                warnings.append(f"{r['kind']} number '{label}' must be 1-16 characters (letters, digits, / or -).")
            if (r["kind"], label) in seen:
                warnings.append(f"Duplicate {r['kind']} number '{label}'.")
            seen.add((r["kind"], label))
        lines = b.conn.execute(
            """SELECT l.*, s.name, s.hsn, s.unit FROM invoice_lines l JOIN stock_items s ON s.id=l.item_id
               WHERE l.voucher_id=?""", (r["id"],)).fetchall()
        docs.append({"vid": r["id"], "kind": r["kind"], "number": r["number"], "label": label, "date": r["date"],
                     "party": party["name"], "gstin": gstin, "pos": party["state"] or gstin[:2] or co["state"],
                     "intra": bool(r["intra_state"]), "total": r["total"], "lines": [dict(l) for l in lines]})
    if any(not l["hsn"] for d in docs if d["kind"] in ("Sales", "Credit Note") for l in d["lines"]):
        names = sorted({l["name"] for d in docs if d["kind"] in ("Sales", "Credit Note") for l in d["lines"] if not l["hsn"]})
        warnings.append("These items have no HSN/SAC code: " + ", ".join(names))
    return docs, co, warnings


def _items(lines):
    """Group a document's lines by GST rate into the portal's itms[] structure."""
    by_rate = defaultdict(lambda: [0, 0, 0, 0])
    for l in lines:
        t = by_rate[l["gst_rate"]]
        t[0] += l["taxable"]; t[1] += l["igst"]; t[2] += l["cgst"]; t[3] += l["sgst"]
    return [{"num": rate * 100 + 1, "itm_det": {"txval": _r(t[0]), "rt": rate, "iamt": _r(t[1]), "camt": _r(t[2]),
                                                 "samt": _r(t[3]), "csamt": 0}}
            for rate, t in sorted(by_rate.items())]


def gstr1(b: Books, year: int, month: int):
    start, end = period(year, month)
    docs, co, warnings = collect(b, start, end)
    pos_home = co["state"]
    b2b, cdnr, b2cl, cdnur = defaultdict(list), defaultdict(list), defaultdict(list), []
    b2cs = defaultdict(lambda: [0, 0, 0, 0])
    hsn = defaultdict(lambda: {"qty": 0.0, "val": 0, "tx": 0, "i": 0, "c": 0, "s": 0, "unit": "", "desc": ""})
    counts = defaultdict(list)
    for d in docs:
        if d["kind"] not in ("Sales", "Credit Note"):
            continue
        credit = d["kind"] == "Credit Note"
        counts[d["kind"]].append(d)
        sign = -1 if credit else 1
        inter = d["pos"] != pos_home
        for l in d["lines"]:                                  # table 12 (HSN summary), net of credit notes
            h = hsn[l["hsn"] or ""]
            h["qty"] += sign * l["qty"]; h["val"] += sign * (l["taxable"] + l["cgst"] + l["sgst"] + l["igst"])
            h["tx"] += sign * l["taxable"]; h["i"] += sign * l["igst"]; h["c"] += sign * l["cgst"]
            h["s"] += sign * l["sgst"]; h["unit"] = l["unit"]; h["desc"] = l["name"]
        base = {"val": _r(d["total"]), "pos": d["pos"], "itms": _items(d["lines"])}
        if d["gstin"]:
            if credit:
                cdnr[d["gstin"]].append({"ntty": "C", "nt_num": d["label"], "nt_dt": display_date(d["date"]),
                                         "rchrg": "N", "inv_typ": "R", **base})
            else:
                b2b[d["gstin"]].append({"inum": d["label"], "idt": display_date(d["date"]), "rchrg": "N",
                                        "inv_typ": "R", **base})
        elif inter and d["total"] > B2CL_LIMIT:
            if credit:
                cdnur.append({"typ": "B2CL", "ntty": "C", "nt_num": d["label"], "nt_dt": display_date(d["date"]), **base})
            else:
                b2cl[d["pos"]].append({"inum": d["label"], "idt": display_date(d["date"]), "val": base["val"],
                                       "itms": base["itms"]})
        else:                                                  # unregistered, small: aggregated by place and rate
            for l in d["lines"]:
                t = b2cs[(d["pos"], l["gst_rate"], "INTER" if inter else "INTRA")]
                t[0] += sign * l["taxable"]; t[1] += sign * l["igst"]; t[2] += sign * l["cgst"]; t[3] += sign * l["sgst"]
    data = {
        "gstin": co["gstin"], "fp": f"{month:02d}{year}", "version": "GST3.0.4", "hash": "hash",
        "b2b": [{"ctin": c, "inv": inv} for c, inv in sorted(b2b.items())],
        "b2cl": [{"pos": p, "inv": inv} for p, inv in sorted(b2cl.items())],
        "b2cs": [{"sply_ty": k[2], "rt": k[1], "typ": "OE", "pos": k[0], "txval": _r(v[0]), "iamt": _r(v[1]),
                  "camt": _r(v[2]), "samt": _r(v[3]), "csamt": 0} for k, v in sorted(b2cs.items())],
        "cdnr": [{"ctin": c, "nt": nt} for c, nt in sorted(cdnr.items())],
        "cdnur": cdnur,
        "hsn": {"data": [{"num": n, "hsn_sc": code, "desc": h["desc"][:30],
                          "uqc": UQC.get(h["unit"].strip().lower(), "OTH"), "qty": round(h["qty"], 3),
                          "val": _r(h["val"]), "txval": _r(h["tx"]), "iamt": _r(h["i"]), "camt": _r(h["c"]),
                          "samt": _r(h["s"]), "csamt": 0}
                         for n, (code, h) in enumerate(sorted(hsn.items()), 1)]},
        "doc_issue": {"doc_det": [
            {"doc_num": num, "doc_typ": name, "docs": [{
                "num": 1, "from": counts[kind][0]["label"], "to": counts[kind][-1]["label"],
                "totnum": len(counts[kind]), "cancel": 0, "net_issue": len(counts[kind])}]}
            for num, name, kind in ((1, "Invoices for outward supply", "Sales"), (5, "Credit Note", "Credit Note"))
            if counts[kind]]},
    }
    warnings.append("Aggregate turnover fields (gt, cur_gt) are not included; enter them in the GST offline tool.")
    return data, warnings


def gstr3b(b: Books, year: int, month: int):
    start, end = period(year, month)
    docs, co, warnings = collect(b, start, end)
    out = {"txval": 0, "iamt": 0, "camt": 0, "samt": 0}
    nil = 0
    unreg_inter = defaultdict(lambda: [0, 0])
    itc = {"iamt": 0, "camt": 0, "samt": 0}
    for d in docs:
        sign = -1 if d["kind"] in ("Credit Note", "Debit Note") else 1
        if d["kind"] in ("Sales", "Credit Note"):
            for l in d["lines"]:
                if l["gst_rate"] == 0:
                    nil += sign * l["taxable"]
                    continue
                out["txval"] += sign * l["taxable"]; out["iamt"] += sign * l["igst"]
                out["camt"] += sign * l["cgst"]; out["samt"] += sign * l["sgst"]
                if not d["gstin"] and d["pos"] != co["state"]:
                    u = unreg_inter[d["pos"]]
                    u[0] += sign * l["taxable"]; u[1] += sign * l["igst"]
        else:
            for l in d["lines"]:
                itc["iamt"] += sign * l["igst"]; itc["camt"] += sign * l["cgst"]; itc["samt"] += sign * l["sgst"]
    zero = {"txval": 0, "iamt": 0, "camt": 0, "samt": 0, "csamt": 0}
    data = {
        "gstin": co["gstin"], "ret_period": f"{month:02d}{year}",
        "sup_details": {
            "osup_det": {**{k: _r(v) for k, v in out.items()}, "csamt": 0},
            "osup_zero": dict(zero), "osup_nil_exmp": {"txval": _r(nil)},
            "isup_rev": dict(zero), "osup_nongst": {"txval": 0}},
        "inter_sup": {"unreg_details": [{"pos": p, "txval": _r(v[0]), "iamt": _r(v[1])}
                                         for p, v in sorted(unreg_inter.items())],
                      "comp_details": [], "uin_details": []},
        "itc_elg": {
            "itc_avl": [{"ty": "OTH", **{k: _r(v) for k, v in itc.items()}, "csamt": 0}],
            "itc_rev": [], "itc_net": {**{k: _r(v) for k, v in itc.items()}, "csamt": 0}, "itc_inelg": []},
    }
    payable = {k: _r(out[k] - itc[k]) for k in ("iamt", "camt", "samt")}
    warnings.append("ITC is taken as all purchases (net of debit notes) under 'All other ITC'. Reverse any "
                    "blocked/ineligible ITC, and set off IGST/CGST/SGST credits as per the GST rules, before filing.")
    return data, payable, warnings


# ---------------------------------------------------------------- Excel working papers
def _sheet(wb, title, header, rows):
    ws = wb.create_sheet(title)
    ws.append(header)
    from openpyxl.styles import Font
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append(r)
    for i, h in enumerate(header, 1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows if i - 1 < len(r)])
        ws.column_dimensions[ws.cell(1, i).column_letter].width = min(max(width + 2, 10), 40)
    return ws


def _tax_rows(inv_list, key_cols):
    rows = []
    for key, inv in inv_list:
        for d in inv:
            for it in d["itms"]:
                x = it["itm_det"]
                rows.append(list(key) + [d.get("inum") or d.get("nt_num"), d.get("idt") or d.get("nt_dt"),
                                         d["val"], d["pos"], x["rt"], x["txval"], x["iamt"], x["camt"], x["samt"]])
    return rows


def gstr1_xlsx(data: dict, warnings: list) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    cols = ["Document no.", "Date", "Doc value", "Place of supply", "Rate %", "Taxable", "IGST", "CGST", "SGST"]
    _sheet(wb, "B2B", ["GSTIN"] + cols, _tax_rows([((x["ctin"],), x["inv"]) for x in data["b2b"]], 1))
    _sheet(wb, "B2CL", ["Place"] + cols, _tax_rows([((x["pos"],), [dict(i, pos=x["pos"]) for i in x["inv"]])
                                                    for x in data["b2cl"]], 1))
    _sheet(wb, "B2CS", ["Supply", "Place of supply", "Rate %", "Taxable", "IGST", "CGST", "SGST"],
           [[x["sply_ty"], x["pos"], x["rt"], x["txval"], x["iamt"], x["camt"], x["samt"]] for x in data["b2cs"]])
    _sheet(wb, "Credit notes (reg.)", ["GSTIN"] + cols, _tax_rows([((x["ctin"],), x["nt"]) for x in data["cdnr"]], 1))
    _sheet(wb, "HSN", ["HSN/SAC", "Description", "UQC", "Qty", "Total value", "Taxable", "IGST", "CGST", "SGST"],
           [[x["hsn_sc"], x["desc"], x["uqc"], x["qty"], x["val"], x["txval"], x["iamt"], x["camt"], x["samt"]]
            for x in data["hsn"]["data"]])
    _sheet(wb, "Documents issued", ["Type", "From", "To", "Total", "Cancelled", "Net issued"],
           [[d["doc_typ"], x["from"], x["to"], x["totnum"], x["cancel"], x["net_issue"]]
            for d in data["doc_issue"]["doc_det"] for x in d["docs"]])
    _sheet(wb, "Warnings", ["Check before filing"], [[w] for w in warnings] or [["None"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def gstr3b_xlsx(data: dict, payable: dict, warnings: list) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    s, t = data["sup_details"], data["itc_elg"]["itc_net"]
    rows = [["3.1(a) Outward taxable supplies", s["osup_det"]["txval"], s["osup_det"]["iamt"],
             s["osup_det"]["camt"], s["osup_det"]["samt"]],
            ["3.1(c) Nil rated / exempt", s["osup_nil_exmp"]["txval"], "", "", ""],
            ["4(A) ITC available (net of debit notes)", "", t["iamt"], t["camt"], t["samt"]],
            ["Tax payable before ITC set-off rules (output - ITC)", "", payable["iamt"], payable["camt"], payable["samt"]]]
    rows += [[f"3.2 Inter-state to unregistered - {u['pos']}", u["txval"], u["iamt"], "", ""]
             for u in data["inter_sup"]["unreg_details"]]
    _sheet(wb, "GSTR-3B", ["Table", "Taxable value", "IGST", "CGST", "SGST"], rows)
    _sheet(wb, "Warnings", ["Check before filing"], [[w] for w in warnings] or [["None"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
