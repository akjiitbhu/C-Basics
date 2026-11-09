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


def gst_summary(b: Books, start, end) -> Report:
    r = Report(f"GST Summary  {display_date(start)} to {display_date(end)}",
               ["Kind", "GST %", "Taxable", "CGST", "SGST", "IGST"])
    rows = b.conn.execute(
        """SELECT i.kind, l.gst_rate, SUM(l.taxable) t, SUM(l.cgst) c, SUM(l.sgst) s, SUM(l.igst) g
           FROM invoice_lines l JOIN invoices i ON i.voucher_id = l.voucher_id
           JOIN vouchers v ON v.id = i.voucher_id
           WHERE v.date BETWEEN ? AND ? GROUP BY i.kind, l.gst_rate ORDER BY i.kind DESC, l.gst_rate""",
        (start, end)).fetchall()
    tot = {"Sales": [0, 0, 0], "Purchase": [0, 0, 0]}
    for x in rows:
        r.rows.append([x["kind"] + (" (output)" if x["kind"] == "Sales" else " (input)"),
                       f"{x['gst_rate']}%", fmt(x["t"]), fmt(x["c"]), fmt(x["s"]), fmt(x["g"])])
        for i, k in enumerate(("c", "s", "g")):
            tot[x["kind"]][i] += x[k]
    for i, name in enumerate(("CGST", "SGST", "IGST")):
        net = tot["Sales"][i] - tot["Purchase"][i]
        r.rows.append([f"Net {name} payable" if net >= 0 else f"Net {name} credit", "", "",
                       fmt(abs(net)) if i == 0 else "", fmt(abs(net)) if i == 1 else "",
                       fmt(abs(net)) if i == 2 else ""])
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
        pq = d["Purchase"]["q"] if "Purchase" in d else 0
        pv = d["Purchase"]["t"] if "Purchase" in d else 0
        sq = d["Sales"]["q"] if "Sales" in d else 0
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
