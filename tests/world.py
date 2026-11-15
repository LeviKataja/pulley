"""A small world for the tests: a repository, a checkout of it that "runs in Docker", and Pulley looking after it.

git is the real thing. Docker is tests/fakedocker.py, which reads the compose files for real (they are
written as JSON, which is valid YAML).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pulley import run
from pulley.engine import Manager

FAKE = Path(__file__).with_name("fakedocker.py")
PLAIN = {"services": {"app": {"build": "."}}}
SETTINGS = '''
check_every = "never"

[[project]]
name = "shop"
path = "{path}"
service = "app"
health_wait = "5s"
settle = "0s"
{more}
'''


def git(folder: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                          cwd=folder, capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


class WorldCase(unittest.TestCase):
    more = ""        # extra lines for the project's settings
    compose = PLAIN  # the compose file the repository starts with

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.origin, self.work, self.home = root / "origin", root / "apps" / "shop", root / "home"
        self.world_file = root / "docker.json"

        old_docker, old_env = list(run.DOCKER), os.environ.get("FAKE_DOCKER_STATE")
        run.DOCKER[:] = [sys.executable, str(FAKE)]
        os.environ["FAKE_DOCKER_STATE"] = str(self.world_file)

        def restore() -> None:
            run.DOCKER[:] = old_docker
            if old_env is None:
                os.environ.pop("FAKE_DOCKER_STATE", None)
            else:
                os.environ["FAKE_DOCKER_STATE"] = old_env

        self.addCleanup(restore)

        self.origin.mkdir(parents=True)
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "app.txt").write_text("version 1\n", "utf-8")
        (self.origin / "README.md").write_text("A shop.\n", "utf-8")
        (self.origin / "compose.yaml").write_text(json.dumps(self.compose), "utf-8")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", "First version")
        self.work.parent.mkdir(parents=True)
        git(self.work.parent, "clone", "-q", str(self.origin), "shop")
        self.prepare()
        self.by_hand()  # the person started it once from the folder, as the README says to

        self.home.mkdir()
        self.settings(self.more)
        self.mgr = Manager(self.home)
        self.mgr.poll = 0.02
        self.mgr.own = lambda: None  # not in a container

    def prepare(self) -> None:
        """For a test class to put things in the checkout before the app is first started."""

    # --- helpers ---------------------------------------------------------------

    def settings(self, more: str = "") -> None:
        (self.home / "pulley.toml").write_text(SETTINGS.format(path=self.work.as_posix(), more=more), "utf-8")

    def docker(self, *args: str, cwd: Path | None = None) -> str:
        done = subprocess.run([sys.executable, str(FAKE), *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8")
        if done.returncode:
            raise AssertionError(f"docker {' '.join(args)} failed: {done.stderr}")
        return done.stdout.strip()

    def by_hand(self, service: str = "app", pull: bool = False) -> None:
        """What a person does in the folder: (pull,) build and start."""
        if pull:
            git(self.work, "pull", "-q", "--ff-only")
        if "build" in json.loads((self.work / "compose.yaml").read_text("utf-8"))["services"][service]:
            self.docker("compose", "build", service, cwd=self.work)
        self.docker("compose", "up", "-d", service, cwd=self.work)

    def world(self) -> dict:
        return json.loads(self.world_file.read_text("utf-8"))

    def change_world(self, **changes) -> None:
        self.world_file.write_text(json.dumps({**self.world(), **changes}), "utf-8")

    def push(self, app: str | None = None, readme: str | None = None, message: str = "A change",
             tag: str = "", compose: dict | None = None) -> str:
        """A new commit in the repository. Returns its id."""
        if app is not None:
            (self.origin / "app.txt").write_text(app + "\n", "utf-8")
        if readme is not None:
            (self.origin / "README.md").write_text(readme + "\n", "utf-8")
        if compose is not None:
            (self.origin / "compose.yaml").write_text(json.dumps(compose), "utf-8")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", message)
        if tag:
            git(self.origin, "tag", tag)
        return git(self.origin, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.work, "rev-parse", "HEAD")

    def container(self) -> tuple[str, dict]:
        """The id and details of the one container of the service."""
        found = [(cid, c) for cid, c in self.world()["containers"].items()
                 if c["labels"]["com.docker.compose.oneoff"] == "False"]
        self.assertEqual(len(found), 1, "there should be exactly one container")
        return found[0]

    def tagged(self, name: str) -> str:
        return self.world()["tags"].get(name if ":" in name else name + ":latest", "")

    def folder(self, name: str) -> str:
        """A folder inside the checkout, written the way Compose reports it."""
        return (self.work / name).as_posix()

    def project(self) -> dict:
        return next(p for p in self.mgr.state()["projects"] if p["name"] == "shop")

    def look(self) -> dict:
        self.mgr.check()
        return self.project()
