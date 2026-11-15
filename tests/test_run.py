"""Small things at the edges: what commands get to see, and telling whether an address answers."""
import os
import socket
import sys
import threading
import unittest

from pulley import engine, run


class Commands(unittest.TestCase):
    def test_pulleys_own_settings_stay_with_pulley(self):
        os.environ["PULLEY_PASSWORD"] = "let-me-in"
        self.addCleanup(os.environ.pop, "PULLEY_PASSWORD", None)
        env = run._env()
        self.assertFalse([name for name in env if name.upper().startswith("PULLEY_")])
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("PATH", {name.upper() for name in env})
        seen = run.out([sys.executable, "-c", "import os; print(os.environ.get('PULLEY_PASSWORD'))"])
        self.assertEqual(seen, "None")

    def test_output_and_warnings_are_kept_apart(self):
        code = "import sys; print('answer'); print('a warning', file=sys.stderr)"
        self.assertEqual(run.both([sys.executable, "-c", code]), ("answer", "a warning"))

    def test_failure_says_why(self):
        with self.assertRaises(run.CommandError) as caught:
            run.out([sys.executable, "-c", "import sys; print('it broke', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((str(caught.exception), caught.exception.code), ("it broke", 3))
        with self.assertRaises(run.CommandError) as caught:
            run.out(["no-such-program-anywhere"])
        self.assertIn("is not installed here", str(caught.exception))
        with self.assertRaises(run.CommandError) as caught:
            run.out([sys.executable, "-c", "pass"], cwd="/no/such/folder/anywhere")
        self.assertIn("does not exist", str(caught.exception))

    def test_time_limit(self):
        with self.assertRaises(run.CommandError) as caught:
            run.stream([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
                       timeout=1)
        self.assertIn("took longer than 1 seconds", str(caught.exception))
        self.assertEqual(caught.exception.output, "started")
        with self.assertRaises(run.CommandError) as caught:
            run.out([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertIn("did not answer within 1 seconds", str(caught.exception))

    def test_lines_arrive_as_they_come(self):
        seen = []
        text = run.stream([sys.executable, "-c", "print('one'); print('two')"], on_line=seen.append)
        self.assertEqual((seen, text), (["one", "two"], "one\ntwo"))


class Answers(unittest.TestCase):
    def saying(self, reply: bytes) -> str:
        """An address where something says exactly this, once, to whoever connects."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def serve() -> None:
            try:
                conn, _ = listener.accept()
                conn.recv(4096)
                conn.sendall(reply)
                conn.close()
            except OSError:
                pass

        threading.Thread(target=serve, daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}/"

    def test_answers(self):
        ok = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        self.assertTrue(engine.answers(self.saying(ok)))
        self.assertTrue(engine.answers(self.saying(ok.replace(b"200 OK", b"401 Unauthorized"))),
                        "a login page means the app is up")
        self.assertFalse(engine.answers(self.saying(ok.replace(b"200 OK", b"503 Service Unavailable"))))

    def test_nonsense_is_not_an_answer(self):
        self.assertFalse(engine.answers(self.saying(b"this is not http at all\r\n\r\n")))
        self.assertFalse(engine.answers(self.saying(b"")))

    def test_nothing_there(self):
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
        spare.close()
        self.assertFalse(engine.answers(f"http://127.0.0.1:{port}/"))


if __name__ == "__main__":
    unittest.main()
