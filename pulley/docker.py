"""What Pulley needs from Docker and Docker Compose."""
from __future__ import annotations

import json
import os
import posixpath
import re
import socket
from pathlib import Path

from . import run

# Pulley's own names for the images it keeps. The host part does not exist, so they can never be pulled.
TAGS = "pulley.local"

# What Compose writes on a container about how it was started.
L_PROJECT = "com.docker.compose.project"
L_FILES = "com.docker.compose.project.config_files"
L_FOLDER = "com.docker.compose.project.working_dir"
L_ENV = "com.docker.compose.project.environment_file"
L_ONEOFF = "com.docker.compose.oneoff"

# Compose's warning for ${NAME} in a compose file when nothing gives NAME a value.
_UNSET = re.compile(r'The \\?"([A-Za-z_][A-Za-z0-9_]*)\\?" variable is not set')
_CONTAINER = re.compile(r"/containers/([0-9a-f]{64})/")


class DockerError(Exception):
    pass


def _out(*args: str, timeout: float = 30) -> str:
    try:
        return run.out([*run.DOCKER, *args], timeout=timeout)
    except run.CommandError as e:
        raise DockerError(str(e)) from e


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        raise DockerError("Docker gave an answer Pulley could not read.") from None


def reachable() -> str:
    """"" when Docker answers, otherwise why it does not."""
    try:
        _out("version", timeout=15)
        return ""
    except DockerError as e:
        return str(e)


def container(ref: str) -> dict | None:
    """The container with this id or name, boiled down to what Pulley uses. None when there is none."""
    try:
        data = _json(_out("container", "inspect", ref))
    except DockerError as e:
        if "no such" in str(e).lower():
            return None
        raise
    if not data:
        return None
    c = data[0]
    state = c.get("State") or {}
    return {
        "id": c.get("Id", ""),
        "image": c.get("Image", ""),  # the image's id
        "status": state.get("Status", ""),
        "health": (state.get("Health") or {}).get("Status", ""),
        "exit": state.get("ExitCode", 0),
        "restarts": c.get("RestartCount", 0),
        "started": state.get("StartedAt", ""),
        "labels": (c.get("Config") or {}).get("Labels") or {},
        "mounts": [(m.get("Type", ""), m.get("Source", ""), m.get("Destination", "")) for m in c.get("Mounts") or []],
    }


def image_id(ref: str) -> str:
    """The id of the image with this name or id, or "" when it is not on this machine."""
    try:
        data = _json(_out("image", "inspect", ref))
    except DockerError:
        return ""
    return data[0].get("Id", "") if data else ""


def tag(source: str, target: str) -> None:
    _out("tag", source, target)


def untag(ref: str) -> None:
    """Drop one of Pulley's names. The image itself goes only if nothing else names or uses it."""
    try:
        _out("rmi", ref)
    except DockerError:
        pass


def logs(cid: str, lines: int = 40) -> str:
    try:
        return run.stream([*run.DOCKER, "logs", "--tail", str(lines), cid], timeout=20)
    except run.CommandError as e:
        return e.output or str(e)


def own() -> tuple[list[tuple[str, str, str]], str] | None:
    """The mounts and id of the container Pulley itself runs in. None when it is not in one."""
    if not os.path.exists("/.dockerenv"):
        return None
    names = [socket.gethostname()]  # a container's hostname is its id, unless it was given another
    try:
        names += _CONTAINER.findall(Path("/proc/self/mountinfo").read_text("utf-8", errors="replace"))
    except OSError:
        pass
    for name in dict.fromkeys(names):
        me = container(name)
        if me is not None:
            return me["mounts"], me["id"]
    raise DockerError("Pulley could not find its own container.")


def on_host(path: str, mounts: list[tuple[str, str, str]]) -> str | None:
    """Where a folder inside Pulley's container really is on the machine. None if it is not mounted from it."""
    best = None
    for kind, source, dest in mounts:
        dest = dest.rstrip("/") or "/"
        inside = path == dest or path.startswith(dest.rstrip("/") + "/")
        if inside and (best is None or len(dest) > len(best[2])):
            best = (kind, source, dest)
    if best is None or best[0] != "bind":
        return None
    rest = path[len(best[2].rstrip("/")):]
    return (best[1].rstrip("/") + rest) or "/"


