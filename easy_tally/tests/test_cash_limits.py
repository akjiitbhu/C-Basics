import os
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally import reports, tally_xml
from easytally.accounting import Books, CashLimitError
from easytally.bank_import import Txn


class CashLimitTests(unittest.TestCase):
    def setUp(self):
        self.b = Books.create(f"Cash {self.id().split('.')[-1]}", state="27")
        self.addCleanup(self.b.close)
        self.cash = self.b.ledger_id("Cash")
        self.acme = self.b.add_ledger("Acme", "Sundry Creditors")
        self.ravi = self.b.add_ledger("Ravi", "Sundry Debtors")
        self.bank = self.b.add_ledger("HDFC", "Bank Accounts")

    def pay(self, amount, party=None, date="05-04-2026", **kw):
        return self.b.add_voucher("Payment", date, [(party or self.acme, "Dr", amount), (self.cash, "Cr", amount)], **kw)

    def receive(self, amount, party=None, date="05-04-2026", **kw):
        return self.b.add_voucher("Receipt", date, [(self.cash, "Dr", amount), (party or self.ravi, "Cr", amount)], **kw)

    def test_defaults_are_the_legal_limits(self):
        self.assertEqual(self.b.cash_limits(), (1_000_000, 19_999_999, "warn"))

    def test_payment_limit(self):
        self.pay("10000")                                                  # exactly at the limit is fine
        with self.assertRaises(CashLimitError) as cm:
            self.pay("10000.01")
        self.assertFalse(cm.exception.blocked)
        self.assertIn("Cash payment of 10,000.01 to Acme is above the limit of 10,000.00", cm.exception.warnings[0])
        self.assertEqual(len(self.b.vouchers()), 1)                        # nothing saved by the failed attempt
        self.pay("25000", cash_check="confirmed")                          # user agreed to the warning
        self.assertEqual(len(self.b.vouchers()), 2)

    def test_receipt_limit_flags_two_lakh_or_more(self):
        self.receive("199999.99")
        with self.assertRaises(CashLimitError) as cm:
            self.receive("200000")
        self.assertIn("Cash receipt of 2,00,000.00 from Ravi", cm.exception.warnings[0])

    def test_modes_and_custom_limits(self):
        self.b.set_settings({"cash_payment_limit": "5000"})
        with self.assertRaises(CashLimitError):
            self.pay("5000.50")
        self.b.set_settings({"cash_limit_mode": "block"})
        with self.assertRaises(CashLimitError) as cm:
            self.pay("6000", cash_check="confirmed")                       # confirmation does not override a block
        self.assertTrue(cm.exception.blocked)
        self.b.set_settings({"cash_limit_mode": "off"})
        self.pay("999999")                                                 # checking switched off
        with self.assertRaises(ValueError):
            self.b.set_settings({"cash_limit_mode": "sometimes"})
        with self.assertRaises(ValueError):
            self.b.set_settings({"cash_receipt_limit": "-5"})
        self.b.set_settings({"cash_payment_limit": ""})                    # blank falls back to the default
        self.assertEqual(self.b.cash_limits()[0], 1_000_000)

    def test_own_account_transfers_are_not_flagged(self):
        self.b.add_voucher("Contra", "05-04-2026", [(self.bank, "Dr", 900000), (self.cash, "Cr", 900000)])   # deposit
        self.b.add_voucher("Contra", "06-04-2026", [(self.cash, "Dr", 900000), (self.bank, "Cr", 900000)])   # withdrawal
        self.assertEqual(reports.cash_exceptions(self.b, "2026-04-01", "2026-04-30").rows[0][0][:2], "No")

    def test_same_day_total_to_one_party(self):
        self.pay("6000")
        with self.assertRaises(CashLimitError) as cm:
            self.pay("6000")                                               # each is small, together over 10,000
        self.assertIn("total 12,000.00", cm.exception.warnings[0])
        self.pay("6000", date="06-04-2026")                                # different day: fine
        self.b.add_voucher("Payment", "05-04-2026", [(self.ravi, "Dr", 6000), (self.cash, "Cr", 6000)])  # other party
        vid = self.pay("3000", date="07-04-2026")
        self.b.update_voucher(vid, "07-04-2026", [(self.acme, "Dr", 9000), (self.cash, "Cr", 9000)])    # edit excludes itself
        with self.assertRaises(CashLimitError):
            self.b.update_voucher(vid, "05-04-2026", [(self.acme, "Dr", 6000), (self.cash, "Cr", 6000)])  # moved onto 5 April

    def test_bank_rows_and_tally_import(self):
        # a bank-statement row posts without prompting; bank<->cash rows are own-account transfers and never flagged
        self.b.add_ledger("Capital", "Capital Account", opening="1000000", side="Cr")
        t = Txn("2026-04-05", "CASH DEPOSIT BY CUSTOMER", credit=30_000_000, balance=None)   # Rs 3 lakh into the bank
        self.b.stage_bank_transactions(self.bank, "s.pdf", [t])
        ids = [x["id"] for x in self.b.bank_txns(self.bank)]
        self.b.set_txn_ledger(ids[0], self.ravi)
        self.assertEqual(self.b.post_bank_transactions(ids), (1, 0))
        # Tally import is historic data: never blocked, but it shows up in the exceptions report
        self.b.set_settings({"cash_limit_mode": "block"})
        xml = ("<ENVELOPE><VOUCHER VCHTYPE='Payment'><DATE>20260410</DATE>"
               "<LEDGERENTRIES.LIST><LEDGERNAME>Acme</LEDGERNAME><AMOUNT>-50000</AMOUNT></LEDGERENTRIES.LIST>"
               "<LEDGERENTRIES.LIST><LEDGERNAME>Cash</LEDGERNAME><AMOUNT>50000</AMOUNT></LEDGERENTRIES.LIST>"
               "</VOUCHER></ENVELOPE>").replace("'", '"')
        self.assertEqual(tally_xml.import_xml(self.b, xml.encode())["vouchers"], 1)
        rep = reports.cash_exceptions(self.b, "2026-04-01", "2026-04-30")
        self.assertEqual([(r[4], r[5], r[6]) for r in rep.rows], [("Payment", "Acme", "50,000.00")])

    def test_reports_show_red(self):
        v_big = self.pay("25000", cash_check="confirmed")
        self.pay("500", date="06-04-2026")
        db = reports.day_book(self.b, "2026-04-01", "2026-04-30")
        red = {tuple(db.rows[i][3:6]): db.red[i] for i in range(len(db.red)) if db.red[i]}
        self.assertEqual(red, {("Cash", "", "25,000.00"): [5]})            # only the big cash credit, only that cell
        led = reports.ledger_statement(self.b, self.cash, "2026-04-01", "2026-04-30")
        flagged = [(led.rows[i][4:6], led.red[i]) for i in range(len(led.red)) if led.red[i]]
        self.assertEqual(flagged, [(["", "25,000.00"], [5])])
        ex = reports.cash_exceptions(self.b, "2026-04-01", "2026-04-30")
        self.assertEqual(len(ex.rows), 1)
        self.assertEqual(ex.red[0], list(range(len(ex.columns))))          # whole row red
        self.assertTrue(self.b.voucher_cash_over(v_big))
        self.assertFalse(self.b.voucher_cash_over(self.b.vouchers()[1]["id"]))
        # day total row
        self.pay("6000", date="08-04-2026", cash_check="confirmed")
        self.pay("6000", date="08-04-2026", cash_check="confirmed")
        kinds = [r[1] for r in reports.cash_exceptions(self.b, "2026-04-01", "2026-04-30").rows]
        self.assertEqual(kinds.count("Day total"), 1)


if __name__ == "__main__":
    unittest.main()
