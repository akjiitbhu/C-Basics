import io
import json
import os
import tempfile
import unittest

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from openpyxl import load_workbook
from easytally import gst_returns as G
from easytally.accounting import Books


class GstReturnTests(unittest.TestCase):
    def setUp(self):
        b = self.b = Books.create(f"GST {self.id().split('.')[-1]}", state="27")
        self.addCleanup(b.close)
        b.set_settings({"gstin": "27ABCDE1234F1Z5"})
        self.w18 = b.add_stock_item("Widget", unit="Nos", hsn="8501", gst_rate=18)
        self.w5 = b.add_stock_item("Snack", unit="Kg", hsn="2106", gst_rate=5)
        self.nil = b.add_stock_item("Book", hsn="4901", gst_rate=0)
        self.reg = b.add_ledger("Reg Local", "Sundry Debtors", gstin="27AAAAA0000A1Z5")     # same state
        self.far = b.add_ledger("Reg Far", "Sundry Debtors", gstin="29BBBBB1111B1Z5")        # inter-state
        self.walk = b.add_ledger("Walk-in", "Sundry Debtors", state="27")
        self.bigunreg = b.add_ledger("Big Unreg", "Sundry Debtors", state="29")
        self.sup = b.add_ledger("Supplier", "Sundry Creditors", state="27")
        b.add_invoice("Sales", "05-04-2026", self.reg, [(self.w18, 10, "100"), (self.w5, 2, "50")], ref="A/1")
        b.add_invoice("Sales", "06-04-2026", self.far, [(self.w18, 1, "1000")], ref="A/2")
        b.add_invoice("Sales", "07-04-2026", self.walk, [(self.w18, 1, "200"), (self.nil, 1, "100")], ref="A/3")
        b.add_invoice("Sales", "08-04-2026", self.bigunreg, [(self.w18, 1000, "300")], ref="A/4")   # 3.54 lakh inter-state
        b.add_invoice("Credit Note", "09-04-2026", self.reg, [(self.w18, 1, "100")], ref="CN/1")
        b.add_invoice("Credit Note", "10-04-2026", self.walk, [(self.w18, 1, "100")], ref="CN/2")
        b.add_invoice("Sales", "05-05-2026", self.reg, [(self.w18, 1, "100")], ref="A/9")            # other month
        b.add_invoice("Purchase", "11-04-2026", self.sup, [(self.w18, 10, "50")], ref="P1")
        b.add_invoice("Debit Note", "12-04-2026", self.sup, [(self.w18, 2, "50")], ref="DN1")

    def test_gstr1_sections(self):
        d, warnings = G.gstr1(self.b, 2026, 4)
        self.assertEqual((d["gstin"], d["fp"]), ("27ABCDE1234F1Z5", "042026"))
        ctins = {x["ctin"]: x["inv"] for x in d["b2b"]}
        self.assertEqual(set(ctins), {"27AAAAA0000A1Z5", "29BBBBB1111B1Z5"})
        inv = ctins["27AAAAA0000A1Z5"][0]
        self.assertEqual((inv["inum"], inv["idt"], inv["pos"], inv["val"]), ("A/1", "05-04-2026", "27", 1285.0))
        rates = {i["itm_det"]["rt"]: i["itm_det"] for i in inv["itms"]}
        self.assertEqual((rates[18]["txval"], rates[18]["camt"], rates[18]["samt"], rates[18]["iamt"]),
                         (1000.0, 90.0, 90.0, 0))
        self.assertEqual(rates[5]["txval"], 100.0)
        far = ctins["29BBBBB1111B1Z5"][0]["itms"][0]["itm_det"]
        self.assertEqual((far["iamt"], far["camt"]), (180.0, 0))
        # >2.5 lakh unregistered inter-state -> B2CL
        self.assertEqual([(x["pos"], x["inv"][0]["inum"]) for x in d["b2cl"]], [("29", "A/4")])
        # small unregistered -> B2CS, net of the credit note CN/2 (200 - 100 at 18%) plus nil-rated 100
        cs = {(x["rt"], x["sply_ty"]): x for x in d["b2cs"]}
        self.assertEqual(cs[(18, "INTRA")]["txval"], 100.0)
        self.assertEqual(cs[(18, "INTRA")]["camt"], 9.0)
        self.assertEqual(cs[(0, "INTRA")]["txval"], 100.0)
        # registered credit note
        self.assertEqual(d["cdnr"][0]["ctin"], "27AAAAA0000A1Z5")
        self.assertEqual(d["cdnr"][0]["nt"][0]["ntty"], "C")
        # HSN summary is net of credit notes: widget outward 10+1+1+1000-1-1 = 1010
        h = {x["hsn_sc"]: x for x in d["hsn"]["data"]}
        self.assertEqual(h["8501"]["qty"], 1010)
        self.assertEqual(h["8501"]["uqc"], "NOS")
        self.assertEqual(h["2106"]["uqc"], "KGS")
        docs = {x["doc_typ"]: x["docs"][0] for x in d["doc_issue"]["doc_det"]}
        self.assertEqual((docs["Invoices for outward supply"]["from"], docs["Invoices for outward supply"]["to"],
                          docs["Invoices for outward supply"]["totnum"]), ("A/1", "A/4", 4))
        json.dumps(d)                                                        # serialisable
        self.assertTrue(any("gt" in w for w in warnings))

    def test_gstr3b(self):
        d, payable, warnings = G.gstr3b(self.b, 2026, 4)
        o = d["sup_details"]["osup_det"]
        # taxable at >0%: A/1 1100 + A/2 1000 + A/3 200 + A/4 300000 - CN/1 100 - CN/2 100 = 302100
        self.assertEqual(o["txval"], 302100.0)
        self.assertEqual(o["camt"], round((1000 * .09 + 100 * .025 + 200 * .09 - 100 * .09 - 100 * .09), 2))
        self.assertEqual(o["iamt"], round(1000 * .18 + 300000 * .18, 2))
        self.assertEqual(d["sup_details"]["osup_nil_exmp"]["txval"], 100.0)
        self.assertEqual(d["inter_sup"]["unreg_details"], [{"pos": "29", "txval": 300000.0, "iamt": 54000.0}])
        itc = d["itc_elg"]["itc_net"]                                        # purchase 500 - debit note 100 @18% intra
        self.assertEqual((itc["camt"], itc["samt"], itc["iamt"]), (36.0, 36.0, 0))
        self.assertEqual(payable["camt"], round(o["camt"] - 36.0, 2))
        self.assertTrue(any("ITC" in w for w in warnings))

    def test_excel_and_warnings(self):
        data, w = G.gstr1(self.b, 2026, 4)
        wb = load_workbook(io.BytesIO(G.gstr1_xlsx(data, w)))
        self.assertIn("B2B", wb.sheetnames)
        self.assertEqual(wb["B2B"]["B2"].value, "A/1")
        d3, pay, w3 = G.gstr3b(self.b, 2026, 4)
        wb3 = load_workbook(io.BytesIO(G.gstr3b_xlsx(d3, pay, w3)))
        self.assertEqual(wb3["GSTR-3B"]["A2"].value, "3.1(a) Outward taxable supplies")

    def test_data_quality_warnings(self):
        b = Books.create("GST Warn Co", state="")
        self.addCleanup(b.close)
        item = b.add_stock_item("NoHSN", gst_rate=18)
        c = b.add_ledger("C", "Sundry Debtors")
        b.add_invoice("Sales", "01-04-2026", c, [(item, 1, "10")], ref="THIS-NUMBER-IS-FAR-TOO-LONG")
        _, warnings = G.gstr1(b, 2026, 4)
        text = " | ".join(warnings)
        for needle in ("GSTIN is not set", "state is not set", "1-16 characters", "no HSN"):
            self.assertIn(needle, text)
        with self.assertRaises(ValueError):
            G.gstr1(b, 2026, 13)


if __name__ == "__main__":
    unittest.main()
