"""nav.py - tabs and the quit-to-terminal signal, shared by every back* tool
that uses backbone's prompt widgets.
"""

class QuitToTerminal(BaseException):
    """Raised to unwind the entire menu stack and exit straight to the
    terminal. Derives from BaseException (not Exception) so it bypasses
    ``except Exception`` handlers in editors/widgets and propagates cleanly
    up to the app's main(), where the alt-screen is restored in a finally.

    `q` quits an app from anywhere by raising this on the spot - there is
    deliberately no "leave this widget and quit later" flag: `q` is never a
    way out of a widget; Esc (or <-/b where a widget has no other use for
    them) is what backs out.
    """


# ---------------------------------------------------------------------------
# Tabs. Each tab's screen loop runs in its own thread, but only one at a time:
# switching hands the turn to the other tab's thread and parks this one where it
# is, so every tab comes back exactly as it was left, however deep its menus go.
import queue as _queue
import sys as _sys
import threading as _threading
from contextlib import contextmanager as _contextmanager

TABS: list = []                 # (name, run) pairs while an app runs as tabs
_active = [0]                   # the tab on screen
_turns: dict = {}               # tab index → Event set while it's that tab's turn
_ended: _queue.Queue = _queue.Queue()
_mine = _threading.local()      # .tab: the tab this thread runs
_modal = [0]                    # screens inside nav.modal(): no switching
_base_tty = [None]              # the terminal as run_tabs found it: each tab starts so
_fresh: set = set()             # tabs to start again from the top when next shown


class _StartAfresh(BaseException):
    """Unwinds a parked tab back to its start (go_to_tab's `fresh`)."""


def _tty():
    """The terminal's attributes, or None (Windows, or not a terminal)."""
    try:
        import termios
        return termios.tcgetattr(_sys.stdin.fileno())
    except Exception:
        return None


def _set_tty(attrs) -> None:
    if attrs is not None:
        import termios
        termios.tcsetattr(_sys.stdin.fileno(), termios.TCSADRAIN, attrs)


def run_tabs(tabs: list, home: int = 0) -> None:
    """Run an app as tabs, `tabs` being (name, run) pairs, starting on `home`,
    until a tab raises (QuitToTerminal to quit, or a crash), which is raised here.

    A tab whose run() returns (backed out of its top level) starts again there,
    on screen: back (←, b, Esc) only ever goes back within a tab, each tab its
    own, and never changes tabs."""
    _base_tty[0] = _tty()
    TABS[:] = tabs
    try:
        _show(home)
        while True:
            try:   # a timeout, so signals (a resize) still reach this, the main thread
                i, exc = _ended.get(timeout=0.2)
            except _queue.Empty:
                continue
            _turns.pop(i, None)
            if exc is not None:
                raise exc
            if _active[0] == i:
                _show(i)
    finally:
        # Tabs still parked stay parked: their threads die with the app.
        TABS.clear()
        _turns.clear(); _fresh.clear()
        _active[0] = 0
        _set_tty(_base_tty[0])  # parked tabs never get to restore it themselves


def _show(i: int) -> None:
    """Make tab `i` the one on screen and give it the turn, starting its thread
    on its first visit."""
    _active[0] = i
    if i not in _turns:
        _turns[i] = _threading.Event()
        _threading.Thread(target=_tab_thread, args=(i,), daemon=True,
                          name=f"tab-{TABS[i][0]}").start()
    _turns[i].set()


def _tab_thread(i: int) -> None:
    _mine.tab = i
    _turns[i].wait()
    _set_tty(_base_tty[0])
    _fresh.discard(i)
    exc = None
    while True:
        try:
            TABS[i][1]()
        except _StartAfresh:
            continue
        except BaseException as e:      # handed to run_tabs, which raises it
            exc = e
        break
    _ended.put((i, exc))


@_contextmanager
def modal():
    """For a screen that mustn't be left for another tab (one playing its own
    audio): inside it, tab keys and clicks do nothing and the bar is dimmed."""
    _modal[0] += 1
    try:
        yield
    finally:
        _modal[0] -= 1


def is_modal() -> bool:
    return _modal[0] > 0


def active_tab() -> int:
    """The tab on screen."""
    return _active[0]


def current_tab() -> int | None:
    """The tab this thread runs, or None outside tabs."""
    return getattr(_mine, 'tab', None) if TABS else None


def switch_to(j: int) -> None:
    """From a tab's thread, show tab `j`; returns once this tab is shown again,
    with the terminal as it was."""
    i = current_tab()
    if i is None or j == i:
        return
    attrs = _tty()
    _turns[i].clear()
    _show(j)
    _turns[i].wait()
    _set_tty(attrs)
    if i in _fresh:
        _fresh.discard(i)
        raise _StartAfresh()


def go_to_tab(name: str, fresh: bool = False) -> bool:
    """Show the tab called `name` from another tab, returning (True) once this
    one is shown again. False, doing nothing, outside tabs or already on it.
    `fresh`: that tab starts again from the top rather than where it was left
    (for a tab told where to open)."""
    names = [n for n, _run in TABS]
    if current_tab() is None or name not in names or names.index(name) == current_tab():
        return False
    if fresh:
        _fresh.add(names.index(name))
    switch_to(names.index(name))
    return True


def tab_for(key: str, use_keys: bool = True) -> int | None:
    """The tab a key or click asks for: a click on the tab bar, a tab's own
    key (tabs.tab_1…: F1, F2…, keys nothing types, so they work on any
    screen), and with `use_keys` the next / previous tab (Tab / Shift-Tab). None when it isn't one, or is the tab already showing."""
    if current_tab() is None or is_modal() or not isinstance(key, str):
        return None
    n, i, j = len(TABS), _active[0], None
    if key.startswith('MOUSE_CLICK:'):
        parts = key.split(':')
        from backbone import ui
        if len(parts) > 3 and parts[2].isdigit() and int(parts[2]) <= ui.tab_rows():
            j = ui.tab_at(int(parts[3]))
    else:
        from backbone import keys                # the tabs group: backbone.prompt.chrome
        act = keys.action(key, 'tabs') or ''
        if use_keys and act in ('tabs.next', 'tabs.prev'):
            j = (i + (1 if act == 'tabs.next' else -1)) % n
        elif act.startswith('tabs.tab_') and int(act[9:]) <= n:
            j = int(act[9:]) - 1
    return j if j != i else None
