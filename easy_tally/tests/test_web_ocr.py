import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally import ocr
from easytally.bank_import import ParseResult
from easytally.ocr import parse_ocr_pages
from easytally.web import api as api_mod
from tests.test_ocr_parser import text_page
from tests.test_web import Client, PW, WebBase

JPEG = b"\xff\xd8\xff\xe0" + b"fake page" * 20


def fake_parse(*args, **kwargs):
    info = {}
    txns = parse_ocr_pages([text_page()], info)
    return ParseResult(txns, "ocr", [JPEG], "RapidOCR (offline)", [f"Row {i}: {t.note}" for i, t in enumerate(txns, 1) if t.note],
                       info["opening"])


class ScannedStatementApi(WebBase):
    def setUp(self):
        super().setUp()
        self.admin(); self.company()
        self.bank = self.c.j("/api/ledgers", {"name": "HDFC", "group": "Bank Accounts", "opening": "50000"})[1]["id"]
        self.c.j("/api/ledgers", {"name": "Capital", "group": "Capital Account", "opening": "50000", "side": "Cr"})
        patcher = mock.patch.object(api_mod, "parse_statement_ex", fake_parse)
        patcher.start()
        self.addCleanup(patcher.stop)

    def do_import(self):
        s, r = self.c.j(f"/api/bank/import?bank_id={self.bank}&filename=scan.pdf", raw=b"%PDF-fake")
        self.assertEqual(s, 200, r)
        return r

    def txns(self, **extra):
        return self.c.j(f"/api/bank/txns?bank_id={self.bank}&import_id={self.imp}")[1]

    def test_full_review_flow(self):
        r = self.do_import()
        self.assertEqual((r["new"], r["ocr"], r["needs_review"], r["engine"]), (6, True, 6, "RapidOCR (offline)"))
        self.assertTrue(any("balance does not follow" in w for w in r["warnings"]))
        self.imp = r["import_id"]
        imports = self.c.j(f"/api/bank/imports?bank_id={self.bank}")[1]
        self.assertEqual((imports[0]["source"], imports[0]["rows"], imports[0]["unverified"], imports[0]["opening"]),
                         ("ocr", 6, 6, "50000.00"))

        # 1. nothing can be posted before a person checks it
        s, post = self.c.j("/api/bank/post", {"all_for_bank": self.bank, "suspense": True})
        self.assertEqual((s, post["posted"], post["unverified"]), (200, 0, 6))
        self.assertEqual(len(self.c.j("/api/vouchers")[1]), 0)

        # 2. rows carry everything the review screen needs
        rows = self.txns()
        self.assertEqual((rows[0]["verified"], rows[0]["source"], rows[0]["page"]), (False, "ocr", 1))
        self.assertTrue(0 < rows[0]["y0"] < rows[0]["y1"] < 1)
        self.assertEqual((rows[0]["date_iso"], rows[0]["debit_val"], rows[0]["balance_val"]), ("2025-04-01", "12500.00", "37500.00"))

        # 3. the page image is served inline
        s, img = self.c.call(f"/api/bank/page?import_id={self.imp}&page=1")
        self.assertEqual((s, img), (200, JPEG))
        self.assertEqual(self.c.last_headers["Content-Type"], "image/jpeg")
        self.assertEqual(self.c.call(f"/api/bank/page?import_id={self.imp}&page=9")[0], 404)

        # 4. reconciliation points at the misread row and says how to fix it
        rec = self.c.j(f"/api/bank/reconcile?import_id={self.imp}")[1]
        self.assertEqual((rec["ok"], rec["bad"], rec["opening"]), (5, 1, "50000.00"))
        bad = [x for x in rec["rows"] if x["ok"] is False][0]
        self.assertEqual((bad["id"], bad["expected"], bad["fix_amount"]), (rows[2]["id"], "147500.00", {"side": "debit", "amount": "5000.00"}))

        # 5. verify what adds up, fix the rest by hand
        self.assertEqual(self.c.j("/api/bank/verify_reconciled", {"import_id": self.imp})[1]["verified"], 5)
        s, e = self.c.j("/api/bank/update_txn", {"id": rows[2]["id"], "date": "2025-04-05", "narration": "ATM WDL MUMBAI",
                                                  "debit": "10", "credit": "10", "balance": "157500"})
        self.assertEqual((s, "either a debit or a credit" in e["error"]), (400, True))
        self.assertEqual(self.c.j("/api/bank/update_txn", {"id": rows[2]["id"], "date": "2025-04-05", "narration": "ATM WDL MUMBAI",
                                                           "debit": "5000", "credit": "", "balance": "157500"})[0], 200)
        rec = self.c.j(f"/api/bank/reconcile?import_id={self.imp}")[1]
        self.assertEqual((rec["ok"], rec["bad"]), (6, 0))
        self.assertEqual(self.c.j("/api/bank/balances", {"import_id": self.imp, "opening": "50000", "closing": "152894.30"})[0], 200)
        rec = self.c.j(f"/api/bank/reconcile?import_id={self.imp}")[1]
        self.assertEqual((rec["closing_computed"], rec["closing_diff"]), ("152894.30", "0.00"))

        # 6. now everything posts and the bank ledger equals the statement
        s, post = self.c.j("/api/bank/post", {"all_for_bank": self.bank, "suspense": True})
        self.assertEqual((post["posted"], post["unverified"]), (6, 0))
        trial = self.c.j("/api/report?name=Trial%20Balance&start=2025-04-01&end=2025-12-31")[1]
        self.assertEqual(trial["rows"][-1][2], trial["rows"][-1][3])
        self.assertIn(["HDFC", "Bank Accounts", "1,52,894.30", ""], trial["rows"])

        # 7. every step is on the audit trail
        actions = {x["action"] for x in self.c.j("/api/audit")[1]}
        self.assertTrue({"Bank statement imported", "Bank row corrected", "Bank rows verified (balances add up)",
                         "Statement balances set", "Bank rows posted"} <= actions)

    def test_insert_a_missing_row_and_delete_junk(self):
        self.imp = self.do_import()["import_id"]
        rows = self.txns()
        s, r = self.c.j("/api/bank/add_txn", {"import_id": self.imp, "after_id": rows[1]["id"], "date": "2025-04-04",
                                              "narration": "Cheque deposit", "debit": "", "credit": "250", "balance": ""})
        self.assertEqual(s, 200)
        order = [x["id"] for x in self.txns()]
        self.assertEqual(order.index(r["id"]), 2)
        self.assertEqual(self.c.j("/api/bank/delete_txn", {"ids": [r["id"], rows[5]["id"]]})[0], 200)
        self.assertEqual(len(self.txns()), 5)
        self.assertEqual(self.c.j("/api/bank/verify", {"ids": [rows[0]["id"]], "verified": True})[0], 200)
        self.assertTrue(self.txns()[0]["verified"])
        self.c.j("/api/bank/verify", {"ids": [rows[0]["id"]], "verified": False})
        self.assertFalse(self.txns()[0]["verified"])

    def test_viewer_can_look_but_not_change(self):
        self.imp = self.do_import()["import_id"]
        rows = self.txns()
        self.c.j("/api/users", {"username": "view1", "password": PW, "role": "viewer"})
        v = Client(self.base); v.j("/api/auth/login", {"username": "view1", "password": PW})
        mine = [x["slug"] for x in v.j("/api/companies")[1] if x["name"] == f"Web {self.tag}"][0]
        v.j("/api/company/open", {"slug": mine})
        self.assertEqual(v.call(f"/api/bank/page?import_id={self.imp}&page=1")[0], 200)
        self.assertEqual(v.j(f"/api/bank/reconcile?import_id={self.imp}")[0], 200)
        for path, body in (("/api/bank/update_txn", {"id": rows[0]["id"]}), ("/api/bank/verify", {"ids": [rows[0]["id"]]}),
                           ("/api/bank/verify_reconciled", {"import_id": self.imp}), ("/api/bank/delete_txn", {"ids": [1]}),
                           ("/api/bank/add_txn", {"import_id": self.imp}), ("/api/bank/balances", {"import_id": self.imp})):
            self.assertEqual(v.j(path, body)[0], 403, path)
        self.assertEqual(v.j(f"/api/bank/import?bank_id={self.bank}", raw=b"%PDF-x")[0], 403)

    def test_same_scan_twice_is_not_duplicated(self):
        self.do_import()
        r = self.do_import()
        self.assertEqual((r["new"], r["duplicates"], r["import_id"], r["needs_review"]), (0, 6, None, 0))


