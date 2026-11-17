"""The five working tabs: Masters, Vouchers, Invoices, Bank Statements, Reports."""
import tkinter as tk
from datetime import date
from tkinter import ttk, filedialog, messagebox

from .. import reports
from ..accounting import VOUCHER_TYPES, CashLimitError, display_date, fy_start, parse_date
from ..bank_import import StatementError, check_consistency, parse_statement
from ..gst import GST_RATES, STATES
from ..money import fmt, fmt_dr_cr, to_paise
from .widgets import DataTable, ask_form

STATE_CHOICES = [""] + [f"{c} - {n}" for c, n in STATES.items()]


def today_str():
    return date.today().strftime("%d-%m-%Y")


def state_code(choice: str) -> str:
    return choice.split(" - ")[0].strip() if choice else ""


def _error(e):
    messagebox.showerror("EasyTally", str(e))


class BaseTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app

    @property
    def books(self):
        return self.app.books

    def refresh(self):  # overridden
        pass

    def ledger_map(self, group=None):
        """{display name: ledger id}"""
        return {l["name"]: l["id"] for l in self.books.ledgers(group)}


# ------------------------------------------------------------------------------ Masters
class MastersTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        left = ttk.LabelFrame(self, text="Ledgers", padding=6)
        right = ttk.LabelFrame(self, text="Stock Items", padding=6)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right.pack(side="left", fill="both", expand=True)

        self.ledger_table = DataTable(left, ["Name", "Group", "Opening", "GSTIN"], [160, 130, 110, 120], 18)
        self.ledger_table.pack(fill="both", expand=True)
        bar = ttk.Frame(left); bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="Add Ledger…", command=self.add_ledger).pack(side="left")
        ttk.Button(bar, text="Delete Selected", command=self.delete_ledger).pack(side="left", padx=4)

        self.item_table = DataTable(right, ["Item", "Unit", "HSN", "GST %", "Opening Qty", "Opening Value"],
                                    [150, 50, 70, 55, 90, 100], 18)
        self.item_table.pack(fill="both", expand=True)
        bar = ttk.Frame(right); bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="Add Item…", command=self.add_item).pack(side="left")
        ttk.Button(bar, text="Delete Selected", command=self.delete_item).pack(side="left", padx=4)

    def refresh(self):
        leds = self.books.ledgers()
        self.ledger_table.set_rows(
            [[l["name"], l["group_name"], fmt_dr_cr(l["opening"]), l["gstin"]] for l in leds],
            iids=[l["id"] for l in leds])
        items = self.books.stock_items()
        self.item_table.set_rows(
            [[i["name"], i["unit"], i["hsn"], i["gst_rate"], f"{i['opening_qty']:g}", fmt(i["opening_value"])]
             for i in items], iids=[i["id"] for i in items])

    def add_ledger(self):
        groups = [g["name"] for g in self.books.groups()]
        f = ask_form(self, "Add Ledger", [
            ("name", "Name", "entry", ""),
            ("group", "Under group", "combo", (groups, "Sundry Debtors")),
            ("opening", "Opening balance", "entry", "0"),
            ("side", "Dr / Cr", "combo", (["Dr", "Cr"], "Dr")),
            ("gstin", "GSTIN (optional)", "entry", ""),
            ("state", "State (for GST)", "combo", (STATE_CHOICES, "")),
        ])
        if not f:
            return
        try:
            self.books.add_ledger(f["name"], f["group"], f["opening"], f["side"], f["gstin"],
                                  state_code(f["state"]))
        except ValueError as e:
            return _error(e)
        self.app.refresh_all()

    def delete_ledger(self):
        for iid in self.ledger_table.selected():
            try:
                self.books.delete_ledger(int(iid))
            except ValueError as e:
                _error(e)
                break
        self.app.refresh_all()

    def add_item(self):
        f = ask_form(self, "Add Stock Item", [
            ("name", "Item name", "entry", ""), ("unit", "Unit", "entry", "Nos"),
            ("hsn", "HSN / SAC", "entry", ""),
            ("rate", "GST %", "combo", ([str(r) for r in GST_RATES], "18")),
            ("qty", "Opening quantity", "entry", "0"), ("value", "Opening value", "entry", "0"),
        ])
        if not f:
            return
        try:
            self.books.add_stock_item(f["name"], f["unit"], f["hsn"], int(f["rate"]), f["qty"], f["value"])
        except ValueError as e:
            return _error(e)
        self.app.refresh_all()

    def delete_item(self):
        for iid in self.item_table.selected():
            try:
                self.books.delete_stock_item(int(iid))
            except ValueError as e:
                _error(e)
                break
        self.app.refresh_all()


