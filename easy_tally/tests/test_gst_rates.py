import io
import json
import os
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

import pdfplumber
from easytally import gst, gst_returns
from easytally.accounting import Books
from easytally.invoice_pdf import invoice_pdf
from tests.test_web import WebBase


def books(testcase, **kw):
    b = Books.create(f"Rates {testcase.id().split('.')[-1]}", **{"state": "27", **kw})
    testcase.addCleanup(b.close)
    return b


class SlabTests(unittest.TestCase):
    def test_only_current_slabs_are_offered(self):
        self.assertEqual(gst.GST_RATES, [0, 5, 18])

    def test_new_items_accept_only_current_slabs(self):
        b = books(self)
        for n, ok in enumerate((0, 5, 18, "5")):                                       # "5" arrives as text from a web form
            b.add_stock_item(f"Item {n}", gst_rate=ok)
        for bad in (12, 28, 7, "abc", None, -5):
            with self.assertRaises(ValueError) as cm:
                b.add_stock_item("Bad", gst_rate=bad)
            self.assertIn("0, 5 or 18", str(cm.exception))
        self.assertEqual(len(b.stock_items()), 4)

    def test_item_rate_can_be_changed_and_old_invoices_keep_theirs(self):
        b = books(self)
        item = b.add_stock_item("Widget", gst_rate=18)
        cust = b.add_ledger("Cust", "Sundry Debtors", state="27")
        old = b.add_invoice("Sales", "01-04-2026", cust, [(item, 1, "1000")])
        b.update_stock_item(item, gst_rate=5, hsn="8501")
        new = b.add_invoice("Sales", "02-04-2026", cust, [(item, 1, "1000")])
        rate = lambda vid: b.conn.execute("SELECT gst_rate FROM invoice_lines WHERE voucher_id=?", (vid,)).fetchone()[0]
        self.assertEqual((rate(old), rate(new)), (18, 5))
        self.assertEqual(b.stock_items()[0]["hsn"], "8501")
        with self.assertRaises(ValueError):
            b.update_stock_item(item, gst_rate=12)
        with self.assertRaises(ValueError):
            b.update_stock_item(9999, gst_rate=5)
        b.add_stock_item("Other")
        with self.assertRaises(ValueError):
            b.update_stock_item(item, name="Other")                            # duplicate name


class PerLineRateTests(unittest.TestCase):
    def setUp(self):
        self.b = books(self)
        self.item = self.b.add_stock_item("Widget", hsn="8501", gst_rate=18)
        self.cust = self.b.add_ledger("Cust", "Sundry Debtors", gstin="27AAAAA0000A1Z5")

    def test_line_rate_overrides_the_item_default(self):
        inv = self.b.preview_invoice("Sales", self.cust, [(self.item, 1, "1000", 5), (self.item, 1, "1000", None),
                                                          (self.item, 1, "1000", 0), (self.item, 1, "1000", "")])
        self.assertEqual([l["gst_rate"] for l in inv["lines"]], [5, 18, 0, 18])
        self.assertEqual((inv["cgst"], inv["sgst"]), (2500 + 9000 + 0 + 9000, 2500 + 9000 + 0 + 9000))
        self.assertEqual(self.b.stock_items()[0]["gst_rate"], 18)             # the item itself is untouched

    def test_only_current_slabs_for_a_line(self):
        for bad in (12, 28, 3):
            with self.assertRaises(ValueError):
                self.b.preview_invoice("Sales", self.cust, [(self.item, 1, "100", bad)])

    def test_legacy_item_rate_still_works_but_cannot_be_chosen_again(self):
        cur = self.b.conn.execute("INSERT INTO stock_items(name, gst_rate) VALUES ('Old stock', 12)")
        self.b.conn.commit()
        old = cur.lastrowid
        inv = self.b.preview_invoice("Sales", self.cust, [(old, 1, "1000")])                # its own stored 12% is allowed
        self.assertEqual(inv["lines"][0]["gst_rate"], 12)
        self.assertEqual(self.b.preview_invoice("Sales", self.cust, [(old, 1, "1000", 18)])["lines"][0]["gst_rate"], 18)
        with self.assertRaises(ValueError):
            self.b.preview_invoice("Sales", self.cust, [(old, 1, "1000", 28)])

    def test_saved_invoice_keeps_line_rates_through_edit_pdf_and_gst_return(self):
        vid = self.b.add_invoice("Sales", "05-04-2026", self.cust, [(self.item, 2, "500", 5), (self.item, 1, "1000", 18)],
                                 ref="TI-1")
        v = self.b.get_voucher(vid)
        self.assertEqual([l["gst_rate"] for l in v["invoice"]["lines"]], [5, 18])
        # edit: keep the first line at 5%, drop the second, and the rate stays
        self.b.update_invoice(vid, "05-04-2026", self.cust, [(self.item, 3, "500", 5)], ref="TI-1")
        self.assertEqual([l["gst_rate"] for l in self.b.get_voucher(vid)["invoice"]["lines"]], [5])
        self.assertEqual(sum(self.b.balances().values()), 0)
        data, _ = gst_returns.gstr1(self.b, 2026, 4)
        rates = [i["itm_det"]["rt"] for x in data["b2b"] for inv in x["inv"] for i in inv["itms"]]
        self.assertEqual(rates, [5])
        with pdfplumber.open(io.BytesIO(invoice_pdf(self.b, vid))) as doc:
            text = doc.pages[0].extract_text()
        self.assertIn("5%", text)
        self.assertNotIn("18%", text)


