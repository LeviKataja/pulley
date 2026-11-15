"""A stand-in for the docker command, for the tests. It keeps its world in a JSON file.

Compose files are read for real, as long as they are written as JSON (which is valid YAML). It knows
`build`, `image`, `command` and `volumes` of a service, `name`, `${VARIABLES}` (from the world's "env" and a
.env file in the project folder), override files, -f, -p, --project-directory and --env-file.

The "apps" it builds are folders with an app.txt. Words in that file decide how the image behaves:

    nobuild     the build fails
    crash       the container stops right after starting
    unhealthy   the container has a health check, and it fails
    healthy     the container has a health check, and it passes
    slow        the container has a health check that never finishes

An image's id is made from app.txt alone, so a change elsewhere in the folder builds the very same image.
The world file can also say: "down" (Docker does not answer), "up_fails" (containers cannot be started),
"crash_images" (ids of images whose containers stop at once), "home" (what ~ means).
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_DOCKER_STATE"])
NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
OVERRIDES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml",
             "docker-compose.override.yml")


def load() -> dict:
    world = {"images": {}, "tags": {}, "containers": {}, "count": 0, "calls": [], "env": {}}
    if STATE.exists():
        world.update(json.loads(STATE.read_text("utf-8")))
    return world


def save(world: dict) -> None:
    STATE.write_text(json.dumps(world, indent=1), "utf-8")


def die(message: str, code: int = 1):
    print(message, file=sys.stderr)
    sys.exit(code)


def norm(name: str) -> str:
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def resolve(world: dict, ref: str) -> str:
    if ref in world["images"]:
        return ref
    return world["tags"].get(norm(ref), "")


class Project:
    """A compose project as the real Compose would understand the command line and the files."""

    def __init__(self, world: dict, args: list[str]):
        cwd = Path.cwd()
        name, files, folder, env_files = "", [], "", []
        while args and args[0] in ("-p", "-f", "--project-directory", "--env-file"):
            flag, value, args = args[0], args[1], args[2:]
            if flag == "-p":
                name = value
            elif flag == "-f":
                files.append(Path(value) if Path(value).is_absolute() else cwd / value)
            elif flag == "--project-directory":
                folder = value
            else:
                env_files.append(Path(value))
        if not files:  # found the way Compose finds them from where it is run
            base = next((cwd / n for n in NAMES if (cwd / n).is_file()), None)
            if base is None:
                die("no configuration file provided: not found", 14)
            files = [base] + [cwd / n for n in OVERRIDES if (cwd / n).is_file()][:1]
        for file in files:
            if not file.is_file():
                die(f"open {file}: The system cannot find the file specified.", 14)
        self.files, self.rest = files, args
        self.folder = Path(folder) if folder else files[0].parent
        env = dict(world.get("env", {}))
        for file in env_files or [self.folder / ".env"]:
            if file.is_file():
                for line in file.read_text("utf-8").splitlines():
                    if "=" in line and not line.strip().startswith("#"):
                        key, value = line.split("=", 1)
                        env[key.strip()] = value.strip()
        self.unset: list[str] = []

        def fill(match) -> str:
            if match.group(1) not in env:
                self.unset.append(match.group(1))
            return env.get(match.group(1), "")

        merged: dict = {"services": {}}
        for file in files:
            data = json.loads(re.sub(r"\$\{(\w+)\}", fill, file.read_text("utf-8")))
            if "name" in data:
                merged["name"] = data["name"]
            for service, spec in (data.get("services") or {}).items():
                into = merged["services"].setdefault(service, {})
                volumes = into.get("volumes", []) + spec.get("volumes", [])
                into.update(spec)
                if volumes:
                    into["volumes"] = volumes
        self.name = name or merged.get("name") or self.folder.name.lower()
        self.services = {}
        home = world.get("home", "/root")
        for service, spec in merged["services"].items():
            out = {}
            if "build" in spec:
                out["build"] = {"context": str((self.folder / spec["build"]).resolve())}
            for key in ("image", "command"):
                if key in spec:
                    out[key] = spec[key]
            for volume in spec.get("volumes", []):
                source, target = volume.rsplit(":", 1)
                if source.startswith("~"):
                    source = home + source[1:]
                elif not source.startswith("/"):
                    source = os.path.normpath(self.folder / source).replace("\\", "/")
                out.setdefault("volumes", []).append({"type": "bind", "source": source, "target": target})
            self.services[service] = out

    def image(self, service: str) -> str:
        return self.services[service].get("image") or f"{self.name}-{service}"

    def warn(self) -> None:
        for name in dict.fromkeys(self.unset):
            print(f'time="2026-01-01T00:00:00Z" level=warning msg="The \\"{name}\\" variable is not set. '
                  'Defaulting to a blank string."', file=sys.stderr)


def compose(world: dict, args: list[str]) -> None:
    project = Project(world, args)
    command, rest = project.rest[0], project.rest[1:]
    if command == "config":
        project.warn()
        print(json.dumps({"name": project.name, "services": project.services}))
        return
    service = rest[-1]
    if service not in project.services:
        save(world)
        die(f"no such service: {service}")
    spec, ref = project.services[service], project.image(service)
    mine = [cid for cid, c in world["containers"].items()
            if c["labels"]["com.docker.compose.project"] == project.name
            and c["labels"]["com.docker.compose.service"] == service]

    if command == "ps":
        print("\n".join(mine))
    elif command == "build":
        text = (Path(spec["build"]["context"]) / "app.txt").read_text("utf-8")
        print(f"#1 [internal] load build definition from Dockerfile\n#2 building {ref}")
        if "nobuild" in text.split():
            print('#5 ERROR: process "/bin/sh -c make" did not complete successfully: exit code: 2')
            save(world)
            die("failed to solve: process \"/bin/sh -c make\" did not complete successfully: exit code: 2", 17)
        image = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        world["images"][image] = {"words": text.split()}
        world["tags"][norm(ref)] = image
        print(f"#8 naming to docker.io/library/{ref} done")
    elif command == "up":
        project.warn()
        if world.get("up_fails"):
            save(world)
            die("Error response from daemon: driver failed programming external connectivity")
        image = world["tags"].get(norm(ref))
        if not image and "build" not in spec:  # an image that is only named gets pulled
            image = "sha256:" + hashlib.sha256(ref.encode()).hexdigest()
            world["images"][image] = {"words": []}
            world["tags"][norm(ref)] = image
        if not image:
            save(world)
            die(f"Error response from daemon: No such image: {ref}")
        for cid in mine:
            if world["containers"][cid]["labels"]["com.docker.compose.oneoff"] == "False":
                del world["containers"][cid]
        world["count"] += 1
        cid = hashlib.sha256(str(world["count"]).encode()).hexdigest()
        words = world["images"][image]["words"]
        c = {"image": image, "ref": ref, "status": "running", "health": "", "exit": 0, "restarts": 0,
             "command": spec.get("command", ""),
             "mounts": [["bind", v["source"], v["target"]] for v in spec.get("volumes", [])],
             "labels": {"com.docker.compose.project": project.name,
                        "com.docker.compose.service": service,
                        "com.docker.compose.project.config_files": ",".join(str(f) for f in project.files),
                        "com.docker.compose.project.working_dir": str(project.folder),
                        "com.docker.compose.oneoff": "False"}}
        if "crash" in words or image in world.get("crash_images", []):
            c.update(status="exited", exit=1)
        elif "unhealthy" in words:
            c["health"] = "unhealthy"
        elif "healthy" in words:
            c["health"] = "healthy"
        elif "slow" in words:
            c["health"] = "starting"
        world["containers"][cid] = c
        print(f" Container {ref}-1  Recreated\n Container {ref}-1  Started")
    else:
        die(f"fake docker compose: unknown command {command}", 2)


def main(argv: list[str]) -> None:
    world = load()
    world["calls"].append(argv)
    if world.get("down"):
        save(world)
        die("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?")
    head = argv[:2]
    if argv[0] == "version":
        print("Client: fake\nServer: fake")
    elif argv[0] == "compose":
        compose(world, argv[1:])
    elif head == ["container", "inspect"]:
        found = [(cid, c) for cid, c in world["containers"].items() if cid.startswith(argv[2])]
        if not found:
            save(world)
            die(f"Error response from daemon: No such container: {argv[2]}")
        cid, c = found[0]
        state = {"Status": c["status"], "ExitCode": c["exit"], "StartedAt": "2026-01-01T00:00:00Z"}
        if c["health"]:
            state["Health"] = {"Status": c["health"]}
        print(json.dumps([{"Id": cid, "Image": c["image"], "Config": {"Image": c["ref"], "Labels": c["labels"]},
                           "State": state, "RestartCount": c["restarts"],
                           "Mounts": [{"Type": k, "Source": s, "Destination": d} for k, s, d in c["mounts"]]}]))
    elif head == ["image", "inspect"]:
        image = resolve(world, argv[2])
        if not image:
            save(world)
            die(f"Error response from daemon: No such image: {argv[2]}")
        print(json.dumps([{"Id": image}]))
    elif argv[0] == "tag":
        image = resolve(world, argv[1])
        if not image:
            save(world)
            die(f"Error response from daemon: No such image: {argv[1]}")
        world["tags"][norm(argv[2])] = image
    elif argv[0] == "rmi":
        name = norm(argv[1])
        if name not in world["tags"]:
            save(world)
            die(f"Error response from daemon: No such image: {argv[1]}")
        image = world["tags"].pop(name)
        print(f"Untagged: {name}")
        used = image in world["tags"].values() or any(c["image"] == image for c in world["containers"].values())
        if not used:
            del world["images"][image]
            print(f"Deleted: {image}")
    elif argv[0] == "logs":
        print(f"fake: the last words of {argv[-1][:12]}")
    else:
        die(f"fake docker: unknown command {argv}", 2)
    save(world)


if __name__ == "__main__":
    main(sys.argv[1:])