class SlowScanDoesNotFreezeTheApp(WebBase):
    def test_other_requests_are_served_while_a_scan_is_being_read(self):
        self.admin(); self.company()
        bank = self.c.j("/api/ledgers", {"name": "HDFC", "group": "Bank Accounts"})[1]["id"]

        def slow(*a, **k):
            time.sleep(1.5)                                  # stands in for several seconds of OCR
            return fake_parse()
        result = {}
        with mock.patch.object(api_mod, "parse_statement_ex", slow):
            t = threading.Thread(target=lambda: result.update(zip(("s", "r"), self.c.j(f"/api/bank/import?bank_id={bank}", raw=b"%PDF-x"))))
            t.start()
            time.sleep(0.3)
            other = Client(self.base); other.token = self.c.token
            started = time.time()
            self.assertEqual(other.j("/api/ledgers")[0], 200)            # needs the lock: must not wait for the scan
            self.assertLess(time.time() - started, 1.0)
            t.join()
        self.assertEqual((result["s"], result["r"]["new"]), (200, 6))

    def test_unauthenticated_users_cannot_trigger_a_scan(self):
        self.admin(); self.company()
        calls = []
        with mock.patch.object(api_mod, "parse_statement_ex", lambda *a, **k: calls.append(1)):
            self.assertEqual(Client(self.base).j("/api/bank/import?bank_id=1", raw=b"%PDF-x")[0], 401)
        self.assertEqual(calls, [])                                       # the expensive step never ran


