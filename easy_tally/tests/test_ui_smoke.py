"""Headless smoke test of the UI wiring using a stub `tkinter` (no display needed).

It cannot check how windows look, but it executes every tab's handlers against a real
database, catching wrong names, bad method calls and logic errors in the UI layer.
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()


class Dummy:
    def __init__(self, *a, **k):
        self._items = {}
        self.kw = k
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: Dummy()
    def __iter__(self):
        return iter([])
    def __setitem__(self, k, v):
        self._items[k] = v
    def __getitem__(self, k):
        return self._items.get(k, [])
    def selection(self):
        return getattr(self, "_sel", [])
    def get_children(self, *a):
        return []
    def index(self, iid):
        return int(iid)
    def winfo_children(self):
        return []


class Var:
    def __init__(self, master=None, value=""):
        self.v = value
    def get(self):
        return self.v
    def set(self, v):
        self.v = v


def install_stub():
    tk = types.ModuleType("tkinter")
    ttk = types.ModuleType("tkinter.ttk")
    mb = types.ModuleType("tkinter.messagebox")
    fd = types.ModuleType("tkinter.filedialog")
    for n in ("Tk", "Toplevel", "Menu", "Listbox"):
        setattr(tk, n, type(n, (Dummy,), {}))
    tk.StringVar, tk.BooleanVar = Var, Var
    for n in ("Frame", "LabelFrame", "Label", "Entry", "Combobox", "Button", "Treeview", "Scrollbar",
              "Notebook", "Separator", "Checkbutton"):
        setattr(ttk, n, type(n, (Dummy,), {}))
    mb.shown = []
    for n in ("showerror", "showinfo"):
        setattr(mb, n, lambda *a, _n=n, **k: mb.shown.append((_n, a)))
    mb.askyesno = lambda *a, **k: True
    fd.askopenfilenames = lambda **k: []
    fd.asksaveasfilename = lambda **k: ""
    tk.ttk, tk.messagebox, tk.filedialog = ttk, mb, fd
    sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": mb,
                        "tkinter.filedialog": fd})
    return mb, fd


MB, FD = install_stub()

from easytally.accounting import Books            # noqa: E402
from easytally.ui import tabs as T                # noqa: E402
from easytally.ui.app import App                  # noqa: E402
from tests import pdf_samples                     # noqa: E402


class UISmoke(unittest.TestCase):
    def setUp(self):
        MB.shown.clear()
        self.books = Books.create(f"UI Smoke {self.id().split('.')[-1]}", state="27")
        self.addCleanup(self.books.close)
        self.app = App()
        self.app.show_main(self.books)
        self.masters, self.vouchers, self.invoices, self.bank, self.reports = self.app.tabs

    def errors(self):
        return [m for kind, m in MB.shown if kind == "showerror"]

    def test_voucher_flow(self):
        self.vouchers.refresh()
        v = self.vouchers
        v.vtype.set("Receipt"); v.vdate.set("01-04-2025")
        v.ledger.set("Cash"); v.side.set("Dr"); v.amount.set("250")
        v.add_line()
        self.assertEqual((v.side.get(), v.amount.get()), ("Cr", "250.00"))   # balancing line suggested
        v.ledger.set("Sales"); v.add_line()
        v.save()
        self.assertEqual(self.errors(), [])
        self.assertEqual(len(self.books.vouchers()), 1)

    def test_desktop_asks_before_saving_over_the_cash_limit(self):
        self.books.add_ledger("Supplier", "Sundry Creditors")
        v = self.vouchers
        v.refresh()
        v.vtype.set("Payment"); v.vdate.set("01-04-2026")
        for name, side in (("Supplier", "Dr"), ("Cash", "Cr")):
            v.ledger.set(name); v.side.set(side); v.amount.set("25000"); v.add_line()
        asked = []
        MB.askyesno = lambda title, text, **k: (asked.append(text), False)[1]       # user says "no"
        v.save()
        self.assertEqual(len(asked), 1)
        self.assertIn("above the limit of 10,000.00", asked[0])
        self.assertEqual(len(self.books.vouchers()), 0)                              # declined: nothing saved
        MB.askyesno = lambda *a, **k: True                                           # user says "yes"
        v.save()
        self.assertEqual(len(self.books.vouchers()), 1)
        self.assertEqual(self.errors(), [])

    def test_voucher_unknown_ledger_rejected(self):
        self.vouchers.ledger.set("Nope"); self.vouchers.amount.set("5"); self.vouchers.add_line()
        self.assertEqual(len(self.errors()), 1)

    def test_desktop_opens_the_printable_invoice_after_saving(self):
        self.books.add_stock_item("Widget", gst_rate=18)
        self.books.add_ledger("Cust", "Sundry Debtors", state="27")
        opened = []
        T.webbrowser.open = lambda url, *a, **k: opened.append(url)
        i = self.invoices
        i.refresh()
        i.kind.set("Sales"); i.idate.set("02-04-2026"); i.party.set("Cust")
        i.item.set("Widget"); i.qty.set("1"); i.rate.set("100"); i.add_line()
        i.save()
        self.assertEqual(self.errors(), [])
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("file://") and opened[0].endswith(".pdf"))
        self.assertTrue(open(opened[0][len("file://"):], "rb").read().startswith(b"%PDF"))

    def test_invoice_flow(self):
        self.books.add_stock_item("Widget", gst_rate=18)
        self.books.add_ledger("Cust", "Sundry Debtors", state="29")
        i = self.invoices
        i.refresh()
        i.kind.set("Sales"); i.idate.set("02-04-2025"); i.party.set("Cust")
        i.item.set("Widget"); i.qty.set("2"); i.rate.set("500"); i.add_line()
        i.save()
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.books.balances()[self.books.ledger_id("Output IGST")], -18000)

    def test_bank_import_and_post(self):
        bank_id = self.books.add_ledger("HDFC", "Bank Accounts", opening="50000")
        pdf = Path(tempfile.mkdtemp()) / "st.pdf"
        pdf_samples.table_pdf(pdf)
        FD.askopenfilenames = lambda **k: [str(pdf)]
        self.bank.refresh()
        self.assertEqual(self.bank.bank.get(), "HDFC")
        self.bank.import_pdf()
        self.assertEqual(len(self.books.bank_txns(bank_id)), 6)
        self.bank.post(selected=False, suspense=True)
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.books.balances()[bank_id], 15289430)

    def test_all_reports_run(self):
        self.books.add_ledger("Cust", "Sundry Debtors")
        for name in T.REPORT_NAMES:
            self.reports.name.set(name)
            self.reports.ledger.set("Cash")
            self.reports.run()
            self.assertIsNotNone(self.reports.report)
        self.assertEqual(self.errors(), [])


if __name__ == "__main__":
    unittest.main()
