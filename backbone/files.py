"""Writing files so a crash mid-write can't leave them truncated."""
import os
import shutil
import tempfile


def write_text_atomic(path, text: str) -> None:
    """Write `text` to `path` via a temporary file in the same directory that
    then replaces it: the file is either the old version or the new one, never
    half of the new one."""
    path = os.fspath(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp_",
                               suffix=os.path.splitext(path)[1])
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_copy(path) -> str | None:
    """Copy `path` to `path.bak` (replacing an older one) before it's
    overwritten; returns the backup's path, or None if there was nothing to copy."""
    path = os.fspath(path)
    if not os.path.isfile(path):
        return None
    dest = path + ".bak"
    shutil.copy2(path, dest)
    return dest


def log_line(path, message: str) -> None:
    """Print `message` with a timestamp and append the same line to the log at
    `path`: a daemon's running record, readable on screen and afterwards."""
    from datetime import datetime
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line)
    with open(path, "a") as f:
        f.write(line + "\n")


def disk_free(path) -> str:
    """Free space on `path`'s volume as `df -h` shows it, or "?"."""
    import subprocess
    try:
        lines = subprocess.run(["df", "-h", os.fspath(path)], capture_output=True, text=True, timeout=10).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    return lines[-1].split()[3] if len(lines) > 1 else "?"


def count_entries(d) -> int:
    """How many entries the folder `d` holds, 0 if it doesn't exist."""
    return len(os.listdir(d)) if os.path.isdir(d) else 0
