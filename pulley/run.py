"""Running git and docker: never through a shell, always with a time limit."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

# Lists, so a test can put a stand-in program (with arguments) in their place.
GIT = [os.environ.get("PULLEY_GIT") or "git"]
DOCKER = [os.environ.get("PULLEY_DOCKER") or "docker"]

# git must never stop to ask for a password; docker should print plain lines, not a redrawn screen.
_ENV = {"GIT_TERMINAL_PROMPT": "0", "BUILDKIT_PROGRESS": "plain", "DOCKER_CLI_HINTS": "false"}


class CommandError(Exception):
    """A command failed. str() is one line fit to show a person; .output is everything it printed."""

    def __init__(self, message: str, output: str = "", code: int | None = None):
        super().__init__(message)
        self.output, self.code = output, code


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def _name(cmd: list[str]) -> str:
    return Path(cmd[0]).stem


def _env() -> dict:
    """What the commands get to see. Pulley's own settings, its password among them, stay with Pulley:
    a compose file could otherwise read them as ${PULLEY_PASSWORD}."""
    kept = {k: v for k, v in os.environ.items() if not k.upper().startswith("PULLEY_")}
    return {**kept, **_ENV}


def _start(cmd: list[str], cwd, stderr) -> subprocess.Popen:
    if cwd and not os.path.isdir(cwd):
        raise CommandError(f"The folder {cwd} does not exist.")
    try:
        # A group of its own, so that stopping it also stops what it started (docker starts compose).
        return subprocess.Popen(cmd, cwd=cwd, env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=stderr, text=True, encoding="utf-8", errors="replace",
                                start_new_session=os.name == "posix")
    except FileNotFoundError:
        raise CommandError(f"{_name(cmd)} is not installed here.") from None
    except OSError as e:
        raise CommandError(f"{_name(cmd)} could not be started ({e}).") from None


def _kill(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    proc.kill()


def out(cmd: list[str], cwd=None, timeout: float = 30) -> str:
    """What a short command prints. Its error text is kept out of the result."""
    return both(cmd, cwd, timeout)[0]


def both(cmd: list[str], cwd=None, timeout: float = 30) -> tuple[str, str]:
    """What a short command prints, and separately what it says on the side (warnings)."""
    proc = _start(cmd, cwd, subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        proc.communicate()
        raise CommandError(f"{_name(cmd)} did not answer within {timeout:g} seconds.") from None
    if proc.returncode:
        message = _last_line(stderr) or _last_line(stdout) or f"{_name(cmd)} failed (exit {proc.returncode})."
        raise CommandError(message, stderr + stdout, proc.returncode)
    return stdout.strip(), stderr.strip()


def stream(cmd: list[str], cwd=None, timeout: float = 600, on_line=None) -> str:
    """Run a long command, handing over each line it prints as it comes."""
    proc = _start(cmd, cwd, subprocess.STDOUT)
    late = threading.Event()

    def stop() -> None:
        late.set()
        _kill(proc)

    timer = threading.Timer(timeout, stop)
    timer.daemon = True
    timer.start()
    lines: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            lines.append(line)
            if on_line:
                on_line(line)
        code = proc.wait()
    finally:
        timer.cancel()
        proc.stdout.close()
    text = "\n".join(lines)
    if late.is_set():
        raise CommandError(f"{_name(cmd)} took longer than {timeout:g} seconds and was stopped.", text)
    if code:
        raise CommandError(_last_line(text) or f"{_name(cmd)} failed (exit {code}).", text, code)
    return text
