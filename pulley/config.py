"""The settings file: which projects Pulley looks after, and how."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
FILE_NAME = "pulley.toml"

_NAME = re.compile(r"(?=.{1,63}\Z)[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")  # also has to do as part of an image name
_SPAN = re.compile(r"\s*(\d+)\s*([smhd]?)\s*\Z")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

EXAMPLE = '''\
# Pulley's settings. Pulley reads this file again whenever it looks for updates.

# How often to look for updates on its own: "30m", "6h", "1d", or "never".
check_every = "6h"

# One block per app. Remove the # signs and fill it in.
#
# [[project]]
# name = "myapp"                    # what the page calls it: lowercase letters, digits, - _ .
# path = "/srv/apps/myapp"          # the git checkout the app is built from
# service = "myapp"                 # its service in the compose file (default: the name)
# compose = "compose.yaml"          # the compose file, if it is not one of the usual names in that folder
# follow = "branch"                 # "branch": every new commit. "tags": only new version tags.
# branch = "main"                   # which branch (default: the one checked out)
# health_url = "http://192.168.1.10:8080/"   # optional: an address that answers once the app is up
# health_wait = "2m"                # how long the new version gets to come up
'''


class ConfigError(Exception):
    pass


def home() -> Path:
    """The folder Pulley keeps its settings, history and logs in."""
    return Path(os.environ.get("PULLEY_HOME") or Path.home() / ".pulley")


def seconds(value, what: str, least: int = 0) -> int:
    """A length of time as written in the settings: a number of seconds, "90s", "30m", "6h", "1d", "never"."""
    if isinstance(value, str) and value.strip().lower() == "never":
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f'{what} should be a length of time such as "30m" or "6h".')
    match = _SPAN.match(str(value))
    if not match:
        raise ConfigError(f'{what} should be a length of time such as "30m" or "6h", not "{value}".')
    total = int(match.group(1)) * _UNIT[match.group(2)]
    if total and total < least:
        raise ConfigError(f"{what} cannot be shorter than {least} seconds.")
    return total


@dataclass
class Project:
    name: str
    path: Path
    service: str = ""
    compose: str = ""          # as written; see compose_file()
    follow: str = "branch"     # or "tags"
    branch: str = ""           # default: the branch that is checked out
    tags: str = "*"            # which tag names count as versions
    prereleases: bool = False  # offer -rc, -beta and the like too
    remote: str = "origin"
    project_name: str = ""     # docker compose -p, when the app was started with one
    health_url: str = ""
    health_wait: int = 120     # seconds the new version gets to come up
    settle: int = 20           # with nothing else to go by: seconds it must stay running
    build_wait: int = 1800     # seconds a build may take
    skip_path_check: bool = False

    def compose_file(self) -> Path | None:
        """The compose file, or None when there is none to be found."""
        if self.compose:
            file = Path(self.compose)
            file = file if file.is_absolute() else self.path / file
            return file if file.is_file() else None
        for name in COMPOSE_NAMES:
            if (self.path / name).is_file():
                return self.path / name
        return None


@dataclass
class Config:
    projects: list[Project] = field(default_factory=list)
    check_every: int = 6 * 3600

    def project(self, name: str) -> Project:
        for p in self.projects:
            if p.name == name:
                return p
        raise KeyError(f'There is no project called "{name}".')


_TEXT = ("service", "compose", "branch", "tags", "remote", "project_name", "health_url")
_SPANS = {"health_wait": 5, "settle": 0, "build_wait": 30}
_FLAGS = ("prereleases", "skip_path_check")
_KNOWN = {"name", "path", "follow", *_TEXT, *_SPANS, *_FLAGS}


def _project(raw, number: int) -> Project:
    if not isinstance(raw, dict):
        raise ConfigError(f"Project {number} is not written as a [[project]] block.")
    name = raw.get("name")
    where = f'Project "{name}"' if isinstance(name, str) and name else f"Project {number}"
    if not isinstance(name, str) or not _NAME.match(name):
        raise ConfigError(f"{where} needs a name made of lowercase letters and digits, with single dots, dashes "
                          "or underscores between them.")
    unknown = sorted(set(raw) - _KNOWN)
    if unknown:
        raise ConfigError(f"{where} has a setting Pulley does not know: {', '.join(unknown)}.")
    path = raw.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ConfigError(f"{where} needs path: the full path of the git checkout it is built from.")
    p = Project(name=name, path=Path(path))
    for key in _TEXT:
        if key in raw:
            if not isinstance(raw[key], str):
                raise ConfigError(f"{where}: {key} should be text in quotes.")
            setattr(p, key, raw[key].strip())
    for key in _FLAGS:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ConfigError(f"{where}: {key} should be true or false.")
            setattr(p, key, raw[key])
    for key, least in _SPANS.items():
        if key in raw:
            setattr(p, key, seconds(raw[key], f"{where}: {key}", least))
    follow = raw.get("follow", "branch")
    if follow not in ("branch", "tags"):
        raise ConfigError(f'{where}: follow should be "branch" or "tags".')
    p.follow = follow
    p.service = p.service or name
    p.remote = p.remote or "origin"
    p.tags = p.tags or "*"
    if p.health_url and not p.health_url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"{where}: health_url should start with http:// or https://.")
    if not p.health_wait:
        raise ConfigError(f"{where}: health_wait cannot be never.")
    if p.settle >= p.health_wait:
        raise ConfigError(f"{where}: settle has to be shorter than health_wait.")
    return p


def parse(text: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"The settings file is not valid TOML: {e}.") from None
    unknown = sorted(set(raw) - {"check_every", "project"})
    if unknown:
        raise ConfigError(f"The settings file has a setting Pulley does not know: {', '.join(unknown)}.")
    cfg = Config()
    if "check_every" in raw:
        cfg.check_every = seconds(raw["check_every"], "check_every", 300)
    blocks = raw.get("project", [])
    if not isinstance(blocks, list):
        raise ConfigError("Projects are written as [[project]] blocks, with double brackets.")
    seen: set[str] = set()
    for number, block in enumerate(blocks, 1):
        p = _project(block, number)
        if p.name in seen:
            raise ConfigError(f'Two projects are called "{p.name}".')
        seen.add(p.name)
        cfg.projects.append(p)
    return cfg


def load(file: Path) -> Config:
    """Read the settings file, writing a commented example first if there is none."""
    if not file.exists():
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(EXAMPLE, "utf-8")
        except OSError as e:
            raise ConfigError(f"There is no settings file at {file} and one could not be written ({e}).") from None
    try:
        return parse(file.read_text("utf-8"))
    except OSError as e:
        raise ConfigError(f"The settings file {file} could not be read ({e}).") from None
