"""Reports. Every report returns a Report(title, columns, rows) so the UI and CSV export share it."""
import csv
from dataclasses import dataclass, field

from .accounting import Books, display_date
from .money import fmt, fmt_dr_cr


@dataclass
class Report:
    title: str
    columns: list
    rows: list = field(default_factory=list)


def _dr(p):  # debit column text
    return fmt(p) if p > 0 else ""


def _cr(p):
    return fmt(-p) if p < 0 else ""


def day_book(b: Books, start, end) -> Report:
    r = Report(f"Day Book  {display_date(start)} to {display_date(end)}",
               ["Date", "Type", "No.", "Particulars", "Debit", "Credit", "Narration"])
    for v in b.vouchers(start, end):
        for e in b.voucher_entries(v["id"]):
            r.rows.append([display_date(v["date"]), v["vtype"], v["number"], e["name"],
                           _dr(e["amount"]), _cr(e["amount"]), v["narration"]])
    return r


def ledger_statement(b: Books, ledger_id: int, start, end) -> Report:
    led = b.get_ledger(ledger_id)
    r = Report(f"Ledger: {led['name']}  {display_date(start)} to {display_date(end)}",
               ["Date", "Type", "No.", "Particulars", "Debit", "Credit", "Balance"])
    running = _opening_on(b, ledger_id, start)
    r.rows.append(["", "", "", "Opening Balance", "", "", fmt_dr_cr(running)])
    rows = b.conn.execute(
        """SELECT v.id, v.date, v.vtype, v.number, e.amount FROM entries e
           JOIN vouchers v ON v.id = e.voucher_id
           WHERE e.ledger_id=? AND v.date BETWEEN ? AND ? ORDER BY v.date, v.id, e.id""",
        (ledger_id, start, end)).fetchall()
    for row in rows:
        others = ", ".join(x["name"] for x in b.voucher_entries(row["id"]) if x["name"] != led["name"])
        running += row["amount"]
        r.rows.append([display_date(row["date"]), row["vtype"], row["number"], others or led["name"],
                       _dr(row["amount"]), _cr(row["amount"]), fmt_dr_cr(running)])
    r.rows.append(["", "", "", "Closing Balance", "", "", fmt_dr_cr(running)])
    return r


def _opening_on(b: Books, ledger_id: int, start: str) -> int:
    """Balance of a ledger just before `start` (ledger opening + earlier vouchers)."""
    led = b.get_ledger(ledger_id)
    row = b.conn.execute(
        "SELECT COALESCE(SUM(e.amount),0) AS s FROM entries e JOIN vouchers v ON v.id=e.voucher_id "
        "WHERE e.ledger_id=? AND v.date < ?", (ledger_id, start)).fetchone()
    return led["opening"] + row["s"]


def trial_balance(b: Books, as_on) -> Report:
    bal = b.balances(upto=as_on)
    r = Report(f"Trial Balance as on {display_date(as_on)}", ["Ledger", "Group", "Debit", "Credit"])
    td = tc = 0
    for led in b.ledgers():
        p = bal.get(led["id"], 0)
        if p == 0:
            continue
        r.rows.append([led["name"], led["group_name"], _dr(p), _cr(p)])
        td += max(p, 0)
        tc += max(-p, 0)
    opening_stock = stock_value(b, "1900-01-01")
    if opening_stock:
        r.rows.append(["Opening Stock", "Stock-in-Hand", fmt(opening_stock), ""])
        td += opening_stock
    r.rows.append(["TOTAL", "", fmt(td), fmt(tc)])
    if td != tc:
        diff = td - tc   # positive: debits exceed credits
        r.rows.append(["Difference (check opening balances)", "", "" if diff > 0 else fmt(-diff),
                       fmt(diff) if diff > 0 else ""])
    return r


