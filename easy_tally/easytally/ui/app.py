"""Main window: company (customer) picker, then the tabbed workspace."""
import tkinter as tk
from tkinter import ttk, messagebox

from ..accounting import Books
from .tabs import (BankTab, InvoiceTab, MastersTab, ReportsTab, VoucherTab, STATE_CHOICES,
                   state_code)
from .widgets import ask_form


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("EasyTally")
        self.geometry("1100x720")
        self.books = None
        self.tabs = []
        self.show_picker()

    def _clear(self):
        for w in self.winfo_children():
            w.destroy()
        self.config(menu=tk.Menu(self))

    # ---- company picker ---------------------------------------------------
    def show_picker(self):
        if self.books:
            self.books.close()
            self.books = None
        self._clear()
        self.title("EasyTally - Select Company")
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Select a customer company", font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        self.company_list = tk.Listbox(frame, height=14, font=("TkDefaultFont", 11))
        self.company_list.pack(fill="both", expand=True, pady=10)
        self.company_list.bind("<Double-Button-1>", lambda *_: self.open_selected())
        self.slugs = []
        for slug, name in Books.list_companies():
            self.slugs.append(slug)
            self.company_list.insert("end", name)
        bar = ttk.Frame(frame); bar.pack(fill="x")
        ttk.Button(bar, text="Open", command=self.open_selected).pack(side="left")
        ttk.Button(bar, text="New Company…", command=self.new_company).pack(side="left", padx=6)

    def open_selected(self):
        sel = self.company_list.curselection()
        if sel:
            self.show_main(Books.open(self.slugs[sel[0]]))

    def new_company(self):
        f = ask_form(self, "New Company", [
            ("name", "Company name", "entry", ""),
            ("state", "State", "combo", (STATE_CHOICES, "")),
            ("gstin", "GSTIN (optional)", "entry", "")])
        if not f:
            return
        try:
            books = Books.create(f["name"], state_code(f["state"]), f["gstin"])
        except ValueError as e:
            return messagebox.showerror("EasyTally", str(e))
        self.show_main(books)

    # ---- workspace --------------------------------------------------------
    def show_main(self, books):
        self.books = books
        self._clear()
        self.title(f"EasyTally - {books.company_name}")
        menu = tk.Menu(self)
        filemenu = tk.Menu(menu, tearoff=0)
        filemenu.add_command(label="Switch Company…", command=self.show_picker)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.destroy)
        menu.add_cascade(label="File", menu=filemenu)
        self.config(menu=menu)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.tabs = []
        for label, cls in (("Masters", MastersTab), ("Vouchers", VoucherTab), ("Invoices (GST)", InvoiceTab),
                           ("Bank Statements", BankTab), ("Reports", ReportsTab)):
            tab = cls(nb, self)
            nb.add(tab, text=label)
            self.tabs.append(tab)
        nb.bind("<<NotebookTabChanged>>", lambda *_: self.refresh_all())
        self.refresh_all()

    def refresh_all(self):
        for t in self.tabs:
            t.refresh()


def main():
    App().mainloop()
