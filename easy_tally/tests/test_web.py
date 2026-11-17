import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally.web.api import Api
from easytally.web.server import make_server
from tests import pdf_samples

PW = "correct horse battery"


class Client:
    def __init__(self, base):
        self.base, self.token, self.last_headers = base, None, {}

    def call(self, path, body=None, raw=None, headers=None):
        h = {"X-EasyTally": "1"}
        data = None
        if raw is not None:
            data, h["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        if self.token:
            h["Cookie"] = f"et_session={self.token}"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                status, content, hd = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, content, hd = e.code, e.read(), e.headers
        self.last_headers = hd
        sc = hd.get("Set-Cookie") or ""
        if sc.startswith("et_session="):
            val = sc.split(";")[0].split("=", 1)[1]
            self.token = val or None
        return status, content

    def j(self, *a, **k):
        s, b = self.call(*a, **k)
        return s, json.loads(b)


class WebBase(unittest.TestCase):
    def setUp(self):
        self.api = Api(users_path=os.path.join(tempfile.mkdtemp(), "users.db"), iterations=1000)
        self.srv = make_server(0, api=self.api)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.c = Client(self.base)
        self.tag = self.id().split(".")[-1]

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        if self.api.books:
            self.api.books.close()
        self.api.users.conn.close()

    def admin(self):
        s, r = self.c.j("/api/auth/setup", {"username": "admin1", "password": PW})
        self.assertEqual((s, r["role"]), (200, "admin"))

    def company(self, **extra):
        self.assertEqual(self.c.j("/api/company/create", {"name": f"Web {self.tag}", "state": "27", **extra})[0], 200)


class AuthFlow(WebBase):
    def test_setup_login_logout(self):
        s, e = self.c.j("/api/ledgers")
        self.assertEqual((s, e.get("setup")), (401, True))                       # nothing works before setup
        self.assertEqual(self.c.j("/api/auth/status")[1], {"setup_needed": True, "user": None})
        self.assertEqual(self.c.j("/api/auth/setup", {"username": "ab", "password": PW})[0], 400)
        self.admin()
        self.assertIn("HttpOnly", self.c.last_headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", self.c.last_headers["Set-Cookie"])
        self.assertEqual(self.c.j("/api/auth/setup", {"username": "other1", "password": PW})[0], 403)  # only once
        self.assertEqual(self.c.j("/api/auth/status")[1]["user"], {"username": "admin1", "role": "admin"})
        self.assertEqual(self.c.call("/api/auth/logout", {})[0], 200)
        self.assertEqual(self.c.j("/api/companies")[0], 401)
        self.assertEqual(self.c.j("/api/auth/login", {"username": "admin1", "password": "wrong password"})[0], 400)
        self.assertEqual(self.c.j("/api/auth/login", {"username": "admin1", "password": PW})[0], 200)
        self.assertEqual(self.c.j("/api/companies")[0], 200)

    def test_logout_closes_the_open_company(self):
        self.admin(); self.company()
        self.assertEqual(self.c.j("/api/ledgers")[0], 200)
        self.c.call("/api/auth/logout", {})
        self.c.j("/api/auth/login", {"username": "admin1", "password": PW})
        self.assertEqual(self.c.j("/api/ledgers")[0], 409)          # next sign-in starts at the company picker
        self.assertIsNone(self.c.j("/api/meta")[1]["company"])

    def test_forged_cookie_and_password_change(self):
        self.admin()
        forged = Client(self.base); forged.token = "x" * 40
        self.assertEqual(forged.j("/api/companies")[0], 401)
        self.assertEqual(self.c.j("/api/auth/password", {"old": "nope nope nope", "new": "another long one"})[0], 400)
        self.assertEqual(self.c.j("/api/auth/password", {"old": PW, "new": "another long one"})[0], 200)
        c2 = Client(self.base)
        self.assertEqual(c2.j("/api/auth/login", {"username": "admin1", "password": "another long one"})[0], 200)

    def test_roles(self):
        self.admin(); self.company()
        for u, role in (("acct1", "accountant"), ("view1", "viewer")):
            self.assertEqual(self.c.j("/api/users", {"username": u, "password": PW, "role": role})[0], 200)
        acct, view = Client(self.base), Client(self.base)
        acct.j("/api/auth/login", {"username": "acct1", "password": PW})
        view.j("/api/auth/login", {"username": "view1", "password": PW})
        led = {"name": "Cust", "group": "Sundry Debtors"}
        self.assertEqual(view.j("/api/ledgers")[0], 200)                         # viewer reads
        self.assertEqual(view.j("/api/ledgers", led)[0], 403)                    # ...but cannot write
        self.assertEqual(acct.j("/api/ledgers", led)[0], 200)                    # accountant writes
        for path, body in (("/api/users", {"username": "zz1", "password": PW, "role": "admin"}),
                           ("/api/company/create", {"name": "X"}), ("/api/settings", {"address": "x"}),
                           ("/api/tally/import", None), ("/api/restore", None)):
            self.assertEqual(acct.j(path, body, raw=b"x" if body is None else None)[0], 403, path)
        self.assertEqual(acct.j("/api/users")[0], 403)
        self.assertEqual(acct.j("/api/backup")[0], 403)
        self.assertEqual(acct.j("/api/audit")[0], 403)
        # demoting/disabling a user signs them out at once
        uid = [u["id"] for u in self.c.j("/api/users")[1] if u["username"] == "acct1"][0]
        self.assertEqual(self.c.j("/api/users/update", {"id": uid, "role": "viewer"})[0], 200)
        self.assertEqual(acct.j("/api/ledgers")[0], 401)
        # admin cannot delete themselves or the last admin
        aid = [u["id"] for u in self.c.j("/api/users")[1] if u["username"] == "admin1"][0]
        self.assertEqual(self.c.j("/api/users/delete", {"id": aid})[0], 400)

    def test_audit_trail(self):
        self.admin(); self.company()
        self.c.j("/api/users", {"username": "acct1", "password": PW, "role": "accountant"})
        acct = Client(self.base); acct.j("/api/auth/login", {"username": "acct1", "password": PW})
        s, led = acct.j("/api/ledgers", {"name": "Cust", "group": "Sundry Debtors"})
        acct.j("/api/vouchers", {"vtype": "Journal", "date": "2026-04-01", "lines": [
            {"ledger_id": led["id"], "side": "Dr", "amount": "5"}, {"ledger_id": 1, "side": "Cr", "amount": "5"}]})
        log = self.c.j("/api/audit")[1]
        self.assertEqual([(x["user"], x["action"]) for x in log][:2], [("acct1", "Voucher added"), ("acct1", "Ledger added")])
        self.assertIn("Cust", log[1]["detail"])


    def test_audit_ignores_plain_views(self):
        self.admin(); self.company()
        for path in ("/api/ledgers", "/api/items", "/api/vouchers", "/api/invoices", "/api/settings"):
            self.assertEqual(self.c.j(path)[0], 200)                          # same paths as the audited POSTs
        self.assertEqual(self.c.j("/api/audit")[1], [])
        self.c.j("/api/ledgers", {"name": "Cust", "group": "Sundry Debtors"})
        self.assertEqual([x["action"] for x in self.c.j("/api/audit")[1]], ["Ledger added"])


class Features(WebBase):
    def setUp(self):
        super().setUp()
        self.admin(); self.company(gstin="")
        self.c.j("/api/settings", {"gstin": "27ABCDE1234F1Z5", "address": "12 MG Road"})
        self.item = self.c.j("/api/items", {"name": "Widget", "hsn": "8501", "gst_rate": 18})[1]["id"]
        self.cust = self.c.j("/api/ledgers", {"name": "Acme", "group": "Sundry Debtors", "gstin": "29AAAAA0000A1Z5"})[1]["id"]
        s, r = self.c.j("/api/invoices", {"kind": "Sales", "date": "2026-04-05", "party_id": self.cust, "ref": "INV-1",
                                          "lines": [{"item_id": self.item, "qty": "2", "rate": "500"}]})
        self.assertEqual(s, 200)
        self.inv = r["id"]

    def test_pdf_gst_and_tally_downloads(self):
        s, pdf = self.c.call(f"/api/invoice.pdf?id={self.inv}")
        self.assertEqual(s, 200); self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(self.c.last_headers["Content-Type"], "application/pdf")
        s, body = self.c.call("/api/gst/gstr1.json?month=2026-04")
        data = json.loads(body)
        self.assertEqual(data["b2b"][0]["ctin"], "29AAAAA0000A1Z5")
        self.assertEqual(data["b2b"][0]["inv"][0]["itms"][0]["itm_det"]["iamt"], 180.0)
        self.assertIn("GSTR1_042026.json", self.c.last_headers["Content-Disposition"])
        for path in ("/api/gst/gstr1.xlsx", "/api/gst/gstr3b.xlsx"):
            s, x = self.c.call(path + "?month=2026-04")
            self.assertEqual((s, x[:2]), (200, b"PK"))
        self.assertEqual(self.c.j("/api/gst/gstr3b.json?month=2026-04")[1]["sup_details"]["osup_det"]["txval"], 1000.0)
        chk = self.c.j("/api/gst/check?month=2026-04")[1]
        self.assertEqual(chk["counts"]["b2b_invoices"], 1)
        self.assertEqual(self.c.j("/api/gst/check?month=bad")[0], 400)
        # Tally round trip into a second company
        masters = self.c.call("/api/tally/masters.xml")[1]
        vouchers = self.c.call("/api/tally/vouchers.xml?start=2026-04-01&end=2026-04-30")[1]
        self.assertIn(b"<LEDGER ", masters)
        self.c.j("/api/company/create", {"name": f"Web {self.tag} copy", "state": "27"})
        self.assertEqual(self.c.j("/api/tally/import", raw=masters)[1]["ledgers"] >= 1, True)
        r = self.c.j("/api/tally/import", raw=vouchers)[1]
        self.assertEqual((r["vouchers"], r["duplicates"]), (1, 0))
        self.assertEqual(self.c.j("/api/tally/import", raw=b"<nope")[0], 400)

    def test_edit_voucher_invoice_and_notes(self):
        v = self.c.j(f"/api/voucher?id={self.inv}")[1]
        self.assertEqual(v["invoice"]["kind"], "Sales")
        self.assertEqual(v["invoice"]["lines"][0]["rate"], "500.00")
        s, _ = self.c.j("/api/invoices/update", {"id": self.inv, "date": "2026-04-06", "party_id": self.cust, "ref": "INV-1B",
                                                 "lines": [{"item_id": self.item, "qty": "3", "rate": "500"}]})
        self.assertEqual(s, 200)
        rows = self.c.j("/api/invoices")[1]
        self.assertEqual((rows[0]["ref"], rows[0]["taxable"]), ("INV-1B", "1,500.00"))
        cn = self.c.j("/api/invoices", {"kind": "Credit Note", "date": "2026-04-07", "party_id": self.cust,
                                        "lines": [{"item_id": self.item, "qty": "1", "rate": "500"}]})
        self.assertEqual(cn[0], 200)
        # edit a plain voucher through the API, and refuse to edit an invoice that way
        cash = [l["id"] for l in self.c.j("/api/ledgers")[1] if l["name"] == "Cash"][0]
        pv = self.c.j("/api/vouchers", {"vtype": "Receipt", "date": "2026-04-08", "lines": [
            {"ledger_id": cash, "side": "Dr", "amount": "100"}, {"ledger_id": self.cust, "side": "Cr", "amount": "100"}]})[1]["id"]
        self.assertEqual(self.c.j("/api/vouchers/update", {"id": pv, "date": "2026-04-09", "narration": "fixed", "lines": [
            {"ledger_id": cash, "side": "Dr", "amount": "250"}, {"ledger_id": self.cust, "side": "Cr", "amount": "250"}]})[0], 200)
        self.assertEqual(self.c.j(f"/api/voucher?id={pv}")[1]["entries"][0]["amount"], "250.00")
        self.assertEqual(self.c.j("/api/vouchers/update", {"id": self.inv, "date": "2026-04-09", "lines": [
            {"ledger_id": cash, "side": "Dr", "amount": "1"}, {"ledger_id": self.cust, "side": "Cr", "amount": "1"}]})[0], 400)
        # outstanding for Acme: invoice 1770 - credit note 590 - receipt 250 = 930
        r = self.c.j(f"/api/report?name=Outstanding%20Bills&start=2026-04-01&end=2026-04-30&ledger_id={self.cust}")[1]
        self.assertEqual(r["rows"][-1][3], "930.00")
        ag = self.c.j("/api/report?name=Receivables%20Ageing&start=2026-04-01&end=2026-04-30")[1]
        self.assertEqual(ag["rows"][-1][1], "930.00")

    def test_backup_restore_api(self):
        s, z = self.c.call("/api/backup")
        self.assertEqual(s, 200)
        self.assertTrue(any(n.startswith("companies/") for n in zipfile.ZipFile(io.BytesIO(z)).namelist()))
        s, r = self.c.j("/api/restore", raw=z)
        self.assertEqual(s, 200)
        self.assertTrue(r["restored"][0]["name"].endswith("(restored)"))
        self.assertEqual(self.c.j("/api/ledgers")[0], 409)                          # company was closed by restore
        self.assertEqual(self.c.j("/api/restore", raw=b"junk")[0], 400)
        self.assertEqual(self.c.call("/api/backup/all")[0], 200)


class Pages(WebBase):
    def test_page_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"EasyTally", r.read())

    def test_ping_and_flow_with_bank_pdf(self):
        self.assertEqual(self.c.j("/api/ping")[1]["app"], "EasyTally")
        self.admin(); self.company()
        bank = self.c.j("/api/ledgers", {"name": "HDFC", "group": "Bank Accounts", "opening": "50000"})[1]["id"]
        self.c.j("/api/ledgers", {"name": "Capital", "group": "Capital Account", "opening": "50000", "side": "Cr"})
        s, e = self.c.j("/api/ledgers", {"name": "HDFC", "group": "Bank Accounts"})
        self.assertEqual((s, "already exists" in e["error"]), (400, True))
        pdf = Path(tempfile.mkdtemp()) / "s.pdf"; pdf_samples.table_pdf(pdf)
        s, res = self.c.j(f"/api/bank/import?bank_id={bank}&filename=s.pdf", raw=pdf.read_bytes())
        self.assertEqual((s, res["new"], res["duplicates"]), (200, 6, 0))
        self.assertEqual(self.c.j(f"/api/bank/import?bank_id={bank}", raw=pdf.read_bytes())[1]["new"], 0)
        self.assertEqual(self.c.j(f"/api/bank/import?bank_id={bank}", raw=b"not a pdf")[0], 400)
        self.assertEqual(self.c.j("/api/bank/post", {"all_for_bank": bank, "suspense": True})[1]["posted"], 6)
        rep = self.c.j("/api/report?name=Trial%20Balance&start=2025-04-01&end=2025-12-31")[1]
        self.assertEqual(rep["rows"][-1][2], rep["rows"][-1][3])
        s, body = self.c.call("/api/report.csv?name=Day%20Book&start=2025-04-01&end=2025-12-31")
        self.assertIn(b"Day Book", body)

    def test_security(self):
        self.admin()
        self.assertEqual(self.c.call("/api/ping", headers={"Host": "evil.example"})[0], 403)
        req = urllib.request.Request(self.base + "/api/company/close", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "Cookie": f"et_session={self.c.token}"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)                                              # no custom header: refused
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(self.c.call("/api/company/close", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.c.call("/../../etc/passwd")[0], 404)
        anon = Client(self.base)
        self.assertEqual(anon.call("/api/shutdown", {})[0], 401)                    # cannot stop the server unsigned


if __name__ == "__main__":
    unittest.main()
