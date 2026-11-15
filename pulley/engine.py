"""Looking for updates, applying one, and putting the old version back when the new one does not come up."""
from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from . import __version__, config, docker
from .config import Config, ConfigError, Project
from .docker import Compose, DockerError
from .git import GitError, Repo

LABELS = {
    "update": {
        "code": "Getting the new code",
        "build": "Building the new image",
        "swap": "Starting the new version",
        "health": "Waiting for it to come up",
        "restore": "Putting the old version back",
    },
    "back": {
        "code": "Putting the earlier code in place",
        "swap": "Starting the earlier version",
        "health": "Waiting for it to come up",
        "restore": "Returning to the version that was running",
    },
}
PLANS = {"update": ("code", "build", "swap", "health"), "back": ("code", "swap", "health")}

# How an attempt can end. "failed": stopped before the running app was touched.
RESULTS = {
    "done": "Done",
    "failed": "Not done",
    "put back": "Put back",
    "stuck": "Needs a look by hand",
    "interrupted": "Interrupted",
}

_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,90}\Z")


class Busy(Exception):
    pass


def _alive(pid) -> bool:
    """Whether a process with this id is running here. Only answered where asking is harmless."""
    if not isinstance(pid, int) or pid <= 0 or os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _elsewhere(left: dict | None) -> bool:
    """Whether an attempt on record belongs to another Pulley that is still at it."""
    return bool(left) and left.get("pid") != os.getpid() and _alive(left.get("pid"))