class TaxInvoiceNaming(unittest.TestCase):
    def test_sale_is_titled_tax_invoice_gst_sale(self):
        b = books(self)
        item = b.add_stock_item("Widget", gst_rate=18)
        cust = b.add_ledger("Cust", "Sundry Debtors")
        sale = b.add_invoice("Sales", "05-04-2026", cust, [(item, 1, "100")], ref="S-1")
        note = b.add_invoice("Credit Note", "06-04-2026", cust, [(item, 1, "100")])
        with pdfplumber.open(io.BytesIO(invoice_pdf(b, sale))) as doc:
            text = doc.pages[0].extract_text()
        for needle in ("TAX INVOICE", "GST Sale Invoice", "Tax Invoice (GST Sale)"):
            self.assertIn(needle, text)
        with pdfplumber.open(io.BytesIO(invoice_pdf(b, note))) as doc:
            text = doc.pages[0].extract_text()
        self.assertIn("CREDIT NOTE", text)
        self.assertNotIn("TAX INVOICE", text)


class GstAtCompanyCreation(unittest.TestCase):
    def test_gst_ledgers_exist_from_the_start(self):
        b = books(self)
        names = {l["name"]: l["group_name"] for l in b.ledgers()}
        for led in ("Output CGST", "Output SGST", "Output IGST", "Input CGST", "Input SGST", "Input IGST"):
            self.assertEqual(names[led], "Duties & Taxes")
        self.assertIn("Sales", names)
        self.assertIn("Purchase", names)

    def test_gstin_is_checked_and_state_derived_when_company_is_created(self):
        b = books(self, state="", gstin="29abcde1234f1z5")
        self.assertEqual((b.setting("gstin"), b.setting("state")), ("29ABCDE1234F1Z5", "29"))
        with self.assertRaises(ValueError):
            Books.create("Bad GSTIN Co", state="27", gstin="123")
        with self.assertRaises(ValueError) as cm:
            Books.create("Mismatch Co", state="27", gstin="29ABCDE1234F1Z5")
        self.assertIn("state code", str(cm.exception))
        self.assertFalse(any(n == "Mismatch Co" for _, n in Books.list_companies()))     # nothing half-created

    def test_company_details_and_party_ledgers_keep_gstin_and_state_consistent(self):
        b = books(self)
        b.set_settings({"gstin": "27ABCDE1234F1Z5"})
        with self.assertRaises(ValueError):
            b.set_settings({"state": "29"})                                            # GSTIN says 27
        with self.assertRaises(ValueError):
            b.add_ledger("Wrong state", "Sundry Debtors", gstin="29AAAAA0000A1Z5", state="27")
        self.assertEqual(b.get_ledger(b.add_ledger("Fine", "Sundry Debtors", gstin="29AAAAA0000A1Z5"))["state"], "29")


class RatesOverApi(WebBase):
    def setUp(self):
        super().setUp()
        self.admin(); self.company()
        self.item = self.c.j("/api/items", {"name": "Widget", "hsn": "8501", "gst_rate": 18})[1]["id"]
        self.cust = self.c.j("/api/ledgers", {"name": "Cust", "group": "Sundry Debtors", "state": "27"})[1]["id"]

    def test_meta_offers_only_current_slabs(self):
        self.assertEqual(self.c.j("/api/meta")[1]["gst_rates"], [0, 5, 18])

    def test_item_rate_rules_over_http(self):
        self.assertEqual(self.c.j("/api/items", {"name": "Old", "gst_rate": 12})[0], 400)
        self.assertEqual(self.c.j("/api/items/update", {"id": self.item, "gst_rate": 5})[0], 200)
        self.assertEqual([i["gst_rate"] for i in self.c.j("/api/items")[1]], [5])
        self.assertEqual(self.c.j("/api/items/update", {"id": self.item, "gst_rate": 28})[0], 400)
        self.assertEqual(self.c.j("/api/audit")[1][0]["action"], "Stock item changed")

    def test_invoice_lines_carry_their_own_rate(self):
        lines = [{"item_id": self.item, "qty": "2", "rate": "500", "gst_rate": 5},
                 {"item_id": self.item, "qty": "1", "rate": "1000"}]
        s, p = self.c.j("/api/invoices/preview", {"kind": "Sales", "party_id": self.cust, "lines": lines})
        self.assertEqual((s, [l["gst_rate"] for l in p["lines"]], p["total"]), (200, [5, 18], "2,230.00"))
        s, r = self.c.j("/api/invoices", {"kind": "Sales", "date": "2026-04-05", "party_id": self.cust, "ref": "TI-9", "lines": lines})
        self.assertEqual(s, 200)
        v = self.c.j(f"/api/voucher?id={r['id']}")[1]
        self.assertEqual([l["gst_rate"] for l in v["invoice"]["lines"]], [5, 18])
        bad = [{"item_id": self.item, "qty": "1", "rate": "10", "gst_rate": 12}]
        self.assertEqual(self.c.j("/api/invoices/preview", {"kind": "Sales", "party_id": self.cust, "lines": bad})[0], 400)

    def test_pdf_is_shown_inline_unless_a_download_is_asked_for(self):
        r = self.c.j("/api/invoices", {"kind": "Sales", "date": "2026-04-05", "party_id": self.cust,
                                       "lines": [{"item_id": self.item, "qty": "1", "rate": "100"}]})[1]["id"]
        self.assertEqual(self.c.call(f"/api/invoice.pdf?id={r}")[0], 200)
        self.assertTrue(self.c.last_headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(self.c.call(f"/api/invoice.pdf?id={r}&download=1")[0], 200)
        self.assertTrue(self.c.last_headers["Content-Disposition"].startswith("attachment"))


if __name__ == "__main__":
    unittest.main()
