"""Small reusable Tkinter widgets."""
import tkinter as tk
from tkinter import ttk


class DataTable(ttk.Frame):
    """Treeview with scrollbars and helpers for replacing columns and rows."""

    def __init__(self, master, columns, widths=None, height=10, selectmode="extended"):
        super().__init__(master)
        self.tree = ttk.Treeview(self, show="headings", height=height, selectmode=selectmode)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.tag_configure("muted", foreground="#888888")
        self.tree.tag_configure("warn", background="#fff3cd")
        self.tree.tag_configure("bold", font=("TkDefaultFont", 10, "bold"))
        self.set_columns(columns, widths)

    def set_columns(self, columns, widths=None):
        self.tree["columns"] = list(columns)
        for i, c in enumerate(columns):
            numeric = c in ("Debit", "Credit", "Amount", "Balance", "Taxable", "CGST", "SGST", "IGST",
                            "Value", "Avg Cost", "Qty", "Rate", "Total", "Opening", "Closing Qty")
            self.tree.heading(c, text=c)
            self.tree.column(c, width=(widths[i] if widths else 110), anchor="e" if numeric else "w",
                             stretch=True)

    def set_rows(self, rows, iids=None, tags=None):
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(rows):
            self.tree.insert("", "end", iid=str(iids[i]) if iids else None, values=list(row),
                             tags=(tags[i],) if tags and tags[i] else ())

    def selected(self):
        return list(self.tree.selection())


def ask_form(parent, title, fields):
    """Modal form. fields: [(key, label, 'entry'|'combo'|'check', value)].

    For 'combo' value is (choices, default). Returns {key: str} (check -> bool) or None if cancelled.
    """
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    vars_ = {}
    for r, (key, label, kind, opt) in enumerate(fields):
        if kind == "check":
            var = tk.BooleanVar(value=bool(opt))
            ttk.Checkbutton(win, text=label, variable=var).grid(row=r, column=0, columnspan=2,
                                                                sticky="w", padx=8, pady=4)
        else:
            ttk.Label(win, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            if kind == "combo":
                choices, default = opt
                var = tk.StringVar(value=default)
                ttk.Combobox(win, textvariable=var, values=list(choices), state="readonly",
                             width=36).grid(row=r, column=1, padx=8, pady=4)
            else:
                var = tk.StringVar(value=opt or "")
                ttk.Entry(win, textvariable=var, width=38).grid(row=r, column=1, padx=8, pady=4)
        vars_[key] = var
    result = {}

    def ok(*_):
        result.update({k: v.get() for k, v in vars_.items()})
        win.destroy()

    bar = ttk.Frame(win)
    bar.grid(row=len(fields), column=0, columnspan=2, pady=8)
    ttk.Button(bar, text="OK", command=ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="left", padx=4)
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda *_: win.destroy())
    win.grab_set()
    win.wait_window()
    return result or None