def image_name(cfg: dict, service: str) -> str:
    """The name Compose gives the image it builds for a service: the one written there, or project-service."""
    spec = (cfg.get("services") or {}).get(service) or {}
    return spec.get("image") or f"{cfg.get('name', '')}-{service}"


def other_folders(cfg: dict, service: str, c: dict) -> list[tuple[str, str, str]]:
    """Folders Compose would now hand the service that differ from what its container has.

    Each is (place inside the app, what Compose would mount there, what is mounted there now).
    """
    spec = (cfg.get("services") or {}).get(service) or {}
    have = {posixpath.normpath(dest): posixpath.normpath(source) for kind, source, dest in c["mounts"] if kind == "bind"}
    wrong = []
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict) or volume.get("type") != "bind":
            continue
        target = posixpath.normpath(str(volume.get("target", "")))
        source = posixpath.normpath(str(volume.get("source", "")).replace("\\", "/"))
        now = have.get(target, "")
        if now.replace("\\", "/") != source:
            wrong.append((target, source, now))
    return wrong


class Compose:
    """Docker Compose, asked and told things for one project.

    With no files given it finds them the way a person standing in the folder would. Given the files,
    folder and name a container says it was started with, it repeats exactly that.
    """

    def __init__(self, folder: Path, files: list[str] | tuple = (), project: str = "", env_files: list[str] | tuple = (),
                 exact: bool = False):
        self.folder = Path(folder)
        self.base = [*run.DOCKER, "compose"]
        if project:
            self.base += ["-p", project]
        if exact:
            self.base += ["--project-directory", str(self.folder)]
        for file in files:
            self.base += ["-f", str(file)]
        for file in env_files:
            self.base += ["--env-file", str(file)]

    @classmethod
    def of(cls, c: dict) -> "Compose":
        """Compose exactly as this container was started. Raises DockerError when that cannot be repeated."""
        labels = c["labels"]
        split = lambda key: [part for part in str(labels.get(key, "")).split(",") if part]  # noqa: E731
        files, folder, project = split(L_FILES), labels.get(L_FOLDER, ""), labels.get(L_PROJECT, "")
        if not files or not folder or not project:
            raise DockerError("The container does not say which compose file it was started from.")
        for path in [folder, *files, *split(L_ENV)]:
            if not os.path.exists(path):
                raise DockerError(f"The app was started from {path}, which Pulley cannot see. "
                                  "Mount that folder at the same path.")
        return cls(Path(folder), files, project, split(L_ENV), exact=True)

    def _out(self, *args: str, timeout: float = 30) -> str:
        try:
            return run.out([*self.base, *args], cwd=str(self.folder), timeout=timeout)
        except run.CommandError as e:
            raise DockerError(str(e)) from e

    def _stream(self, *args: str, timeout: float, on_line=None) -> None:
        try:
            run.stream([*self.base, *args], cwd=str(self.folder), timeout=timeout, on_line=on_line)
        except run.CommandError as e:
            raise DockerError(str(e)) from e

    def config(self) -> tuple[dict, list[str]]:
        """The compose files as Compose understands them, and the variables they wanted but did not find."""
        try:
            text, side = run.both([*self.base, "config", "--format", "json"], cwd=str(self.folder))
        except run.CommandError as e:
            raise DockerError(str(e)) from e
        data = _json(text)
        if not isinstance(data, dict):
            raise DockerError("Docker Compose gave an answer Pulley could not read.")
        return data, list(dict.fromkeys(_UNSET.findall(side)))

    def containers(self, service: str) -> list[dict]:
        """The containers of this service, running or not. One-off `compose run` leftovers do not count."""
        found = []
        for cid in self._out("ps", "-a", "-q", service).split():
            c = container(cid)
            if c is not None and str(c["labels"].get(L_ONEOFF, "")).lower() != "true":
                found.append(c)
        return found

    def build(self, service: str, on_line=None, timeout: float = 1800) -> None:
        self._stream("build", service, timeout=timeout, on_line=on_line)

    def up(self, service: str, on_line=None) -> None:
        """Replace the service's container with one made from whatever image its name points at now."""
        self._stream("up", "-d", "--no-deps", "--no-build", "--force-recreate", service, timeout=300, on_line=on_line)
