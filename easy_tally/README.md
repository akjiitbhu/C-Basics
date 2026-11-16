# EasyTally

A simplified, Tally-style accounting desktop app for small businesses, built for an
accountant or consultant who serves **many customers**, with **bank statement PDF import**.

## Run

```bash
cd easy_tally
pip install -r requirements.txt     # pdfplumber
python run.py                        # needs Python 3.9+ with Tkinter
```
Linux: if `import tkinter` fails, install it (`sudo apt install python3-tk`).

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
| Multi-customer | Each customer company is its own SQLite file in `~/EasyTally/companies/` (override with `EASYTALLY_HOME`). Pick or create a company at start; back up by copying the file. |
| Masters | Account groups (Tally-style defaults), ledgers with opening balances, GSTIN and state, stock items with HSN and GST rate. |
| Vouchers | Payment, Receipt, Journal, Contra. Double entry is enforced: debits must equal credits. |
| GST invoices | Sales and purchase invoices; CGST+SGST for same-state parties, IGST otherwise; stock movement recorded. |
| **Bank statement import** | Choose a bank account, pick one or more statement PDFs. Rows are parsed, shown for review, then posted as Payment (withdrawal) or Receipt (deposit) vouchers. |
| Reports | Day Book, Ledger Statement, Trial Balance, Profit & Loss, Balance Sheet, GST Summary, Stock Summary; all export to CSV (opens in Excel). |

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
- Stock is valued at weighted-average cost. The Profit & Loss includes opening and closing
  stock, and the Balance Sheet includes stock-in-hand. Opening stock has no ledger, so include
  its value in your capital or opening balances, or the Trial Balance shows a difference.
- Not yet included: GST returns (GSTR-1/3B) files, e-invoice or e-way bill, TDS, payroll,
  PDF invoice printing, user logins, editing of posted vouchers (delete and re-enter).

## Tests

```bash
pip install reportlab       # only to generate sample PDFs for tests
python -m unittest discover -s tests -t .
```
The UI test uses a stub `tkinter`, so it checks wiring and logic, not appearance.

## Layout

```
easytally/
  db.py           schema, default groups/ledgers, company files
  accounting.py   Books: masters, vouchers, GST invoices, bank staging/posting
  bank_import.py  PDF statement parser and consistency checks
  gst.py          GST rates, state codes, CGST/SGST/IGST maths
  reports.py      all reports + CSV export
  ui/             Tkinter screens (thin layer over the modules above)
```
