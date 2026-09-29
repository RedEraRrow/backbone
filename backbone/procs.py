"""Background processes a tool starts, finds and stops: its daemons, however
they were launched."""
import os
import signal
import subprocess
import sys
from itertools import dropwhile
from pathlib import Path


def ps_listing() -> str:
    """Every running process's full command line, one per line."""
    try:
        return subprocess.run(["ps", "-Awwo", "command"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def find_processes(*names: str, launcher: str | None = None) -> list:
    """(pid, command) for every running process whose program is one of
    `names`, however it was started: `name`, `name.py`, `python3 name.py`, or
    through a `launcher` command (`launcher name`). The calling process is
    never included."""
    try:
        out = subprocess.run(["ps", "-Awwo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        args = list(dropwhile(lambda a: a.startswith("-") or Path(a).name.lower().startswith("python"), command.split()))
        if not args:
            continue
        prog = Path(args[0]).name.removesuffix(".py")
        if prog in names or (launcher and prog == launcher and len(args) > 1 and args[1] in names):
            found.append((int(pid), command))
    return found


def stop_processes(*names: str, launcher: str | None = None) -> int:
    """SIGTERMs every running process find_processes finds; returns how many."""
    stopped = 0
    for pid, _ in find_processes(*names, launcher=launcher):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
        except OSError:
            pass
    return stopped


def spawn_script(script: Path, out_path: Path) -> None:
    """Start the Python `script` in the background with this interpreter, its
    output (unbuffered, so it can be read while it runs) appended to `out_path`."""
    with open(out_path, "ab") as out:     # the child keeps its own copy of the handle
        subprocess.Popen([sys.executable, "-u", str(script)], stdout=out, stderr=subprocess.STDOUT)
