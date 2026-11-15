"""Made-up projects that behave like real ones, so the page can be tried with no Docker in sight."""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

from . import __version__
from .engine import Busy, Job, PLANS


def _commit(short: str, subject: str, hours_ago: float, tag: str = "") -> dict:
    sha = short + hashlib.sha1(short.encode()).hexdigest()[: 40 - len(short)]
    date = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return {"sha": sha, "short": short, "date": date, "subject": subject, "tag": tag}


def _since(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _project(name: str, follows: str, current: dict, changes: list[dict], **more) -> dict:
    p = {"name": name, "path": f"/srv/apps/{name}", "service": name, "follows": follows, "state": "ok",
         "current": current, "target": None, "behind": len(changes), "changes": changes, "problems": [],
         "notes": [], "container": {"status": "running", "health": "healthy", "started": _since(200)},
         "previous": None, "history": [], "at": time.time(), "_fate": "ok"}
    p.update(more)
    if changes:
        p["target"] = {**changes[0], "label": follows.removeprefix("branch ")}
        p["state"] = "blocked" if p["problems"] else "waiting"
    return p


def _made_up() -> list[dict]:
    wiki_old = _commit("c41be07", "Search inside attachments", 300)
    wiki_new = _commit("e9d3a52", "Keep the sidebar open between pages", 52)
    return [
        _project("recipes", "branch main", _commit("3f2a9c1", "Fix the shopping list losing its order", 220), [
            _commit("b81d4e7", "Add a print view for single recipes", 5),
            _commit("9c0e2a4", "Scale ingredients when the serving count changes", 27),
            _commit("57aa1f0", "Move to a newer base image", 51),
        ]),
        _project("photo-frame", "version tags", _commit("a7714c9", "Release 1.4.0", 900, "v1.4.0"), [
            _commit("d02f6b8", "Release 1.5.0", 30, "v1.5.0"),
            _commit("6be1d93", "Read the frame size from the settings", 31),
        ], container={"status": "running", "health": "", "started": _since(880)}, _fate="crash"),
        _project("notes-wiki", "branch main", wiki_new, [], previous=wiki_old,
                 container={"status": "running", "health": "healthy", "started": _since(49)},
                 history=[{"id": "notes-wiki-earlier", "kind": "update", "at": time.time() - 49 * 3600,
                           "from": wiki_old, "to": wiki_new, "result": "done", "note": "", "seconds": 71}]),
        _project("house-bot", "branch main", _commit("18c3f5d", "Announce the bin days", 400), [
            _commit("4a6e0b2", "Retry when the weather service is down", 70),
            _commit("f3309ad", "Add a quiet-hours setting", 96),
        ], problems=["This copy has been edited by hand (bot/settings.py). Pulley leaves it alone."]),
    ]


_BUILD = ["#1 [internal] load build definition from Dockerfile", "#4 [1/4] FROM docker.io/library/python:3.12-slim",
          "#6 [3/4] COPY . .", "#7 [4/4] RUN pip install --no-cache-dir .", "#8 exporting to image",
          "#8 naming to docker.io/library/{name}-{name} done"]
_SWAP = [" Container {name}-{name}-1  Recreate", " Container {name}-{name}-1  Recreated",
         " Container {name}-{name}-1  Started"]
_CODE = ["From https://example.org/{name}", "Updating {old}..{new}", "Fast-forward"]
_CRASH = ["It did not come up: it stopped right after starting (exit code 1).", "-- the last lines it printed:",
          "   Traceback (most recent call last):", '     File "/app/frame/main.py", line 12, in <module>',
          "   KeyError: 'FRAME_SIZE'", *_SWAP]
_LOGS = {"notes-wiki-earlier": ["== Getting the new code", "Updating c41be07..e9d3a52", "Fast-forward",
                                "== Building the new image", *[ln.format(name="notes-wiki") for ln in _BUILD],
                                "== Starting the new version", *[ln.format(name="notes-wiki") for ln in _SWAP],
                                "== Waiting for it to come up", "== Done."]}


class Demo:
    demo = True
    config_file = "pulley.toml"
    config_error = ""

    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self.projects = _made_up()
        self.logs: dict[str, list[str]] = {k: list(v) for k, v in _LOGS.items()}
        self.job: Job | None = None
        self.checking = False
        self.checked = time.time() - 240
        self._lock = threading.RLock()

    def watch(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def _find(self, name: str) -> dict:
        for p in self.projects:
            if p["name"] == name:
                return p
        raise KeyError(f'There is no project called "{name}".')

    def state(self) -> dict:
        with self._lock:
            job = self.job
            projects = []
            for p in self.projects:
                item = {k: v for k, v in p.items() if not k.startswith("_")}
                item["job"] = job.json() if job and job.name == p["name"] else None
                projects.append(item)
            return {"version": __version__, "demo": True, "checked": self.checked, "checking": self.checking,
                    "busy": job.name if job else None, "config_file": self.config_file, "config_error": "",
                    "docker_error": "", "check_every": 6 * 3600, "projects": projects}

    def log(self, ident: str) -> str:
        if ident not in self.logs:
            raise KeyError("There is no log by that name.")
        return "\n".join(self.logs[ident])

    def check(self, fetch: bool = True) -> None:
        self.checking = True
        time.sleep(1.2 * self.speed)
        self.checked = time.time()
        self.checking = False

    def check_soon(self) -> None:
        if not self.checking:
            threading.Thread(target=self.check, daemon=True).start()

    def start(self, kind: str, name: str, expect: str = "") -> str:
        if kind not in PLANS:
            raise ValueError("Pulley can update a project or take it back, nothing else.")
        with self._lock:
            if self.job:
                raise Busy(f"{self.job.name} is being worked on. One at a time.")
            p = self._find(name)
            if kind == "update" and p["state"] != "waiting":
                raise ValueError("There is nothing to update there.")
            if kind == "back" and not p["previous"]:
                raise ValueError("No earlier version is kept for this project.")
            ident = f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
            lines = self.logs[ident] = []
            self.job = Job(kind, name, ident, echo=lines.append)
            job = self.job
        threading.Thread(target=self._play, args=(p, job), daemon=True).start()
        return ident

    def _stage(self, job: Job, key: str, seconds: float, lines: list[str], words: dict, failed: bool = False) -> None:
        job.step(key, failed)
        pause = seconds * self.speed / (len(lines) + 1)
        for line in lines:
            time.sleep(pause)
            job.say(line.format(**words))
        time.sleep(pause)

    def _play(self, p: dict, job: Job) -> None:
        old = p["current"]
        new = p["target"] if job.kind == "update" else p["previous"]
        words = {"name": p["name"], "old": old["short"], "new": new["short"]}
        result, note = "done", ""
        try:
            self._stage(job, "code", .8, _CODE if job.kind == "update" else ["HEAD is now at {new}"], words)
            if job.kind == "update":
                self._stage(job, "build", 2.8, _BUILD, words)
            self._stage(job, "swap", 1.0, _SWAP, words)
            self._stage(job, "health", 2.2, [], words)
            if job.kind == "update" and p["_fate"] == "crash":
                self._stage(job, "restore", 2.4, _CRASH, words, failed=True)
                result = "put back"
                note = ("It did not come up: it stopped right after starting (exit code 1). "
                        "The version that was running is running again.")
        finally:
            with self._lock:
                if result == "done" and job.kind == "update":
                    p.update(current=new, previous=old, target=None, behind=0, changes=[], state="ok")
                elif result == "done":
                    left = [old] if not p["changes"] else [*p["changes"]]
                    p.update(current=new, previous=None, target={**left[0], "label": "main"}, behind=len(left),
                             changes=left, state="waiting")
                if result == "done":
                    p["container"] = {**p["container"], "started": _since(0)}
                p["history"].insert(0, {"id": job.id, "kind": job.kind, "at": job.started, "from": old, "to": new,
                                        "result": result, "note": note,
                                        "seconds": round(time.time() - job.started, 1)})
                job.say("== Done." if result == "done" else f"== Put back: {note}")
                job.close(result == "done")
                self.job = None