def profit_and_loss(b: Books, start, end) -> Report:
    bal = b.balances(upto=end, start=start, with_opening=False)
    r = Report(f"Profit & Loss  {display_date(start)} to {display_date(end)}", ["Particulars", "Amount"])
    income = expense = 0
    from datetime import date, timedelta
    open_stock = stock_value(b, (date.fromisoformat(start) - timedelta(days=1)).isoformat())
    close_stock = stock_value(b, end)
    sections = (("Income", "Income"), ("Expense", "Expenses"))
    for nature, label in sections:
        r.rows.append([label.upper(), ""])
        total = 0
        if nature == "Income" and close_stock:
            total += close_stock
            r.rows.append(["   Closing Stock", fmt(close_stock)])
        if nature == "Expense" and open_stock:
            total += open_stock
            r.rows.append(["   Opening Stock", fmt(open_stock)])
        for led in b.ledgers():
            if led["nature"] != nature:
                continue
            p = bal.get(led["id"], 0)
            if p == 0:
                continue
            amount = -p if nature == "Income" else p
            total += amount
            r.rows.append([f"   {led['name']}", fmt(amount)])
        r.rows.append([f"Total {label}", fmt(total)])
        if nature == "Income":
            income = total
        else:
            expense = total
    net = income - expense
    r.rows.append(["NET PROFIT" if net >= 0 else "NET LOSS", fmt(abs(net))])
    return r


def balance_sheet(b: Books, as_on) -> Report:
    """Assets vs Liabilities. Net profit to date (incl. stock movement) is carried in.

    Opening stock is not a ledger, so enter capital/opening balances that include its value.
    """
    bal = b.balances(upto=as_on)
    r = Report(f"Balance Sheet as on {display_date(as_on)}", ["Particulars", "Amount"])
    nominal = sum(p for led in b.ledgers() if led["nature"] in ("Income", "Expense")
                  for p in [bal.get(led["id"], 0)])
    closing = stock_value(b, as_on)
    opening_all = stock_value(b, "1900-01-01")          # opening stock before any transaction
    profit = -nominal + closing - opening_all
    liab = 0
    r.rows.append(["LIABILITIES", ""])
    for led in b.ledgers():
        if led["nature"] == "Liability" and bal.get(led["id"], 0):
            amt = -bal[led["id"]]
            liab += amt
            r.rows.append([f"   {led['name']}", fmt(amt)])
    r.rows.append(["   Profit & Loss A/c (net, to date)", fmt(profit)])
    liab += profit
    r.rows.append(["Total Liabilities", fmt(liab)])
    assets = 0
    r.rows.append(["ASSETS", ""])
    if closing:
        assets += closing
        r.rows.append(["   Stock-in-Hand (closing)", fmt(closing)])
    for led in b.ledgers():
        if led["nature"] == "Asset" and bal.get(led["id"], 0):
            assets += bal[led["id"]]
            r.rows.append([f"   {led['name']}", fmt(bal[led["id"]])])
    r.rows.append(["Total Assets", fmt(assets)])
    if assets != liab:
        r.rows.append(["Difference (check opening balances)", fmt(assets - liab)])
    return r


KIND_LABEL = {"Sales": "Sales (output)", "Credit Note": "Credit Note (reduces output)",
              "Purchase": "Purchase (input)", "Debit Note": "Debit Note (reduces input)"}
KIND_ORDER = {"Sales": 0, "Credit Note": 1, "Purchase": 2, "Debit Note": 3}


def gst_summary(b: Books, start, end) -> Report:
    r = Report(f"GST Summary  {display_date(start)} to {display_date(end)}",
               ["Kind", "GST %", "Taxable", "CGST", "SGST", "IGST"])
    rows = b.conn.execute(
        """SELECT i.kind, l.gst_rate, SUM(l.taxable) t, SUM(l.cgst) c, SUM(l.sgst) s, SUM(l.igst) g
           FROM invoice_lines l JOIN invoices i ON i.voucher_id = l.voucher_id
           JOIN vouchers v ON v.id = i.voucher_id
           WHERE v.date BETWEEN ? AND ? GROUP BY i.kind, l.gst_rate""", (start, end)).fetchall()
    rows = sorted(rows, key=lambda x: (KIND_ORDER.get(x["kind"], 9), x["gst_rate"]))
    out = [0, 0, 0]      # net output tax: Sales - Credit Notes
    inp = [0, 0, 0]      # net input tax:  Purchase - Debit Notes
    for x in rows:
        r.rows.append([KIND_LABEL.get(x["kind"], x["kind"]), f"{x['gst_rate']}%", fmt(x["t"]),
                       fmt(x["c"]), fmt(x["s"]), fmt(x["g"])])
        sign = -1 if x["kind"] in ("Credit Note", "Debit Note") else 1
        bucket = out if x["kind"] in ("Sales", "Credit Note") else inp
        for i, k in enumerate(("c", "s", "g")):
            bucket[i] += sign * x[k]
    for i, name in enumerate(("CGST", "SGST", "IGST")):
        net = out[i] - inp[i]
        cells = ["", "", ""]
        cells[i] = fmt(abs(net))
        r.rows.append([f"Net {name} payable" if net >= 0 else f"Net {name} credit", "", ""] + cells)
    return r


