"""The page's server: who gets in, and what the requests do. Runs against the made-up projects."""
import base64
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from pulley import server
from pulley.demo import Demo
from pulley.server import Handler


def basic(password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"me:{password}".encode()).decode()}


class Served(unittest.TestCase):
    password = "let-me-in"

    def setUp(self) -> None:
        Handler.mgr = Demo(speed=0.02)
        Handler.password = self.password
        Handler.open_host = False
        Handler._wrong.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def ask(self, path: str, body=None, headers=None, password=None):
        """(status, headers, text) of one request."""
        heads = dict(headers or {})
        if password is not False:
            heads.update(basic(self.password if password is None else password))
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            heads.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.base + path, data=data, headers=heads)
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, reply.headers, reply.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8")

    def post(self, path: str, body: dict, **kw):
        return self.ask(path, body, {"X-Pulley": "1"}, **kw)

    def state(self) -> dict:
        return json.loads(self.ask("/api/state")[2])

    def settle(self) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = self.state()
            if not state["busy"]:
                return state
            time.sleep(0.05)
        self.fail("the job never finished")


class Doors(Served):
    def test_password_is_asked_for(self):
        code, heads, _ = self.ask("/", password=False)
        self.assertEqual(code, 401)
        self.assertIn('Basic realm="Pulley"', heads["WWW-Authenticate"])
        self.assertEqual(self.ask("/api/state", password=False)[0], 401)
        self.assertEqual(self.ask("/api/state", password="wrong")[0], 401)

    def test_right_password(self):
        code, heads, text = self.ask("/")
        self.assertEqual(code, 200)
        self.assertIn("<title>Pulley</title>", text)
        self.assertEqual(heads["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", heads["Content-Security-Policy"])
        self.assertIn("script-src 'self'", heads["Content-Security-Policy"])
        for path in ("/app.js", "/app.css", "/icon.svg"):
            self.assertEqual(self.ask(path)[0], 200, path)

    def test_too_many_wrong_passwords(self):
        for _ in range(server.TRIES):
            self.assertEqual(self.ask("/api/state", password="guess")[0], 401)
        self.assertEqual(self.ask("/api/state", password="guess")[0], 429)
        self.assertEqual(self.ask("/api/state")[0], 429, "even the right one has to wait now")

    def test_no_password_on_this_machine_only(self):
        Handler.password = ""
        self.assertEqual(self.ask("/api/state", password=False)[0], 200)
        self.assertEqual(self.ask("/api/state", headers={"Host": "pulley.example:8790"}, password=False)[0], 403)

    def test_will_not_listen_wide_open(self):
        old = os.environ.pop("PULLEY_PASSWORD", None)
        try:
            self.assertEqual(server.serve(object(), "0.0.0.0", 0, ""), 2)
        finally:
            if old is not None:
                os.environ["PULLEY_PASSWORD"] = old

    def test_changes_need_the_header(self):
        code, _, text = self.ask("/api/update", {"name": "recipes"})
        self.assertEqual(code, 403)
        self.assertIsNone(self.state()["busy"])

    def test_another_site_is_refused(self):
        code, _, _ = self.ask("/api/update", {"name": "recipes"},
                              {"X-Pulley": "1", "Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.ask("/api/state", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertIsNone(self.state()["busy"])

    def test_behind_a_reverse_proxy(self):
        proxied = {"X-Pulley": "1", "Origin": "https://pulley.example", "X-Forwarded-Host": "pulley.example"}
        self.assertEqual(self.ask("/api/check", {}, proxied)[0], 200)
        self.assertEqual(self.ask("/api/check", {}, {**proxied, "Origin": "https://evil.example"})[0], 403)

    def test_body_in_a_form_pulley_does_not_read(self):
        code, _, text = self.ask("/api/check", {}, {"X-Pulley": "1", "Transfer-Encoding": "chunked"})
        self.assertEqual(code, 400)
        self.assertEqual(self.ask("/api/state")[0], 200, "and the next request is unharmed")


class Requests(Served):
    def test_state(self):
        state = self.state()
        self.assertTrue(state["demo"])
        self.assertEqual([p["name"] for p in state["projects"]], ["recipes", "photo-frame", "notes-wiki", "house-bot"])
        self.assertEqual([p["state"] for p in state["projects"]], ["waiting", "waiting", "ok", "blocked"])
        self.assertFalse(any(key.startswith("_") for p in state["projects"] for key in p))

    def test_update(self):
        target = self.state()["projects"][0]["target"]["sha"]
        code, _, text = self.post("/api/update", {"name": "recipes", "expect": target})
        self.assertEqual(code, 200)
        ident = json.loads(text)["id"]
        self.assertEqual(self.post("/api/update", {"name": "photo-frame"})[0], 409, "one at a time")
        recipes = self.settle()["projects"][0]
        self.assertEqual((recipes["state"], recipes["behind"]), ("ok", 0))
        self.assertEqual(recipes["history"][0]["result"], "done")
        self.assertEqual(recipes["previous"]["short"], "3f2a9c1")
        code, heads, log = self.ask(f"/api/log?id={ident}")
        self.assertEqual(code, 200)
        self.assertTrue(heads["Content-Type"].startswith("text/plain"))
        self.assertIn("== Building the new image", log)

    def test_update_that_is_put_back(self):
        self.assertEqual(self.post("/api/update", {"name": "photo-frame"})[0], 200)
        frame = self.settle()["projects"][1]
        self.assertEqual(frame["state"], "waiting")
        self.assertEqual(frame["history"][0]["result"], "put back")
        self.assertEqual(frame["current"]["tag"], "v1.4.0")

    def test_back(self):
        self.assertEqual(self.post("/api/back", {"name": "notes-wiki"})[0], 200)
        wiki = self.settle()["projects"][2]
        self.assertEqual((wiki["state"], wiki["current"]["short"]), ("waiting", "c41be07"))
        self.assertIsNone(wiki["previous"])

    def test_check(self):
        before = self.state()["checked"]
        self.assertEqual(self.post("/api/check", {})[0], 200)
        deadline = time.monotonic() + 10
        while self.state()["checked"] == before and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreater(self.state()["checked"], before)

    def test_refusals(self):
        self.assertEqual(self.post("/api/update", {"name": "nope"})[0], 404)
        self.assertEqual(self.post("/api/update", {"name": 5})[0], 400)
        self.assertEqual(self.post("/api/update", {"name": "notes-wiki"})[0], 400, "nothing to update there")
        self.assertEqual(self.post("/api/back", {"name": "recipes"})[0], 400, "nothing kept there")
        self.assertEqual(self.ask("/api/update", "[1]", {"X-Pulley": "1"})[0], 400)
        self.assertEqual(self.ask("/api/nothing")[0], 404)
        self.assertEqual(self.ask("/api/log?id=nope")[0], 404)
        self.assertEqual(self.ask("/../pulley/server.py")[0], 404)
        self.assertEqual(self.ask("/app.js", {"x": 1}, {"X-Pulley": "1"})[0], 404, "nothing to post there")


if __name__ == "__main__":
    unittest.main()
