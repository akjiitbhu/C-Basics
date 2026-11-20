import os
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally.accounting import Books
from easytally.invoice_pdf import invoice_html
from tests.test_web import Client, WebBase


class InvoiceHtml(unittest.TestCase):
    def setUp(self):
        self.b = Books.create(f"Html {self.id().split('.')[-1]}", state="27")
        self.addCleanup(self.b.close)
        self.b.set_settings({"gstin": "27ABCDE1234F1Z5", "address": "12 MG Road, Pune", "bank_details": "HDFC 0001",
                             "terms": "Payment in 15 days"})
        self.item = self.b.add_stock_item("Widget", hsn="8501", gst_rate=18)
        self.cust = self.b.add_ledger("Acme Customer", "Sundry Debtors", gstin="29AAAAA0000A1Z5")

    def test_content_matches_the_pdf(self):
        vid = self.b.add_invoice("Sales", "05-04-2026", self.cust,
                                 [(self.item, 2, "500", 5), (self.item, 1, "1000", 18)], ref="TI-1")
        html = invoice_html(self.b, vid)
        for needle in ("TAX INVOICE", "GST Sale Invoice", "Tax Invoice (GST Sale)", "TI-1", "05-04-2026",
                       "Acme Customer", "29AAAAA0000A1Z5", "27ABCDE1234F1Z5", "8501", "5%", "18%", "IGST",
                       "230.00", "2,230.00", "Two Thousand Two Hundred Thirty", "HDFC 0001", "Payment in 15 days",
                       "Karnataka", "Authorised signatory"):
            self.assertIn(needle, html)
        self.assertNotIn("CGST", html)                                    # inter-state: IGST only
        self.assertIn("@page", html)                                      # sized and margined for printing

    def test_intra_state_shows_cgst_and_sgst(self):
        local = self.b.add_ledger("Local", "Sundry Debtors", state="27")
        html = invoice_html(self.b, self.b.add_invoice("Sales", "05-04-2026", local, [(self.item, 1, "1000")]))
        self.assertIn("CGST", html)
        self.assertIn("SGST", html)
        self.assertNotIn("IGST", html)

    def test_credit_note_is_titled_as_one(self):
        vid = self.b.add_invoice("Credit Note", "06-04-2026", self.cust, [(self.item, 1, "100")])
        html = invoice_html(self.b, vid)
        self.assertIn("CREDIT NOTE", html)
        self.assertNotIn("TAX INVOICE", html)

    def test_hostile_names_are_escaped(self):
        evil_item = self.b.add_stock_item("<script>alert(1)</script>", hsn='"><b>x', gst_rate=5)
        evil_party = self.b.add_ledger('"><img src=x onerror=alert(2)>', "Sundry Debtors")
        vid = self.b.add_invoice("Sales", "05-04-2026", evil_party, [(evil_item, 1, "10")],
                                 narration="<svg onload=alert(3)>", ref="<i>REF</i>")
        html = invoice_html(self.b, vid)
        for raw in ("<script>alert(1)", "<img src=x", "<svg onload", "<i>REF</i>", '"><b>x'):
            self.assertNotIn(raw, html)
        for shown in ("&lt;script&gt;alert(1)&lt;/script&gt;", "&lt;img src=x onerror=alert(2)&gt;",
                      "&lt;svg onload=alert(3)&gt;", "&lt;i&gt;REF&lt;/i&gt;"):
            self.assertIn(shown, html)

    def test_unknown_invoice(self):
        with self.assertRaises(ValueError):
            invoice_html(self.b, 99999)


class InvoiceHtmlOverHttp(WebBase):
    def test_served_inline_with_locked_down_policy_and_needs_sign_in(self):
        self.admin(); self.company()
        item = self.c.j("/api/items", {"name": "Widget", "gst_rate": 18})[1]["id"]
        cust = self.c.j("/api/ledgers", {"name": "Cust", "group": "Sundry Debtors"})[1]["id"]
        vid = self.c.j("/api/invoices", {"kind": "Sales", "date": "2026-04-05", "party_id": cust,
                                         "lines": [{"item_id": item, "qty": "1", "rate": "100"}]})[1]["id"]
        s, body = self.c.call(f"/api/invoice.html?id={vid}")
        self.assertEqual(s, 200)
        h = self.c.last_headers
        self.assertTrue(h["Content-Type"].startswith("text/html"))
        self.assertTrue(h["Content-Disposition"].startswith("inline"))
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])        # no scripts could run in it anyway
        self.assertIn(b"TAX INVOICE", body)
        self.assertEqual(Client(self.base).call(f"/api/invoice.html?id={vid}")[0], 401)
        self.assertEqual(self.c.call("/api/invoice.html?id=99999")[0], 400)


if __name__ == "__main__":
    unittest.main()
