"""Local web server: serves the single-page UI and the JSON API on 127.0.0.1 only (works offline)."""
import json
import sys
import threading
import urllib.request
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .api import Api

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 8765
MAX_UPLOAD = 300 * 1024 * 1024     # statements are small; this allows large backups to be restored


class Handler(BaseHTTPRequestHandler):
    server_version = "EasyTally"
    api: Api = None
    allowed_hosts = set()

    def log_message(self, *args):    # keep the console/exe quiet
        pass

    # -- security: only the local machine may talk to us, and only from our own page ----------
    def _host_ok(self):
        return (self.headers.get("Host") or "") in self.allowed_hosts

    def _token(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        return c["et_session"].value if "et_session" in c else None

    def _send(self, status, payload, ctype="application/json; charset=utf-8", extra=None, cookie=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if cookie:
            kind, token = cookie
            if kind == "set":
                self.send_header("Set-Cookie", f"et_session={token}; HttpOnly; SameSite=Strict; Path=/")
            else:
                self.send_header("Set-Cookie", "et_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "Forbidden host"})
        url = urlsplit(self.path)
        query = dict(parse_qsl(url.query))
        if method == "GET" and not url.path.startswith("/api/"):
            page = STATIC / ("index.html" if url.path in ("/", "/index.html") else "")
            if page.name != "index.html" or not page.exists():
                return self._send(404, {"error": "Not found"})
            return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
        body, raw = {}, None
        if method == "POST":
            origin = self.headers.get("Origin")
            if self.headers.get("X-EasyTally") != "1" or (origin and urlsplit(origin).netloc not in self.allowed_hosts):
                return self._send(403, {"error": "Forbidden"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                return self._send(413, {"error": "File too large"})
            data = self.rfile.read(n)
            if "json" in (self.headers.get("Content-Type") or ""):
                try:
                    body = json.loads(data or b"{}")
                except ValueError:
                    return self._send(400, {"error": "Bad JSON"})
            else:
                raw = data
            if url.path == "/api/shutdown":
                if self.api.users.has_users() and not self.api.sessions.get(self._token()):
                    return self._send(401, {"error": "Please sign in."})
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        status, payload, cookie = self.api.handle(method, url.path, query, body, raw, self._token())
        if isinstance(payload, tuple):
            ctype, data, filename = payload
            inline = ctype.split(";")[0] in ("application/pdf", "image/jpeg", "text/html") and not query.get("download")
            headers = {"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{filename}"'}
            if ctype.startswith("text/html"):          # user-entered names are escaped, and scripts are blocked anyway
                headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
            return self._send(status, data, ctype, headers, cookie)
        self._send(status, payload, cookie=cookie)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def make_server(port=0, api=None):
    """Create (not start) a server bound to localhost. port=0 picks a free port."""
    handler = type("BoundHandler", (Handler,), {"api": api or Api()})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    p = srv.server_address[1]
    handler.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
    srv.daemon_threads = True
    return srv


def _already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == "EasyTally"
    except Exception:
        return False


def main(open_browser=True):
    port = DEFAULT_PORT
    if _already_running(port):                 # second launch: just show the existing instance
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    srv = None
    for p in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        try:
            srv = make_server(p)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        sys.exit("EasyTally could not find a free port.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
