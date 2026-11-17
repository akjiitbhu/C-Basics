import os
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally import tally_xml as T
from easytally.accounting import Books


def new(testcase, label):
    b = Books.create(f"Tally {label} {testcase.id().split('.')[-1]}", state="27")
    testcase.addCleanup(b.close)
    return b


def named_balances(b):
    bal = b.balances()
    return {l["name"]: bal[l["id"]] for l in b.ledgers() if bal[l["id"]]}


class TallyXmlTests(unittest.TestCase):
    def test_round_trip(self):
        a = new(self, "A")
        item = a.add_stock_item("W", gst_rate=18)
        cust = a.add_ledger("Acme", "Sundry Debtors", opening="500", gstin="29AAAAA0000A1Z5")
        bank = a.add_ledger("HDFC", "Bank Accounts", opening="10000")
        a.add_ledger("Capital", "Capital Account", opening="10500", side="Cr")
        a.add_invoice("Sales", "05-04-2026", cust, [(item, 2, "500")], ref="INV-1")
        a.add_voucher("Receipt", "10-04-2026", [(bank, "Dr", 700), (cust, "Cr", 700)], "part payment & <thanks>")
        masters, vouchers = T.export_masters(a), T.export_vouchers(a)
        self.assertIn(b"<AMOUNT>-1180.00</AMOUNT>", vouchers)                 # debit is negative in Tally XML
        self.assertIn(b"<PARTYGSTIN>29AAAAA0000A1Z5</PARTYGSTIN>", masters)

        b = new(self, "B")
        r1 = T.import_xml(b, masters)
        self.assertGreaterEqual(r1["ledgers"], 3)
        r2 = T.import_xml(b, vouchers)
        self.assertEqual((r2["vouchers"], r2["duplicates"], r2["skipped"]), (2, 0, 0))
        self.assertEqual(named_balances(b), named_balances(a))                # identical books
        self.assertEqual(b.get_ledger(b.ledger_id("Acme"))["state"], "29")
        self.assertEqual(b.get_ledger(b.ledger_id("Acme"))["opening"], 50000)  # opening Dr survived the sign flip
        # importing again changes nothing
        r3 = T.import_xml(b, vouchers)
        self.assertEqual((r3["vouchers"], r3["duplicates"]), (0, 2))
        self.assertEqual(named_balances(b), named_balances(a))
        self.assertTrue(any(v["narration"] == "part payment & <thanks>" for v in b.vouchers()))

    TALLY_SAMPLE = """<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <GROUP NAME="Dealers" ACTION="Create"><NAME>Dealers</NAME><PARENT>Sundry Debtors</PARENT></GROUP>
      </TALLYMESSAGE>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <LEDGER NAME="Ravi &amp; Sons" ACTION="Create"><PARENT>Dealers</PARENT><OPENINGBALANCE>-2500.00</OPENINGBALANCE>
          <LEDSTATENAME>Gujarat</LEDSTATENAME></LEDGER>
        <LEDGER NAME="State Bank"><PARENT>Bank Accounts</PARENT><OPENINGBALANCE>-9000</OPENINGBALANCE></LEDGER>
        <LEDGER NAME="Weird"><PARENT>Some Unknown Group</PARENT><PARTYGSTIN>BADGSTIN</PARTYGSTIN></LEDGER>
        <LEDGER NAME="Opening Capital"><PARENT>Capital Account</PARENT><OPENINGBALANCE>11500.00</OPENINGBALANCE></LEDGER>
      </TALLYMESSAGE>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <VOUCHER VCHTYPE="Receipt" ACTION="Create"><DATE>20260412</DATE><VOUCHERNUMBER>7</VOUCHERNUMBER>
          <NARRATION>Cheque &#4; received</NARRATION>
          <ALLLEDGERENTRIES.LIST><LEDGERNAME>State Bank</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-1000.00</AMOUNT></ALLLEDGERENTRIES.LIST>
          <ALLLEDGERENTRIES.LIST><LEDGERNAME>Ravi &amp; Sons</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>1000.00</AMOUNT></ALLLEDGERENTRIES.LIST>
        </VOUCHER>
        <VOUCHER VCHTYPE="Journal"><DATE>20260413</DATE><ISCANCELLED>Yes</ISCANCELLED>
          <LEDGERENTRIES.LIST><LEDGERNAME>State Bank</LEDGERNAME><AMOUNT>-5.00</AMOUNT></LEDGERENTRIES.LIST>
          <LEDGERENTRIES.LIST><LEDGERNAME>Ravi &amp; Sons</LEDGERNAME><AMOUNT>5.00</AMOUNT></LEDGERENTRIES.LIST></VOUCHER>
        <VOUCHER VCHTYPE="Payment"><DATE>20260414</DATE>
          <LEDGERENTRIES.LIST><LEDGERNAME>State Bank</LEDGERNAME><AMOUNT>300.00</AMOUNT></LEDGERENTRIES.LIST>
          <LEDGERENTRIES.LIST><LEDGERNAME>Not In Masters</LEDGERNAME><AMOUNT>-300.00</AMOUNT></LEDGERENTRIES.LIST></VOUCHER>
        <VOUCHER VCHTYPE="Journal"><DATE>20260415</DATE>
          <LEDGERENTRIES.LIST><LEDGERNAME>State Bank</LEDGERNAME><AMOUNT>-5.00</AMOUNT></LEDGERENTRIES.LIST>
          <LEDGERENTRIES.LIST><LEDGERNAME>Ravi &amp; Sons</LEDGERNAME><AMOUNT>4.00</AMOUNT></LEDGERENTRIES.LIST></VOUCHER>
      </TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""

    def test_real_world_tally_file(self):
        b = new(self, "C")
        r = T.import_xml(b, self.TALLY_SAMPLE.encode("utf-16"))               # Tally often exports UTF-16
        self.assertEqual((r["groups"], r["ledgers"], r["vouchers"], r["skipped"]), (1, 4, 2, 2))
        text = " ".join(r["warnings"])
        self.assertIn("Suspense", text)                                       # unknown group and unknown ledger
        self.assertIn("GSTIN 'BADGSTIN'", text)
        self.assertIn("not balanced", text)
        bal = named_balances(b)
        self.assertEqual(bal["Ravi & Sons"], 250000 - 100000)                 # opening 2500 Dr, receipt 1000 Cr
        self.assertEqual(bal["State Bank"], 900000 + 100000 - 30000)          # + receipt Dr, - payment Cr
        self.assertEqual(bal["Not In Masters"], 30000)
        self.assertEqual(b.get_ledger(b.ledger_id("Ravi & Sons"))["group_name"], "Dealers")
        self.assertEqual(b.get_ledger(b.ledger_id("Ravi & Sons"))["state"], "24")
        self.assertEqual(b.get_ledger(b.ledger_id("Weird"))["group_name"], "Suspense A/c")
        self.assertEqual(sum(b.balances().values()), 0)
        v = [x for x in b.vouchers() if x["vtype"] == "Receipt"][0]
        self.assertEqual((v["ref"], v["narration"]), ("Tally #7", "Cheque  received"))

    def test_rejects_entities_and_garbage(self):
        b = new(self, "D")
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ENVELOPE>&a;</ENVELOPE>'
        with self.assertRaises(ValueError):
            T.import_xml(b, bomb)
        with self.assertRaises(ValueError):
            T.import_xml(b, bomb.decode().encode("utf-16"))                    # same attack, UTF-16 encoded
        with self.assertRaises(ValueError):
            T.import_xml(b, b"this is not xml")
        self.assertEqual(T.import_xml(b, b"<ENVELOPE/>")["vouchers"], 0)


if __name__ == "__main__":
    unittest.main()