def answers(url: str) -> bool:
    """Whether something answers at this address. Any answer short of a server error counts."""
    request = urllib.request.Request(url, headers={"User-Agent": f"pulley/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (OSError, ValueError, http.client.HTTPException):  # nothing there yet, or not yet talking sense
        return False


class Job:
    """One update or step back while it runs: where it is, and what the commands printed."""

    def __init__(self, kind: str, name: str, ident: str, log_file: Path | None = None, echo=None, on_step=None):
        self.kind, self.name, self.id = kind, name, ident
        self.labels = LABELS[kind]
        self.steps = [{"key": k, "label": self.labels[k], "state": "todo"} for k in PLANS[kind]]
        self.tail: deque[str] = deque(maxlen=60)
        self.started = time.time()
        self.touched = False  # set once something on the machine has been changed
        self.echo, self.on_step = echo, on_step
        self._lock = threading.Lock()
        self._file = open(log_file, "a", encoding="utf-8") if log_file else None

    def say(self, line) -> None:
        line = str(line).rstrip()
        with self._lock:
            self.tail.append(line[:400])
            if self._file:
                self._file.write(line + "\n")
                self._file.flush()
        if self.echo:
            self.echo(line)

    def step(self, key: str, failed: bool = False) -> None:
        """Move on to a step. `failed` says the one before it did not work out."""
        with self._lock:
            for s in self.steps:
                if s["state"] == "now":
                    s["state"] = "failed" if failed else "done"
            found = next((s for s in self.steps if s["key"] == key), None)
            if found is None:
                found = {"key": key, "label": self.labels[key], "state": "todo"}
                self.steps.append(found)
            found["state"] = "now"
        self.say(f"== {found['label']}")
        if self.on_step:
            self.on_step(key)

    def close(self, ok: bool) -> None:
        with self._lock:
            for s in self.steps:
                if s["state"] == "now":
                    s["state"] = "done" if ok else "failed"
            if self._file:
                self._file.close()
                self._file = None

    def json(self) -> dict:
        with self._lock:
            return {"kind": self.kind, "name": self.name, "id": self.id, "started": self.started,
                    "steps": [dict(s) for s in self.steps], "tail": list(self.tail)}


class Store:
    """What Pulley remembers between runs: past attempts, the version kept to go back to, and their logs."""

    KEEP = 40

    def __init__(self, home: Path):
        self.home = Path(home)
        self.logs = self.home / "logs"
        self.file = self.home / "state.json"
        self._lock = threading.Lock()
        self.data: dict = {"history": {}, "previous": {}, "pending": None}
        self.logs.mkdir(parents=True, exist_ok=True)
        self._read()

    def _read(self) -> None:
        """Take what is on disk. Another Pulley (the command line beside the page) may have written since."""
        try:
            loaded = json.loads(self.file.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(loaded, dict):
            self.data.update({k: loaded[k] for k in self.data if k in loaded})

    def _save(self) -> None:
        tmp = self.file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.file)

    def history(self, name: str) -> list[dict]:
        with self._lock:
            self._read()
            return list(self.data["history"].get(name, []))

    def add(self, name: str, entry: dict) -> None:
        with self._lock:
            self._read()
            items = self.data["history"].setdefault(name, [])
            items.insert(0, entry)
            for old in items[self.KEEP:]:
                try:
                    self.log_file(old.get("id", "")).unlink()
                except (OSError, ValueError):
                    pass
            del items[self.KEEP:]
            self._save()

    def previous(self, name: str) -> dict | None:
        with self._lock:
            self._read()
            return self.data["previous"].get(name)

    def set_previous(self, name: str, value: dict | None) -> None:
        with self._lock:
            self._read()
            if value is None:
                self.data["previous"].pop(name, None)
            else:
                self.data["previous"][name] = value
            self._save()

    def pending(self) -> dict | None:
        with self._lock:
            self._read()
            return self.data["pending"]

    def set_pending(self, value: dict | None) -> None:
        with self._lock:
            self._read()
            self.data["pending"] = value
            self._save()

    def log_file(self, ident: str) -> Path:
        if not _ID.match(ident or ""):
            raise ValueError("That is not the name of a log.")
        return self.logs / f"{ident}.log"

    def log(self, ident: str) -> str:
        try:
            return self.log_file(ident).read_text("utf-8", errors="replace")
        except OSError:
            raise KeyError("There is no log by that name.") from None


class Manager:
    poll = 2.0  # seconds between looks at a container that is coming up

    def __init__(self, home: Path, config_file: Path | None = None):
        self.home = Path(home)
        self.config_file = Path(config_file) if config_file else self.home / config.FILE_NAME
        self.store = Store(self.home)
        self.cfg = Config()
        self.config_error = ""
        self.docker_error = ""
        self.status: dict[str, dict] = {}
        self.job: Job | None = None
        self.checking = False
        self.checked = 0.0
        self.own = docker.own  # a test puts its own in here
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._load()
        self._recover()

    # --- settings ----------------------------------------------------------------

    def _load(self) -> None:
        try:
            self.cfg = config.load(self.config_file)
            self.config_error = ""
        except ConfigError as e:
            self.config_error = str(e)  # keep working with the settings read before

    def _recover(self) -> None:
        """An attempt that was under way when Pulley last stopped: say so in the history."""
        left = self.store.pending()
        if not left or _elsewhere(left):
            return
        label = LABELS.get(left.get("kind"), {}).get(left.get("step"), "starting")
        self.store.add(left.get("name", ""), {
            "id": left.get("id", ""), "kind": left.get("kind", "update"), "at": left.get("at", time.time()),
            "from": left.get("from"), "to": left.get("to"), "result": "interrupted", "seconds": 0,
            "note": f"Pulley was stopped while this was at “{label}”. Check that the app is running."})
        self.store.set_pending(None)

    # --- looking -----------------------------------------------------------------

    def _environment(self) -> dict:
        err, mine, mine_err = docker.reachable(), None, ""
        if not err:
            try:
                mine = self.own()
            except DockerError as e:
                mine_err = str(e)
        return {"docker_error": err, "own": mine, "own_error": mine_err}

    def _survey(self, p: Project, fetch: bool, env: dict, say=None) -> dict:
        """Everything about one project: where it is, what is waiting, and what stands in the way."""
        st: dict = {"state": "unknown", "current": None, "target": None, "behind": 0, "changes": [],
                    "problems": [], "notes": [], "container": None, "at": time.time(),
                    "follows": "version tags" if p.follow == "tags" else f"branch {p.branch}".strip()}
        problems, notes = st["problems"], st["notes"]
        repo = Repo(p.path, p.remote)
        if not p.path.is_dir():
            problems.append(f"The folder {p.path} does not exist here.")
            return self._settle(st)
        if not repo.exists():
            problems.append(f"{p.path} is not a git checkout.")
            return self._settle(st)
        try:
            if fetch:
                try:
                    repo.fetch(say)
                except GitError as e:
                    notes.append(f"The repository could not be reached, so this may be out of date ({e}).")
            head = repo.commit("HEAD")
            st["current"] = head.json()
            sha, label = repo.target(p.follow, p.branch, p.tags, p.prereleases)
            if p.follow == "branch" and label:
                st["follows"] = f"branch {label}"
            if not sha and p.follow == "tags":
                problems.append("The repository has no version tags to follow"
                                + ("." if p.tags == "*" else f' that match "{p.tags}".'))
            elif not sha:
                problems.append(f'The repository has no branch called "{label}".' if label else
                                "This checkout is not on a branch. Say which one to follow with branch = \"...\".")
            else:
                how = repo.relation(head.sha, sha)
                if how == "behind":
                    st["target"] = {**repo.commit(sha).json(), "label": label}
                    st["behind"] = repo.count(head.sha, sha)
                    st["changes"] = [c.json() for c in repo.between(head.sha, sha)]
                elif how == "diverged":
                    problems.append("This copy has commits of its own that the repository does not have, "
                                    "so it cannot simply move forward. Pulley leaves it alone.")
                elif how == "ahead":
                    notes.append("This copy is newer than what it follows.")
            edited = repo.edited()
            if edited:
                shown = ", ".join(edited[:3]) + (f" and {len(edited) - 3} more" if len(edited) > 3 else "")
                problems.append(f"This copy has been edited by hand ({shown}). Pulley leaves it alone.")
        except (GitError, ValueError) as e:
            problems.append(f"Git could not read this checkout: {e}")
        self._survey_docker(p, st, env)
        return self._settle(st)

    def _survey_docker(self, p: Project, st: dict, env: dict) -> None:
        problems, notes = st["problems"], st["notes"]
        if env["docker_error"]:
            problems.append(f"Docker cannot be reached: {env['docker_error']}")
            return
        file = p.compose_file()
        if file is None:
            problems.append(f"No compose file was found at {p.path / p.compose}." if p.compose else
                            f"No compose file was found in {p.path}. Say where it is with compose = \"...\".")
            return
        # First find the container the way a person standing in that folder would...
        finder = (Compose(file.parent, [file], p.project_name) if p.compose else
                  Compose(p.path, project=p.project_name))
        try:
            found = finder.containers(p.service)
        except DockerError as e:
            problems.append(f"Docker Compose could not be asked about this service: {e}")
            return
        if len(found) > 1:
            problems.append("This service runs as more than one container. Pulley handles one.")
            return
        if not found:
            problems.append("Pulley cannot see a container for this service. Start it once from that folder "
                            "with docker compose up -d --build. If it runs under another project name, "
                            "set project_name.")
            return
        c = found[0]
        st["container"] = {"status": c["status"], "health": c["health"], "started": c["started"]}
        # ...then do everything else exactly as that container says it was started: the same compose
        # files (an override file among them), the same folder, the same project name.
        try:
            compose = Compose.of(c)
            cfg, unset = compose.config()
        except DockerError as e:
            problems.append(str(e) if str(e).startswith("The ") else
                            f"Docker Compose could not read the compose file: {e}")
            return
        service = (cfg.get("services") or {}).get(p.service)
        if service is None:
            problems.append(f'The compose file has no service called "{p.service}".')
            return
        if not service.get("build"):
            problems.append(f'"{p.service}" is not built from source in that compose file, '
                            "so there is nothing for Pulley to rebuild.")
            return
        st["_container"], st["_compose"], st["_cfg"] = c, compose, cfg
        if unset:
            problems.append(f"The compose file uses {', '.join(unset)}, which Pulley has no value for, so the "
                            "app would be restarted without it. Put it in a .env file next to the compose file.")
        if c["status"] != "running":
            problems.append(f"The container is not running (it is {c['status'] or 'in an unknown state'}). "
                            "Start it first.")
        if not p.skip_path_check:
            for inside, would, now in docker.other_folders(cfg, p.service, c):
                problems.append(f"As Pulley reads the compose file, the app would get {would} as its {inside}, "
                                f"but the running one has {now or 'nothing'} there. Restarting it would "
                                "change which folder it uses, so Pulley leaves it alone.")
        if env["own"] is not None:
            mounts, me = env["own"]
            if c["id"] == me:
                problems.append("This is Pulley's own container. Pulley cannot update itself yet.")
            if not p.skip_path_check:
                for folder in dict.fromkeys([compose.folder.as_posix(), p.path.as_posix()]):
                    real = docker.on_host(folder, mounts)
                    if real is None:
                        problems.append(f"{folder} exists only inside Pulley's container. Mount the folder from "
                                        "the machine instead, at the same path.")
                    elif real != folder:
                        problems.append(f"Inside Pulley this folder is {folder}, but on the machine it is {real}. "
                                        "Mount it at the same path on both sides, or paths in the compose file "
                                        "would point at the wrong place.")

    @staticmethod
    def _settle(st: dict) -> dict:
        if st["problems"]:
            st["state"] = "blocked" if st["behind"] else "trouble"
        else:
            st["state"] = "waiting" if st["behind"] else "ok"
        return st

    def _latest(self, name: str) -> str:
        """The id of the newest attempt on record for a project, whichever Pulley made it."""
        history = self.store.history(name)
        return history[0].get("id", "") if history else ""

    def _remember(self, p: Project, st: dict) -> None:
        st["_latest"] = self._latest(p.name)
        with self._lock:
            self.status[p.name] = st

    def _claim(self) -> bool:
        """Mark a look as under way. False when one already is."""
        with self._lock:
            if self.checking:
                return False
            self.checking = True
            return True

    def check(self, fetch: bool = True) -> None:
        """Look at every project. One look at a time; a second call while one runs does nothing."""
        if self._claim():
            self._check(fetch)

    def check_soon(self, fetch: bool = True) -> None:
        """The same in the background. The page sees "checking" from the moment this returns."""
        if self._claim():
            threading.Thread(target=self._check, args=(fetch,), daemon=True).start()

    def _check(self, fetch: bool) -> None:
        try:
            self._load()
            env = self._environment()
            for p in list(self.cfg.projects):
                if self.job and self.job.name == p.name:
                    continue
                self._remember(p, self._survey(p, fetch, env))
            self.docker_error = env["docker_error"]
            if fetch:
                self.checked = time.time()
        finally:
            self.checking = False

    def watch(self) -> None:
        """Look now, then again every so often, until stop() is called."""
        self.check()
        while not self._stop.wait(30):
            every = self.cfg.check_every
            if every and not self.job and time.time() - self.checked >= every:
                self.check()

    def stop(self) -> None:
        self._stop.set()

    def finish(self, tell=None, patience: float = 150) -> None:
        """Before Pulley exits: give an update that is under way the time to end properly."""
        job = self.job
        if job is None:
            return
        if tell:
            tell(job.name)
        until = time.monotonic() + patience
        while self.job is not None and time.monotonic() < until:
            try:
                time.sleep(0.2)
            except KeyboardInterrupt:  # asked twice: go now
                return

    # --- telling -----------------------------------------------------------------

    def state(self) -> dict:
        with self._lock:
            job = self.job
            projects = []
            stale = False
            for p in self.cfg.projects:
                st = self.status.get(p.name) or {"state": "unknown", "current": None, "target": None, "behind": 0,
                                                 "changes": [], "problems": [], "notes": [], "container": None,
                                                 "follows": "", "at": 0}
                # Another Pulley (the command line) may have updated this project since it was looked at.
                stale = stale or ("_latest" in st and st["_latest"] != self._latest(p.name))
                item = {k: v for k, v in st.items() if not k.startswith("_")}
                item.update(name=p.name, path=str(p.path), service=p.service,
                            job=job.json() if job and job.name == p.name else None,
                            previous=(self.store.previous(p.name) or {}).get("commit"),
                            history=self.store.history(p.name)[:12])
                projects.append(item)
            if stale and not job and not _elsewhere(self.store.pending()):
                self.check_soon(fetch=False)
            return {"version": __version__, "demo": False, "checked": self.checked, "checking": self.checking,
                    "busy": job.name if job else None, "config_file": str(self.config_file),
                    "config_error": self.config_error, "docker_error": self.docker_error,
                    "check_every": self.cfg.check_every, "projects": projects}

    def log(self, ident: str) -> str:
        return self.store.log(ident)

    # --- doing -------------------------------------------------------------------

    def _job(self, kind: str, name: str, echo=None) -> tuple[Project, Job]:
        if kind not in PLANS:
            raise ValueError("Pulley can update a project or take it back, nothing else.")
        with self._lock:
            if self.job:
                raise Busy(f"{self.job.name} is being worked on. One at a time.")
            left = self.store.pending()
            if _elsewhere(left):
                raise Busy(f"{left.get('name')} is being worked on by another Pulley on this machine. "
                           "One at a time.")
            p = self.cfg.project(name)
            ident = f"{p.name}-{time.strftime('%Y%m%d-%H%M%S')}"
            self.job = Job(kind, p.name, ident, self.store.log_file(ident), echo,
                           lambda step: self._mark(step))
            return p, self.job

    def _mark(self, step: str) -> None:
        left = self.store.pending()
        if left:
            self.store.set_pending({**left, "step": step})

    def start(self, kind: str, name: str, expect: str = "") -> str:
        """Begin an update ("update") or a step back ("back") in the background. Returns the attempt's id."""
        p, job = self._job(kind, name)
        threading.Thread(target=self._run, args=(p, job, expect), daemon=True).start()
        return job.id

    def run_now(self, kind: str, name: str, echo=None) -> dict:
        """The same, but wait for it. Returns how it went."""
        p, job = self._job(kind, name, echo)
        return self._run(p, job, "")

    def _run(self, p: Project, job: Job, expect: str) -> dict:
        entry = {"id": job.id, "kind": job.kind, "at": job.started, "from": None, "to": None}
        patience = time.monotonic() + 150
        while self.checking and time.monotonic() < patience:
            time.sleep(0.1)  # a look in progress uses git in the same folder; let it finish
        try:
            try:
                self.store.set_pending({"id": job.id, "kind": job.kind, "name": p.name, "at": job.started,
                                        "step": "", "pid": os.getpid()})
                result, note = (self._update if job.kind == "update" else self._back)(p, job, entry, expect)
            except Exception as e:  # nothing may leave a job hanging
                job.say(traceback.format_exc())
                result = "stuck" if job.touched else "failed"
                note = f"Pulley ran into an error of its own ({e}). " + (
                    "Check the app by hand." if job.touched else "Nothing was changed.")
            entry.update(result=result, note=note, seconds=round(time.time() - job.started, 1))
            job.say(f"== {RESULTS[result]}" + (f": {note}" if note else "."))
            job.close(result == "done")
            try:
                self.store.add(p.name, entry)
                self.store.set_pending(None)
                self._remember(p, self._survey(p, False, self._environment()))
            except Exception as e:  # a full disk must not keep Pulley busy for ever
                job.say(f"This could not be written down: {e}")
        finally:
            with self._lock:
                self.job = None
        return entry

    def _plan(self, entry: dict, old: dict, new: dict) -> None:
        """Note what this attempt moves between, also on disk, in case Pulley is stopped half way."""
        entry["from"], entry["to"] = old, new
        left = self.store.pending()
        if left:
            self.store.set_pending({**left, "from": old, "to": new})

    def _usable(self, p: Project, compose: Compose) -> tuple[dict, str]:
        """The compose files as they are in the checkout right now, and the image name they give the service.

        Raises DockerError when they cannot be used to start it.
        """
        cfg, unset = compose.config()
        if unset:
            raise DockerError(f"its compose file uses {', '.join(unset)}, which Pulley has no value for")
        service = (cfg.get("services") or {}).get(p.service)
        if not service or not service.get("build"):
            raise DockerError(f'its compose file does not build a service called "{p.service}"')
        return cfg, docker.image_name(cfg, p.service)

    def _code_back(self, repo: Repo, sha: str, job: Job) -> str:
        """Move the checkout back. Returns "" or a sentence saying that it could not be done."""
        try:
            repo.back(sha, job.say)
            return ""
        except GitError as e:
            job.say(f"The code could not be moved back: {e}")
            return f" The code folder could not be moved back ({e}), so it is not at the version that is running."

    def _update(self, p: Project, job: Job, entry: dict, expect: str) -> tuple[str, str]:
        st = self._survey(p, True, self._environment(), job.say)
        self._remember(p, st)
        if st["problems"]:
            return "failed", st["problems"][0] + " Nothing was changed."
        if not st["behind"]:
            return "failed", "There is nothing newer to move to. Nothing was changed."
        old, target, c, compose = st["current"], st["target"], st["_container"], st["_compose"]
        if expect and target["sha"] != expect:
            return "failed", ("Something newer arrived after the list of changes was shown. "
                              "Look at the list again first. Nothing was changed.")
        self._plan(entry, old, target)
        repo = Repo(p.path, p.remote)
        old_cfg, old_name = st["_cfg"], docker.image_name(st["_cfg"], p.service)
        outgoing, previous = f"{docker.TAGS}/{p.name}:outgoing", f"{docker.TAGS}/{p.name}:previous"
        job.say(f"Moving {p.name} from {old['short']} to {target['short']}: {st['behind']} change(s).")

        job.step("code")
        try:
            docker.tag(c["image"], outgoing)  # under a name of its own, the running image cannot be lost
        except DockerError as e:
            return "failed", f"The running image could not be set aside ({e}). Nothing was changed."
        job.touched = True
        try:
            repo.forward(target["sha"], job.say)
        except GitError as e:
            docker.untag(outgoing)
            return "failed", f"The new code could not be put in place ({e}). The running version was not touched."
        try:
            new_cfg, new_name = self._usable(p, compose)  # the compose file may have changed with the code
        except DockerError as e:
            docker.untag(outgoing)
            return "failed", (f"The new version cannot be started as it is: {e}. The running version was not "
                              "touched." + self._code_back(repo, old["sha"], job))

        job.step("build")
        try:
            compose.build(p.service, job.say, p.build_wait)
            built = docker.image_id(new_name)
            if not built:
                raise DockerError(f"the build left no image called {new_name}")
        except DockerError as e:
            job.say(f"The build did not work: {e}")
            self._point(outgoing, old_name, job)
            return "failed", ("The new version could not be built. The running version was not touched."
                              + self._code_back(repo, old["sha"], job))
        if built == c["image"] and new_cfg == old_cfg:
            docker.untag(outgoing)
            return "done", "The new code builds into the very same image, so nothing was restarted."

        job.step("swap")
        try:
            compose.up(p.service, job.say)
            reason = ""
        except DockerError as e:
            reason = f"the new container could not be started ({e})"
        if not reason:
            job.step("health")
            ok, reason = self._wait(p, compose, job)
            if ok:
                try:
                    docker.tag(c["image"], previous)
                    self.store.set_previous(p.name, {"image": previous, "commit": old, "at": time.time()})
                except DockerError as e:
                    job.say(f"The old image could not be kept to go back to: {e}")
                docker.untag(outgoing)
                return "done", ""
        return self._restore(p, repo, compose, old["sha"], old_name, outgoing, reason, job)

    def _back(self, p: Project, job: Job, entry: dict, expect: str) -> tuple[str, str]:
        kept = self.store.previous(p.name)
        if not kept:
            return "failed", "No earlier version is kept for this project. Nothing was changed."
        st = self._survey(p, False, self._environment(), job.say)
        self._remember(p, st)
        if st["problems"]:
            return "failed", st["problems"][0] + " Nothing was changed."
        old, c, compose = st["current"], st["_container"], st["_compose"]
        earlier = docker.image_id(kept["image"])
        if not earlier or (earlier == c["image"] and kept["commit"]["sha"] == old["sha"]):
            self.store.set_previous(p.name, None)
            return "failed", ("The earlier image is no longer on this machine. Nothing was changed." if not earlier
                              else "The earlier version is the one that is running. Nothing was changed.")
        self._plan(entry, old, kept["commit"])
        repo = Repo(p.path, p.remote)
        old_name = docker.image_name(st["_cfg"], p.service)
        outgoing = f"{docker.TAGS}/{p.name}:outgoing"
        job.say(f"Taking {p.name} back from {old['short']} to {kept['commit']['short']}.")

        job.step("code")
        try:
            docker.tag(c["image"], outgoing)
        except DockerError as e:
            return "failed", f"The running image could not be set aside ({e}). Nothing was changed."
        job.touched = True
        try:
            repo.back(kept["commit"]["sha"], job.say)
            _, name = self._usable(p, compose)  # the earlier commit's compose file may name the image otherwise
            docker.tag(kept["image"], name)
        except (GitError, DockerError) as e:
            code = self._code_back(repo, old["sha"], job)
            self._point(outgoing, old_name, job)
            return "failed", (f"The earlier version could not be put in place ({e}). "
                              "The running version was not touched." + code)

        job.step("swap")
        try:
            compose.up(p.service, job.say)
            reason = ""
        except DockerError as e:
            reason = f"the container could not be started ({e})"
        if not reason:
            job.step("health")
            ok, reason = self._wait(p, compose, job)
            if ok:
                self.store.set_previous(p.name, None)
                docker.untag(kept["image"])
                docker.untag(outgoing)
                return "done", ""
        return self._restore(p, repo, compose, old["sha"], old_name, outgoing, reason, job)

    def _point(self, kept: str, ref: str, job: Job) -> None:
        """Make the name the compose file uses mean the kept image again, then drop the spare name."""
        try:
            docker.tag(kept, ref)
            docker.untag(kept)
        except DockerError as e:
            job.say(f"The image name {ref} could not be pointed back at the running image: {e}")

    def _restore(self, p: Project, repo: Repo, compose: Compose, sha: str, name: str, outgoing: str,
                 reason: str, job: Job) -> tuple[str, str]:
        """The version just started is no good: bring back the one that was running.

        `sha` is the commit that was checked out and `name` what its compose file calls the image.
        """
        job.step("restore", failed=True)
        job.say(f"It did not come up: {reason}.")
        try:
            found = compose.containers(p.service)
            if found:
                job.say("-- the last lines it printed:")
                for line in docker.logs(found[0]["id"]).splitlines():
                    job.say("   " + line)
        except DockerError:
            pass
        # The code first: the old image has to be started from the compose file it came with.
        code = self._code_back(repo, sha, job)
        ok, why = False, ""
        try:
            docker.tag(outgoing, name)
            compose.up(p.service, job.say)
            ok, why = self._wait(p, compose, job)
        except DockerError as e:
            why = str(e)
        if ok:
            docker.untag(outgoing)
            return "put back", f"It did not come up: {reason}. The version that was running is running again." + code
        return "stuck", (f"It did not come up: {reason}. Bringing back the version that was running did not work "
                         f"either: {why}. It needs a look by hand. The image that was running is kept as {outgoing}."
                         + code)

    def _wait(self, p: Project, compose: Compose, job: Job) -> tuple[bool, str]:
        """Watch the new container until it is plainly well, plainly not, or out of time."""
        deadline = time.monotonic() + p.health_wait
        running_since = None
        waiting_for = "to start"
        while True:
            try:
                found = compose.containers(p.service)
                c = found[0] if found else None
            except DockerError:
                c = None
            if c is None:
                running_since, waiting_for = None, "for the container to appear"
            elif c["status"] in ("exited", "dead"):
                return False, f"it stopped right after starting (exit code {c['exit']})"
            elif c["status"] == "restarting" or c["restarts"]:
                return False, "it keeps crashing and being restarted"
            elif c["health"] == "unhealthy":
                return False, "its own health check says it is not well"
            elif c["status"] == "running":
                running_since = running_since or time.monotonic()
                if c["health"] == "starting":
                    waiting_for = "for its own health check to pass"
                elif p.health_url:
                    if answers(p.health_url):
                        return True, ""
                    waiting_for = f"for {p.health_url} to answer"
                elif c["health"] == "healthy" or time.monotonic() - running_since >= p.settle:
                    return True, ""
                else:
                    waiting_for = "to see that it stays up"
            else:
                running_since = None
            if time.monotonic() >= deadline:
                return False, f"after {p.health_wait} seconds it was still waiting {waiting_for}"
            time.sleep(self.poll)
