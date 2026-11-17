"""Local user accounts, roles and sessions for the browser version.

Passwords are stored as salted PBKDF2-SHA256 hashes, never in clear text. Accounts live in
`users.db` next to the company files (not inside any company). Roles:

    admin       everything, including users, restore and Tally import
    accountant  read, enter and delete vouchers/masters
    viewer      read-only (reports, exports)
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from pathlib import Path

ROLE_PERMS = {
    "admin": {"read", "write", "delete", "admin"},
    "accountant": {"read", "write", "delete"},
    "viewer": {"read"},
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,40}$")
MIN_PASSWORD = 8
ITERATIONS = 260_000
SESSION_IDLE_SECONDS = 12 * 3600
MAX_FAILS, LOCK_SECONDS = 5, 60


class AuthError(ValueError):
    pass


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


class UserStore:
    def __init__(self, path, iterations: int = ITERATIONS):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.iterations = iterations
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            disabled INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        self.conn.commit()
        self._dummy = hash_password("not-a-real-password", iterations)   # equalises timing for unknown users
        self.fails = {}                                                   # username -> [count, locked_until]

    # -- validation -----------------------------------------------------------------
    @staticmethod
    def _check_new(username, password, role):
        if not USERNAME_RE.match(username or ""):
            raise AuthError("Username must be 3-40 characters: letters, digits, . _ @ -")
        if len(password or "") < MIN_PASSWORD:
            raise AuthError(f"Password must be at least {MIN_PASSWORD} characters")
        if role not in ROLE_PERMS:
            raise AuthError("Role must be admin, accountant or viewer")

    def has_users(self) -> bool:
        return self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def _admins(self):
        return self.conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]

    # -- CRUD -------------------------------------------------------------------------
    def create(self, username, password, role="viewer") -> int:
        username = (username or "").strip()
        self._check_new(username, password, role)
        try:
            cur = self.conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?,?,?)",
                                    (username, hash_password(password, self.iterations), role))
        except sqlite3.IntegrityError:
            raise AuthError(f"User '{username}' already exists")
        self.conn.commit()
        return cur.lastrowid

    def get(self, username):
        return self.conn.execute("SELECT * FROM users WHERE username=?", (username or "",)).fetchone()

    def get_id(self, user_id):
        return self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def list(self):
        return [{"id": r["id"], "username": r["username"], "role": r["role"], "disabled": bool(r["disabled"]),
                 "created": r["created"]} for r in self.conn.execute("SELECT * FROM users ORDER BY username")]

    def update(self, user_id, role=None, password=None, disabled=None):
        u = self.get_id(user_id)
        if not u:
            raise AuthError("User not found")
        new_role = role or u["role"]
        new_disabled = u["disabled"] if disabled is None else int(bool(disabled))
        if role is not None and role not in ROLE_PERMS:
            raise AuthError("Role must be admin, accountant or viewer")
        losing_admin = u["role"] == "admin" and not u["disabled"] and (new_role != "admin" or new_disabled)
        if losing_admin and self._admins() <= 1:
            raise AuthError("There must always be at least one active administrator")
        if password is not None:
            if len(password) < MIN_PASSWORD:
                raise AuthError(f"Password must be at least {MIN_PASSWORD} characters")
            self.conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                              (hash_password(password, self.iterations), user_id))
        self.conn.execute("UPDATE users SET role=?, disabled=? WHERE id=?", (new_role, new_disabled, user_id))
        self.conn.commit()

    def delete(self, user_id):
        u = self.get_id(user_id)
        if not u:
            raise AuthError("User not found")
        if u["role"] == "admin" and not u["disabled"] and self._admins() <= 1:
            raise AuthError("There must always be at least one active administrator")
        self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        self.conn.commit()

    # -- login --------------------------------------------------------------------------
    def verify(self, username, password):
        """Return the user row, or raise AuthError (generic message, with lockout after repeated failures)."""
        key = (username or "").strip().lower()
        count, until = self.fails.get(key, [0, 0])
        now = time.time()
        if until > now:
            raise AuthError(f"Too many failed attempts. Try again in {int(until - now) + 1} seconds.")
        u = self.get(username.strip() if username else "")
        ok = check_password(password or "", u["password_hash"] if u else self._dummy)
        if ok and u and not u["disabled"]:
            self.fails.pop(key, None)
            return u
        count += 1
        self.fails[key] = [count, now + LOCK_SECONDS if count >= MAX_FAILS else 0]
        if count >= MAX_FAILS:
            self.fails[key][0] = 0
        raise AuthError("Wrong username or password")


class Sessions:
    def __init__(self, idle: int = SESSION_IDLE_SECONDS):
        self.idle = idle
        self.tokens = {}          # token -> [username, last_seen]

    def create(self, username) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens[token] = [username, time.time()]
        return token

    def get(self, token):
        entry = self.tokens.get(token or "")
        if not entry:
            return None
        if time.time() - entry[1] > self.idle:
            del self.tokens[token]
            return None
        entry[1] = time.time()
        return entry[0]

    def destroy(self, token):
        self.tokens.pop(token or "", None)

    def drop_user(self, username):
        for t in [t for t, e in self.tokens.items() if e[0].lower() == username.lower()]:
            del self.tokens[t]
