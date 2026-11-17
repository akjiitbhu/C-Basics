"""Backup and restore of company files.

A backup is a zip holding one consistent snapshot (taken with SQLite's online backup API, so it is
safe while the app is running) per company. User accounts are deliberately NOT included.
"""
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__, db
from .accounting import Books

MAX_RESTORE_BYTES = 1024 * 1024 * 1024        # refuse zips that expand beyond 1 GB
REQUIRED_TABLES = {"settings", "ledgers", "groups", "vouchers", "entries"}


def _snapshot(path: Path) -> bytes:
    """Consistent copy of a live SQLite file."""
    src = sqlite3.connect(str(path))
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    try:
        dst = sqlite3.connect(fd.name)
        src.backup(dst)
        dst.close()
        return Path(fd.name).read_bytes()
    finally:
        src.close()
        Path(fd.name).unlink(missing_ok=True)


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"app": "EasyTally", "version": __version__,
                                                "created": datetime.now().isoformat(timespec="seconds"),
                                                "companies": sorted(files)}))
        for name, data in files.items():
            z.writestr(f"companies/{name}.db", data)
    return buf.getvalue()


def backup_company(b: Books) -> bytes:
    b.conn.commit()
    return _zip({b.path.stem: _snapshot(b.path)})


def backup_all() -> bytes:
    return _zip({p.stem: _snapshot(p) for p in sorted(db.companies_dir().glob("*.db"))})


def _validate(data: bytes):
    """Return (company name) if `data` is a healthy EasyTally company database, else raise ValueError."""
    if not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("That file is not an EasyTally company database.")
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    try:
        fd.write(data)
        fd.close()
        conn = sqlite3.connect(fd.name)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("The backup file is damaged (integrity check failed).")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED_TABLES <= tables:
                raise ValueError("That database is not an EasyTally company file.")
            row = conn.execute("SELECT value FROM settings WHERE key='name'").fetchone()
            return row[0] if row else "Restored company"
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise ValueError(f"Could not read the backup: {e}")
    finally:
        Path(fd.name).unlink(missing_ok=True)


def _extract(data: bytes):
    """-> [(slug, bytes)] from a backup zip or a single .db file."""
    if not data.startswith(b"PK"):
        return [("restored", data)]
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That file is not a valid backup.")
    infos = [i for i in z.infolist() if i.filename.startswith("companies/") and i.filename.endswith(".db")]
    if not infos:
        raise ValueError("The backup contains no companies.")
    if sum(i.file_size for i in infos) > MAX_RESTORE_BYTES:
        raise ValueError("The backup is too large to restore.")
    return [(db.slugify(Path(i.filename).stem), z.read(i)) for i in infos]     # never use zip paths directly


def restore(data: bytes, overwrite: bool = False):
    """Restore companies. Existing companies are kept unless overwrite=True (then the old file is kept
    as <name>.db.bak). Without overwrite, a clash is restored as a new company named '... (restored)'.
    Returns [{'slug', 'name', 'replaced'}]."""
    items = _extract(data)
    checked = [(slug, blob, _validate(blob)) for slug, blob in items]          # validate everything first
    done = []
    for slug, blob, name in checked:
        target = db.company_path(slug)
        replaced = False
        if target.exists():
            if overwrite:
                target.replace(target.with_suffix(".db.bak"))
                replaced = True
            else:
                n = 1
                while True:
                    cand = f"{slug}_restored" + (f"_{n}" if n > 1 else "")
                    if not db.company_path(cand).exists():
                        slug, name = cand, f"{name} (restored{'' if n == 1 else ' ' + str(n)})"
                        target = db.company_path(slug)
                        break
                    n += 1
        target.write_bytes(blob)
        conn = db.connect(target)
        try:
            db.migrate(conn)                                                   # bring older backups up to date
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('name', ?)", (name,))
            conn.commit()
        finally:
            conn.close()
        done.append({"slug": slug, "name": name, "replaced": replaced})
    return done
