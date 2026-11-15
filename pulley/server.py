"""The page and the few requests behind it."""
from __future__ import annotations

import base64
import hmac
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .engine import Busy

WEB = Path(__file__).parent / "web"
STATIC = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/app.css": ("app.css", "text/css"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
LOCAL = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 64 * 1024
TRIES, SHUT_FOR = 8, 600  # wrong passwords one address gets, and how long it is then turned away

# The page loads nothing from anywhere else and may not be shown inside another page.
POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
          "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Pulley"
    mgr = None  # set by serve()
    open_host = False
    password = ""
    _wrong: dict[str, list[float]] = {}
    _wrong_lock = threading.Lock()

    def log_message(self, *args) -> None:  # quiet
        pass

    # --- helpers ---------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        text = ctype.startswith("text/") or "json" in ctype or "svg" in ctype
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if text else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", POLICY)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

    def _refuse(self, code: int, message: str, extra: dict | None = None) -> None:
        """Turn a request away. Its body is taken off the wire first, or the reply never arrives."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if 0 < length <= MAX_BODY and not self.headers.get("Transfer-Encoding"):
            self.rfile.read(length)
        elif length or self.headers.get("Transfer-Encoding"):
            self.close_connection = True  # a body that is not read must not be taken for the next request
        self._send(code, json.dumps({"error": message}).encode("utf-8"), "application/json", extra)

    def _payload(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ValueError("That request is too large or not in a form Pulley reads.")
        raw = self.rfile.read(length) if length > 0 else b""
        data = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _allowed(self, mutating: bool) -> bool:
        host = self.headers.get("Host") or ""
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
        if not self.open_host and name.lower() not in LOCAL:
            return False  # another name pointed at this machine
        origin = self.headers.get("Origin")
        # Behind a reverse proxy the page's own address arrives as X-Forwarded-Host.
        if origin and urlparse(origin).netloc not in (host, self.headers.get("X-Forwarded-Host")):
            return False  # another site's page
        return not mutating or self.headers.get("X-Pulley") == "1"

    def _given(self) -> str | None:
        """The password sent with the request, or None when there is none."""
        head = self.headers.get("Authorization") or ""
        if not head.startswith("Basic "):
            return None
        try:
            return base64.b64decode(head[6:]).decode("utf-8").split(":", 1)[1]
        except (ValueError, IndexError):
            return ""

    def _shut_out(self, address: str, wrong: bool = False) -> bool:
        """Whether this address has guessed too often. `wrong` counts one more bad guess first."""
        now = time.time()
        with self._wrong_lock:
            recent = [t for t in self._wrong.get(address, []) if now - t < SHUT_FOR]
            if wrong:
                recent.append(now)
            if recent:
                self._wrong[address] = recent
            else:
                self._wrong.pop(address, None)
            return len(recent) >= TRIES

    def _let_in(self) -> bool:
        """Check the password. Sends the refusal itself when the answer is no."""
        if not self.password:
            return True
        address = self.client_address[0]
        ask = {"WWW-Authenticate": 'Basic realm="Pulley", charset="UTF-8"'}
        if self._shut_out(address):
            self._refuse(429, "Too many wrong passwords from this address. Try again in a few minutes.")
            return False
        given = self._given()
        if given is None:
            self._refuse(401, "Password required.", ask)
            return False
        if not hmac.compare_digest(given.encode("utf-8"), self.password.encode("utf-8")):
            self._shut_out(address, wrong=True)
            self._refuse(401, "Wrong password.", ask)
            return False
        return True

    # --- routing ---------------------------------------------------------------

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        try:
            if not self._let_in():
                return
            if not self._allowed(method != "GET"):
                return self._refuse(403, "Request refused.")
            if method == "GET" and url.path in STATIC:
                name, ctype = STATIC[url.path]
                return self._send(200, (WEB / name).read_bytes(), ctype)
            result = self._api(method, url.path, parse_qs(url.query))
            if result is not None:
                self._json(result)
        except KeyError as e:
            self._json({"error": str(e).strip("'\"")}, 404)
        except Busy as e:
            self._json({"error": str(e)}, 409)
        except (ValueError, IndexError) as e:
            self._json({"error": str(e) or "Bad request."}, 400)
        except (BrokenPipeError, ConnectionError):
            pass

    def _api(self, method: str, path: str, query: dict):
        m = self.mgr
        if method == "GET" and path == "/api/state":
            return m.state()
        if method == "GET" and path == "/api/log":
            text = m.log((query.get("id") or [""])[0])
            return self._send(200, text.encode("utf-8"), "text/plain")
        if method == "POST" and path == "/api/check":
            self._payload()
            m.check_soon()
            return {"ok": True}
        if method == "POST" and path in ("/api/update", "/api/back"):
            body = self._payload()
            name, expect = body.get("name"), body.get("expect") or ""
            if not isinstance(name, str) or not isinstance(expect, str):
                raise ValueError("Say which project.")
            return {"ok": True, "id": m.start(path.rsplit("/", 1)[-1], name, expect)}
        raise KeyError("Not found.")


def serve(mgr, host: str = "127.0.0.1", port: int = 8790, password: str = "") -> int:
    Handler.mgr = mgr
    Handler.open_host = host not in LOCAL
    Handler.password = password or os.environ.get("PULLEY_PASSWORD", "")
    demo = getattr(mgr, "demo", False)
    if Handler.open_host and not Handler.password and not demo:
        print("Pulley controls Docker, so it does not listen beyond this machine without a password.")
        print("Set PULLEY_PASSWORD (or use --password) and start it again.")
        return 2
    ThreadingHTTPServer.daemon_threads = True
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"Could not listen on {host}:{port} ({e}). Try --port with another number.")
        return 1

    def leave(*_):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, leave)  # what `docker stop` sends
    except (ValueError, OSError, AttributeError):
        pass
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"Pulley {__version__} is running at http://{shown}:{port}"
          + ("  (demo: made-up projects, nothing is touched)" if demo else ""), flush=True)
    if not demo:
        print(f"Settings: {mgr.config_file}", flush=True)
        if mgr.config_error:
            print(f"Settings problem: {mgr.config_error}", flush=True)
    threading.Thread(target=mgr.watch, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop()
        httpd.server_close()
        finish = getattr(mgr, "finish", None)
        if finish:
            finish(lambda name: print(f"Finishing the work on {name} before stopping…", flush=True))
        print("Stopped.", flush=True)
    return 0
