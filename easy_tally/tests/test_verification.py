import os
import sqlite3
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally import db
from easytally.accounting import Books
from easytally.ocr import OcrPage, parse_ocr_pages
from tests.test_ocr_parser import B, table_page, text_page

JPEG = b"\xff\xd8\xff\xe0fake-page-image"


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.b = Books.create(f"Verify {self.id().split('.')[-1]}", state="27")
        self.addCleanup(self.b.close)
        self.bank = self.b.add_ledger("HDFC", "Bank Accounts", opening="50000")
        self.b.add_ledger("Capital", "Capital Account", opening="50000", side="Cr")

    def stage(self, page=None, filename="scan.pdf"):
        info = {}
        txns = parse_ocr_pages([page or text_page()], info)
        new, dup = self.b.stage_bank_transactions(self.bank, filename, txns, pages=[JPEG], engine="RapidOCR (offline)",
                                                  opening=info.get("opening"))
        self.imp = self.b.bank_imports(self.bank)[0]["id"]
        return txns, new, dup

    def rows(self):
        return self.b.bank_txns(self.bank, import_id=self.imp)

    # ------------------------------------------------------------------ staging
    def test_ocr_rows_start_unverified_and_keep_the_page(self):
        txns, new, dup = self.stage()
        self.assertEqual((new, dup), (6, 0))
        imp = self.b.bank_imports(self.bank)[0]
        self.assertEqual((imp["source"], imp["pages"], imp["rows"], imp["unverified"], imp["posted"]), ("ocr", 1, 6, 6, 0))
        self.assertTrue(all(r["verified"] == 0 and r["source"] == "ocr" for r in self.rows()))
        self.assertEqual(self.b.bank_page(self.imp, 1), ("image/jpeg", JPEG))
        self.assertIsNone(self.b.bank_page(self.imp, 2))
        r = self.rows()[0]
        self.assertTrue(0 < r["y0"] < r["y1"] < 1 and r["page"] == 1 and r["confidence"] > 0)

    def test_same_scan_twice_adds_nothing(self):
        self.stage()
        txns = parse_ocr_pages([text_page()])
        self.assertEqual(self.b.stage_bank_transactions(self.bank, "again.pdf", txns, pages=[JPEG]), (0, 6))
        self.assertEqual(len(self.b.bank_imports(self.bank)), 1)                 # empty import removed, no extra page copy

    def test_text_pdf_rows_are_verified_from_the_start(self):
        from easytally.bank_import import Txn
        self.b.stage_bank_transactions(self.bank, "t.pdf", [Txn("2026-04-01", "UPI X", debit=100, balance=None)])
        self.assertEqual(self.b.bank_txns(self.bank)[0]["verified"], 1)

    # ------------------------------------------------------------------ the hard rule
    def test_unverified_rows_cannot_be_posted(self):
        self.stage()
        ids = [r["id"] for r in self.rows()]
        self.assertEqual(self.b.post_bank_transactions(ids, use_suspense=True), (0, 6))
        self.assertEqual(len(self.b.vouchers()), 0)
        self.b.verify_bank_txns(ids[:2])                                          # a person checked two rows
        self.assertEqual(self.b.post_bank_transactions(ids, use_suspense=True), (2, 4))
        self.assertEqual(len(self.b.vouchers()), 2)

    # ------------------------------------------------------------------ reconciliation
    def test_reconcile_flags_the_misread_amount_and_suggests_a_fix(self):
        self.stage()                                                              # row 3 was read as 15,000 instead of 5,000
        rec = self.b.reconcile(self.imp)
        self.assertEqual((rec["ok"], rec["bad"]), (5, 1))
        bad = [x for x in rec["rows"] if x["ok"] is False][0]
        self.assertEqual(bad["expected"], 14750000)                               # 1,62,500 - 15,000
        self.assertEqual(bad["fix_amount"], {"side": "debit", "amount": 500000})   # balance moved by 5,000 -> amount is 5,000
        self.assertEqual(bad["missing_row"], {"side": "credit", "amount": 1000000})
        self.assertEqual(self.rows()[2]["id"], bad["id"])
        self.assertEqual((rec["opening"], rec["opening_inferred"]), (5000000, False))      # printed on the scan, kept

    def test_correcting_the_row_makes_everything_reconcile(self):
        self.stage()
        bad = self.rows()[2]
        self.b.update_bank_txn(bad["id"], "05-04-2025", "ATM WDL MUMBAI", "5000", "", "157500")
        rec = self.b.reconcile(self.imp)
        self.assertEqual((rec["ok"], rec["bad"]), (6, 0))
        self.assertEqual(self.b.bank_txns(self.bank, import_id=self.imp)[2]["verified"], 1)   # saving a correction verifies it

    def test_verify_reconciled_only_touches_rows_that_add_up(self):
        self.stage()
        n = self.b.verify_reconciled(self.imp)
        self.assertEqual(n, 5)                                                    # all but the misread row
        flags = [r["verified"] for r in self.rows()]
        self.assertEqual(flags, [1, 1, 0, 1, 1, 1])
        self.assertEqual(self.b.bank_imports(self.bank)[0]["unverified"], 1)

    def test_missing_row_is_detected_and_can_be_inserted_in_place(self):
        page = table_page()
        boxes = [x for x in page.boxes if x.text not in ("05/0.4/2025", "05/04/2025", "ATM:WDL MUMBAI", "5,000.00", "1,57,500.00")]
        self.stage(OcrPage(boxes, page.width, page.height))                       # the 5,000 ATM row went missing
        self.b.set_import_balances(self.imp, "50000", "")
        rec = self.b.reconcile(self.imp)
        bad = [x for x in rec["rows"] if x["ok"] is False]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["missing_row"], {"side": "debit", "amount": 500000})
        rows = self.rows()
        self.assertEqual(bad[0]["id"], rows[2]["id"])                              # the row *after* the gap is flagged
        new_id = self.b.add_bank_txn(self.imp, rows[1]["id"], "05-04-2025", "ATM WDL MUMBAI", "5000", "", "157500")
        ordered = [r["id"] for r in self.rows()]
        self.assertEqual(ordered.index(new_id), 2)                                 # sits between rows 2 and 3
        rec = self.b.reconcile(self.imp)
        self.assertEqual((rec["ok"], rec["bad"]), (4, 0))
        added = [r for r in self.rows() if r["id"] == new_id][0]
        self.assertEqual((added["source"], added["verified"]), ("manual", 1))

    def test_opening_and_closing_balances_from_the_statement(self):
        self.stage()
        self.b.verify_bank_txns([r["id"] for r in self.rows()])
        self.b.update_bank_txn(self.rows()[2]["id"], "05-04-2025", "ATM WDL MUMBAI", "5000", "", "157500")
        self.b.set_import_balances(self.imp, "50000", "152894.30")
        rec = self.b.reconcile(self.imp)
        self.assertEqual((rec["opening_inferred"], rec["closing_computed"], rec["closing_diff"]), (False, 15289430, 0))
        self.b.set_import_balances(self.imp, "50000", "152900")
        self.assertEqual(self.b.reconcile(self.imp)["closing_diff"], 15290000 - 15289430)

    # ------------------------------------------------------------------ editing rules
    def test_edit_validation_and_posted_rows_are_locked(self):
        self.stage()
        r = self.rows()[0]
        for bad in (dict(debit="10", credit="10"), dict(debit="", credit=""), dict(debit="-5", credit="")):
            with self.assertRaises(ValueError):
                self.b.update_bank_txn(r["id"], "01-04-2025", "x", bad["debit"], bad["credit"], "")
        with self.assertRaises(ValueError):
            self.b.update_bank_txn(r["id"], "not a date", "x", "10", "", "")
        with self.assertRaises(ValueError):
            self.b.update_bank_txn(r["id"], "01-04-2025", "  ", "10", "", "")
        self.b.update_bank_txn(r["id"], "02-04-2025", "Fixed narration", "", "99.50", "")        # credit, no balance
        r2 = self.rows()[0]
        self.assertEqual((r2["date"], r2["narration"], r2["debit"], r2["credit"], r2["balance"]),
                         ("2025-04-02", "Fixed narration", 0, 9950, None))
        self.b.post_bank_transactions([r["id"]], use_suspense=True)
        for fn in (lambda: self.b.update_bank_txn(r["id"], "02-04-2025", "x", "1", "", ""),
                   lambda: self.b.delete_bank_txns([r["id"]])):
            with self.assertRaises(ValueError):
                fn()

    def test_delete_junk_rows(self):
        self.stage()
        ids = [r["id"] for r in self.rows()]
        self.b.delete_bank_txns(ids[:2])
        self.assertEqual(len(self.rows()), 4)
        with self.assertRaises(ValueError):
            self.b.delete_bank_txns([99999])


