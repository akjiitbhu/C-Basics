"""JSON API over the accounting engine. Pure Python (no HTTP) so it is easy to test.

Every route declares the permission it needs: None (public), "auth" (any signed-in user),
or one of read / write / delete / admin, which roles map to in auth.ROLE_PERMS.
"""
import csv
import io
import json
import os
import tempfile
import threading
from calendar import monthrange

from .. import auth, backup, db, gst_returns, reports, tally_xml
from ..accounting import INVOICE_KINDS, Books, CashLimitError, display_date, parse_date
from ..bank_import import StatementError, check_consistency, parse_statement_ex
from ..gst import GST_RATES, STATES
from ..invoice_pdf import invoice_pdf
from ..accounting import VOUCHER_TYPES
from ..money import fmt, fmt_dr_cr


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _int(v, what="id"):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(f"Invalid {what}")


class Ctx:
    """Per-request context (the API lock guarantees one request at a time)."""
    def __init__(self, user=None):
        self.user = user          # sqlite Row from UserStore or None
        self.cookie = None        # ("set", token) | ("clear", None)
        self.token = None


class Api:
    def __init__(self, users_path=None, iterations=auth.ITERATIONS):
        self.books = None
        self.lock = threading.Lock()      # sqlite connections are shared across server threads
        self.users = auth.UserStore(users_path or (db.home_dir() / "users.db"), iterations)
        self.sessions = auth.Sessions()
        self.ctx = Ctx()

    # ---- helpers -----------------------------------------------------------
    def _b(self) -> Books:
        if not self.books:
            raise ApiError("No company is open", 409)
        return self.books

    @property
    def username(self):
        return self.ctx.user["username"] if self.ctx.user else ""

    def _authenticate(self, token, perm):
        """Resolve the signed-in user and check the route's permission. Sets self.ctx. Call with the lock held."""
        user = None
        name = self.sessions.get(token)
        if name:
            user = self.users.get(name)
            if not user or user["disabled"]:
                self.sessions.destroy(token)
                user = None
        ctx = self.ctx = Ctx(user)
        ctx.token = token
        if perm is not None:
            if not self.users.has_users():
                raise ApiError("Create the first administrator account to begin.", 401, setup=True)
            if not user:
                raise ApiError("Please sign in.", 401)
            if perm != "auth" and perm not in auth.ROLE_PERMS[user["role"]]:
                raise ApiError("Your role does not allow this action.", 403)
        return user

    def handle(self, method, path, query, body=None, raw=None, token=None):
        """Returns (status, payload, cookie). payload is JSON-able or (content_type, bytes, filename).

        A route may have a third element, a `prep` function for slow work that needs no database (reading a
        scanned statement with OCR). It runs after the permission check but OUTSIDE the lock, so one long
        scan never freezes every other request; its result is passed to the route as a fourth argument.
        """
        route = ROUTES.get((method, path))
        if not route:
            return 404, {"error": "Not found"}, None
        func, perm = route[0], route[1]
        prep = route[2] if len(route) > 2 else None
        prepared = None
        if prep is not None:
            try:
                with self.lock:
                    self._authenticate(token, perm)
                prepared = prep(self, query, body or {}, raw)
            except ApiError as e:
                return e.status, {"error": str(e), **e.extra}, None
            except (ValueError, auth.AuthError) as e:
                return 400, {"error": str(e)}, None
        with self.lock:
            ctx = None
            try:
                user = self._authenticate(token, perm)
                ctx = self.ctx
                result = func(self, query, body or {}, raw) if prep is None else func(self, query, body or {}, raw, prepared)
                if method == "POST" and path in AUDITED and self.books and user:   # changes only, never plain views
                    info = body if body else query
                    self.books.audit(self.username, AUDITED[path], json.dumps(info, default=str)[:400] if info else "")
                return 200, result, ctx.cookie
            except ApiError as e:
                return e.status, {"error": str(e), **e.extra}, ctx.cookie if ctx else None
            except CashLimitError as e:
                # Not blocked: 409 so the page can ask "post anyway?". Blocked by the company's setting: 400.
                return (400 if e.blocked else 409), {"error": str(e), "cash_warning": e.warnings,
                                                     "blocked": e.blocked}, ctx.cookie
            except (ValueError, auth.AuthError) as e:     # validation errors raised by the engine
                return 400, {"error": str(e)}, ctx.cookie if ctx else None

    # ---- authentication ------------------------------------------------------
    def auth_status(self, q, body, raw):
        u = self.ctx.user
        return {"setup_needed": not self.users.has_users(),
                "user": {"username": u["username"], "role": u["role"]} if u else None}

    def _login_as(self, user):
        self.ctx.cookie = ("set", self.sessions.create(user["username"]))
        return {"username": user["username"], "role": user["role"]}

    def auth_setup(self, q, body, raw):
        if self.users.has_users():
            raise ApiError("An administrator already exists.", 403)
        self.users.create(body.get("username", ""), body.get("password", ""), "admin")
        return self._login_as(self.users.get(body["username"].strip()))

    def auth_login(self, q, body, raw):
        if not self.users.has_users():
            raise ApiError("Create the first administrator account to begin.", 401, setup=True)
        return self._login_as(self.users.verify(body.get("username", ""), body.get("password", "")))

    def auth_logout(self, q, body, raw):
        self.sessions.destroy(self.ctx.token)
        self._close()                       # the open company belongs to the signed-in session
        self.ctx.cookie = ("clear", None)
        return {"ok": True}

    def auth_password(self, q, body, raw):
        u = self.ctx.user
        if not auth.check_password(body.get("old", ""), u["password_hash"]):
            raise ApiError("Current password is wrong.")
        self.users.update(u["id"], password=body.get("new", ""))
        return {"ok": True}

    # ---- user administration ---------------------------------------------------
    def users_list(self, q, body, raw):
        return self.users.list()

    def user_add(self, q, body, raw):
        return {"id": self.users.create(body.get("username", ""), body.get("password", ""), body.get("role", "viewer"))}

    def user_update(self, q, body, raw):
        uid = _int(body.get("id"))
        target = self.users.get_id(uid)
        self.users.update(uid, role=body.get("role"), password=body.get("password") or None,
                          disabled=body.get("disabled"))
        if target:
            self.sessions.drop_user(target["username"])         # force re-login with the new settings
        return {"ok": True}

    def user_delete(self, q, body, raw):
        uid = _int(body.get("id"))
        target = self.users.get_id(uid)
        if target and target["username"].lower() == self.username.lower():
            raise ApiError("You cannot delete your own account.")
        self.users.delete(uid)
        if target:
            self.sessions.drop_user(target["username"])
        return {"ok": True}

    # ---- company -----------------------------------------------------------
    def ping(self, q, body, raw):
        return {"ok": True, "app": "EasyTally"}

    def meta(self, q, body, raw):
        signed_in = self.ctx.user is not None
        return {"company": self.books.company_name if (self.books and signed_in) else None,
                "states": [{"code": c, "name": n} for c, n in STATES.items()],
                "vtypes": VOUCHER_TYPES, "invoice_kinds": INVOICE_KINDS, "gst_rates": GST_RATES,
                "groups": [g["name"] for g in self.books.groups()] if (self.books and signed_in) else [],
                "reports": reports.REPORT_NAMES,
                "cash_limits": self._cash_meta() if (self.books and signed_in) else None}

    def _cash_meta(self):
        pay, recv, mode = self.books.cash_limits()
        return {"payment": pay / 100, "receipt": recv / 100, "mode": mode}

    def companies(self, q, body, raw):
        return [{"slug": s, "name": n} for s, n in Books.list_companies()]

    def company_create(self, q, body, raw):
        self._close()
        self.books = Books.create(body.get("name", ""), body.get("state", ""), body.get("gstin", ""))
        return {"ok": True}

    def company_open(self, q, body, raw):
        slugs = [s for s, _ in Books.list_companies()]
        if body.get("slug") not in slugs:
            raise ApiError("Unknown company")
        self._close()
        self.books = Books.open(body["slug"])
        return {"ok": True}

    def _close(self):
        if self.books:
            self.books.close()
            self.books = None

    def company_close(self, q, body, raw):
        self._close()
        return {"ok": True}

    def settings_get(self, q, body, raw):
        return self._b().settings()

    def settings_set(self, q, body, raw):
        self._b().set_settings(body)
        return {"ok": True}

    # ---- masters -----------------------------------------------------------
    def ledgers(self, q, body, raw):
        return [{"id": l["id"], "name": l["name"], "group": l["group_name"],
                 "opening": fmt_dr_cr(l["opening"]), "gstin": l["gstin"], "state": l["state"]}
                for l in self._b().ledgers(q.get("group"))]

    def ledger_add(self, q, body, raw):
        i = self._b().add_ledger(body.get("name", ""), body.get("group", ""), body.get("opening") or "0",
                                 body.get("side", "Dr"), body.get("gstin", ""), body.get("state", ""))
        return {"id": i}

    def ledger_delete(self, q, body, raw):
        self._b().delete_ledger(_int(body.get("id")))
        return {"ok": True}

    def items(self, q, body, raw):
        return [{"id": i["id"], "name": i["name"], "unit": i["unit"], "hsn": i["hsn"],
                 "gst_rate": i["gst_rate"], "opening_qty": f"{i['opening_qty']:g}",
                 "opening_value": fmt(i["opening_value"])} for i in self._b().stock_items()]

    def item_add(self, q, body, raw):
        i = self._b().add_stock_item(body.get("name", ""), body.get("unit", "Nos"), body.get("hsn", ""),
                                     _int(body.get("gst_rate", 18), "GST rate"),
                                     body.get("opening_qty") or 0, body.get("opening_value") or "0")
        return {"id": i}

    def item_delete(self, q, body, raw):
        self._b().delete_stock_item(_int(body.get("id")))
        return {"ok": True}

    # ---- vouchers ----------------------------------------------------------
    def vouchers(self, q, body, raw):
        b, out = self._b(), []
        for v in list(reversed(b.vouchers()))[:200]:
            es = b.voucher_entries(v["id"])
            cash = b.voucher_cash_warnings(v["id"])
            out.append({"id": v["id"], "date": display_date(v["date"]), "vtype": v["vtype"],
                        "number": v["number"], "particulars": " / ".join(e["name"] for e in es),
                        "amount": fmt(sum(e["amount"] for e in es if e["amount"] > 0)),
                        "narration": v["narration"], "invoice": b._is_invoice(v["id"]),
                        "cash_over": bool(cash), "cash_note": "; ".join(cash)})
        return out

    def voucher_get(self, q, body, raw):
        v = self._b().get_voucher(_int(q.get("id")))
        for e in v["entries"]:
            e["amount"] = f"{e['amount']:.2f}"
        if v["invoice"]:
            for l in v["invoice"]["lines"]:
                l["rate"] = f"{l['rate']:.2f}"
                l["qty"] = f"{l['qty']:g}"
        return v

    @staticmethod
    def _voucher_lines(body):
        return [(_int(l.get("ledger_id"), "ledger"), l.get("side"), l.get("amount")) for l in body.get("lines", [])]

    def voucher_add(self, q, body, raw):
        vid = self._b().add_voucher(body.get("vtype", ""), body.get("date", ""), self._voucher_lines(body),
                                    body.get("narration", ""), body.get("ref", ""),
                                    cash_check="confirmed" if body.get("confirm_cash") else "ask")
        return {"id": vid}

    def voucher_update(self, q, body, raw):
        self._b().update_voucher(_int(body.get("id")), body.get("date", ""), self._voucher_lines(body),
                                 body.get("narration", ""), body.get("ref", ""),
                                 cash_check="confirmed" if body.get("confirm_cash") else "ask")
        return {"ok": True}

    def voucher_delete(self, q, body, raw):
        for i in body.get("ids", []):
            self._b().delete_voucher(_int(i))
        return {"ok": True}

    # ---- invoices ----------------------------------------------------------
    def _inv_lines(self, body):
        return [(_int(l.get("item_id"), "item"), l.get("qty"), l.get("rate") or "0")
                for l in body.get("lines", [])]

    def invoice_preview(self, q, body, raw):
        b = self._b()
        inv = b.preview_invoice(body.get("kind"), _int(body.get("party_id"), "party"), self._inv_lines(body))
        return {"intra_state": inv["intra_state"],
                "lines": [{"item_id": l["item_id"], "qty": f"{l['qty']:g}", "rate": fmt(l["rate"]),
                           "gst_rate": l["gst_rate"], "taxable": fmt(l["taxable"]), "cgst": fmt(l["cgst"]),
                           "sgst": fmt(l["sgst"]), "igst": fmt(l["igst"])} for l in inv["lines"]],
                **{k: fmt(inv[k]) for k in ("taxable", "cgst", "sgst", "igst", "total")}}

    def invoice_add(self, q, body, raw):
        vid = self._b().add_invoice(body.get("kind"), body.get("date", ""), _int(body.get("party_id"), "party"),
                                    self._inv_lines(body), ref=body.get("ref", ""))
        return {"id": vid}

    def invoice_update(self, q, body, raw):
        self._b().update_invoice(_int(body.get("id")), body.get("date", ""), _int(body.get("party_id"), "party"),
                                 self._inv_lines(body), ref=body.get("ref", ""))
        return {"ok": True}

    def invoices(self, q, body, raw):
        rows = self._b().conn.execute(
            """SELECT v.id, v.date, v.number, v.ref, i.*, l.name pname FROM invoices i
               JOIN vouchers v ON v.id=i.voucher_id JOIN ledgers l ON l.id=i.party_id
               ORDER BY v.date DESC, v.id DESC LIMIT 200""").fetchall()
        return [{"id": r["id"], "date": display_date(r["date"]), "kind": r["kind"], "number": r["number"],
                 "ref": r["ref"], "party": r["pname"], "taxable": fmt(r["taxable"]), "cgst": fmt(r["cgst"]),
                 "sgst": fmt(r["sgst"]), "igst": fmt(r["igst"]), "total": fmt(r["total"])} for r in rows]

    def invoice_pdf(self, q, body, raw):
        vid = _int(q.get("id"))
        b = self._b()
        v = b.conn.execute("SELECT number, ref FROM vouchers WHERE id=?", (vid,)).fetchone()
        data = invoice_pdf(b, vid)
        name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (v["ref"] or f"{v['number']}"))
        return ("application/pdf", data, f"invoice_{name}.pdf")

    # ---- bank statements ---------------------------------------------------
    def bank_accounts(self, q, body, raw):
        return [{"id": l["id"], "name": l["name"]} for l in self._b().ledgers("Bank Accounts")]

    @staticmethod
    def _plain(paise):
        """Paise -> '12500.00' for input boxes ('' when unknown)."""
        return "" if paise is None else f"{paise / 100:.2f}"

    def bank_txns(self, q, body, raw):
        b = self._b()
        rows = b.bank_txns(_int(q.get("bank_id"), "bank account"), pending_only=q.get("posted") != "1",
                           import_id=_int(q["import_id"]) if q.get("import_id") else None)
        return [{"id": t["id"], "import_id": t["import_id"], "date": display_date(t["date"]), "date_iso": t["date"],
                 "narration": t["narration"],
                 "debit": fmt(t["debit"]) if t["debit"] else "", "credit": fmt(t["credit"]) if t["credit"] else "",
                 "debit_val": self._plain(t["debit"] or None), "credit_val": self._plain(t["credit"] or None),
                 "balance_val": self._plain(t["balance"]),
                 "ledger_id": t["counter_ledger_id"], "posted": bool(t["voucher_id"]),
                 "uncertain": bool(t["uncertain"]), "verified": bool(t["verified"]), "source": t["source"],
                 "confidence": t["confidence"], "page": t["page"], "y0": t["y0"], "y1": t["y1"], "note": t["note"]}
                for t in rows]

    def ocr_status(self, q, body, raw):
        from .. import ocr
        return ocr.engine_status()

    def _import_prep(self, q, body, raw):
        """Slow part of a statement import (text parsing or OCR). Needs no database, so it runs unlocked."""
        _int(q.get("bank_id"), "bank account")
        if not raw:
            raise ApiError("No file received.")
        mode = q.get("ocr") or "auto"
        if mode not in ("auto", "force", "off"):
            raise ApiError("ocr must be auto, force or off")
        try:
            return parse_statement_ex(raw, ocr=mode)
        except StatementError as e:
            raise ApiError(str(e))

    def bank_import(self, q, body, raw, parsed):
        b = self._b()
        bank_id = _int(q.get("bank_id"), "bank account")
        if not b.get_ledger(bank_id):
            raise ApiError("Unknown bank account")
        new, dup = b.stage_bank_transactions(bank_id, q.get("filename") or "statement.pdf", parsed.txns,
                                             pages=parsed.pages, engine=parsed.engine, opening=parsed.opening)
        imp = b.bank_imports(bank_id)[0] if new else None
        warnings = (parsed.warnings if parsed.source == "ocr" else check_consistency(parsed.txns))[:12]
        return {"new": new, "duplicates": dup, "warnings": warnings, "ocr": parsed.source == "ocr",
                "engine": parsed.engine, "import_id": imp["id"] if imp else None,
                "needs_review": (imp["unverified"] or 0) if imp else 0}

    def bank_imports(self, q, body, raw):
        b = self._b()
        out = []
        for i in b.bank_imports(_int(q.get("bank_id"), "bank account")):
            out.append({"id": i["id"], "filename": i["filename"], "source": i["source"], "pages": i["pages"],
                        "engine": i["engine"], "imported_at": i["imported_at"], "rows": i["rows"],
                        "unverified": i["unverified"] or 0, "posted": i["posted"] or 0,
                        "opening": self._plain(i["opening_balance"]), "closing": self._plain(i["closing_balance"])})
        return out

    def bank_page(self, q, body, raw):
        got = self._b().bank_page(_int(q.get("import_id")), _int(q.get("page") or 1))
        if not got:
            raise ApiError("No page image for this statement", 404)
        return (got[0], got[1], f"statement-page-{q.get('page') or 1}.jpg")

    def bank_update_txn(self, q, body, raw):
        self._b().update_bank_txn(_int(body.get("id")), body.get("date", ""), body.get("narration", ""),
                                  body.get("debit", ""), body.get("credit", ""), body.get("balance", ""),
                                  verify=body.get("verify", True))
        return {"ok": True}

    def bank_add_txn(self, q, body, raw):
        after = body.get("after_id")
        i = self._b().add_bank_txn(_int(body.get("import_id")), _int(after) if after else None, body.get("date", ""),
                                   body.get("narration", ""), body.get("debit", ""), body.get("credit", ""),
                                   body.get("balance", ""), verify=body.get("verify", True))
        return {"id": i}

    def bank_delete_txn(self, q, body, raw):
        self._b().delete_bank_txns([_int(i) for i in body.get("ids", [])])
        return {"ok": True}

    def bank_verify(self, q, body, raw):
        self._b().verify_bank_txns([_int(i) for i in body.get("ids", [])], bool(body.get("verified", True)))
        return {"ok": True}

    def bank_verify_reconciled(self, q, body, raw):
        return {"verified": self._b().verify_reconciled(_int(body.get("import_id")))}

    def bank_balances(self, q, body, raw):
        self._b().set_import_balances(_int(body.get("import_id")), body.get("opening"), body.get("closing"))
        return {"ok": True}

    def bank_reconcile(self, q, body, raw):
        r = self._b().reconcile(_int(q.get("import_id")))
        money = lambda p: None if p is None else f"{p / 100:.2f}"
        rows = []
        for x in r["rows"]:
            item = {"id": x["id"], "ok": x["ok"]}
            if x["ok"] is False:
                item["expected"] = money(x["expected"])
                for k in ("fix_amount", "missing_row"):
                    item[k] = {"side": x[k]["side"], "amount": money(x[k]["amount"])} if x[k] else None
            rows.append(item)
        return {"rows": rows, "ok": r["ok"], "bad": r["bad"], "unchecked": r["unchecked"],
                "debit_total": money(r["debit_total"]), "credit_total": money(r["credit_total"]),
                "opening": money(r["opening"]), "opening_inferred": r["opening_inferred"],
                "closing_computed": money(r["closing_computed"]), "closing_stated": money(r["closing_stated"]),
                "closing_diff": money(r["closing_diff"])}

    def bank_set_ledger(self, q, body, raw):
        b = self._b()
        lid = body.get("ledger_id")
        for i in body.get("ids", []):
            b.set_txn_ledger(_int(i), _int(lid, "ledger") if lid else None, body.get("learn", ""))
        return {"ok": True}

    def bank_post(self, q, body, raw):
        b = self._b()
        if body.get("all_for_bank"):
            ids = [t["id"] for t in b.bank_txns(_int(body["all_for_bank"]), pending_only=True)]
        else:
            ids = [_int(i) for i in body.get("ids", [])]
        marks = ",".join("?" * len(ids))
        unverified = b.conn.execute(
            f"SELECT COUNT(*) FROM bank_txns WHERE verified = 0 AND voucher_id IS NULL AND id IN ({marks})",
            ids).fetchone()[0] if ids else 0
        posted, skipped = b.post_bank_transactions(ids, use_suspense=bool(body.get("suspense")))
        return {"posted": posted, "skipped": skipped, "unverified": unverified}

    # ---- reports -----------------------------------------------------------
    def _report(self, q):
        return reports.run_report(self._b(), q.get("name", ""), parse_date(q.get("start", "")),
                                  parse_date(q.get("end", "")), q.get("ledger_id") or None)

    def report(self, q, body, raw):
        r = self._report(q)
        return {"title": r.title, "columns": r.columns, "rows": r.rows, "red": [list(x) for x in r.red]}

    def report_csv(self, q, body, raw):
        r = self._report(q)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([r.title]); w.writerow(r.columns); w.writerows(r.rows)
        return ("text/csv; charset=utf-8", ("\ufeff" + buf.getvalue()).encode("utf-8"), "report.csv")

    # ---- GST returns ---------------------------------------------------------
    @staticmethod
    def _month(q):
        try:
            y, m = q.get("month", "").split("-")
            return int(y), int(m)
        except ValueError:
            raise ApiError("Choose a month")

    def gst_check(self, q, body, raw):
        y, m = self._month(q)
        d1, w1 = gst_returns.gstr1(self._b(), y, m)
        d3, payable, w3 = gst_returns.gstr3b(self._b(), y, m)
        return {"warnings": list(dict.fromkeys(w1 + w3)),
                "counts": {"b2b_invoices": sum(len(x["inv"]) for x in d1["b2b"]),
                           "b2cl_invoices": sum(len(x["inv"]) for x in d1["b2cl"]),
                           "b2cs_rows": len(d1["b2cs"]), "credit_notes": sum(len(x["nt"]) for x in d1["cdnr"]) + len(d1["cdnur"]),
                           "hsn_rows": len(d1["hsn"]["data"])},
                "gstr3b": {"taxable": d3["sup_details"]["osup_det"]["txval"], "igst": d3["sup_details"]["osup_det"]["iamt"],
                           "cgst": d3["sup_details"]["osup_det"]["camt"], "sgst": d3["sup_details"]["osup_det"]["samt"],
                           "itc": d3["itc_elg"]["itc_net"], "payable": payable}}

    def gstr1_json(self, q, body, raw):
        y, m = self._month(q)
        data, _ = gst_returns.gstr1(self._b(), y, m)
        return ("application/json", json.dumps(data, indent=1).encode("utf-8"), f"GSTR1_{m:02d}{y}.json")

    def gstr1_xlsx(self, q, body, raw):
        y, m = self._month(q)
        data, w = gst_returns.gstr1(self._b(), y, m)
        return (XLSX, gst_returns.gstr1_xlsx(data, w), f"GSTR1_{m:02d}{y}.xlsx")

    def gstr3b_json(self, q, body, raw):
        y, m = self._month(q)
        data, _, _ = gst_returns.gstr3b(self._b(), y, m)
        return ("application/json", json.dumps(data, indent=1).encode("utf-8"), f"GSTR3B_{m:02d}{y}.json")

    def gstr3b_xlsx(self, q, body, raw):
        y, m = self._month(q)
        data, pay, w = gst_returns.gstr3b(self._b(), y, m)
        return (XLSX, gst_returns.gstr3b_xlsx(data, pay, w), f"GSTR3B_{m:02d}{y}.xlsx")

    # ---- Tally exchange -------------------------------------------------------
    def tally_masters(self, q, body, raw):
        return ("application/xml", tally_xml.export_masters(self._b()), "Tally_masters.xml")

    def tally_vouchers(self, q, body, raw):
        start = parse_date(q["start"]) if q.get("start") else None
        end = parse_date(q["end"]) if q.get("end") else None
        return ("application/xml", tally_xml.export_vouchers(self._b(), start, end), "Tally_vouchers.xml")

    def tally_import(self, q, body, raw):
        if not raw:
            raise ApiError("No file received.")
        return tally_xml.import_xml(self._b(), raw)

    # ---- backup / restore / audit ---------------------------------------------
    def backup_company(self, q, body, raw):
        b = self._b()
        return ("application/zip", backup.backup_company(b), f"EasyTally_backup_{b.path.stem}.zip")

    def backup_all(self, q, body, raw):
        return ("application/zip", backup.backup_all(), "EasyTally_backup_all.zip")

    def restore(self, q, body, raw):
        if not raw:
            raise ApiError("No file received.")
        if self.books:                       # release the open file so it can be replaced safely
            self._close()
        return {"restored": backup.restore(raw, overwrite=q.get("overwrite") == "1")}

    def audit_list(self, q, body, raw):
        return [{"time": r["ts"], "user": r["user"], "action": r["action"], "detail": r["detail"]}
                for r in self._b().audit_log()]



XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# (method, path) -> (handler, permission)
ROUTES = {
    ("GET", "/api/ping"): (Api.ping, None), ("GET", "/api/meta"): (Api.meta, None),
    ("GET", "/api/auth/status"): (Api.auth_status, None), ("POST", "/api/auth/setup"): (Api.auth_setup, None),
    ("POST", "/api/auth/login"): (Api.auth_login, None), ("POST", "/api/auth/logout"): (Api.auth_logout, "auth"),
    ("POST", "/api/auth/password"): (Api.auth_password, "auth"),
    ("GET", "/api/users"): (Api.users_list, "admin"), ("POST", "/api/users"): (Api.user_add, "admin"),
    ("POST", "/api/users/update"): (Api.user_update, "admin"), ("POST", "/api/users/delete"): (Api.user_delete, "admin"),
    ("GET", "/api/companies"): (Api.companies, "read"), ("POST", "/api/company/create"): (Api.company_create, "admin"),
    ("POST", "/api/company/open"): (Api.company_open, "read"), ("POST", "/api/company/close"): (Api.company_close, "read"),
    ("GET", "/api/settings"): (Api.settings_get, "read"), ("POST", "/api/settings"): (Api.settings_set, "admin"),
    ("GET", "/api/ledgers"): (Api.ledgers, "read"), ("POST", "/api/ledgers"): (Api.ledger_add, "write"),
    ("POST", "/api/ledgers/delete"): (Api.ledger_delete, "delete"),
    ("GET", "/api/items"): (Api.items, "read"), ("POST", "/api/items"): (Api.item_add, "write"),
    ("POST", "/api/items/delete"): (Api.item_delete, "delete"),
    ("GET", "/api/vouchers"): (Api.vouchers, "read"), ("GET", "/api/voucher"): (Api.voucher_get, "read"),
    ("POST", "/api/vouchers"): (Api.voucher_add, "write"), ("POST", "/api/vouchers/update"): (Api.voucher_update, "write"),
    ("POST", "/api/vouchers/delete"): (Api.voucher_delete, "delete"),
    ("GET", "/api/invoices"): (Api.invoices, "read"), ("POST", "/api/invoices"): (Api.invoice_add, "write"),
    ("POST", "/api/invoices/update"): (Api.invoice_update, "write"),
    ("POST", "/api/invoices/preview"): (Api.invoice_preview, "read"), ("GET", "/api/invoice.pdf"): (Api.invoice_pdf, "read"),
    ("GET", "/api/bank/accounts"): (Api.bank_accounts, "read"), ("GET", "/api/bank/txns"): (Api.bank_txns, "read"),
    ("POST", "/api/bank/import"): (Api.bank_import, "write", Api._import_prep),
    ("POST", "/api/bank/set_ledger"): (Api.bank_set_ledger, "write"),
    ("GET", "/api/ocr/status"): (Api.ocr_status, "read"), ("GET", "/api/bank/imports"): (Api.bank_imports, "read"),
    ("GET", "/api/bank/page"): (Api.bank_page, "read"), ("GET", "/api/bank/reconcile"): (Api.bank_reconcile, "read"),
    ("POST", "/api/bank/update_txn"): (Api.bank_update_txn, "write"), ("POST", "/api/bank/add_txn"): (Api.bank_add_txn, "write"),
    ("POST", "/api/bank/delete_txn"): (Api.bank_delete_txn, "write"), ("POST", "/api/bank/verify"): (Api.bank_verify, "write"),
    ("POST", "/api/bank/verify_reconciled"): (Api.bank_verify_reconciled, "write"),
    ("POST", "/api/bank/balances"): (Api.bank_balances, "write"),
    ("POST", "/api/bank/post"): (Api.bank_post, "write"),
    ("GET", "/api/report"): (Api.report, "read"), ("GET", "/api/report.csv"): (Api.report_csv, "read"),
    ("GET", "/api/gst/check"): (Api.gst_check, "read"),
    ("GET", "/api/gst/gstr1.json"): (Api.gstr1_json, "read"), ("GET", "/api/gst/gstr1.xlsx"): (Api.gstr1_xlsx, "read"),
    ("GET", "/api/gst/gstr3b.json"): (Api.gstr3b_json, "read"), ("GET", "/api/gst/gstr3b.xlsx"): (Api.gstr3b_xlsx, "read"),
    ("GET", "/api/tally/masters.xml"): (Api.tally_masters, "read"), ("GET", "/api/tally/vouchers.xml"): (Api.tally_vouchers, "read"),
    ("POST", "/api/tally/import"): (Api.tally_import, "admin"),
    ("GET", "/api/backup"): (Api.backup_company, "admin"), ("GET", "/api/backup/all"): (Api.backup_all, "admin"),
    ("POST", "/api/restore"): (Api.restore, "admin"), ("GET", "/api/audit"): (Api.audit_list, "admin"),
}

# Changes that are written to the company's audit trail (who did what).
AUDITED = {
    "/api/ledgers": "Ledger added", "/api/ledgers/delete": "Ledger deleted", "/api/items": "Stock item added",
    "/api/items/delete": "Stock item deleted", "/api/vouchers": "Voucher added", "/api/vouchers/update": "Voucher edited",
    "/api/vouchers/delete": "Voucher deleted", "/api/invoices": "Invoice added", "/api/invoices/update": "Invoice edited",
    "/api/bank/import": "Bank statement imported", "/api/bank/post": "Bank rows posted",
    "/api/bank/update_txn": "Bank row corrected", "/api/bank/add_txn": "Bank row added by hand",
    "/api/bank/delete_txn": "Bank rows deleted", "/api/bank/verify": "Bank rows verified",
    "/api/bank/verify_reconciled": "Bank rows verified (balances add up)", "/api/bank/balances": "Statement balances set",
    "/api/tally/import": "Tally XML imported", "/api/settings": "Company details changed",
}