class ImportMessages(WebBase):
    def setUp(self):
        super().setUp()
        self.admin(); self.company()
        self.bank = self.c.j("/api/ledgers", {"name": "HDFC", "group": "Bank Accounts"})[1]["id"]

    def scanned_pdf(self):
        from PIL import Image
        d = tempfile.mkdtemp()
        Image.new("L", (800, 600), 255).save(os.path.join(d, "blank.pdf"), "PDF", resolution=150)
        return Path(d, "blank.pdf").read_bytes()

    def test_scan_without_ocr_engine_explains_how_to_install_it(self):
        with mock.patch.object(ocr, "engine_status", lambda: {"available": False, "engine": None, "detail": ocr.INSTALL_HINT}):
            s, r = self.c.j(f"/api/bank/import?bank_id={self.bank}", raw=self.scanned_pdf())
        self.assertEqual(s, 400)
        self.assertIn("no selectable text", r["error"])
        self.assertIn("pip install rapidocr-onnxruntime", r["error"])

    def test_ocr_off_gives_the_old_message_and_garbage_is_refused(self):
        s, r = self.c.j(f"/api/bank/import?bank_id={self.bank}&ocr=off", raw=self.scanned_pdf())
        self.assertEqual(s, 400)
        self.assertIn("no selectable text", r["error"])
        self.assertEqual(self.c.j(f"/api/bank/import?bank_id={self.bank}", raw=b"just text")[0], 400)
        self.assertEqual(self.c.j(f"/api/bank/import?bank_id={self.bank}&ocr=maybe", raw=self.scanned_pdf())[0], 400)

    @unittest.skipUnless(ocr.engine_status()["available"], "OCR engine not installed")
    def test_blank_scan_reports_no_transactions(self):
        s, r = self.c.j(f"/api/bank/import?bank_id={self.bank}", raw=self.scanned_pdf())
        self.assertEqual(s, 400)
        self.assertIn("no transactions were recognised", r["error"])

    def test_ocr_status_endpoint(self):
        s, r = self.c.j("/api/ocr/status")
        self.assertEqual(s, 200)
        self.assertEqual(set(r), {"available", "engine", "detail"})


@unittest.skipUnless(ocr.engine_status()["available"], "OCR engine not installed")
class RealScanThroughTheApi(WebBase):
    def test_scanned_pdf_and_photo_end_to_end(self):
        import io
        import pypdfium2 as pdfium
        from PIL import ImageFilter
        from tests import pdf_samples
        self.admin(); self.company()
        bank = self.c.j("/api/ledgers", {"name": "HDFC", "group": "Bank Accounts", "opening": "50000"})[1]["id"]
        d = tempfile.mkdtemp()
        pdf_samples.table_pdf(os.path.join(d, "t.pdf"))
        img = pdfium.PdfDocument(os.path.join(d, "t.pdf"))[0].render(scale=200 / 72).to_pil().convert("L")
        img = img.rotate(0.6, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.7))
        img.save(os.path.join(d, "scan.pdf"), "PDF", resolution=200)
        s, r = self.c.j(f"/api/bank/import?bank_id={bank}&filename=scan.pdf", raw=Path(d, "scan.pdf").read_bytes())
        self.assertEqual((s, r["ocr"]), (200, True))
        self.assertGreaterEqual(r["new"], 5)
        self.assertEqual(r["needs_review"], r["new"])
        s, page = self.c.call(f"/api/bank/page?import_id={r['import_id']}&page=1")
        self.assertEqual((s, page[:3]), (200, b"\xff\xd8\xff"))               # a real JPEG of the straightened page
        # a photo (JPEG) of the same page is accepted too
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=85)
        s, r2 = self.c.j(f"/api/bank/import?bank_id={bank}&filename=photo.jpg", raw=buf.getvalue())
        self.assertEqual(s, 200)
        self.assertTrue(r2["ocr"])


if __name__ == "__main__":
    unittest.main()
