"""What a tool needs besides Python, checked in one place: at startup for what
it can't run without (`require`), and as a report for everything (each tool's
`doctor` command), each with how to install it on the platform it's on.

    VLC = Dep("VLC", probe=..., needed_for="playback", required=True,
              hints={"macos": "brew install vlc", "debian": "sudo apt install vlc"})
    deps.require([VLC])        # prints the hint and exits when VLC is missing
"""
from __future__ import annotations
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Callable

from backbone.log import log


@dataclass(frozen=True)
class Dep:
    name: str
    probe: Callable[[], str | None]   # what was found (a path, a version), or None
    needed_for: str
    required: bool = False
    # Install hint per platform (see platform_key); "other" when none fits.
    hints: dict = field(default_factory=dict)


def which(*commands: str) -> Callable[[], str | None]:
    """A probe for the first of `commands` on the PATH."""
    return lambda: next((p for c in commands if (p := shutil.which(c))), None)


def platform_key() -> str:
    """macos, windows, debian (Ubuntu too), fedora, arch, or linux."""
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    ids = ""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith(("ID=", "ID_LIKE=")):
                    ids += " " + line.split("=", 1)[1].strip().strip('"').lower()
    except OSError:
        pass
    for key, names in (("debian", ("debian", "ubuntu")), ("fedora", ("fedora", "rhel")),
                       ("arch", ("arch",))):
        if any(n in ids.split() for n in names):
            return key
    return "linux"


def hint(dep: Dep) -> str:
    """How to install `dep` here."""
    key = platform_key()
    return (dep.hints.get(key) or (dep.hints.get("linux") if key in ("debian", "fedora", "arch") else None)
            or dep.hints.get("other", ""))


def check(deps: list[Dep]) -> list[tuple[Dep, str | None]]:
    """Each dependency with what was found, or None when it's missing. A probe
    that fails counts as missing (and is logged)."""
    out = []
    for dep in deps:
        try:
            found = dep.probe() or None
        except Exception as exc:
            log.info("dependency %s: probe failed: %s", dep.name, exc)
            found = None
        out.append((dep, found))
    return out


def report_lines(deps: list[Dep]) -> list[str]:
    """One line per dependency, for a plain-text doctor."""
    lines = []
    for dep, found in check(deps):
        if found:
            lines.append(f"  ok       {dep.name}: {found}")
        else:
            need = "MISSING " if dep.required else "missing "
            lines.append(f"  {need} {dep.name} (for {dep.needed_for}): {hint(dep)}")
    return lines


def require(deps: list[Dep], tool: str) -> None:
    """Exit with what to install when a required dependency is missing, before
    the tool starts rather than as a traceback once it's running."""
    missing = [d for d, found in check(deps) if d.required and not found]
    if not missing:
        return
    print(f"{tool} can't start: something it needs isn't installed.", file=sys.stderr)
    for d in missing:
        print(f"  {d.name} (for {d.needed_for}): {hint(d)}", file=sys.stderr)
    print(f"`{tool} doctor` lists everything it uses.", file=sys.stderr)
    raise SystemExit(1)