# ------------------------------------------------------------------------------ Vouchers
class VoucherTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.lines = []   # (ledger_name, side, amount_str)
        head = ttk.Frame(self); head.pack(fill="x")
        self.vtype = tk.StringVar(value="Payment"); self.vdate = tk.StringVar(value=today_str())
        self.narr = tk.StringVar(); self.ref = tk.StringVar()
        for col, (label, widget) in enumerate([
                ("Type", ttk.Combobox(head, textvariable=self.vtype, values=VOUCHER_TYPES, state="readonly", width=10)),
                ("Date (DD-MM-YYYY)", ttk.Entry(head, textvariable=self.vdate, width=12)),
                ("Narration", ttk.Entry(head, textvariable=self.narr, width=40)),
                ("Ref / Cheque no.", ttk.Entry(head, textvariable=self.ref, width=16))]):
            ttk.Label(head, text=label).grid(row=0, column=col, sticky="w", padx=4)
            widget.grid(row=1, column=col, padx=4, pady=(0, 6))

        edit = ttk.Frame(self); edit.pack(fill="x")
        self.ledger = tk.StringVar(); self.side = tk.StringVar(value="Dr"); self.amount = tk.StringVar()
        self.ledger_combo = ttk.Combobox(edit, textvariable=self.ledger, width=36)
        self.ledger_combo.pack(side="left", padx=4)
        ttk.Combobox(edit, textvariable=self.side, values=["Dr", "Cr"], state="readonly", width=4).pack(side="left")
        ttk.Entry(edit, textvariable=self.amount, width=14).pack(side="left", padx=4)
        ttk.Button(edit, text="Add Line", command=self.add_line).pack(side="left")
        ttk.Button(edit, text="Remove Selected", command=self.remove_line).pack(side="left", padx=4)

        self.line_table = DataTable(self, ["Ledger", "Dr/Cr", "Amount"], [320, 60, 120], 5, "browse")
        self.line_table.pack(fill="x", pady=6)
        foot = ttk.Frame(self); foot.pack(fill="x")
        self.totals = ttk.Label(foot, text="Dr 0.00   Cr 0.00")
        self.totals.pack(side="left")
        ttk.Button(foot, text="Save Voucher", command=self.save).pack(side="right")
        ttk.Button(foot, text="Clear", command=self.clear).pack(side="right", padx=4)

        ttk.Separator(self).pack(fill="x", pady=8)
        ttk.Label(self, text="Recent vouchers").pack(anchor="w")
        self.recent = DataTable(self, ["Date", "Type", "No.", "Particulars", "Amount", "Narration"],
                                [90, 80, 50, 320, 110, 260], 10)
        self.recent.pack(fill="both", expand=True)
        ttk.Button(self, text="Delete Selected Voucher", command=self.delete_voucher).pack(anchor="e", pady=4)

    def refresh(self):
        self.ledger_combo["values"] = list(self.ledger_map())
        vs = list(reversed(self.books.vouchers()))[:200]
        rows = []
        for v in vs:
            es = self.books.voucher_entries(v["id"])
            amount = sum(e["amount"] for e in es if e["amount"] > 0)
            rows.append([display_date(v["date"]), v["vtype"], v["number"],
                         " / ".join(e["name"] for e in es), fmt(amount), v["narration"]])
        self.recent.set_rows(rows, iids=[v["id"] for v in vs])

    def _update_totals(self):
        dr = sum(to_paise(a) for _, s, a in self.lines if s == "Dr")
        cr = sum(to_paise(a) for _, s, a in self.lines if s == "Cr")
        status = "balanced" if dr == cr and dr else f"difference {fmt(abs(dr - cr))}"
        self.totals.config(text=f"Dr {fmt(dr)}   Cr {fmt(cr)}   ({status})")
        self.line_table.set_rows([[n, s, fmt(to_paise(a))] for n, s, a in self.lines])

    def add_line(self):
        name = self.ledger.get().strip()
        if name not in self.ledger_map():
            return _error("Choose an existing ledger (create it in Masters first).")
        try:
            if to_paise(self.amount.get()) <= 0:
                raise ValueError("Amount must be greater than zero")
        except ValueError as e:
            return _error(e)
        self.lines.append((name, self.side.get(), self.amount.get()))
        # Convenience: pre-fill the balancing line
        dr = sum(to_paise(a) for _, s, a in self.lines if s == "Dr")
        cr = sum(to_paise(a) for _, s, a in self.lines if s == "Cr")
        self.side.set("Cr" if dr > cr else "Dr")
        self.amount.set(f"{abs(dr - cr) / 100:.2f}" if dr != cr else "")
        self.ledger.set("")
        self._update_totals()

    def remove_line(self):
        sel = self.line_table.tree.selection()
        if sel:
            idx = self.line_table.tree.index(sel[0])
            del self.lines[idx]
            self._update_totals()

    def clear(self):
        self.lines.clear(); self.amount.set(""); self.ledger.set(""); self.narr.set(""); self.ref.set("")
        self._update_totals()

    def save(self):
        ids = self.ledger_map()
        args = (self.vtype.get(), self.vdate.get(), [(ids[n], s, a) for n, s, a in self.lines],
                self.narr.get(), self.ref.get())
        try:
            try:
                self.books.add_voucher(*args)
            except CashLimitError as e:
                if e.blocked:
                    return _error("Cash limit: " + "; ".join(e.warnings) + "\n\nThis company blocks entries above the limit.")
                if not messagebox.askyesno("EasyTally - cash limit",
                                           "\n".join(e.warnings) + "\n\nSave this entry anyway?"):
                    return
                self.books.add_voucher(*args, cash_check="confirmed")
        except (ValueError, KeyError) as e:
            return _error(e)
        self.clear()
        self.app.refresh_all()

    def delete_voucher(self):
        sel = self.recent.selected()
        if sel and messagebox.askyesno("EasyTally", "Delete the selected voucher(s)?"):
            for iid in sel:
                self.books.delete_voucher(int(iid))
            self.app.refresh_all()


