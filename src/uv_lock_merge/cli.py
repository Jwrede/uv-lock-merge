"""Command line: git merge driver, conflict resolver and installer for uv.lock."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import __version__
from .merge import Unresolved, merge_locks
from .serialize import detect_style, dumps

DRIVER = "uv-lock"
ATTRIBUTE_LINE = f"uv.lock merge={DRIVER}"


def _say(msg: str) -> None:
    print(f"uv-lock-merge: {msg}", file=sys.stderr)


def _parse(text: str, what: str) -> dict:
    if not text.strip():
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise Unresolved(f"{what} is not valid TOML ({e})") from None


def merge_texts(base: str, ours: str, theirs: str) -> tuple[str, list[str]]:
    merged, notes = merge_locks(
        _parse(base, "base"), _parse(ours, "ours"), _parse(theirs, "theirs")
    )
    return dumps(merged, detect_style(ours)), notes


def _report(path: str, notes: list[str]) -> None:
    _say(f"merged {path}")
    for n in notes:
        _say(f"  {n}")


def cmd_driver(args) -> int:
    base = Path(args.base).read_text(encoding="utf-8")
    ours = Path(args.ours).read_text(encoding="utf-8")
    theirs = Path(args.theirs).read_text(encoding="utf-8")
    path = args.path or "uv.lock"
    try:
        text, notes = merge_texts(base, ours, theirs)
    except Unresolved as e:
        _say(f"{path}: {e}; leaving a normal conflict")
        subprocess.run(
            ["git", "merge-file", "-L", "ours", "-L", "base", "-L", "theirs",
             args.ours, args.base, args.theirs],
            check=False,
        )
        return 1
    Path(args.ours).write_text(text, encoding="utf-8")
    _report(path, notes)
    return 0


def _git(*argv: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *argv], capture_output=True, text=True, cwd=cwd)


def _stage(path: str, n: int) -> str:
    r = _git("show", f":{n}:{path}")
    return r.stdout if r.returncode == 0 else ""


def cmd_resolve(args) -> int:
    status = 0
    for path in args.paths:
        rel = _git("ls-files", "--full-name", "-u", "--", path).stdout.split()
        if not rel:
            _say(f"{path}: not in a conflicted state")
            status = 1
            continue
        ours, theirs = _stage(path, 2), _stage(path, 3)
        try:
            text, notes = merge_texts(_stage(path, 1), ours, theirs)
        except Unresolved as e:
            _say(f"{path}: {e}; file left unchanged")
            status = 1
            continue
        Path(path).write_text(text, encoding="utf-8")
        _report(path, notes)
        if args.lock:
            project = Path(path).parent / "pyproject.toml"
            if project.exists() and "<<<<<<<" in project.read_text(encoding="utf-8"):
                _say(f"{project} still has conflict markers; not running uv lock")
            else:
                r = subprocess.run(["uv", "lock"], cwd=Path(path).parent or ".")
                if r.returncode != 0:
                    status = 1
                    continue
        _say(f"next: git add {path}")
    return status


def _driver_command() -> str:
    if shutil.which("uv-lock-merge"):
        return "uv-lock-merge driver %O %A %B %P"
    return f"{shlex.quote(sys.executable)} -m uv_lock_merge driver %O %A %B %P"


def _global_attributes_file() -> Path:
    r = _git("config", "--global", "--path", "core.attributesFile")
    if r.returncode == 0 and r.stdout.strip():
        return Path(os.path.expanduser(r.stdout.strip()))
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "git" / "attributes"


def cmd_install(args) -> int:
    scope = ["--global"] if args.global_ else []
    if not args.global_:
        top = _git("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            _say("not inside a git repository (use --global to set it up for all repositories)")
            return 1
    for key, value in (
        (f"merge.{DRIVER}.name", "three-way merge for uv.lock (uv-lock-merge)"),
        (f"merge.{DRIVER}.driver", _driver_command()),
    ):
        r = _git("config", *scope, key, value)
        if r.returncode != 0:
            _say(r.stderr.strip())
            return 1
    if args.global_:
        attributes = _global_attributes_file()
    elif args.local:
        attributes = Path(_git("rev-parse", "--git-path", "info/attributes").stdout.strip())
    else:
        attributes = Path(top.stdout.strip()) / ".gitattributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    existing = attributes.read_text(encoding="utf-8") if attributes.exists() else ""
    if ATTRIBUTE_LINE not in existing.splitlines():
        sep = "" if not existing or existing.endswith("\n") else "\n"
        attributes.write_text(existing + sep + ATTRIBUTE_LINE + "\n", encoding="utf-8")
    _say(f"merge driver configured; attribute in {attributes}")
    return 0


def cmd_merge(args) -> int:
    """Merge three files given on the command line and print the result."""
    try:
        text, notes = merge_texts(
            Path(args.base).read_text(encoding="utf-8"),
            Path(args.ours).read_text(encoding="utf-8"),
            Path(args.theirs).read_text(encoding="utf-8"),
        )
    except Unresolved as e:
        _say(str(e))
        return 1
    sys.stdout.write(text)
    for n in notes:
        _say(n)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="uv-lock-merge",
        description="Three-way merge for uv.lock files, as a git merge driver or after a conflict.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("driver", help="git merge driver entry point (%%O %%A %%B %%P)")
    d.add_argument("base")
    d.add_argument("ours")
    d.add_argument("theirs")
    d.add_argument("path", nargs="?")
    d.set_defaults(func=cmd_driver)

    r = sub.add_parser("resolve", help="resolve a uv.lock that is already in conflict")
    r.add_argument("paths", nargs="*", default=["uv.lock"])
    r.add_argument("--lock", action="store_true", help="run `uv lock` afterwards")
    r.set_defaults(func=cmd_resolve)

    i = sub.add_parser("install", help="register the merge driver")
    i.add_argument("--global", dest="global_", action="store_true",
                   help="for all repositories (global git config and attributes file)")
    i.add_argument("--local", action="store_true",
                   help="write the attribute to .git/info/attributes instead of .gitattributes")
    i.set_defaults(func=cmd_install)

    m = sub.add_parser("merge", help="merge BASE OURS THEIRS and print the result")
    m.add_argument("base")
    m.add_argument("ours")
    m.add_argument("theirs")
    m.set_defaults(func=cmd_merge)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
