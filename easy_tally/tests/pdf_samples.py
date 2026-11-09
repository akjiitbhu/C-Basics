"""Generate sample bank statement PDFs (needs reportlab - test-only dependency)."""
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# (date, narration, debit, credit, balance)  opening balance 50,000.00
ROWS = [
    ("01/04/2025", "UPI/ACME TRADERS/payment", "12,500.00", "", "37,500.00"),
    ("03/04/2025", "NEFT CR-GLOBAL SOLUTIONS LTD", "", "1,25,000.00", "1,62,500.00"),
    ("05/04/2025", "ATM WDL MUMBAI", "5,000.00", "", "1,57,500.00"),
    ("05/04/2025", "ATM WDL MUMBAI", "5,000.00", "", "1,52,500.00"),
    ("10/04/2025", "SMS CHARGES Q1", "17.70", "", "1,52,482.30"),
    ("30/04/2025", "INTEREST CREDIT", "", "412.00", "1,52,894.30"),
]


def table_pdf(path):
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4))
    styles = getSampleStyleSheet()
    data = [["Txn Date", "Value Date", "Narration", "Cheque/Ref", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]]
    for d, n, dr, cr, bal in ROWS:
        data.append([d, d, n, "", dr, cr, bal])
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                           ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    doc.build([Paragraph("Statement of Account", styles["Title"]), Spacer(1, 12), t])


def text_pdf(path):
    """Line-based layout with no ruled table, single amount + balance per row."""
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "STATEMENT OF ACCOUNT"); y -= 14
    c.drawString(40, y, "Opening Balance 50,000.00"); y -= 20
    c.drawString(40, y, "Date  Narration  Amount  Balance"); y -= 16
    for d, n, dr, cr, bal in ROWS:
        amt = dr or cr
        c.drawString(40, y, f"{d.replace('/', '-')} {n} {amt} {bal}")
        y -= 14
        if n.startswith("UPI"):
            c.drawString(60, y, "Ref 4412 Sept invoice"); y -= 14   # continuation line
    c.drawString(40, y - 10, "Page 1 of 1")
    c.save()


def blank_image_pdf(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.rect(50, 50, 200, 200, fill=1)   # drawing only: no text layer
    c.save()