# ------------------------------------------------------------------------------ Invoices
class InvoiceTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        self.lines = []   # (item_name, qty, rate)
        head = ttk.Frame(self); head.pack(fill="x")
        self.kind = tk.StringVar(value="Sales"); self.idate = tk.StringVar(value=today_str())
        self.party = tk.StringVar(); self.inv_no = tk.StringVar()
        self.party_combo = ttk.Combobox(head, textvariable=self.party, width=34)
        for col, (label, w) in enumerate([
                ("Invoice type", ttk.Combobox(head, textvariable=self.kind, values=["Sales", "Purchase", "Credit Note", "Debit Note"],
                                              state="readonly", width=12)),
                ("Date (DD-MM-YYYY)", ttk.Entry(head, textvariable=self.idate, width=12)),
                ("Party (debtor / creditor)", self.party_combo),
                ("Invoice no.", ttk.Entry(head, textvariable=self.inv_no, width=16))]):
            ttk.Label(head, text=label).grid(row=0, column=col, sticky="w", padx=4)
            w.grid(row=1, column=col, padx=4, pady=(0, 6))

        edit = ttk.Frame(self); edit.pack(fill="x")
        self.item = tk.StringVar(); self.qty = tk.StringVar(value="1"); self.rate = tk.StringVar()
        self.item_combo = ttk.Combobox(edit, textvariable=self.item, width=30, state="readonly")
        self.item_combo.pack(side="left", padx=4)
        ttk.Label(edit, text="Qty").pack(side="left"); ttk.Entry(edit, textvariable=self.qty, width=8).pack(side="left", padx=4)
        ttk.Label(edit, text="Rate").pack(side="left"); ttk.Entry(edit, textvariable=self.rate, width=12).pack(side="left", padx=4)
        ttk.Button(edit, text="Add Item", command=self.add_line).pack(side="left", padx=4)
        ttk.Button(edit, text="Remove Selected", command=self.remove_line).pack(side="left")

        self.line_table = DataTable(self, ["Item", "Qty", "Rate", "GST %", "Taxable", "CGST", "SGST", "IGST"],
                                    [200, 60, 90, 60, 100, 90, 90, 90], 6, "browse")
        self.line_table.pack(fill="x", pady=6)
        foot = ttk.Frame(self); foot.pack(fill="x")
        self.totals = ttk.Label(foot, text="")
        self.totals.pack(side="left")
        ttk.Button(foot, text="Save Invoice", command=self.save).pack(side="right")
        ttk.Button(foot, text="Clear", command=self.clear).pack(side="right", padx=4)

        ttk.Separator(self).pack(fill="x", pady=8)
        ttk.Label(self, text="Recent invoices").pack(anchor="w")
        self.recent = DataTable(self, ["Date", "Type", "No.", "Party", "Taxable", "CGST", "SGST", "IGST", "Total"],
                                [90, 70, 50, 200, 100, 90, 90, 90, 110], 9)
        self.recent.pack(fill="both", expand=True)
        ttk.Button(self, text="Delete Selected Invoice", command=self.delete_invoice).pack(anchor="e", pady=4)

    def _parties(self):
        m = {}
        for g in ("Sundry Debtors", "Sundry Creditors"):
            m.update({l["name"]: l["id"] for l in self.books.ledgers(g)})
        return m

    def refresh(self):
        self.party_combo["values"] = list(self._parties())
        self.item_combo["values"] = [i["name"] for i in self.books.stock_items()]
        rows = self.books.conn.execute(
            """SELECT v.id, v.date, v.number, i.*, l.name pname FROM invoices i
               JOIN vouchers v ON v.id=i.voucher_id JOIN ledgers l ON l.id=i.party_id
               ORDER BY v.date DESC, v.id DESC LIMIT 200""").fetchall()
        self.recent.set_rows(
            [[display_date(r["date"]), r["kind"], r["number"], r["pname"], fmt(r["taxable"]),
              fmt(r["cgst"]), fmt(r["sgst"]), fmt(r["igst"]), fmt(r["total"])] for r in rows],
            iids=[r["id"] for r in rows])

    def _lines_as_ids(self):
        items = {i["name"]: i["id"] for i in self.books.stock_items()}
        return [(items[n], q, r) for n, q, r in self.lines]

    def _redraw(self):
        pid = self._parties().get(self.party.get())
        if not (self.lines and pid):
            self.line_table.set_rows([[n, q, r, "", "", "", "", ""] for n, q, r in self.lines])
            self.totals.config(text="Select a party and add items to see GST.")
            return
        try:
            inv = self.books.preview_invoice(self.kind.get(), pid, self._lines_as_ids())
        except ValueError as e:
            self.totals.config(text=str(e)); return
        names = [n for n, _, _ in self.lines]
        self.line_table.set_rows([[names[i], f"{l['qty']:g}", fmt(l["rate"]), l["gst_rate"], fmt(l["taxable"]),
                                   fmt(l["cgst"]), fmt(l["sgst"]), fmt(l["igst"])]
                                  for i, l in enumerate(inv["lines"])])
        self.totals.config(text=(f"Taxable {fmt(inv['taxable'])}   CGST {fmt(inv['cgst'])}   SGST {fmt(inv['sgst'])}"
                                 f"   IGST {fmt(inv['igst'])}   TOTAL {fmt(inv['total'])}   "
                                 f"({'intra-state' if inv['intra_state'] else 'inter-state'})"))

    def add_line(self):
        if not self.item.get():
            return _error("Choose an item.")
        try:
            if float(self.qty.get()) <= 0 or to_paise(self.rate.get()) < 0:
                raise ValueError
        except ValueError:
            return _error("Enter a valid quantity and rate.")
        self.lines.append((self.item.get(), self.qty.get(), self.rate.get()))
        self.item.set(""); self.qty.set("1"); self.rate.set("")
        self._redraw()

    def remove_line(self):
        sel = self.line_table.tree.selection()
        if sel:
            del self.lines[self.line_table.tree.index(sel[0])]
            self._redraw()

    def clear(self):
        self.lines.clear(); self.inv_no.set(""); self._redraw()

    def save(self):
        pid = self._parties().get(self.party.get())
        if not pid:
            return _error("Choose a party.")
        try:
            self.books.add_invoice(self.kind.get(), self.idate.get(), pid, self._lines_as_ids(),
                                   ref=self.inv_no.get())
        except ValueError as e:
            return _error(e)
        self.clear()
        self.app.refresh_all()

    def delete_invoice(self):
        sel = self.recent.selected()
        if sel and messagebox.askyesno("EasyTally", "Delete the selected invoice(s)?"):
            for iid in sel:
                self.books.delete_voucher(int(iid))
            self.app.refresh_all()


