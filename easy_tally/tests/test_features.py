import os
import sqlite3
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally import db, reports
from easytally.accounting import Books


def mk(testcase, state="27"):
    b = Books.create(f"Feat {testcase.id().split('.')[-1]}", state=state)
    testcase.addCleanup(b.close)
    return b


class AgeingTests(unittest.TestCase):
    def test_fifo_settlement_and_buckets(self):
        b = mk(self)
        item = b.add_stock_item("Widget", gst_rate=0)
        cust = b.add_ledger("Cust", "Sundry Debtors", state="27")
        b.add_invoice("Sales", "01-01-2026", cust, [(item, 1, "1000")], ref="INV-1")   # 69 days old as on 11-Mar
        b.add_invoice("Sales", "05-03-2026", cust, [(item, 1, "500")], ref="INV-2")    # 6 days old
        cash = b.ledger_id("Cash")
        b.add_voucher("Receipt", "10-02-2026", [(cash, "Dr", 400), (cust, "Cr", 400)])  # settles INV-1 partly
        bills, adv = reports.party_bills(b, cust, "2026-03-11")
        self.assertEqual([(x["label"], x["pending"]) for x in bills], [("INV-1", 60000), ("INV-2", 50000)])
        self.assertEqual(adv, 0)
        ag = reports.ageing(b, "2026-03-11", True)
        row = ag.rows[0]
        self.assertEqual(row[:2], ["Cust", "1,100.00"])
        self.assertEqual(row[2:6], ["500.00", "", "600.00", ""])        # 0-30 holds INV-2, 61-90 holds INV-1 balance
        self.assertEqual(ag.rows[-1][1], "1,100.00")

    def test_advance_and_creditors(self):
        b = mk(self)
        item = b.add_stock_item("Widget", gst_rate=0)
        sup = b.add_ledger("Supplier", "Sundry Creditors", state="27")
        cash = b.ledger_id("Cash")
        b.add_voucher("Payment", "01-03-2026", [(sup, "Dr", 300), (cash, "Cr", 300)])   # advance to supplier
        bills, adv = reports.party_bills(b, sup, "2026-03-31")
        self.assertEqual((bills, adv), ([], 30000))
        b.add_invoice("Purchase", "10-03-2026", sup, [(item, 1, "1000")])
        bills, adv = reports.party_bills(b, sup, "2026-03-31")
        self.assertEqual((bills[0]["pending"], adv), (70000, 0))

    def test_opening_balance_is_oldest_bill(self):
        b = mk(self)
        cust = b.add_ledger("Old Cust", "Sundry Debtors", opening="250")
        ag = reports.ageing(b, "2026-03-31", True)
        self.assertEqual(ag.rows[0][1], "250.00")
        self.assertEqual(ag.rows[0][5], "250.00")             # lands in "Over 90"
        ob = reports.outstanding_bills(b, cust, "2026-03-31")
        self.assertEqual(ob.rows[0][1], "Opening balance")


