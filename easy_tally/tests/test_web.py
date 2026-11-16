import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally.web.server import make_server
from tests import pdf_samples


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = make_server(0)
        cls.port = cls.srv.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        d = Path(tempfile.mkdtemp())
        cls.pdf = d / "s.pdf"
        pdf_samples.table_pdf(cls.pdf)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def call(self, path, body=None, raw=None, headers=None, method=None):
        h = {"X-EasyTally": "1"}
        data = None
        if raw is not None:
            data, h["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def j(self, *a, **k):
        s, b = self.call(*a, **k)
        return s, json.loads(b)

    def test_full_flow(self):
        self.assertEqual(self.j("/api/ping")[1]["app"], "EasyTally")
        self.assertEqual(self.j("/api/ledgers")[0], 409)                    # no company yet
        self.assertEqual(self.j("/api/company/create", {"name": "Web Co", "state": "27"})[0], 200)
        s, bank = self.j("/api/ledgers", {"name": "HDFC", "group": "Bank Accounts", "opening": "50000"})
        self.assertEqual(s, 200)
        self.j("/api/ledgers", {"name": "Capital", "group": "Capital Account", "opening": "50000", "side": "Cr"})
        # validation error comes back as 400 with a message
        s, e = self.j("/api/ledgers", {"name": "HDFC", "group": "Bank Accounts"})
        self.assertEqual((s, "already exists" in e["error"]), (400, True))

        s, r = self.call("/api/bank/import?bank_id=%d&filename=s.pdf" % bank["id"], raw=self.pdf.read_bytes())
        res = json.loads(r)
        self.assertEqual((s, res["new"], res["duplicates"], res["warnings"]), (200, 6, 0, []))
        self.assertEqual(self.j("/api/bank/import?bank_id=%d" % bank["id"], raw=self.pdf.read_bytes())[1]["new"], 0)
        s, bad = self.j("/api/bank/import?bank_id=%d" % bank["id"], raw=b"not a pdf")
        self.assertEqual(s, 400)

        txns = self.j("/api/bank/txns?bank_id=%d" % bank["id"])[1]
        self.assertEqual(len(txns), 6)
        s, post = self.j("/api/bank/post", {"all_for_bank": bank["id"], "suspense": True})
        self.assertEqual(post["posted"], 6)
        rep = self.j("/api/report?name=Trial%20Balance&start=2025-04-01&end=2025-12-31")[1]
        total = rep["rows"][-1]
        self.assertEqual(total[2], total[3])
        s, body = self.call("/api/report.csv?name=Day%20Book&start=2025-04-01&end=2025-12-31")
        self.assertEqual(s, 200)
        self.assertIn(b"Day Book", body)

    def test_page_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"EasyTally", r.read())

    def test_security(self):
        # wrong Host header (DNS-rebinding style)
        self.assertEqual(self.call("/api/ping", headers={"Host": "evil.example"})[0], 403)
        # POST without our custom header (what a cross-site form would send)
        req = urllib.request.Request(self.base + "/api/company/close", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)
        # foreign Origin
        self.assertEqual(self.call("/api/company/close", {}, headers={"Origin": "http://evil.example"})[0], 403)
        # no path traversal / arbitrary files
        self.assertEqual(self.call("/../../etc/passwd")[0], 404)


if __name__ == "__main__":
    unittest.main()
