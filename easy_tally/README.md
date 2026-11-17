# EasyTally

A simplified, Tally-style accounting desktop app for small businesses, built for an
accountant or consultant who serves **many customers**, with **bank statement PDF import**.

## Run

```bash
cd easy_tally
pip install -r requirements.txt     # pdfplumber, reportlab, openpyxl
python run.py                        # needs Python 3.9+ with Tkinter
```
Linux: if `import tkinter` fails, install it (`sudo apt install python3-tk`).

## Two ways to use it (same data, same features)

- **Desktop window:** `EasyTally` shortcut (or `python run.py`).
- **In your browser, offline:** `EasyTally (Browser)` shortcut (or `python run.py --web`). It starts a
  small server on `127.0.0.1:8765` (this PC only, nothing is sent over the internet, no internet needed)
  and opens your browser. Use the **Quit** button to stop it. Needs no Tkinter, so it also works on
  Mac/Linux with just Python.

Both versions read the same company files, so you can switch between them.

## Install on Windows (no Python needed)

Download `EasyTally-Setup.exe` from the **Actions** tab of the GitHub repository (open the latest
"Build Windows installer" run, then the `EasyTally-Setup` artifact), or from a Release if you attach it.
Double-click, press Next, and EasyTally appears in the Start menu and on the desktop. It runs fully
offline on that one machine. All customer data is in `%USERPROFILE%\EasyTally` and is kept when the
app is uninstalled. To back up or move to another PC, copy that folder.

Windows may show a "SmartScreen" warning because the installer is not code-signed; choose
*More info -> Run anyway*. (A code-signing certificate removes this.)

To build it yourself on Windows: run `build_windows.bat` in this folder.

## Features

| Area | What it does |
|---|---|
| Multi-customer | Each customer company is its own SQLite file in `~/EasyTally/companies/` (override with `EASYTALLY_HOME`). |
| Masters | Tally-style groups, ledgers (opening balance, GSTIN, state), stock items (HSN, GST rate). GSTINs are format-checked. |
| Vouchers | Payment, Receipt, Journal, Contra. Debits must equal credits. **Vouchers can be edited** in place (type and number are kept). |
| GST documents | Sales and purchase invoices, **credit notes** (sales returns) and **debit notes** (purchase returns). CGST+SGST or IGST by state; stock follows. Invoices and notes can be edited. |
| **PDF invoices** | One-click printable tax invoice / credit note / debit note with company details, HSN, tax split, amount in words (Indian system), bank details and terms. |
| **Bank statement import** | Pick PDFs, review, map to ledgers, post as vouchers (details below). |
| **Receivables and payables** | Ageing (0-30 / 31-60 / 61-90 / over 90 days) and bill-wise outstanding per customer or supplier, with receipts, payments and notes settled oldest-first (FIFO). |
| **GST returns** | **GSTR-1** (B2B, B2C large and small, credit notes, HSN summary, document series) and **GSTR-3B** summary as JSON in the GST offline-tool layout, plus Excel working papers and a list of warnings (missing HSN, bad GSTIN, long invoice numbers...). |
| **Tally exchange** | Export ledgers and vouchers as Tally XML; import a Tally XML export (groups, ledgers, opening balances, accounting vouchers). Imports are de-duplicated. |
| **Backup and restore** | One-click zip backup of a company or all companies (consistent even while running). Restore as a copy, or replace (the old file is kept as `.bak`). |
| **Users, roles, audit** | Browser version: sign-in required. Roles: admin (everything), accountant (enter, edit, delete data), viewer (reports only). Every change is written to a per-company audit trail. |
| Reports | Day Book, Ledger, Trial Balance, P&L (with opening/closing stock), Balance Sheet, GST Summary, Stock Summary, the ageing reports, all exportable to CSV. |

## How bank statement import works

1. **Parse** (`easytally/bank_import.py`). Text-based PDFs only (the kind downloaded from net
   banking). Two strategies: ruled tables located by header names (Date / Narration /
   Withdrawal / Deposit / Balance), then line-by-line text. In text mode, debit vs credit is
   derived from the change in the running balance, not guessed from words.
