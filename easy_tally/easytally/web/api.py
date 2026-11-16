"""JSON API over the accounting engine. Pure Python (no HTTP) so it is easy to test."""
import csv
import io
import os
import tempfile
import threading

from .. import reports
from ..accounting import Books, VOUCHER_TYPES, display_date
from ..bank_import import StatementError, check_consistency, parse_statement
from ..gst import GST_RATES, STATES
from ..money import fmt, fmt_dr_cr


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _int(v, what="id"):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(f"Invalid {what}")


class Api:
    def __init__(self):
        self.books = None
        self.lock = threading.Lock()      # sqlite connection is shared across server threads

    # ---- helpers -----------------------------------------------------------
    def _b(self) -> Books:
        if not self.books:
            raise ApiError("No company is open", 409)
        return self.books

    def handle(self, method, path, query, body=None, raw=None):
        """Returns (status, payload). payload is a dict/list, or (content_type, bytes) for files."""
        route = ROUTES.get((method, path))
        if not route:
            return 404, {"error": "Not found"}
        try:
            with self.lock:
                return 200, route(self, query, body or {}, raw)
        except ApiError as e:
            return e.status, {"error": str(e)}
        except ValueError as e:        # validation errors raised by the engine
            return 400, {"error": str(e)}

    # ---- company -----------------------------------------------------------
    def ping(self, q, body, raw):
        return {"ok": True, "app": "EasyTally"}

    def meta(self, q, body, raw):
        return {"company": self.books.company_name if self.books else None,
                "states": [{"code": c, "name": n} for c, n in STATES.items()],
                "vtypes": VOUCHER_TYPES, "gst_rates": GST_RATES,
                "groups": [g["name"] for g in self.books.groups()] if self.books else [],
                "reports": reports.REPORT_NAMES}

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
            out.append({"id": v["id"], "date": display_date(v["date"]), "vtype": v["vtype"],
                        "number": v["number"], "particulars": " / ".join(e["name"] for e in es),
                        "amount": fmt(sum(e["amount"] for e in es if e["amount"] > 0)),
                        "narration": v["narration"]})
        return out

    def voucher_add(self, q, body, raw):
        lines = [(_int(l.get("ledger_id"), "ledger"), l.get("side"), l.get("amount"))
                 for l in body.get("lines", [])]
        vid = self._b().add_voucher(body.get("vtype", ""), body.get("date", ""), lines,
                                    body.get("narration", ""), body.get("ref", ""))
        return {"id": vid}

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

    def invoices(self, q, body, raw):
        rows = self._b().conn.execute(
            """SELECT v.id, v.date, v.number, v.ref, i.*, l.name pname FROM invoices i
               JOIN vouchers v ON v.id=i.voucher_id JOIN ledgers l ON l.id=i.party_id
               ORDER BY v.date DESC, v.id DESC LIMIT 200""").fetchall()
        return [{"id": r["id"], "date": display_date(r["date"]), "kind": r["kind"], "number": r["number"],
                 "ref": r["ref"], "party": r["pname"], "taxable": fmt(r["taxable"]), "cgst": fmt(r["cgst"]),
                 "sgst": fmt(r["sgst"]), "igst": fmt(r["igst"]), "total": fmt(r["total"])} for r in rows]

    # ---- bank statements ---------------------------------------------------
    def bank_accounts(self, q, body, raw):
        return [{"id": l["id"], "name": l["name"]} for l in self._b().ledgers("Bank Accounts")]

    def bank_txns(self, q, body, raw):
        b = self._b()
        rows = b.bank_txns(_int(q.get("bank_id"), "bank account"), pending_only=q.get("posted") != "1")
        return [{"id": t["id"], "date": display_date(t["date"]), "narration": t["narration"],
                 "debit": fmt(t["debit"]) if t["debit"] else "", "credit": fmt(t["credit"]) if t["credit"] else "",
                 "ledger_id": t["counter_ledger_id"], "posted": bool(t["voucher_id"]),
                 "uncertain": bool(t["uncertain"])} for t in rows]

    def bank_import(self, q, body, raw):
        b = self._b()
        bank_id = _int(q.get("bank_id"), "bank account")
        if not b.get_ledger(bank_id):
            raise ApiError("Unknown bank account")
        if not raw or not raw.startswith(b"%PDF"):
            raise ApiError("That file is not a PDF.")
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            txns = parse_statement(tmp)
        except StatementError as e:
            raise ApiError(str(e))
        finally:
            os.unlink(tmp)
        new, dup = b.stage_bank_transactions(bank_id, q.get("filename") or "statement.pdf", txns)
        return {"new": new, "duplicates": dup, "warnings": check_consistency(txns)[:10]}

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
        posted, skipped = b.post_bank_transactions(ids, use_suspense=bool(body.get("suspense")))
        return {"posted": posted, "skipped": skipped}

    # ---- reports -----------------------------------------------------------
    def _report(self, q):
        from ..accounting import parse_date
        return reports.run_report(self._b(), q.get("name", ""), parse_date(q.get("start", "")),
                                  parse_date(q.get("end", "")), q.get("ledger_id") or None)

    def report(self, q, body, raw):
        r = self._report(q)
        return {"title": r.title, "columns": r.columns, "rows": r.rows}

    def report_csv(self, q, body, raw):
        r = self._report(q)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([r.title]); w.writerow(r.columns); w.writerows(r.rows)
        return ("text/csv; charset=utf-8", ("﻿" + buf.getvalue()).encode("utf-8"))


ROUTES = {
    ("GET", "/api/ping"): Api.ping, ("GET", "/api/meta"): Api.meta,
    ("GET", "/api/companies"): Api.companies, ("POST", "/api/company/create"): Api.company_create,
    ("POST", "/api/company/open"): Api.company_open, ("POST", "/api/company/close"): Api.company_close,
    ("GET", "/api/ledgers"): Api.ledgers, ("POST", "/api/ledgers"): Api.ledger_add,
    ("POST", "/api/ledgers/delete"): Api.ledger_delete,
    ("GET", "/api/items"): Api.items, ("POST", "/api/items"): Api.item_add,
    ("POST", "/api/items/delete"): Api.item_delete,
    ("GET", "/api/vouchers"): Api.vouchers, ("POST", "/api/vouchers"): Api.voucher_add,
    ("POST", "/api/vouchers/delete"): Api.voucher_delete,
    ("GET", "/api/invoices"): Api.invoices, ("POST", "/api/invoices"): Api.invoice_add,
    ("POST", "/api/invoices/preview"): Api.invoice_preview,
    ("GET", "/api/bank/accounts"): Api.bank_accounts, ("GET", "/api/bank/txns"): Api.bank_txns,
    ("POST", "/api/bank/import"): Api.bank_import, ("POST", "/api/bank/set_ledger"): Api.bank_set_ledger,
    ("POST", "/api/bank/post"): Api.bank_post,
    ("GET", "/api/report"): Api.report, ("GET", "/api/report.csv"): Api.report_csv,
}