def stock_positions(b: Books, as_on):
    """[(item_row, inward_qty, outward_qty, closing_qty, avg_cost_paise, value_paise)] as on a date.

    Valuation is weighted-average cost over opening stock plus purchases up to the date.
    """
    out = []
    for it in b.stock_items():
        q = b.conn.execute(
            """SELECT i.kind, SUM(l.qty) q, SUM(l.taxable) t FROM invoice_lines l
               JOIN invoices i ON i.voucher_id=l.voucher_id JOIN vouchers v ON v.id=i.voucher_id
               WHERE l.item_id=? AND v.date <= ? GROUP BY i.kind""", (it["id"], as_on)).fetchall()
        d = {x["kind"]: x for x in q}

        def g(kind, f):
            return d[kind][f] if kind in d else 0
        # Debit notes (purchase returns) reduce purchases; credit notes (sales returns) reduce sales.
        pq = g("Purchase", "q") - g("Debit Note", "q")
        pv = g("Purchase", "t") - g("Debit Note", "t")
        sq = g("Sales", "q") - g("Credit Note", "q")
        in_qty = it["opening_qty"] + pq
        avg = (it["opening_value"] + pv) / in_qty if in_qty else 0
        close = in_qty - sq
        out.append((it, in_qty, sq, close, avg, int(round(avg * close))))
    return out


def stock_value(b: Books, as_on) -> int:
    return sum(p[5] for p in stock_positions(b, as_on))


def stock_summary(b: Books, as_on) -> Report:
    r = Report(f"Stock Summary as on {display_date(as_on)}",
               ["Item", "Unit", "Inward", "Outward", "Closing Qty", "Avg Cost", "Value"])
    total = 0
    for it, in_qty, sq, close, avg, value in stock_positions(b, as_on):
        total += value
        r.rows.append([it["name"], it["unit"], f"{in_qty:g}", f"{sq:g}", f"{close:g}",
                       fmt(int(round(avg))), fmt(value)])
    r.rows.append(["TOTAL", "", "", "", "", "", fmt(total)])
    return r