class NotesAndEditTests(unittest.TestCase):
    def test_credit_note_reverses_sale_and_stock(self):
        b = mk(self)
        item = b.add_stock_item("Widget", gst_rate=18, opening_qty=10, opening_value="1000")
        cust = b.add_ledger("Cust", "Sundry Debtors", state="27")
        b.add_invoice("Sales", "01-04-2026", cust, [(item, 4, "200")])
        b.add_invoice("Credit Note", "05-04-2026", cust, [(item, 1, "200")], ref="CN-1")
        bal = b.balances()
        self.assertEqual(bal[b.ledger_id("Sales")], -60000)                 # 800 - 200 = 600
        self.assertEqual(bal[cust], 70800)                                   # 944 - 236
        self.assertEqual(bal[b.ledger_id("Output CGST")], -5400)             # 72 - 18
        self.assertEqual(sum(bal.values()), 0)
        stock = reports.stock_summary(b, "2026-04-30")
        self.assertEqual(stock.rows[0][3], "3")                              # net outward 4 - 1
        self.assertEqual(stock.rows[0][4], "7")                              # closing 10 - 3
        gst = reports.gst_summary(b, "2026-04-01", "2026-04-30")
        labels = [r[0] for r in gst.rows]
        self.assertIn("Credit Note (reduces output)", labels)
        self.assertEqual(gst.rows[-3][3], "54.00")                           # net CGST payable
        # the credit note settles part of the invoice in ageing
        self.assertEqual(reports.party_bills(b, cust, "2026-04-30")[0][0]["pending"], 70800)

    def test_debit_note_reduces_purchases_and_stock(self):
        b = mk(self)
        item = b.add_stock_item("Widget", gst_rate=12)
        sup = b.add_ledger("Supplier", "Sundry Creditors", state="29")       # inter-state
        b.add_invoice("Purchase", "01-04-2026", sup, [(item, 10, "100")])
        b.add_invoice("Debit Note", "03-04-2026", sup, [(item, 2, "100")])
        bal = b.balances()
        self.assertEqual(bal[b.ledger_id("Purchase")], 80000)
        self.assertEqual(bal[b.ledger_id("Input IGST")], 9600)
        self.assertEqual(bal[sup], -89600)
        self.assertEqual(reports.stock_summary(b, "2026-04-30").rows[0][4], "8")

    def test_edit_plain_voucher(self):
        b = mk(self)
        cash, sales = b.ledger_id("Cash"), b.ledger_id("Sales")
        vid = b.add_voucher("Receipt", "01-04-2026", [(cash, "Dr", 100), (sales, "Cr", 100)])
        number = b.get_voucher(vid)["number"]
        b.update_voucher(vid, "02-04-2026", [(cash, "Dr", 250), (sales, "Cr", 250)], "fixed", "R9")
        v = b.get_voucher(vid)
        self.assertEqual((v["number"], v["date"], v["narration"], v["ref"]), (number, "2026-04-02", "fixed", "R9"))
        self.assertEqual(b.balances()[cash], 25000)
        with self.assertRaises(ValueError):                                   # still must balance
            b.update_voucher(vid, "02-04-2026", [(cash, "Dr", 5), (sales, "Cr", 4)])
        self.assertEqual(b.balances()[cash], 25000)                           # failed edit changed nothing

    def test_edit_invoice(self):
        b = mk(self)
        item = b.add_stock_item("Widget", gst_rate=18)
        a = b.add_ledger("A", "Sundry Debtors", state="27")
        c = b.add_ledger("C", "Sundry Debtors", state="29")
        vid = b.add_invoice("Sales", "01-04-2026", a, [(item, 1, "100")])
        b.update_invoice(vid, "03-04-2026", c, [(item, 2, "100")], ref="X1")   # now inter-state
        bal = b.balances()
        self.assertEqual((bal[a], bal[c]), (0, 23600))
        self.assertEqual((bal[b.ledger_id("Output CGST")], bal[b.ledger_id("Output IGST")]), (0, -3600))
        self.assertEqual(sum(bal.values()), 0)
        v = b.get_voucher(vid)
        self.assertEqual(v["invoice"]["party_id"], c)
        self.assertEqual(v["invoice"]["lines"][0]["qty"], 2)
        with self.assertRaises(ValueError):
            b.update_voucher(vid, "03-04-2026", [(a, "Dr", 1), (c, "Cr", 1)])  # invoices not editable here


class SettingsAndMigration(unittest.TestCase):
    def test_gstin_validation_and_state_derivation(self):
        b = mk(self)
        with self.assertRaises(ValueError):
            b.add_ledger("Bad", "Sundry Debtors", gstin="123")
        lid = b.add_ledger("Good", "Sundry Debtors", gstin="29abcde1234f1z5")
        self.assertEqual(b.get_ledger(lid)["state"], "29")
        b.set_settings({"gstin": "27ABCDE1234F1Z5", "address": "Pune"})
        self.assertEqual(b.settings()["address"], "Pune")
        with self.assertRaises(ValueError):
            b.set_settings({"gstin": "nope"})

    def test_old_database_is_migrated(self):
        """A file created before credit notes existed (kind CHECK constraint) opens and accepts notes."""
        b = mk(self)
        b.close()
        conn = sqlite3.connect(str(b.path))
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE invoices")
        conn.execute("""CREATE TABLE invoices (
            voucher_id INTEGER PRIMARY KEY REFERENCES vouchers(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('Sales','Purchase')),
            party_id INTEGER NOT NULL REFERENCES ledgers(id), intra_state INTEGER NOT NULL,
            taxable INTEGER NOT NULL, cgst INTEGER NOT NULL, sgst INTEGER NOT NULL, igst INTEGER NOT NULL,
            total INTEGER NOT NULL)""")
        conn.execute("DROP TABLE audit_log")
        conn.execute("PRAGMA user_version = 0")
        conn.commit(); conn.close()
        b2 = Books(b.path)
        self.addCleanup(b2.close)
        item = b2.add_stock_item("W", gst_rate=5)
        cust = b2.add_ledger("C", "Sundry Debtors")
        b2.add_invoice("Credit Note", "01-04-2026", cust, [(item, 1, "10")])     # would violate old CHECK
        b2.audit("tester", "x")
        self.assertEqual(b2.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