# ------------------------------------------------------------------------------ Bank statements
class BankTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        top = ttk.Frame(self); top.pack(fill="x")
        ttk.Label(top, text="Bank account:").pack(side="left")
        self.bank = tk.StringVar()
        self.bank_combo = ttk.Combobox(top, textvariable=self.bank, state="readonly", width=30)
        self.bank_combo.pack(side="left", padx=4)
        self.bank_combo.bind("<<ComboboxSelected>>", lambda *_: self.refresh_rows())
        ttk.Button(top, text="New Bank Account…", command=self.new_bank).pack(side="left", padx=4)
        ttk.Button(top, text="Import Statement PDF…", command=self.import_pdf).pack(side="left", padx=12)
        self.show_posted = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show posted", variable=self.show_posted,
                        command=self.refresh_rows).pack(side="right")

        self.table = DataTable(self, ["Date", "Narration", "Debit", "Credit", "Ledger", "Status"],
                               [90, 380, 100, 100, 180, 110], 18)
        self.table.pack(fill="both", expand=True, pady=6)
        bar = ttk.Frame(self); bar.pack(fill="x")
        ttk.Button(bar, text="Set Ledger for Selected…", command=self.set_ledger).pack(side="left")
        ttk.Button(bar, text="Post Selected", command=lambda: self.post(selected=True)).pack(side="left", padx=4)
        ttk.Button(bar, text="Post All with Ledger", command=lambda: self.post(selected=False)).pack(side="left")
        ttk.Button(bar, text="Post All (rest to Suspense)",
                   command=lambda: self.post(selected=False, suspense=True)).pack(side="left", padx=4)
        self.summary = ttk.Label(bar, text="")
        self.summary.pack(side="right")

    def _banks(self):
        return {l["name"]: l["id"] for l in self.books.ledgers("Bank Accounts")}

    def refresh(self):
        banks = self._banks()
        self.bank_combo["values"] = list(banks)
        if self.bank.get() not in banks:
            self.bank.set(next(iter(banks), ""))
        self.refresh_rows()

    def refresh_rows(self):
        bank_id = self._banks().get(self.bank.get())
        if not bank_id:
            self.table.set_rows([]); self.summary.config(text="Create a bank account to begin."); return
        txns = self.books.bank_txns(bank_id, pending_only=not self.show_posted.get())
        rows, tags = [], []
        for t in txns:
            status = "Posted" if t["voucher_id"] else ("Check Dr/Cr" if t["uncertain"] else "Pending")
            rows.append([display_date(t["date"]), t["narration"], fmt(t["debit"]) if t["debit"] else "",
                         fmt(t["credit"]) if t["credit"] else "", t["counter_name"] or "", status])
            tags.append("muted" if t["voucher_id"] else ("warn" if t["uncertain"] else None))
        self.table.set_rows(rows, iids=[t["id"] for t in txns], tags=tags)
        pending = [t for t in txns if not t["voucher_id"]]
        unmapped = sum(1 for t in pending if not t["counter_ledger_id"])
        self.summary.config(text=f"{len(pending)} pending, {unmapped} without ledger")

    def new_bank(self):
        f = ask_form(self, "New Bank Account", [
            ("name", "Account name (e.g. HDFC Current A/c)", "entry", ""),
            ("opening", "Opening balance", "entry", "0"),
            ("side", "Dr / Cr", "combo", (["Dr", "Cr"], "Dr"))])
        if not f:
            return
        try:
            self.books.add_ledger(f["name"], "Bank Accounts", f["opening"], f["side"])
        except ValueError as e:
            return _error(e)
        self.bank.set(f["name"].strip())
        self.app.refresh_all()

    def import_pdf(self):
        bank_id = self._banks().get(self.bank.get())
        if not bank_id:
            return _error("Create or choose a bank account first.")
        paths = filedialog.askopenfilenames(title="Select bank statement PDF(s)",
                                            filetypes=[("PDF files", "*.pdf")])
        report = []
        for path in paths:
            try:
                txns = parse_statement(path)
            except StatementError as e:
                hint = ("\n   Scanned statements can be read with OCR and verified in the browser version "
                        "(EasyTally (Browser))." if "no selectable text" in str(e) else "")
                report.append(f"{path.split('/')[-1]}: {e}{hint}")
                continue
            new, dup = self.books.stage_bank_transactions(bank_id, path, txns)
            report.append(f"{path.split('/')[-1]}: {new} new, {dup} already imported.")
            report += [f"   ! {w}" for w in check_consistency(txns)[:8]]
        if report:
            messagebox.showinfo("Statement import", "\n".join(report))
        self.refresh_rows()

    def set_ledger(self):
        sel = self.table.selected()
        if not sel:
            return _error("Select one or more rows first.")
        names = list(self.ledger_map())
        first = next(t for t in self.books.bank_txns() if str(t["id"]) == sel[0])
        guess = " ".join(first["narration"].split()[:2])
        f = ask_form(self, "Set Ledger", [
            ("ledger", "Ledger", "combo", (names, first["counter_name"] or "")),
            ("learn", "Remember this keyword for future imports (optional)", "entry", guess),
            ("use", "Remember it", "check", False)])
        if not f or not f["ledger"]:
            return
        lid = self.ledger_map()[f["ledger"]]
        for iid in sel:
            self.books.set_txn_ledger(int(iid), lid, f["learn"] if f["use"] else "")
        self.refresh_rows()

    def post(self, selected, suspense=False):
        if selected:
            ids = [int(i) for i in self.table.selected()]
        else:
            ids = [t["id"] for t in self.books.bank_txns(self._banks().get(self.bank.get()), pending_only=True)]
        if suspense and not messagebox.askyesno(
                "EasyTally", "Rows without a ledger will be booked to 'Suspense A/c'. Continue?"):
            return
        try:
            posted, skipped = self.books.post_bank_transactions(ids, use_suspense=suspense)
        except ValueError as e:
            return _error(e)
        messagebox.showinfo("EasyTally", f"Posted {posted} voucher(s). Skipped {skipped} (no ledger or already posted).")
        self.app.refresh_all()


