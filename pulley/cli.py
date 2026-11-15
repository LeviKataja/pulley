"""Command line.

    pulley                     run the page (the default)
    pulley check               look for updates once and print what was found
    pulley update NAME         update one project from the terminal
    pulley back NAME           take one project back to the version kept from before
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, config


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _manager(args):
    from .engine import Manager

    if args.data:
        os.environ["PULLEY_HOME"] = args.data
    return Manager(config.home(), Path(args.config) if args.config else None)


def _called(commit: dict) -> str:
    return commit.get("tag") or commit["short"]


def _describe(p: dict) -> list[str]:
    here = _called(p["current"]) if p["current"] else "?"
    if p["state"] == "ok":
        lines = [f"{p['name']}: up to date ({here})"]
    elif p["behind"]:
        n = p["behind"]
        head = f"{p['name']}: {n} change{'s' if n != 1 else ''} waiting ({here} -> {_called(p['target'])})"
        lines = [head + (", but it cannot be updated" if p["problems"] else "")]
        lines += [f"    {_called(c)}  {c['subject']}" for c in p["changes"][:20]]
    else:
        lines = [f"{p['name']}: needs a look ({here})"]
    lines += [f"    ! {text}" for text in p["problems"]]
    lines += [f"    - {text}" for text in p["notes"]]
    return lines


def cmd_check(args) -> int:
    mgr = _manager(args)
    if mgr.config_error:
        print(f"Settings problem: {mgr.config_error}", file=sys.stderr)
        return 2
    mgr.check()
    state = mgr.state()
    if not state["projects"]:
        print(f"No projects yet. Add one to {state['config_file']}.")
    for p in state["projects"]:
        print("\n".join(_describe(p)))
    return 0


def cmd_job(args) -> int:
    mgr = _manager(args)
    if mgr.config_error:
        print(f"Settings problem: {mgr.config_error}", file=sys.stderr)
        return 2
    try:
        mgr.cfg.project(args.name)
    except KeyError as e:
        print(str(e).strip("'\""), file=sys.stderr)
        return 2
    if args.command == "update":
        mgr.check()
        p = next(p for p in mgr.state()["projects"] if p["name"] == args.name)
        print("\n".join(_describe(p)))
        if p["state"] != "waiting":
            return 0 if p["state"] == "ok" else 1
    if not args.yes:
        try:
            answer = input("Go ahead? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Left as it is.")
            return 1
    entry = mgr.run_now(args.command, args.name, echo=print)
    return 0 if entry["result"] == "done" else 1


def cmd_serve(args) -> int:
    from .server import serve

    if args.demo:
        from .demo import Demo

        return serve(Demo(), args.host, args.port, args.password or "")
    return serve(_manager(args), args.host, args.port, args.password or "")


def main(argv: list[str] | None = None) -> int:
    _utf8()
    parser = argparse.ArgumentParser(
        prog="pulley", description="Updates for apps that run in Docker and are built from a git checkout.")
    parser.add_argument("--version", action="version", version=f"pulley {__version__}")
    sub = parser.add_subparsers(dest="command")

    def common(p) -> None:
        p.add_argument("--data", help="folder for settings, history and logs (default: ~/.pulley)")
        p.add_argument("--config", help="the settings file (default: pulley.toml in the data folder)")

    def serve_args(p) -> None:
        common(p)
        p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this machine only)")
        p.add_argument("--port", type=int, default=8790)
        p.add_argument("--password", help="the password the page asks for (or set PULLEY_PASSWORD)")
        p.add_argument("--demo", action="store_true", help="made-up projects, to try the page: nothing is touched")

    serve_args(parser)
    serve_args(sub.add_parser("serve", help="run the page (the default)"))
    common(sub.add_parser("check", help="look for updates once and print what was found"))
    for name, text in (("update", "update one project now"), ("back", "take one project back to the version before")):
        p = sub.add_parser(name, help=text)
        p.add_argument("name", help="the project, as named in the settings file")
        p.add_argument("--yes", action="store_true", help="do not ask first")
        common(p)

    args = parser.parse_args(argv)
    if args.command == "check":
        return cmd_check(args)
    if args.command in ("update", "back"):
        return cmd_job(args)
    return cmd_serve(args)