BUCKETS = [("0-30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("Over 90", 91, 10 ** 6)]


def party_bills(b: Books, ledger_id: int, as_on: str):
    """Open bills for a debtor/creditor using FIFO settlement.

    Bills are entries on the party's natural side (Dr for debtors, Cr for creditors): invoices,
    debit/credit adjustments and the opening balance. Entries on the other side (receipts, payments,
    credit/debit notes) settle the oldest bills first. Returns (open_bills, advance_paise) where each
    bill is dict(date, label, amount, pending, age) and `advance` is unmatched settlement.
    """
    from datetime import date
    led = b.get_ledger(ledger_id)
    sign = 1 if led["group_name"] == "Sundry Debtors" else -1
    bills, settle = [], 0
    if led["opening"] * sign > 0:
        bills.append({"date": None, "label": "Opening balance", "amount": abs(led["opening"])})
    elif led["opening"] * sign < 0:
        settle += abs(led["opening"])
    rows = b.conn.execute(
        """SELECT v.date, v.vtype, v.number, v.ref, e.amount FROM entries e JOIN vouchers v ON v.id=e.voucher_id
           WHERE e.ledger_id=? AND v.date <= ? ORDER BY v.date, v.id, e.id""", (ledger_id, as_on)).fetchall()
    for x in rows:
        amt = x["amount"] * sign
        if amt > 0:
            bills.append({"date": x["date"], "amount": amt,
                          "label": x["ref"] or f"{x['vtype']} {x['number']}"})
        else:
            settle += -amt
    today = date.fromisoformat(as_on)
    for bill in bills:
        use = min(bill["amount"], settle)
        bill["pending"] = bill["amount"] - use
        settle -= use
        bill["age"] = (today - date.fromisoformat(bill["date"])).days if bill["date"] else None
    return [x for x in bills if x["pending"] > 0], settle


def ageing(b: Books, as_on, receivable=True) -> Report:
    group = "Sundry Debtors" if receivable else "Sundry Creditors"
    r = Report(f"{'Receivables' if receivable else 'Payables'} Ageing as on {display_date(as_on)}",
               ["Party", "Outstanding"] + [n for n, _, _ in BUCKETS] + ["Advance"])
    tot = [0] * (len(BUCKETS) + 2)
    for led in b.ledgers(group):
        open_bills, advance = party_bills(b, led["id"], as_on)
        cells = [0] * len(BUCKETS)
        for bill in open_bills:
            age = bill["age"] if bill["age"] is not None else 10 ** 6      # opening balance -> oldest
            for i, (_, lo, hi) in enumerate(BUCKETS):
                if lo <= age <= hi:
                    cells[i] += bill["pending"]
                    break
        pending = sum(cells)
        if not pending and not advance:
            continue
        r.rows.append([led["name"], fmt(pending)] + [fmt(c) if c else "" for c in cells]
                      + [fmt(advance) if advance else ""])
        for i, v in enumerate([pending] + cells + [advance]):
            tot[i] += v
    r.rows.append(["TOTAL"] + [fmt(v) for v in tot])
    return r


def outstanding_bills(b: Books, ledger_id: int, as_on) -> Report:
    led = b.get_ledger(ledger_id)
    r = Report(f"Outstanding bills: {led['name']} as on {display_date(as_on)}",
               ["Date", "Bill", "Amount", "Pending", "Age (days)"])
    bills, advance = party_bills(b, ledger_id, as_on)
    for x in bills:
        r.rows.append([display_date(x["date"]) if x["date"] else "", x["label"], fmt(x["amount"]),
                       fmt(x["pending"]), "" if x["age"] is None else x["age"]])
    r.rows.append(["", "TOTAL PENDING", "", fmt(sum(x["pending"] for x in bills)), ""])
    if advance:
        r.rows.append(["", "Advance / on account", "", fmt(advance), ""])
    return r


def bank_ledger_check(b: Books, bank_ledger_id: int) -> Report:
    """Staged bank rows not yet posted - quick view of what still needs a ledger."""
    r = Report("Unposted bank transactions", ["Date", "Narration", "Debit", "Credit", "Ledger"])
    for t in b.bank_txns(bank_ledger_id, pending_only=True):
        r.rows.append([display_date(t["date"]), t["narration"], _dr(t["debit"]), _dr(t["credit"]),
                       t["counter_name"] or ""])
    return r


def export_csv(report: Report, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig so Excel opens it cleanly
        w = csv.writer(f)
        w.writerow([report.title])
        w.writerow(report.columns)
        w.writerows(report.rows)


REPORT_NAMES = ["Day Book", "Ledger Statement", "Trial Balance", "Profit & Loss", "Balance Sheet",
                "GST Summary", "Stock Summary", "Receivables Ageing", "Payables Ageing", "Outstanding Bills"]


def run_report(b: Books, name: str, start: str, end: str, ledger_id=None) -> Report:
    """Run a report by display name. `start`/`end` are ISO dates (end doubles as 'as on')."""
    if name == "Day Book":
        return day_book(b, start, end)
    if name == "Ledger Statement":
        if not ledger_id:
            raise ValueError("Choose a ledger for the Ledger Statement.")
        return ledger_statement(b, int(ledger_id), start, end)
    if name == "Trial Balance":
        return trial_balance(b, end)
    if name == "Profit & Loss":
        return profit_and_loss(b, start, end)
    if name == "Balance Sheet":
        return balance_sheet(b, end)
    if name == "GST Summary":
        return gst_summary(b, start, end)
    if name == "Stock Summary":
        return stock_summary(b, end)
    if name == "Receivables Ageing":
        return ageing(b, end, True)
    if name == "Payables Ageing":
        return ageing(b, end, False)
    if name == "Outstanding Bills":
        if not ledger_id:
            raise ValueError("Choose a customer or supplier ledger for Outstanding Bills.")
        return outstanding_bills(b, int(ledger_id), end)
    raise ValueError(f"Unknown report: {name}")
