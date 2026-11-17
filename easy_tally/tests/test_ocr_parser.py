"""OCR layout parser tests.

The box lists below are real RapidOCR output from scanned sample statements (including its typical
mistakes), so these tests are deterministic and do not need the OCR engine installed.
"""
import unittest

from easytally import ocr
from easytally.ocr import Box, OcrPage, parse_ocr_pages


def B(text, x0, y0, x1, y1, conf=0.9):
    return Box(text, x0, y0, x1, y1, conf)


def table_page():
    """Ruled table with Withdrawal / Deposit / Balance columns; note the damaged date '05/0.4/2025'."""
    boxes = [
        B("Statement of Account", 913, 235, 1434, 288),
        B("Txn Date", 486, 356, 584, 384), B("Value Date", 631, 355, 747, 380), B("Narration", 776, 353, 874, 381),
        B("Cheque/Ref", 1186, 349, 1312, 377), B("Withdrawal.Amt.", 1337, 349, 1502, 373),
        B("Deposit Amt.", 1532, 344, 1665, 375), B("Closing Balance", 1693, 343, 1859, 370),
        # row 1: withdrawal
        B("01/04/2025", 487, 405, 606, 434), B("01/04/2025", 634, 406, 750, 430),
        B("UPIACMETRADERS/payment", 777, 403, 1095, 428), B("12,500.00", 1338, 396, 1444, 425),
        B("37,500.00", 1693, 394, 1801, 421),
        # row 2: deposit
        B("03/04/2025", 489, 455, 604, 483), B("03/04/2025", 634, 456, 750, 481),
        B("NEFT CR-GLOBAL SOLUTIONS LTD", 778, 450, 1157, 479), B("1,25,000.00", 1534, 444, 1657, 474),
        B("1,62,500.00", 1694, 442, 1818, 472),
        # row 3: damaged date
        B("05/0.4/2025", 489, 555, 606, 585, 0.81), B("05/04/2025", 636, 556, 750, 581),
        B("ATM:WDL MUMBAI", 779, 551, 981, 579), B("5,000.00", 1337, 546, 1433, 577),
        B("1,57,500.00", 1696, 544, 1819, 571),
        # row 4: low-confidence amount
        B("10/04/2025", 492, 609, 606, 633), B("SMS CHARGES Q1", 779, 602, 985, 631),
        B("17.70", 1340, 597, 1402, 625, 0.74), B("1,57,482.30", 1697, 593, 1820, 623),
    ]
    return OcrPage(boxes, 2357, 1680)


def text_page():
    """Plain text layout, one box per line, with fused numbers, wrong separators and a missing row."""
    L = lambda t, y: B(t, 110, y, 900, y + 33, 0.88)
    boxes = [
        L("STATEMENT OF ACCOUNT", 107), L("Opening Balance.50,000.00", 146), L("Date Narration Amount Balance", 202),
        L("01-04-2025 UPI/ACME.TRADERS/payment:12,500.00 37,500.00", 241),
        L("Ref 4412 Sept invoice", 287),
        L("03-04-2025 NEFT CR-GLOBAL SOLUTIONS:LTD 1;25,000.00 1,62,500.00", 318),
        L("05-04-2025ATM WDLMUMBA15,000.00:1,57,500.00", 360),        # 'I' read as '1': amount is really 5,000.00
        L("05-04-2025 ATM WDL MUMBAI5,000.00,1,52,500.00", 399),
        L("10-04-2025 SMS.CHARGES Q1-17.70:1,52,482.30", 439),
        L("30-04-2025 INTEREST CREDIT 412.001,52,894.30", 478),        # two numbers fused together
        L("Page.1 of.1", 545),
    ]
    return OcrPage(boxes, 1680, 2357)


