import io
import os
import tempfile
import unittest
import zipfile

os.environ["EASYTALLY_HOME"] = tempfile.mkdtemp()

from easytally import auth, backup, db
from easytally.accounting import Books

FAST = 1000          # PBKDF2 iterations: fast for tests only


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.u = auth.UserStore(os.path.join(tempfile.mkdtemp(), "users.db"), iterations=FAST)

    def test_hash_is_salted_and_verifies(self):
        h1, h2 = auth.hash_password("secret123", FAST), auth.hash_password("secret123", FAST)
        self.assertNotEqual(h1, h2)
        self.assertNotIn("secret123", h1)
        self.assertTrue(auth.check_password("secret123", h1))
        self.assertFalse(auth.check_password("secret124", h1))
        self.assertFalse(auth.check_password("x", "garbage"))

    def test_create_validate_and_login(self):
        self.assertFalse(self.u.has_users())
        for bad in (("ab", "longenough1", "admin"), ("alice", "short", "admin"), ("alice", "longenough1", "root")):
            with self.assertRaises(auth.AuthError):
                self.u.create(*bad)
        self.u.create("Alice", "correct horse", "admin")
        with self.assertRaises(auth.AuthError):
            self.u.create("alice", "another pass1", "viewer")                  # case-insensitive unique
        self.assertEqual(self.u.verify("alice", "correct horse")["role"], "admin")
        with self.assertRaises(auth.AuthError):
            self.u.verify("alice", "wrong")
        with self.assertRaises(auth.AuthError):
            self.u.verify("nobody", "whatever1")                               # same generic error

    def test_lockout_after_repeated_failures(self):
        self.u.create("bob", "bobs password", "viewer")
        for _ in range(auth.MAX_FAILS):
            with self.assertRaises(auth.AuthError):
                self.u.verify("bob", "nope")
        with self.assertRaises(auth.AuthError) as cm:
            self.u.verify("bob", "bobs password")                              # even the right password is refused
        self.assertIn("Too many", str(cm.exception))

    def test_last_admin_protected_and_disable(self):
        a = self.u.create("root1", "password one", "admin")
        v = self.u.create("view1", "password two", "viewer")
        for fn in (lambda: self.u.delete(a), lambda: self.u.update(a, role="viewer"),
                   lambda: self.u.update(a, disabled=True)):
            with self.assertRaises(auth.AuthError):
                fn()
        self.u.update(v, disabled=True)
        with self.assertRaises(auth.AuthError):
            self.u.verify("view1", "password two")                             # disabled accounts cannot sign in
        self.u.update(v, password="new password 3", disabled=False)
        self.assertEqual(self.u.verify("view1", "new password 3")["username"], "view1")
        b = self.u.create("root2", "password four", "admin")
        self.u.delete(a)                                                        # fine once another admin exists
        self.assertEqual(len(self.u.list()), 2)

    def test_sessions(self):
        s = auth.Sessions(idle=0.05)
        t = s.create("alice")
        self.assertEqual(s.get(t), "alice")
        self.assertIsNone(s.get("forged"))
        s.drop_user("ALICE")
        self.assertIsNone(s.get(t))
        t2 = s.create("alice")
        import time; time.sleep(0.08)
        self.assertIsNone(s.get(t2))                                            # idle timeout


class BackupTests(unittest.TestCase):
    def make(self, name):
        b = Books.create(name, state="27")
        self.addCleanup(b.close)
        cash = b.ledger_id("Cash")
        b.add_voucher("Receipt", "01-04-2026", [(cash, "Dr", 100), (b.ledger_id("Sales"), "Cr", 100)])
        return b

    def test_backup_and_restore_as_copy_then_overwrite(self):
        b = self.make("Backup Co One")
        data = backup.backup_company(b)
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertIn("companies/backup_co_one.db", z.namelist())
        self.assertNotIn("users", " ".join(z.namelist()))
        # modify the live company, then restore without overwrite -> separate copy with the old state
        b.add_voucher("Receipt", "02-04-2026", [(b.ledger_id("Cash"), "Dr", 50), (b.ledger_id("Sales"), "Cr", 50)])
        res = backup.restore(data)
        self.assertEqual(res[0]["name"], "Backup Co One (restored)")
        copy = Books.open(res[0]["slug"]); self.addCleanup(copy.close)
        self.assertEqual(len(copy.vouchers()), 1)
        self.assertEqual(len(b.vouchers()), 2)                                  # original untouched
        # overwrite replaces it and keeps a .bak of the replaced file
        b.close()
        res = backup.restore(data, overwrite=True)
        self.assertTrue(res[0]["replaced"])
        self.assertTrue(db.company_path("backup_co_one").with_suffix(".db.bak").exists())
        again = Books.open("backup_co_one"); self.addCleanup(again.close)
        self.assertEqual(len(again.vouchers()), 1)

    def test_backup_all_and_bad_inputs(self):
        self.make("Backup Co Two")
        z = zipfile.ZipFile(io.BytesIO(backup.backup_all()))
        self.assertTrue(any(n.endswith("backup_co_two.db") for n in z.namelist()))
        for bad in (b"hello", b"PK not a zip", b"SQLite format 3\x00" + b"\x00" * 200):
            with self.assertRaises(ValueError):
                backup.restore(bad)
        evil = io.BytesIO()
        with zipfile.ZipFile(evil, "w") as zz:
            zz.writestr("../../evil.db", b"x")
        with self.assertRaises(ValueError):
            backup.restore(evil.getvalue())                                     # no companies/ entries -> refused
        # a plain SQLite file that is not ours
        import sqlite3
        p = os.path.join(tempfile.mkdtemp(), "x.db")
        c = sqlite3.connect(p); c.execute("create table t(a)"); c.commit(); c.close()
        with self.assertRaises(ValueError):
            backup.restore(open(p, "rb").read())

    def test_restore_plain_db_file(self):
        b = self.make("Backup Co Three")
        raw = backup._snapshot(b.path)
        res = backup.restore(raw)
        self.assertEqual(len(res), 1)
        self.addCleanup(lambda: None)


if __name__ == "__main__":
    unittest.main()
