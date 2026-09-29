"""Which program is running, so shared code can find its files: a name and a
config folder, set once at startup by the host program (its config module)."""
import os
from pathlib import Path


def _default_dir(name: str) -> Path:
    """The platform's usual config folder for `name`."""
    if os.name == "nt":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / name.capitalize()
    xdg = os.getenv("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / name


name = "backbone"
config_dir = _default_dir(name)


def configure(app_name: str, app_config_dir=None) -> None:
    """Name the running program and its config folder (the platform default for
    that name when not given). The diagnostics log and the saved hints switch
    live there."""
    global name, config_dir
    name = app_name
    config_dir = Path(app_config_dir) if app_config_dir else _default_dir(app_name)