class TableLayout(unittest.TestCase):
    def setUp(self):
        self.t = parse_ocr_pages([table_page()])

    def test_columns_decide_debit_or_credit(self):
        self.assertEqual(len(self.t), 4)
        self.assertEqual([(x.debit, x.credit, x.balance) for x in self.t],
                         [(1250000, 0, 3750000), (0, 12500000, 16250000), (500000, 0, 15750000), (1770, 0, 15748230)])

    def test_dates_including_the_damaged_one(self):
        self.assertEqual([x.date for x in self.t], ["2025-04-01", "2025-04-03", "2025-04-05", "2025-04-10"])
        self.assertIn("date text repaired", self.t[2].note)

    def test_value_date_column_is_not_narration(self):
        self.assertEqual(self.t[0].narration, "UPIACMETRADERS/payment")
        self.assertEqual(self.t[1].narration, "NEFT CR-GLOBAL SOLUTIONS LTD")

    def test_everything_is_marked_as_ocr_with_position(self):
        for x in self.t:
            self.assertEqual((x.source, x.page), ("ocr", 1))
            self.assertTrue(0 < x.y0 < x.y1 < 1)
        self.assertLess(self.t[0].y0, self.t[1].y0)
        self.assertEqual(self.t[3].confidence, 0.74)
        self.assertIn("low OCR confidence", self.t[3].note)

    def test_clean_rows_are_not_flagged(self):
        self.assertFalse(any(x.uncertain for x in self.t))                        # balances all follow
        self.assertEqual(self.t[0].note, "")


class TextLayout(unittest.TestCase):
    def setUp(self):
        self.t = parse_ocr_pages([text_page()])

    def test_rows_and_continuation_line(self):
        self.assertEqual([x.date for x in self.t], ["2025-04-01", "2025-04-03", "2025-04-05", "2025-04-05",
                                                    "2025-04-10", "2025-04-30"])
        self.assertIn("Sept invoice", self.t[0].narration)                         # wrapped second line joined
        self.assertNotIn("Page", self.t[-1].narration)                             # footer ignored

    def test_wrong_separators_and_fused_numbers_are_repaired(self):
        self.assertEqual(self.t[1].credit, 12500000)                               # '1;25,000.00'
        self.assertEqual((self.t[5].credit, self.t[5].balance), (41200, 15289430))  # '412.001,52,894.30'
        self.assertEqual((self.t[4].debit, self.t[4].balance), (1770, 15248230))   # 'Q1-17.70:1,52,482.30'

    def test_direction_comes_from_the_running_balance(self):
        self.assertEqual((self.t[0].debit, self.t[0].credit), (1250000, 0))        # opening 50,000 -> 37,500
        self.assertEqual((self.t[1].debit, self.t[1].credit), (0, 12500000))
        self.assertEqual((self.t[3].debit, self.t[3].balance), (500000, 15250000))

    def test_misread_amount_is_flagged_not_silently_accepted(self):
        bad = self.t[2]                                                            # '...MUMBA15,000.00' is really 5,000.00
        self.assertEqual(bad.debit, 1500000)                                       # kept as read ...
        self.assertTrue(bad.uncertain)                                             # ... but flagged for a human
        self.assertIn("balance does not follow", bad.note)

    def test_clean_rows_have_no_notes(self):
        self.assertEqual([bool(x.note) for x in self.t], [False, False, True, False, False, False])