# ------------------------------------------------------------------------------ Reports
REPORT_NAMES = reports.REPORT_NAMES


class ReportsTab(BaseTab):
    def __init__(self, master, app):
        super().__init__(master, app)
        top = ttk.Frame(self); top.pack(fill="x")
        self.name = tk.StringVar(value=REPORT_NAMES[0])
        self.start = tk.StringVar(value=display_date(fy_start()))
        self.end = tk.StringVar(value=today_str())
        self.ledger = tk.StringVar()
        ttk.Combobox(top, textvariable=self.name, values=REPORT_NAMES, state="readonly", width=18).pack(side="left")
        ttk.Label(top, text=" From").pack(side="left")
        ttk.Entry(top, textvariable=self.start, width=12).pack(side="left", padx=2)
        ttk.Label(top, text="To / As on").pack(side="left")
        ttk.Entry(top, textvariable=self.end, width=12).pack(side="left", padx=2)
        ttk.Label(top, text=" Ledger").pack(side="left")
        self.ledger_combo = ttk.Combobox(top, textvariable=self.ledger, width=26)
        self.ledger_combo.pack(side="left", padx=2)
        ttk.Button(top, text="Run", command=self.run).pack(side="left", padx=6)
        ttk.Button(top, text="Export CSV…", command=self.export).pack(side="left")
        self.title = ttk.Label(self, text="", font=("TkDefaultFont", 11, "bold"))
        self.title.pack(anchor="w", pady=6)
        self.table = DataTable(self, ["Report"], height=22)
        self.table.pack(fill="both", expand=True)
        self.report = None

    def refresh(self):
        self.ledger_combo["values"] = list(self.ledger_map())
        if self.report:
            self.run(quiet=True)

    def run(self, quiet=False):
        b = self.books
        try:
            start, end = parse_date(self.start.get()), parse_date(self.end.get())
            lid = self.ledger_map().get(self.ledger.get())
            rep = reports.run_report(b, self.name.get(), start, end, lid)
        except ValueError as e:
            if not quiet:
                _error(e)
            return
        self.report = rep
        self.title.config(text=rep.title)
        self.table.set_columns(rep.columns, [260 if c in ("Particulars", "Ledger", "Item", "Narration") else 110
                                             for c in rep.columns])
        self.table.set_rows(rep.rows)

    def export(self):
        if not self.report:
            return _error("Run a report first.")
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV (Excel)", "*.csv")],
                                            initialfile=self.report.title.split("  ")[0].replace(" ", "_") + ".csv")
        if path:
            reports.export_csv(self.report, path)
            messagebox.showinfo("EasyTally", f"Saved {path}")