class MigrationTest(unittest.TestCase):
    def test_company_file_from_before_ocr_is_upgraded_in_place(self):
        b = Books.create("Pre OCR Co", state="27")
        bank = b.add_ledger("HDFC", "Bank Accounts")
        path = b.path
        b.close()
        c = sqlite3.connect(str(path))
        c.execute("PRAGMA foreign_keys=OFF")
        for t in ("bank_pages", "bank_txns", "bank_imports"):
            c.execute(f"DROP TABLE {t}")
        c.execute("""CREATE TABLE bank_imports (id INTEGER PRIMARY KEY, bank_ledger_id INTEGER NOT NULL,
                     filename TEXT NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE bank_txns (id INTEGER PRIMARY KEY, import_id INTEGER NOT NULL, bank_ledger_id INTEGER NOT NULL,
                     date TEXT NOT NULL, narration TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '', debit INTEGER NOT NULL DEFAULT 0,
                     credit INTEGER NOT NULL DEFAULT 0, balance INTEGER, uncertain INTEGER NOT NULL DEFAULT 0,
                     counter_ledger_id INTEGER, voucher_id INTEGER, fingerprint TEXT NOT NULL UNIQUE)""")
        c.execute("INSERT INTO bank_imports(id, bank_ledger_id, filename) VALUES (1, ?, 'old.pdf')", (bank,))
        c.execute("INSERT INTO bank_txns(id, import_id, bank_ledger_id, date, narration, debit, fingerprint) "
                  "VALUES (7, 1, ?, '2025-04-01', 'OLD ROW', 500, 'fp-old')", (bank,))
        c.execute("PRAGMA user_version = 1")
        c.commit(); c.close()
        b2 = Books(path)
        self.addCleanup(b2.close)
        row = b2.bank_txns(bank)[0]
        self.assertEqual((row["source"], row["verified"], row["ord"], row["page"]), ("pdf", 1, 7, 0))     # old rows stay verified
        self.assertEqual(b2.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(b2.bank_imports(bank)[0]["source"], "pdf")
        self.assertEqual(b2.post_bank_transactions([7], use_suspense=True), (1, 0))                       # and can be posted


if __name__ == "__main__":
    unittest.main()