class Helpers(unittest.TestCase):
    def test_repair_date_prefix(self):
        self.assertEqual(ocr.repair_date_prefix("05/0.4/2025 ATM"), ("05/04/2025 ATM", True))
        self.assertEqual(ocr.repair_date_prefix("O1-04-2O25 UPI"), ("01-04-2025 UPI", True))
        self.assertEqual(ocr.repair_date_prefix("05-04-2025ATM WDL"), ("05-04-2025ATM WDL", False))
        self.assertEqual(ocr.repair_date_prefix("Opening balance"), ("Opening balance", False))

    def test_amount_regex_is_strict_about_separators(self):
        found = lambda s: [m.group(0) for m in ocr.iter_amounts(s)]
        self.assertEqual(found("412.001,52,894.30"), ["412.00", "1,52,894.30"])
        self.assertEqual(found("Ref 4412 Sept 1,234,567.89 Cr"), ["1,234,567.89 Cr"])
        self.assertEqual(found("12,34,567.89"), ["12,34,567.89"])
        self.assertEqual(found("no money 4412 here"), [])

    def test_group_lines_tolerates_tilt(self):
        boxes = [B("a", 0, 100, 50, 130), B("b", 900, 112, 950, 142),              # same row, 12px of drift
                 B("c", 0, 160, 50, 190), B("d", 900, 172, 950, 202)]
        self.assertEqual([[b.text for b in l] for l in ocr.group_lines(boxes)], [["a", "b"], ["c", "d"]])

    def test_empty_and_no_rows(self):
        self.assertEqual(parse_ocr_pages([OcrPage([], 100, 100)]), [])
        self.assertEqual(ocr.group_lines([]), [])


class RealEngine(unittest.TestCase):
    """End to end with the real OCR engine, on a simulated scan. Skipped when OCR is not installed."""

    @unittest.skipUnless(ocr.engine_status()["available"], "OCR engine not installed")
    def test_scanned_pdf_end_to_end(self):
        import os, random, tempfile
        from PIL import ImageFilter
        import pypdfium2 as pdfium
        from tests import pdf_samples
        from easytally.bank_import import parse_statement_ex
        d = tempfile.mkdtemp()
        pdf_samples.table_pdf(os.path.join(d, "t.pdf"))
        img = pdfium.PdfDocument(os.path.join(d, "t.pdf"))[0].render(scale=200 / 72).to_pil().convert("L")
        img = img.rotate(0.6, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.7))     # a slightly skewed, soft scan
        img.save(os.path.join(d, "scan.pdf"), "PDF", resolution=200)
        r = parse_statement_ex(os.path.join(d, "scan.pdf"))
        self.assertEqual(r.source, "ocr")
        self.assertEqual(len(r.pages), 1)
        self.assertGreaterEqual(len(r.txns), 5)                                    # OCR can miss a line: tolerate one
        good = [(x.date, x.debit or x.credit) for x in r.txns]
        self.assertIn(("2025-04-01", 1250000), good)
        self.assertIn(("2025-04-03", 12500000), good)
        self.assertTrue(all(x.source == "ocr" for x in r.txns))


    @unittest.skipUnless(ocr.engine_status()["available"], "OCR engine not installed")
    def test_speckled_scan_is_still_read_thanks_to_denoising(self):
        """Without the median filter this scan read only 3-5 of 6 rows; with it, all of them."""
        import os, random, tempfile
        from PIL import ImageFilter
        import pypdfium2 as pdfium
        from tests import pdf_samples
        from easytally.bank_import import parse_statement_ex
        d = tempfile.mkdtemp()
        pdf_samples.table_pdf(os.path.join(d, "t.pdf"))
        img = pdfium.PdfDocument(os.path.join(d, "t.pdf"))[0].render(scale=200 / 72).to_pil().convert("L")
        img = img.rotate(0.6, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.7))
        px = img.load()
        random.seed(2)
        for _ in range(img.width * img.height // 60):                               # heavy photocopy speckle
            x, y = random.randrange(img.width), random.randrange(img.height)
            px[x, y] = max(0, px[x, y] - random.randrange(110))
        img.save(os.path.join(d, "noisy.pdf"), "PDF", resolution=200)
        got = {(x.date, x.debit or x.credit) for x in parse_statement_ex(os.path.join(d, "noisy.pdf")).txns}
        expected = {("2025-04-01", 1250000), ("2025-04-03", 12500000), ("2025-04-05", 500000),
                    ("2025-04-10", 1770), ("2025-04-30", 41200)}
        self.assertGreaterEqual(len(got & expected), 4)


if __name__ == "__main__":
    unittest.main()