2. **Verify.** The running balance is re-checked row by row; breaks (a missing or misread row)
   and guessed directions are reported and highlighted yellow ("Check Dr/Cr").
3. **Stage + dedupe.** Rows go to a review grid. Re-importing the same or an overlapping
   statement never creates duplicates, and two genuinely identical same-day transactions are
   both kept.
4. **Map to ledgers.** Keyword rules suggest a ledger (for example `SMS CHARGES` -> Bank Charges).
   Set a ledger by hand, and tick "Remember" to learn a keyword for later imports.
5. **Post.** Post selected rows, all rows with a ledger, or everything (unmapped rows go to
   `Suspense A/c` so the bank ledger still matches the statement). Deleting a voucher returns
   its bank row to Pending.

Scanned or photographed statements are detected and rejected with a clear message (OCR is not
included). Every bank formats statements differently, so if a bank is not recognised, add a
sample to `tests/` and extend `_locate_columns` or the text parser.

## Accounting notes

- Amounts are stored as integer paise, never floats.
- Stock is valued at weighted-average cost. Credit and debit notes reduce sales and purchases. P&L
  includes opening and closing stock, and the Balance Sheet includes stock-in-hand. Opening stock has no
  ledger, so include its value in capital or opening balances, or the Trial Balance shows a difference.
- Outstanding bills use **FIFO settlement**: any receipt or payment settles the oldest open bill. There is
  no manual "against reference" allocation.

## Security model (browser version)

- The server listens on `127.0.0.1` only, refuses other Host/Origin headers, and requires a custom header
  on every change, so other websites open in your browser cannot read or alter your books.
- Passwords are salted PBKDF2-SHA256 hashes in `users.db` (not inside company files). Five failed
  sign-ins lock that username for a minute. Sessions are HttpOnly, SameSite=Strict cookies, and expire
  after 12 idle hours. Signing out closes the open company.
- **The desktop window has no sign-in.** Use the browser version if you need logins.
- Forgot the admin password? Close EasyTally and delete `users.db` in the EasyTally folder. Accounts
  reset; accounting data is untouched.
- Only one company is open at a time on the server, shared by everyone signed in on that PC.

## Known limits

- **GST returns are prepared, not filed.** Always read the warnings, then validate the JSON in the GST
  offline tool or portal: the portal's schema changes. Not covered: GSTR-2A/2B matching, RCM, composition
  and SEZ/export cases, e-invoice IRN, e-way bills, TDS/TCS, GSTR-9.
- **Tally exchange** covers ledgers and accounting vouchers only. Stock items, inventory entries and bill
  references are not carried over, and imported sales are plain vouchers, so they do not feed the GST returns.
  Tally's XML varies by version; test with a copy before relying on it.
- Not included: payroll, cost centres, budgets, multi-currency, bank reconciliation screen, financial-year
  closing, multi-PC or network use (one machine only).
- The desktop window has the same books but not every new screen: editing vouchers and invoices, PDF
  invoices, GST returns, Tally exchange, backup, users and audit are in the browser version. Credit/debit
  notes and the new reports are available in both.

## Tests

```bash
python -m unittest discover -s tests -t .
```
The web tests start a real local server and check the JSON API and its security rules
(foreign hosts/origins are refused). The desktop UI test uses a stub `tkinter`, so it checks wiring and logic, not appearance.

## Layout

```
easytally/
  db.py           schema, default groups/ledgers, company files
  accounting.py   Books: masters, vouchers, GST invoices, bank staging/posting
  bank_import.py  PDF statement parser and consistency checks
  gst.py          GST rates, state codes, CGST/SGST/IGST maths
  gst_returns.py  GSTR-1 / GSTR-3B JSON and Excel
  invoice_pdf.py  printable invoices, amount in words
  tally_xml.py    Tally XML import and export
  backup.py       backup / restore of company files
  auth.py         users, roles, sessions (browser version)
  reports.py      all reports + CSV export
  ui/             Tkinter desktop screens (thin layer over the modules above)
  web/            offline browser version: api.py (JSON API), server.py (localhost server), static/index.html
```
