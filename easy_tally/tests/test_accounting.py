import os
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally.accounting import Books
from easytally import reports
from easytally.money import fmt, to_paise


class MoneyTests(unittest.TestCase):
    def test_indian_format(self):
        self.assertEqual(fmt(12345678), "1,23,456.78")
        self.assertEqual(fmt(5), "0.05")
        self.assertEqual(fmt(-100000000), "-10,00,000.00")

    def test_rounding(self):
        self.assertEqual(to_paise("1,234.565"), 123457)
        self.assertEqual(to_paise(0.1 + 0.2), 30)


class BooksTests(unittest.TestCase):
    def setUp(self):
        name = f"Test Co {self.id().split('.')[-1]}"
        self.b = Books.create(name, state="27")   # Maharashtra
        self.addCleanup(self.b.close)

    def test_voucher_must_balance(self):
        cash, sales = self.b.ledger_id("Cash"), self.b.ledger_id("Sales")
        with self.assertRaises(ValueError):
            self.b.add_voucher("Receipt", "01-04-2025", [(cash, "Dr", 100), (sales, "Cr", 90)])
        vid = self.b.add_voucher("Receipt", "01-04-2025", [(cash, "Dr", 100), (sales, "Cr", 100)])
        self.assertEqual(self.b.balances()[cash], 10000)
        self.assertEqual(self.b.balances()[sales], -10000)
        self.b.delete_voucher(vid)
        self.assertEqual(self.b.balances()[cash], 0)

    def test_gst_intra_and_inter_state(self):
        item = self.b.add_stock_item("Widget", gst_rate=18)
        local = self.b.add_ledger("Local Cust", "Sundry Debtors", state="27")
        far = self.b.add_ledger("Far Cust", "Sundry Debtors", state="29")
        self.b.add_invoice("Sales", "05-04-2025", local, [(item, 10, "100")])
        self.b.add_invoice("Sales", "06-04-2025", far, [(item, 10, "100")])
        bal = self.b.balances()
        self.assertEqual(bal[self.b.ledger_id("Output CGST")], -9000)
        self.assertEqual(bal[self.b.ledger_id("Output SGST")], -9000)
        self.assertEqual(bal[self.b.ledger_id("Output IGST")], -18000)
        self.assertEqual(bal[local], 118000)
        self.assertEqual(bal[far], 118000)
        self.assertEqual(sum(bal.values()), 0)

    def test_reports_balance(self):
        item = self.b.add_stock_item("Widget", gst_rate=12, opening_qty=5, opening_value="500")
        supplier = self.b.add_ledger("Supplier", "Sundry Creditors", state="27")
        cust = self.b.add_ledger("Cust", "Sundry Debtors", state="27")
        bank = self.b.add_ledger("HDFC", "Bank Accounts", opening="10000", side="Dr")
        cap = self.b.add_ledger("Capital", "Capital Account", opening="10500", side="Cr")  # bank 10000 + stock 500
        self.b.add_invoice("Purchase", "02-04-2025", supplier, [(item, 20, "80")])
        self.b.add_invoice("Sales", "10-04-2025", cust, [(item, 10, "150")])
        tb = reports.trial_balance(self.b, "2025-04-30")
        self.assertEqual(tb.rows[-1][2], tb.rows[-1][3])      # debits == credits
        bs = reports.balance_sheet(self.b, "2025-04-30")
        self.assertFalse(any("Difference" in r[0] for r in bs.rows))
        pl = reports.profit_and_loss(self.b, "2025-04-01", "2025-04-30")
        # Sales 1500 + closing stock (15 qty @ avg 80*... ) - opening 500 - purchases 1600
        # avg cost = (500+1600)/25 = 84 ; closing = 15*84 = 1260 ; profit = 1500+1260-500-1600 = 660
        self.assertEqual(pl.rows[-1], ["NET PROFIT", "660.00"])
        stock = reports.stock_summary(self.b, "2025-04-30")
        self.assertEqual(stock.rows[0][4], "15")               # 5 + 20 - 10
        gst = reports.gst_summary(self.b, "2025-04-01", "2025-04-30")
        self.assertTrue(any(r[0].startswith("Sales") for r in gst.rows))

    def test_ledger_statement_running_balance(self):
        bank = self.b.add_ledger("HDFC", "Bank Accounts", opening="1000")
        cash = self.b.ledger_id("Cash")
        self.b.add_voucher("Contra", "01-04-2025", [(cash, "Dr", 500), (bank, "Cr", 500)])
        r = reports.ledger_statement(self.b, bank, "2025-04-01", "2025-04-30")
        self.assertEqual(r.rows[0][-1], "1,000.00 Dr")
        self.assertEqual(r.rows[-1][-1], "500.00 Dr")


if __name__ == "__main__":
    unittest.main()
