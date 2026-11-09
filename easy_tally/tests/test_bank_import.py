import os
import tempfile
import unittest
from pathlib import Path

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally.accounting import Books
from easytally.bank_import import (parse_statement, check_consistency, StatementError,
                                   amount_to_paise, balance_to_paise)
from tests import pdf_samples as S

EXPECTED = [(r[0], r[1]) for r in S.ROWS]


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.table = cls.dir / "table.pdf"; S.table_pdf(cls.table)
        cls.text = cls.dir / "text.pdf"; S.text_pdf(cls.text)
        cls.blank = cls.dir / "blank.pdf"; S.blank_image_pdf(cls.blank)

    def _check(self, txns):
        self.assertEqual(len(txns), len(S.ROWS))
        self.assertEqual((txns[0].debit, txns[0].credit), (1250000, 0))
        self.assertEqual((txns[1].debit, txns[1].credit), (0, 12500000))
        self.assertEqual(txns[1].date, "2025-04-03")
        self.assertEqual(txns[-1].credit, 41200)
        self.assertEqual(txns[-1].balance, 15289430)
        self.assertEqual(check_consistency(txns), [])
        # two identical ATM withdrawals on the same day must both survive
        self.assertEqual(sum(1 for t in txns if "ATM" in t.narration), 2)

    def test_table_mode(self):
        self._check(parse_statement(self.table))

    def test_text_mode_uses_balance_to_find_direction(self):
        txns = parse_statement(self.text)
        self._check(txns)
        self.assertIn("Sept invoice", txns[0].narration)      # continuation line joined
        self.assertNotIn("Page", txns[-1].narration)          # footer not glued on

    def test_scanned_pdf_rejected(self):
        with self.assertRaises(StatementError) as cm:
            parse_statement(self.blank)
        self.assertIn("scanned", str(cm.exception))

    def test_amount_helpers(self):
        self.assertEqual(amount_to_paise("1,25,000.00"), 12500000)
        self.assertEqual(amount_to_paise("(250.50)"), -25050)
        self.assertIsNone(amount_to_paise(""))
        self.assertEqual(balance_to_paise("500.00 Dr"), -50000)
        self.assertEqual(balance_to_paise("500.00 Cr"), 50000)


class ImportFlowTests(unittest.TestCase):
    def setUp(self):
        self.b = Books.create(f"Flow {self.id().split('.')[-1]}")
        self.addCleanup(self.b.close)
        self.bank = self.b.add_ledger("HDFC Bank", "Bank Accounts", opening="50000")
        self.b.add_ledger("Capital", "Capital Account", opening="50000", side="Cr")
        self.dir = Path(tempfile.mkdtemp())
        self.pdf = self.dir / "s.pdf"; S.table_pdf(self.pdf)

    def test_stage_dedupe_post(self):
        txns = parse_statement(self.pdf)
        new, dup = self.b.stage_bank_transactions(self.bank, str(self.pdf), txns)
        self.assertEqual((new, dup), (6, 0))
        # importing the same file again adds nothing
        self.assertEqual(self.b.stage_bank_transactions(self.bank, str(self.pdf), txns), (0, 6))
        rows = self.b.bank_txns(self.bank)
        by_narr = {r["narration"]: r for r in rows}
        # built-in rules suggested ledgers
        self.assertEqual(by_narr["SMS CHARGES Q1"]["counter_name"], "Bank Charges")
        self.assertEqual(by_narr["INTEREST CREDIT"]["counter_name"], "Interest Received")
        self.assertIsNone(by_narr["UPI/ACME TRADERS/payment"]["counter_name"])

        # unmapped rows are skipped unless suspense is allowed
        posted, skipped = self.b.post_bank_transactions([r["id"] for r in rows])
        self.assertEqual((posted, skipped), (4, 2))
        # teach a rule from one row; it fills the other unmapped row too
        acme = self.b.add_ledger("Acme Traders", "Sundry Creditors")
        glob = self.b.add_ledger("Global Solutions", "Sundry Debtors")
        self.b.set_txn_ledger(by_narr["UPI/ACME TRADERS/payment"]["id"], acme, learn_keyword="ACME TRADERS")
        self.b.set_txn_ledger(by_narr["NEFT CR-GLOBAL SOLUTIONS LTD"]["id"], glob)
        posted, skipped = self.b.post_bank_transactions([r["id"] for r in self.b.bank_txns(self.bank, pending_only=True)])
        self.assertEqual((posted, skipped), (2, 0))

        # bank ledger now equals the statement's closing balance
        bal = self.b.balances()
        self.assertEqual(bal[self.bank], 15289430)
        self.assertEqual(sum(bal.values()), 0)
        # posting twice is a no-op
        self.assertEqual(self.b.post_bank_transactions([r["id"] for r in self.b.bank_txns(self.bank)]), (0, 6))

    def test_suspense(self):
        txns = parse_statement(self.pdf)
        self.b.stage_bank_transactions(self.bank, str(self.pdf), txns)
        ids = [r["id"] for r in self.b.bank_txns(self.bank)]
        posted, _ = self.b.post_bank_transactions(ids, use_suspense=True)
        self.assertEqual(posted, 6)
        self.assertNotEqual(self.b.balances()[self.b.ledger_id("Suspense A/c")], 0)

    def test_deleting_voucher_releases_row(self):
        txns = parse_statement(self.pdf)
        self.b.stage_bank_transactions(self.bank, str(self.pdf), txns)
        ids = [r["id"] for r in self.b.bank_txns(self.bank)]
        self.b.post_bank_transactions(ids, use_suspense=True)
        v = self.b.vouchers()[0]
        self.b.delete_voucher(v["id"])
        self.assertEqual(len(self.b.bank_txns(self.bank, pending_only=True)), 1)


if __name__ == "__main__":
    unittest.main()
