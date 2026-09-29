"""Telling someone something happened: a push to their phone through ntfy, or
a chime at the machine."""
import subprocess
import threading
import urllib.request


def ntfy(server: str, topic: str, title: str, message: str, priority: str = "default", tags: str = "") -> None:
    """Push `message` to an ntfy topic in the background. Nothing is sent
    without a topic, and a failed send is dropped rather than raised: a missed
    notification must never stop the work it reports on."""
    if not topic:
        return

    def _send():
        req = urllib.request.Request(
            f"{server.rstrip('/')}/{topic}", data=message.encode(), method="POST",
            headers={"Title": title, "Priority": priority, "Tags": tags},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def chime() -> None:
    """The macOS done sound, or the terminal bell where there's no afplay."""
    try:
        if subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"],
                          capture_output=True, timeout=10).returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    print("\a", end="", flush=True)
