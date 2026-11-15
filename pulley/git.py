"""What Pulley needs from git: where a checkout is, what is new upstream, and moving it."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import run

_FMT = "--format=%H%x1f%cI%x1f%s"
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]*\Z")
_STATUS = re.compile(r"\s*\S{1,2}\s+(\S.*)")
_PRE = re.compile(r"[-._]?(rc|alpha|beta|pre|preview|dev|nightly)[-._]?\d*\Z", re.I)


class GitError(Exception):
    pass


def _why(e: run.CommandError) -> str:
    """git's reason in one line. Its first "fatal:" line says what went wrong; its last line rarely does."""
    for line in e.output.splitlines():
        kind, _, rest = line.strip().partition(":")
        if kind.lower() in ("fatal", "error") and rest.strip():
            return rest.strip()[:300]
    return str(e)


@dataclass
class Commit:
    sha: str
    date: str
    subject: str
    tag: str = ""

    def json(self) -> dict:
        return {"sha": self.sha, "short": self.sha[:7], "date": self.date, "subject": self.subject, "tag": self.tag}


def _commit(line: str) -> Commit:
    sha, date, subject = (line.split("\x1f", 2) + ["", ""])[:3]
    return Commit(sha, date, subject)


class Repo:
    def __init__(self, path: Path, remote: str = "origin"):
        self.path, self.remote = Path(path), remote

    def _cmd(self, *args: str) -> list[str]:
        # safe.directory: the checkout usually belongs to another user than the one Pulley runs as.
        # The other two: looking at a checkout must not run programs that the checkout itself names.
        return [*run.GIT, "-c", "safe.directory=*", "-c", "core.fsmonitor=false",
                "-c", f"core.hooksPath={os.devnull}", "-C", str(self.path), *args]

    def _out(self, *args: str, timeout: float = 30) -> str:
        try:
            return run.out(self._cmd(*args), timeout=timeout)
        except run.CommandError as e:
            raise GitError(_why(e)) from e

    def _stream(self, *args: str, timeout: float = 120, on_line=None) -> None:
        try:
            run.stream(self._cmd(*args), timeout=timeout, on_line=on_line)
        except run.CommandError as e:
            raise GitError(_why(e)) from e

    def _yes(self, *args: str) -> bool:
        """For git commands that answer with their exit code: 0 yes, 1 no."""
        try:
            run.out(self._cmd(*args))
            return True
        except run.CommandError as e:
            if e.code == 1:
                return False
            raise GitError(_why(e)) from e

    def _rev(self, ref: str) -> str:
        try:
            return self._out("rev-parse", "--verify", "-q", ref)
        except GitError:
            return ""

    # --- reading ---------------------------------------------------------------

    def exists(self) -> bool:
        try:
            return self._out("rev-parse", "--is-inside-work-tree") == "true"
        except GitError:
            return False

    def branch(self) -> str:
        """The branch that is checked out, or "" when the checkout sits on a bare commit."""
        try:
            return self._out("symbolic-ref", "-q", "--short", "HEAD")
        except GitError:
            return ""

    def upstream(self) -> str:
        """The branch on the remote that the checked-out branch tracks, without the remote's name."""
        try:
            name = self._out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        except GitError:
            return ""
        return name[len(self.remote) + 1:] if name.startswith(self.remote + "/") else ""

    def commit(self, rev: str = "HEAD") -> Commit:
        found = _commit(self._out("log", "-1", _FMT, rev, "--"))
        tags = self._out("tag", "--points-at", found.sha).split()
        found.tag = tags[0] if tags else ""
        return found

    def edited(self) -> list[str]:
        """Tracked files that were changed by hand. Files git does not track are nobody's business."""
        lines = self._out("status", "--porcelain", "--untracked-files=no").splitlines()
        found = (_STATUS.match(ln) for ln in lines)  # "XY path"; the X may be a space
        return [m.group(1) for m in found if m]

    def target(self, follow: str, branch: str = "", pattern: str = "*", prereleases: bool = False) -> tuple[str, str]:
        """The commit this checkout should be at, and what to call it. ("", name) when there is none."""
        if follow == "tags":
            names = self._out("-c", "versionsort.suffix=-", "tag", "--list", pattern or "*", "--sort=-v:refname")
            for name in names.splitlines():
                name = name.strip()
                if not _REF.match(name) or (not prereleases and _PRE.search(name)):
                    continue
                sha = self._rev(f"refs/tags/{name}^{{commit}}")
                if sha:
                    return sha, name
            return "", ""
        name = branch or self.upstream() or self.branch()
        if not name or not _REF.match(name):
            return "", name
        return self._rev(f"refs/remotes/{self.remote}/{name}^{{commit}}"), name

    def relation(self, here: str, there: str) -> str:
        """How `here` stands to `there`: same, behind (can move forward), ahead, or diverged."""
        if here == there:
            return "same"
        if self._yes("merge-base", "--is-ancestor", here, there):
            return "behind"
        if self._yes("merge-base", "--is-ancestor", there, here):
            return "ahead"
        return "diverged"

    def count(self, here: str, there: str) -> int:
        return int(self._out("rev-list", "--count", f"{here}..{there}") or 0)

    def between(self, here: str, there: str, limit: int = 50) -> list[Commit]:
        """The commits `there` has and `here` does not, newest first."""
        text = self._out("log", _FMT, "-n", str(limit), f"{here}..{there}", "--")
        return [_commit(line) for line in text.splitlines() if line]

    # --- changing --------------------------------------------------------------

    def fetch(self, on_line=None) -> None:
        self._stream("fetch", self.remote, on_line=on_line)

    def forward(self, target: str, on_line=None) -> None:
        """Move the checkout forward to `target`. Fails rather than merge or overwrite anything."""
        if self.branch():
            self._stream("merge", "--ff-only", target, on_line=on_line)
        else:
            self._stream("checkout", "--detach", target, on_line=on_line)

    def back(self, commit: str, on_line=None) -> None:
        """Move the checkout back to `commit`. --keep refuses if that would lose a change made by hand."""
        if self.branch():
            self._stream("reset", "--keep", commit, on_line=on_line)
        else:
            self._stream("checkout", "--detach", commit, on_line=on_line)
