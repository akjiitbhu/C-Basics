import io
import os
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

import pdfplumber
from easytally.accounting import Books
from easytally.invoice_pdf import amount_in_words, invoice_pdf


class WordsTests(unittest.TestCase):
    def test_indian_words(self):
        self.assertEqual(amount_in_words(118000), "Rupees One Thousand One Hundred Eighty Only")
        self.assertEqual(amount_in_words(12345678),
                         "Rupees One Lakh Twenty Three Thousand Four Hundred Fifty Six and Seventy Eight Paise Only")
        self.assertEqual(amount_in_words(10 ** 9), "Rupees One Crore Only")
        self.assertEqual(amount_in_words(0), "Rupees Zero Only")
        self.assertEqual(amount_in_words(5), "Rupees Zero and Five Paise Only")
        self.assertEqual(amount_in_words(25000000 * 100), "Rupees Two Crore Fifty Lakh Only")
        self.assertEqual(amount_in_words(2500000 * 100), "Rupees Twenty Five Lakh Only")


class PdfTests(unittest.TestCase):
    def test_invoice_pdf_contents(self):
        b = Books.create("PDF Co", state="27", gstin="")
        self.addCleanup(b.close)
        b.set_settings({"address": "12 MG Road, Pune", "gstin": "27ABCDE1234F1Z5", "bank_details": "HDFC 0001",
                        "terms": "Payment in 15 days"})
        item = b.add_stock_item("Blue <Widget> & Co", hsn="8501", gst_rate=18)
        cust = b.add_ledger("Acme Customer", "Sundry Debtors", gstin="29ABCDE1234F1Z5")
        vid = b.add_invoice("Sales", "05-04-2026", cust, [(item, 2, "500")], ref="INV-77")
        pdf = invoice_pdf(b, vid)
        self.assertTrue(pdf.startswith(b"%PDF"))
        with pdfplumber.open(io.BytesIO(pdf)) as doc:
            text = "\n".join(p.extract_text() for p in doc.pages)
        for needle in ("TAX INVOICE", "INV-77", "05-04-2026", "Acme Customer", "29ABCDE1234F1Z5", "27ABCDE1234F1Z5",
                       "8501", "Blue <Widget> & Co", "IGST", "1,180.00", "One Thousand One Hundred Eighty",
                       "HDFC 0001", "Karnataka"):
            self.assertIn(needle, text)
        cn = b.add_invoice("Credit Note", "06-04-2026", cust, [(item, 1, "500")])
        with pdfplumber.open(io.BytesIO(invoice_pdf(b, cn))) as doc:
            self.assertIn("CREDIT NOTE", doc.pages[0].extract_text())
        with self.assertRaises(ValueError):
            invoice_pdf(b, 99999)


if __name__ == "__main__":
    unittest.main()
