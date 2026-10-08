"""Terminal helpers: ANSI colours, sizing and resize tracking, the display-width
scanner, formatting, the status bar, the now-playing box registry, the alt
screen and the progress bar."""
from __future__ import annotations
import os
import sys
import shutil
import signal
import time as _time
from typing import Any
import re
import unicodedata
from itertools import groupby
from pathlib import Path

from backbone import nav
from backbone.nav import NAV_STACK
from backbone.log import quietly

# Status-bar messages: an ordinary one, and a warning or error worth reading.
STATUS_S = 3.0
STATUS_WARNING_S = 5.0

_resize_flag = False
_footer_layout_dirty = False

# Memoised terminal size. `get_terminal_size` was an ioctl per call and the render
# path calls it per *line* (clipping) as well as per frame; the size only changes
# on SIGWINCH, which clears this. Without SIGWINCH (Windows) it re-reads on a
# short TTL instead.
_size_cache: tuple[int, int] | None = None
_size_cache_at: float = 0.0
_SIZE_TTL = 0.25

_last_resize_signal = 0.0               # monotonic time of the last SIGWINCH
_cell_aspect_cache: float | None = None # see cell_aspect(); a resize clears it


def _sigwinch_handler(signum: int, frame: Any) -> None:
    """Mark that the terminal was resized; consume_resize() picks this up."""
    global _resize_flag, _size_cache, _cell_aspect_cache, _last_resize_signal
    _resize_flag = True
    _size_cache = None                   # the memoised size is now wrong
    _cell_aspect_cache = None            # and so may the cell size be (a font change)
    _last_resize_signal = _time.monotonic()


def request_relayout() -> None:
    """Have the next consume_resize() say yes: a screen that missed a resize
    (something over it took the signal) lays itself out again."""
    global _resize_flag
    _resize_flag = True


def last_resize_signal_at() -> float:
    """Monotonic time of the last resize report, to tell whether one arrived
    while something slow was under way."""
    return _last_resize_signal


def ms_since_resize_signal() -> float:
    """How long ago the terminal last reported a resize, in ms, for the
    diagnostics log, to show how far behind the resize a redraw landed."""
    return (_time.monotonic() - _last_resize_signal) * 1000

# SIGWINCH doesn't exist on Windows; guard so importing ui never raises there.
_HAS_SIGWINCH = hasattr(signal, "SIGWINCH")
if _HAS_SIGWINCH:
    signal.signal(signal.SIGWINCH, _sigwinch_handler)


def mark_footer_layout_dirty() -> None:
    """Signal that the now-playing box's height changed (it appeared or vanished,
    e.g. the player view opened in another window), so menus re-render and
    re-reserve rows for it via consume_resize()."""
    global _footer_layout_dirty
    _footer_layout_dirty = True


def consume_resize() -> bool:
    """True (and clears the flags) if the terminal was resized *or* the now-playing
    box changed height since last call: both need a full re-render/re-layout."""
    global _resize_flag, _footer_layout_dirty
    if _resize_flag or _footer_layout_dirty:
        _resize_flag = False
        _footer_layout_dirty = False
        return True
    return False

# Global content margins.  All widgets and the playback UI read from here:
# change these two values to tune the whole app at once.
MARGIN_H = 2   # columns reserved on each horizontal side (left and right)
MARGIN_V = 1   # rows reserved on each vertical side (top and bottom)

# Now-playing box transport geometry, shared so both places that depend on it
# can't drift apart: `now_playing_box.format_now_playing_bar` draws the glyphs here
# and `prompt.core.footer_click_action` maps a click back to the one under
# the pointer. (start column, width) of ⏸/⏵ and ⏭ in the box's content columns.
#
# No previous-track button: the box is an ambient reminder of what is playing,
# and stepping backwards from it is a rarer thing to want than the space a third
# control costs. ^B still works, and is still advertised in the hint bar.
FOOTER_GLYPH_COLS = ((0, 2), (4, 2))

class Colors:
    """The named palette every widget uses, plus semantic colours for a tool's
    own views (FRAME, TEAL, AMBER, RED, TXT, MUTE) and the short aliases R and B.
    All empty when colour is off (NO_COLOR, or set_colour(False))."""
    PRIMARY = "\033[1;37m" # Bold white
    WHITE = "\033[37m" # Normal white
    ACCENT = "\033[1;32m"   # green; a host's setting changes it through set_accent
    ACCENT2 = "\033[1;36m"  # the second accent (borders, the active tab): set_accent(…, secondary=True)
    CYAN = "\033[1;36m"
    YELLOW = "\033[1;33m"
    MAGENTA = "\033[1;35m"
    GREEN = "\033[1;32m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    RESET = "\033[0m"
    BACK = "\x1b[47m"
    INVERT = "\033[7m"
    # Highlight bars, a soft grey behind a row: the highlighted row of the list
    # you're in, and (fainter) the row a column of the level above opened.
    BAR = "\033[48;5;237m"
    BAR_DIM = "\033[48;5;235m"
    HIDE = "\033[?25l"
    SHOW = "\033[?25h"
    # semantic colours for a tool's own views (backcrack's watch uses RED for failures)
    FRAME = "\033[38;5;239m"
    TEAL = "\033[38;5;43m"
    AMBER = "\033[38;5;179m"
    RED = "\033[38;5;167m"
    TXT = "\033[38;5;252m"
    MUTE = "\033[38;5;243m"
    R = RESET            # short aliases
    B = BOLD


# The styling half of Colors: everything that paints rather than moves the
# cursor. Suppressing colour must not suppress HIDE/SHOW, which are cursor
# control and still needed on a pipe.
_STYLE_NAMES = ('PRIMARY', 'WHITE', 'ACCENT', 'ACCENT2', 'CYAN', 'YELLOW', 'MAGENTA', 'GREEN',
                'DIM', 'BOLD', 'ITALIC', 'UNDERLINE', 'RESET', 'BACK', 'INVERT', 'BAR', 'BAR_DIM',
                'FRAME', 'TEAL', 'AMBER', 'RED', 'TXT', 'MUTE', 'R', 'B')
_STYLE_CODES = {name: getattr(Colors, name) for name in _STYLE_NAMES}


def on_bar(text: str, width: int, bar: str | None = None) -> str:
    """`text` on a highlight bar `width` columns wide (Colors.BAR unless
    `bar`), the bar kept up through any resets inside it."""
    bar = Colors.BAR if bar is None else bar
    if not bar:
        return text + " " * max(0, width - visual_len(text))
    body = text.replace(Colors.RESET, Colors.RESET + bar) if Colors.RESET else text
    return f"{bar}{body}{' ' * max(0, width - visual_len(text))}{Colors.RESET}"


def colour_enabled() -> bool:
    """Whether colour should be emitted: a terminal, and NO_COLOR unset.

    An empty NO_COLOR still counts as set: that is what the convention says,
    and `NO_COLOR=` in an environment is a deliberate act.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def set_colour(enabled: bool) -> None:
    """Turn every style code in `Colors` on or off, for the whole process.

    One switch rather than a check at each of the several hundred places a style
    is interpolated: the table renderer, the hint engine, the status bar and
    every screen already read their codes from here, so a pipe gets plain text
    without any of them knowing about it.
    """
    global _colour_on
    _colour_on = enabled
    for name, code in _STYLE_CODES.items():
        setattr(Colors, name, code if enabled else "")


_colour_on = True

# NO_COLOR turns colour off from the start; a host can also call set_colour.
USE_COLOR = os.environ.get("NO_COLOR") is None
if not USE_COLOR:
    set_colour(False)

# The accent: terminal-palette colours first (they follow the terminal's own
# theme), then fixed ones. Stored in `accent_colour` as a key or as "#RRGGBB".
ACCENT_PRESETS = [
    ('green', 'Green', 32), ('red', 'Red', 31), ('yellow', 'Yellow', 33),
    ('blue', 'Blue', 34), ('magenta', 'Magenta', 35), ('cyan', 'Cyan', 36),
    ('amber', 'Amber', '#FFB000'), ('coral', 'Coral', '#FF7F66'), ('rose', 'Rose', '#F06292'),
    ('lavender', 'Lavender', '#B39DDB'), ('sky', 'Sky', '#4FC3F7'), ('mint', 'Mint', '#6FDFA8'),
]
DEFAULT_ACCENT = 'green'
DEFAULT_ACCENT2 = 'cyan'


def parse_hex_colour(text: str) -> tuple[int, int, int] | None:
    """(r, g, b) from "#RRGGBB", "RRGGBB" or "#RGB", or None."""
    h = (text or '').strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def _rgb_code(r: int, g: int, b: int) -> str:
    """Bold foreground in exactly this colour on a 24-bit terminal, else the
    nearest of the 256-colour palette's 6×6×6 cube or grey ramp."""
    if os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit'):
        return f"\033[1;38;2;{r};{g};{b}m"
    if max(r, g, b) - min(r, g, b) < 12:                       # a grey
        n = 232 + min(23, max(0, round((r - 8) / 10)))
    else:
        n = 16 + sum(round(v / 255 * 5) * m for v, m in ((r, 36), (g, 6), (b, 1)))
    return f"\033[1;38;5;{n}m"


def accent_code(value) -> str | None:
    """The escape code for an `accent_colour` value (a preset key or a hex
    colour), or None when it is neither."""
    for key, _name, colour in ACCENT_PRESETS:
        if value == key:
            return f"\033[1;{colour}m" if isinstance(colour, int) else _rgb_code(*parse_hex_colour(colour))
    rgb = parse_hex_colour(value) if isinstance(value, str) and value.startswith('#') else None
    return _rgb_code(*rgb) if rgb else None


def accent_label(value) -> str:
    """What Settings calls an `accent_colour` value."""
    return next((name for key, name, _ in ACCENT_PRESETS if key == value),
                str(value).upper() if accent_code(value) else 'Green')


def set_accent(value, secondary: bool = False) -> None:
    """Use `value` (a preset key or "#RRGGBB") as the accent from now on, or as
    the second accent; an unknown value falls back to that one's default."""
    slot = 'ACCENT2' if secondary else 'ACCENT'
    code = accent_code(value) or accent_code(DEFAULT_ACCENT2 if secondary else DEFAULT_ACCENT)
    _STYLE_CODES[slot] = code
    if _colour_on:
        setattr(Colors, slot, code)


_screen_invalidator = None


def set_screen_invalidator(fn) -> None:
    """Register the painter's "forget what's on screen" hook.

    Registered by `prompt.core` (which can't be imported here, as it imports this
    module), so every existing `clear_screen()` keeps meaning "the screen is now
    blank" for the diffed painter as well.
    """
    global _screen_invalidator
    _screen_invalidator = fn


def _screen_cleared() -> None:
    """Tell the painter the screen was wiped outside its own frame writes."""
    if _screen_invalidator is not None:
        with quietly():
            _screen_invalidator()


def enter_alt_screen() -> None:
    """Switch to the terminal alternate screen buffer (no scrollback).

    Also enables focus in/out reporting (\\033[?1004h) so editors can show a
    hollow cursor when the window loses focus; unsupported terminals ignore it.
    And turns auto-wrap off (\\033[?7l): every row is placed explicitly, so a
    row too wide for the window should be cut at the edge, not run onto the
    next, which is what a write still sized for the old width does in the
    moment between a resize and the redraw that answers it.
    """
    sys.stdout.write("\033[?1049h\033[?1004h\033[?7l\033[H\033[3J\033[J" + Colors.HIDE)
    sys.stdout.flush()
    _screen_cleared()
    _in_app[0] = True


def exit_alt_screen() -> None:
    """Restore the main screen buffer, auto-wrap and focus reporting, and show
    the cursor."""
    sys.stdout.write("\033[?25h\033[?7h\033[?1004l\033[?1049l")
    sys.stdout.flush()
    _in_app[0] = False


_in_app = [False]   # on the alternate screen, where everything is laid out in boxes


def _on_app_screen() -> bool:
    """Writing to the app's screen: in the app, and not to output being
    captured (a command run from its `:` line)."""
    return _in_app[0] and sys.stdout is sys.__stdout__


def clear_screen() -> None:
    """Overwrite screen content from home without triggering scrollback save.

    Leaves the cursor **hidden**: a bare clear parks it at home, where it blinks
    in the top-left corner until the next frame happens to hide it; during a
    library build or any slow step, that's a visible flashing caret.
    """
    sys.stdout.write("\033[H\033[3J\033[J" + Colors.HIDE)
    sys.stdout.flush()
    _screen_cleared()

BACKGROUND_TASKS: dict[str, str] = {}

_toast_message: str = ""
_toast_expiry: float = 0.0

# Now-playing box: the playback layer registers a provider so the widget
# layer can draw a background-audio box without importing playback (keeps the
# dependency flowing one way). provider(width) -> list[str] | None (styled rows,
# top to bottom; drawn just above the breadcrumb status line).
_footer_provider = None
_footer_lines_cache: list[str] = []
# A cheap identity of the currently-shown track (file/generation/paused/index …).
# The idle-tick redraw keys off this so a background track change always repaints
# the box, even in the rare case two tracks render to byte-identical rows.
_footer_sig: tuple | None = None


def set_footer_provider(fn) -> None:
    """Register a ``callable(width) -> list[str] | None`` that renders the now-playing box."""
    global _footer_provider
    _footer_provider = fn


# The now-playing box's clock, as (first, last) column on its content row, when
# a click on it does something (set by the box provider; None otherwise).
_footer_time_cols: tuple | None = None


def set_footer_time_cols(cols: tuple | None) -> None:
    global _footer_time_cols
    _footer_time_cols = cols


def footer_time_cols() -> tuple | None:
    return _footer_time_cols


def set_footer_signature(sig: tuple | None) -> None:
    """Record the identity of the track the box provider just rendered."""
    global _footer_sig
    _footer_sig = sig


# Event-driven repaint: background threads (a joined window's snapshot
# receiver, the host's auto-advance tick) call pulse_footer() when the
# now-playing state changes so the menu poll repaints the box *immediately*
# instead of only on the next keystroke. The waker is registered by the input
# layer (a self-pipe that wakes its select); this hook keeps the playback/IPC
# threads free of any input-layer import.
_footer_waker = None


def set_footer_waker(fn) -> None:
    """Register a ``callable()`` that nudges the menu poll to repaint the box."""
    global _footer_waker
    _footer_waker = fn


def pulse_footer() -> None:
    """Ask the active menu poll to repaint the now-playing box now (no-op if no
    poll is listening, e.g. the full player view drives its own redraws)."""
    if _footer_waker is not None:
        with quietly():
            _footer_waker()


def footer_signature() -> tuple | None:
    """The identity of the currently-shown track (see :func:`set_footer_signature`)."""
    return _footer_sig


def footer_lines(width: int) -> list[str]:
    """The now-playing box rows for ``width`` (empty list when nothing is playing)."""
    global _footer_lines_cache
    if _footer_provider is None:
        _footer_lines_cache = []
        return []
    try:
        lines = _footer_provider(width) or []
    except Exception:
        # A provider that raised tells us nothing about what's playing; keep the
        # box exactly as it was rather than blinking it out and back next tick.
        return _footer_lines_cache
    _footer_lines_cache = list(lines)
    return _footer_lines_cache


def footer_active() -> bool:
    """Whether a now-playing box is currently shown (cached from the last draw)."""
    return bool(_footer_lines_cache)


# Audio is playing but the box could not be drawn: the terminal is too narrow
# for it. The box normally advertises the transport keys in its own top border,
# so this is the one state where the hint bar has to advertise them instead
# (see `prompt.chrome_hint_pairs`). Deliberately *not* set when the full player
# view owns the display: that view shows its own transport.
_footer_unboxed: bool = False


def set_footer_unboxed(value: bool) -> None:
    """Record whether transport is live with no now-playing box to advertise it."""
    global _footer_unboxed
    _footer_unboxed = bool(value)


def footer_unboxed() -> bool:
    """Whether transport is live but no box is drawn to show its keys."""
    return _footer_unboxed


def footer_height() -> int:
    """How many rows the now-playing box currently occupies (0 when inactive)."""
    return len(_footer_lines_cache)


def set_status(task_id: str, message: str | None) -> None:
    """Update or remove a background task status."""
    if message is None:
        BACKGROUND_TASKS.pop(task_id, None)
    else:
        BACKGROUND_TASKS[task_id] = message


def has_background_tasks() -> bool:
    """Whether any background activity is currently running (drives the live,
    pulsing status indicator so the notice stays up until the work is done)."""
    return bool(BACKGROUND_TASKS)


# 256-colour greyscale brightness ramp (dim → white → dim) for the pulsing beacon.
_PULSE_RAMP = (238, 243, 248, 253, 255, 253, 248, 243)


def pulse_circle() -> str:
    """A white ● whose brightness pulses over time: the beacon next to a running
    background activity. The status bar is re-rendered ~8 Hz while a task is
    active (see the menu idle tick), which animates this."""
    code = _PULSE_RAMP[int(_time.time() * 6) % len(_PULSE_RAMP)]
    return f"\033[38;5;{code}m●{Colors.RESET}"


def print_inline_progress(message: str, progress: float) -> None:
    """Redraw a single in-place line for a blocking, no-other-redraw loop
    (a per-track ffmpeg pass): pulsing beacon + bar so a slow scan still
    looks alive instead of a hung terminal, inset by MARGIN_H and centred
    like the rest of the chrome. Call `clear_inline_progress()` once the
    loop finishes. In the app (the alternate screen) it's a box over the
    middle of the screen instead, not a line wherever the cursor is."""
    if _on_app_screen():
        from backbone.prompt.core import progress_float
        progress_float(message, progress)
        return
    bar = get_progress_bar(progress, 24)
    width = get_terminal_width()
    avail = max(1, width - 2 * MARGIN_H)
    prefix_len = visual_len(f"{pulse_circle()} {bar} ")
    if prefix_len + len(message) > avail:
        message = message[:max(0, avail - prefix_len - 1)] + "…"
    content = f"{pulse_circle()} {bar} {Colors.DIM}{message}{Colors.RESET}"
    pad = max(MARGIN_H, (width - visual_len(content)) // 2)
    sys.stdout.write(f"\r{' ' * pad}{content}\033[K")
    sys.stdout.flush()


def clear_inline_progress() -> None:
    """Erase the line (or the box) left by `print_inline_progress()`."""
    if _on_app_screen():
        from backbone.prompt.core import screen_float_close
        screen_float_close()
        return
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def show_status(message: str, duration: float = STATUS_S) -> None:
    """Flash a one-shot message in the status bar for `duration` seconds."""
    global _toast_message, _toast_expiry
    _toast_message = message
    _toast_expiry = _time.time() + duration


def show_error(message: str) -> None:
    """Flash "Error: message" in the status bar, held longer, and log it."""
    from backbone.log import log
    log.warning("%s", message)
    show_status(f"Error: {message}", STATUS_WARNING_S)


def show_loading(message: str) -> None:
    """Clear the screen and display a greyed loading message during long operations."""
    clear_screen()
    sys.stdout.write(f"\n  {Colors.DIM}{message}{Colors.RESET}\n")
    sys.stdout.flush()


# A full-screen view that is its own context (a player) hides the breadcrumb
# while it is open; toasts and background tasks still show.
_breadcrumb_hidden = [0]


def hide_breadcrumb(hidden: bool) -> None:
    """Leave the menu breadcrumb off the status bar (True), or put it back
    (False). Calls nest: each True needs its False."""
    _breadcrumb_hidden[0] = max(0, _breadcrumb_hidden[0] + (1 if hidden else -1))


def get_status_line() -> str:
    """Return the current status bar content (breadcrumb + tasks + toast)."""
    global _toast_message
    cols = get_terminal_width()

    if cols <= 0:
        return ""

    if _toast_message and _time.time() > _toast_expiry:
        _toast_message = ""

    sep = f"  {Colors.DIM}·{Colors.RESET}  "

    right_parts: list[str] = []
    for msg in BACKGROUND_TASKS.values():
        right_parts.append(f"{pulse_circle()} {Colors.DIM}{msg}{Colors.RESET}")
    if _toast_message:
        right_parts.append(f"{Colors.DIM}{_toast_message}{Colors.RESET}")
    right = sep.join(right_parts)

    crumb = _get_breadcrumb_str(cols // 2) if NAV_STACK and not _breadcrumb_hidden[0] else ""
    left = f"  {Colors.DIM}{crumb}{Colors.RESET}" if crumb else ""

    if left and right:
        gap = max(2, cols - visual_len(left) - visual_len(right) - 2)
        status = left + " " * gap + right + "  "
    elif left:
        status = left
    elif right:                             # toasts and tasks keep to the right, crumb or none
        status = " " * max(2, cols - visual_len(right) - 2) + right + "  "
    else:
        status = ""

    if visual_len(status) > cols:
        status = clip_ansi(status, cols)
    return status

_tab_spans: list = []   # (first col, last col, tab) of each label in the bar last drawn


def tab_bar_lines() -> list[str]:
    """The top rows while an app runs as tabs ([] otherwise): the tabs' names
    along one row, the one showing outlined like a folder's tab over it, its
    outline running down into the screen's top border (tab_notch: the painter
    cuts the opening there). Their keys (F1, F2…) show with the hints (the
    `?` toggle), in the accent, as hint keys are; in a window too narrow for
    every name only the showing tab keeps its name, the others their keys."""
    _tab_spans.clear()
    _notch[0] = None
    names = [name for name, _run in nav.TABS]
    if not names:
        return []
    on = nav.active_tab()
    total = get_terminal_width() - 2 * MARGIN_H
    keys_on = _tab_keys_shown()
    modal = nav.is_modal()

    def labels(full: bool) -> list:
        """(key, name) per tab: the key shown with the hints, or in place of
        the name when the tabs don't all fit."""
        return [(f"F{i + 1}" if keys_on or not (full or i == on) else "",
                 n if full or i == on else "") for i, n in enumerate(names)]

    def width(ls) -> int:
        return sum(len(k) + len(n) + (1 if k and n else 0) for k, n in ls) + _TAB_GAP * (len(ls) - 1) + 2

    # The other tabs give their names way first, then (very narrow) the showing one is cut.
    for full in (True, False):
        ls = labels(full)
        if width(ls) <= total:
            break
    else:
        ls = [(k, clip_ansi(n, max(1, total - 6))) if i == on else ("", "") for i, (k, n) in enumerate(labels(False))]

    text = " " * (MARGIN_H + 2)                         # the first name two in from the screen's left border
    col = MARGIN_H + 2                                  # 0-based column the next name starts at
    for i, (k, n) in enumerate(ls):
        if not (k or n):
            continue
        if col > MARGIN_H + 2:
            text += " " * _TAB_GAP
            col += _TAB_GAP
        plain = f"{k} {n}" if k and n else k or n
        key = f"{Colors.ACCENT if keys_on else Colors.DIM}{k}{Colors.RESET}" if k else ""
        if i == on and not modal:
            name = f"{Colors.BOLD}{n}{Colors.RESET}"
        else:
            name = f"{Colors.DIM}{n}{Colors.RESET}"
        text += (key + " " + name) if k and n else (key or name)
        _tab_spans.append((col, col + 1 + len(plain), i))      # 1-based, with a space either side
        if i == on:
            _notch[0] = (col - 2, col + len(plain) + 1)         # 0-based: its outline's two sides
        col += len(plain)
    mid = text
    top = ""
    if _notch[0]:
        a, b = _notch[0]
        top = " " * a + f"{Colors.DIM}╭{'─' * (b - a - 1)}╮{Colors.RESET}"
        mid += " " * max(0, b + 1 - visual_len(mid))          # out to its right side (the last tab's too)
        mid = _overlay(mid, a, f"{Colors.DIM}│{Colors.RESET}")
        mid = _overlay(mid, b, f"{Colors.DIM}│{Colors.RESET}")
    return [top, mid]


def _overlay(line: str, col: int, glyph: str) -> str:
    """`line` with the one cell at 0-based column `col` (a space) swapped for `glyph`."""
    out, at, i = [], 0, 0
    while i < len(line):
        m = _ANSI_AT.match(line, i)
        if m:
            out.append(m.group(0)); i = m.end(); continue
        out.append(glyph if at == col else line[i])
        at += char_cols(line[i]); i += 1
    return "".join(out)


_ANSI_AT = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_TAB_GAP = 4                       # between two tabs' names: room for the outline's sides
_notch: list = [None]              # the showing tab's outline, (left, right) 0-based columns


def tab_notch() -> tuple | None:
    """Where the showing tab's outline meets the screen under the bar, laid
    out for the window as it is now (a layout asking before the bar's next
    paint, just after a resize, mustn't get where it was)."""
    if not (nav.TABS and tab_rows()):
        return None
    tab_bar_lines()
    return _notch[0]


_tab_keys_shown = lambda: False      # noqa: E731 (the hints' switch, set by prompt: set_tab_keys_shown)


def set_tab_keys_shown(fn) -> None:
    """Whether the tabs show their keys: the hints' switch (prompt.core)."""
    global _tab_keys_shown
    _tab_keys_shown = fn


_tabs_hidden = [False]


def set_tabs_hidden(hidden: bool) -> bool:
    """Leave the tab bar off the screen (a full-screen view that offers it,
    like a player); its keys still switch tabs. Returns what it was."""
    was, _tabs_hidden[0] = _tabs_hidden[0], hidden
    return was


_focused = [True]                  # the terminal's window has focus (its focus reports, when it sends them)


def set_window_focused(focused: bool) -> None:
    _focused[0] = focused


def window_focused() -> bool:
    """Whether the window has focus, as the terminal last reported (True if
    it never says). A terminal may read a window's output slowly while it's
    in the background: heavy writes (a full-size image) can wait for focus."""
    return _focused[0]


_CHROME_MIN_ROWS = 6               # a window shorter than this has no room for the tab bar or status line


def chrome_fits() -> bool:
    """Whether the window has room for the tab bar and the status line: a
    shorter one gives every row to the screen itself."""
    return get_terminal_height() >= _CHROME_MIN_ROWS


def tab_rows() -> int:
    """Rows the tab bar takes at the top: 2 (the showing tab's outline, then
    the names) while an app runs as tabs, unless it's hidden or the window is
    too short for it. The row under them is the screen's top border, the
    tab's outline running into it."""
    return 2 if nav.TABS and not _tabs_hidden[0] and chrome_fits() else 0


def top_margin() -> int:
    """Rows above every screen's content: the margin, or the tab bar."""
    return max(MARGIN_V, tab_rows())


def tab_at(col: int) -> int | None:
    """The tab whose label is at column `col` of the bar."""
    return next((i for first, last, i in _tab_spans if first <= col <= last), None)


def _tty_size() -> os.terminal_size:
    """Ask the terminal itself. shutil.get_terminal_size() prefers exported
    COLUMNS/LINES, which some shells and terminals export once at startup,
    and that froze the size for good. Those only count when no stream is a tty."""
    for stream in (sys.__stdout__, sys.__stdin__, sys.__stderr__):
        try:
            return os.get_terminal_size(stream.fileno())
        except (AttributeError, ValueError, OSError):
            continue
    return shutil.get_terminal_size()


def get_terminal_size(default: tuple = (80, 24)) -> tuple:
    """Terminal (columns, rows), falling back to `default` if the query fails.

    Memoised; see `_size_cache`; a resize (SIGWINCH) clears it.
    """
    global _size_cache, _size_cache_at
    if _size_cache is not None:
        if _HAS_SIGWINCH or (_time.monotonic() - _size_cache_at) < _SIZE_TTL:
            return _size_cache
    try:
        size = _tty_size()
    except OSError:
        return default
    _size_cache = (size.columns, size.lines)
    _size_cache_at = _time.monotonic()
    return _size_cache



def cell_aspect(default: float = 2.0) -> float:
    """How many times taller than wide one character cell is on screen, from
    the pixel size the terminal reports. Anything drawn in cells (half-block
    art, an inline image's box) needs it to keep an image's proportions;
    `default` when the terminal doesn't report pixels. Re-read after a resize,
    which is also what a font change sends."""
    global _cell_aspect_cache
    if _cell_aspect_cache is None:
        _cell_aspect_cache = default
        try:
            import fcntl, struct, termios
            for stream in (sys.__stdout__, sys.__stdin__, sys.__stderr__):
                try:
                    r, c, xpx, ypx = struct.unpack(
                        "HHHH", fcntl.ioctl(stream.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
                except (AttributeError, ValueError, OSError):
                    continue
                if r and c and xpx and ypx:
                    _cell_aspect_cache = (ypx / r) / (xpx / c)
                break
        except ImportError:                          # Windows: no ioctl
            pass
    return _cell_aspect_cache


def get_terminal_width(default: int = 80) -> int:
    """Terminal width in columns."""
    cols, _ = get_terminal_size((default, default))
    return cols


def get_terminal_height(default: int = 24) -> int:
    """Terminal height in rows."""
    _, rows = get_terminal_size((default, default))
    return rows


# ---------------------------------------------------------------------------
# Display width: one ANSI scanner, one column table, for the whole app.
#
# Every module that measures, clips or pads a styled line goes through this
# block.
# ---------------------------------------------------------------------------

# The escape sequences a terminal consumes without drawing anything. In order:
# CSI (including private `?` parameters and intermediate bytes, so `\033[?25l`
# and `\033[3J` are recognised, not just SGR); the Kitty graphics protocol used
# by the album-art renderer; OSC, terminated by either ST or BEL; the remaining
# string-introducers (DCS/SOS/PM/APC); and a bare two-byte escape as a backstop.
_ANSI_RE = re.compile(
    r'(\x1b\[[0-9;?]*[ -/]*[@-~])'
    r'|(\x1b_G[^\x1b]*\x1b\\)'
    r'|(\x1b\][^\x1b\x07]*(?:\x1b\\|\x07))'
    r'|(\x1b[PX^_].*?\x1b\\)'
    r'|(\x1b.)'
)


def _scan(s: str):
    """Yield ``(is_escape, chunk)`` across `s`.

    One chunk per escape sequence, one per printable character: the single
    walk every width routine below is built on, so measuring and clipping can
    never disagree about where an escape starts or ends.
    """
    i = 0
    n = len(s)
    while i < n:
        if s[i] == '\x1b':
            m = _ANSI_RE.match(s, i)
            if m:
                yield True, m.group(0)
                i = m.end()
                continue
        yield False, s[i]
        i += 1


def strip_ansi(s: str) -> str:
    """`s` with every ANSI escape sequence removed."""
    if '\x1b' not in s:
        return s
    return _ANSI_RE.sub('', s)


# Codepoints that occupy no column of their own: the variation selectors that
# pick a glyph's text/emoji presentation, the zero-width joiner family, and
# combining marks that stack onto the character before them.
_ZERO_WIDTH = ('︎', '️', '​', '‌', '‍')

_ZERO_WIDTH_SET = frozenset(_ZERO_WIDTH)

# Codepoints a terminal draws two cells wide. Two sources: East Asian Wide and
# Fullwidth (handled below via unicodedata), and emoji-presentation characters,
# which UTR#51 says to render wide and which every modern terminal does. The
# media-control glyphs are the app's own case, and their true width is not
# knowable from Unicode alone. U+23EE ⏮ and U+23ED ⏭ are emoji-by-default; U+23F8
# ⏸ and U+23F5 ⏵ are text-by-default and "should" be one cell, but no monospace
# font on a stock macOS box carries any of them, so they are drawn by whichever
# fallback font does, and Apple Color Emoji (which has ⏮ ⏸ ⏭) draws two cells
# wide whatever the default presentation says. All four report east-asian-width
# N, so nothing available here can tell them apart.
#
# They are listed as wide deliberately, as an over-estimate. Reserving two cells
# and getting one leaves a small gap; reserving one and getting two overruns
# whatever sits to the right, and that is the now-playing box's closing border.
AMBIGUOUS_WIDE = frozenset('⏮⏭⏸⏵⏪⏩⏫⏬⏯⏱⏲⏰')

_cols_cache: dict = {}


def char_cols(ch: str) -> int:
    """How many terminal columns `ch` occupies: 0, 1 or 2."""
    w = _cols_cache.get(ch)
    if w is None:
        if ch in _ZERO_WIDTH_SET or unicodedata.combining(ch):
            w = 0
        elif ch in AMBIGUOUS_WIDE or unicodedata.east_asian_width(ch) in ('W', 'F'):
            w = 2
        else:
            w = 1
        _cols_cache[ch] = w
    return w


def display_text(s: str) -> str:
    """`s` with ANSI escapes and zero-width codepoints removed.

    Every remaining character occupies at least one column, but *not* always
    exactly one: a wide glyph still takes two, so `visual_len` is what you
    want for width maths. This is for callers that need the plain characters
    themselves (cursor hit-testing, writing a styled report out as text).
    """
    out = strip_ansi(s)
    if out.isascii():
        return out
    return ''.join(ch for ch in out if char_cols(ch) != 0)


def visual_len(s: str) -> int:
    """Columns `s` occupies on screen, ignoring ANSI escapes.

    Not the same as `len`: escapes and combining marks take no column, and an
    emoji-presentation or East-Asian-wide character takes two. The ASCII fast
    path keeps the common case a plain length, as this is called per line in the
    render path.
    """
    if not s:
        return 0
    if s.isascii() and '\x1b' not in s:
        return len(s)
    return sum(char_cols(ch) for esc, ch in _scan(s) if not esc)


def clip_ansi(text: str, max_cols: int, reset: bool = True) -> str:
    """`text` truncated to `max_cols` visible columns, escapes preserved.

    Escapes ride along without being counted, so the styling that survives the
    cut still closes properly; a zero-width mark rides along with the glyph it
    belongs to; and a two-cell glyph is never split across the boundary: it is
    dropped whole, leaving a one-column gap, because half of one renders as a
    stray cell that pushes everything after it out of line.

    `reset` appends a reset when the string was actually cut, so the clipped
    line can't bleed colour into whatever is drawn after it. Callers that go on
    to concatenate more styled content onto the result pass False.
    """
    if max_cols <= 0:
        return ""
    out: list[str] = []
    visible = 0
    truncated = False
    for esc, chunk in _scan(text):
        if esc:
            out.append(chunk)
            continue
        w = char_cols(chunk)
        if w and visible + w > max_cols:
            truncated = True
            break
        out.append(chunk)
        visible += w
    res = "".join(out)
    if truncated and reset and not res.endswith(Colors.RESET):
        res += Colors.RESET
    return res


# How often something scrolling through a marquee needs redrawing.
MARQUEE_BPM = 100                    # scrolling text moves a column per beat: unhurried
MARQUEE_STEP_S = 60 / MARQUEE_BPM


def set_marquee_bpm(bpm: float) -> None:
    """How fast scrolling text moves: a column per beat at `bpm`."""
    global MARQUEE_BPM, MARQUEE_STEP_S
    MARQUEE_BPM = max(1.0, float(bpm))
    MARQUEE_STEP_S = 60 / MARQUEE_BPM


def marquee(text: str, width: int, t: float, speed: float | None = None, pause: float = 2.0) -> str:
    """Plain `text` fitted to `width` columns at time `t` (seconds): as it is
    when it fits, else scrolling through it `speed` columns a second (a column
    a beat at MARQUEE_BPM unless given), held for
    `pause` seconds at each end before starting over."""
    over = visual_len(text) - width
    if over <= 0:
        return text
    speed = speed or MARQUEE_BPM / 60
    run = over / speed
    p = t % (2 * pause + run)
    skip = 0 if p < pause else over if p >= pause + run else int((p - pause) * speed)
    for i, ch in enumerate(text):                  # drop `skip` columns from the front
        if skip <= 0:
            text = text[i:]
            break
        skip -= char_cols(ch)
    return clip_ansi(text, width, reset=False)


def truncate_text(text: str, max_width: int, placeholder: str = "…", front: bool = False) -> str:
    """Truncate `text` to `max_width` columns, replacing the cut end (or start,
    if `front`) with `placeholder`.

    Measured in columns rather than codepoints, so a CJK or emoji run is cut
    where it actually reaches the edge instead of a character count that
    overruns it by up to 2×.
    """
    if text is None:
        return ""
    if visual_len(text) <= max_width:
        return text
    ph_w = visual_len(placeholder)
    if max_width <= ph_w:
        return clip_ansi(text, max_width, reset=False)

    keep = max_width - ph_w
    if front:
        # Walk from the right, taking whole glyphs until `keep` columns are full.
        taken: list[str] = []
        used = 0
        for ch in reversed(text):
            w = char_cols(ch)
            if w and used + w > keep:
                break
            taken.append(ch)
            used += w
        return placeholder + "".join(reversed(taken))
    return clip_ansi(text, keep, reset=False) + placeholder


def plural(n: int, singular: str, many: str | None = None) -> str:
    """``"1 result"`` / ``"156 results"``: the count and its noun, agreeing."""
    return f"{n} {singular if abs(n) == 1 else (many or singular + 's')}"


def divider(width: int | None = None, char: str = "─") -> str:
    """A horizontal rule of `char` spanning `width` (or the terminal width)."""
    width = width or get_terminal_width()
    return char * width
def format_time(seconds: int | float) -> str:
    """Convert seconds (may be float) to a compact time string.

    Preserves sub-second precision by appending centiseconds when the
    input contains a fractional portion.
    """
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        total = 0.0

    int_sec = int(total)
    frac_cs = int(round((total - int_sec) * 100))  # centiseconds (0-99)

    intervals = [31536000, 2592000, 86400, 3600, 60, 1]
    parts = []
    rem = int_sec
    for unit in intervals:
        parts.append(rem // unit)
        rem %= unit

    start = max(0, next((i for i, p in enumerate(parts[:-2]) if p > 0), len(parts) - 2))
    start = min(start, len(parts) - 2)

    result = [str(parts[start])]
    for p in parts[start + 1:]:
        result.append(str(p).zfill(2))

    base = ":".join(result)
    if frac_cs:
        return f"{base}.{frac_cs:02d}"
    return base


def _get_breadcrumb_str(width: int) -> str:
    """Render NAV_STACK as a '>'-joined breadcrumb that fits `width`.

    Over-long trails shed whole path components from the front, keeping the
    deepest ones: those say where you are; the ones above are context you can
    infer. Slicing the joined string by character instead turned "Bleak
    Expectations > A Childhood Cruelly Kippered" into "…pectations > A Childhood
    Cruelly Kippered", where the leading fragment is a word that was never in
    the path and reads as one.

    The last component is kept whatever it costs: a breadcrumb that has dropped
    everything still has to name where you are. If it alone doesn't fit, it is
    truncated at its own end so it starts with something real.
    """
    if width <= 1 or not NAV_STACK:
        return ""

    sep = " > "
    max_length = max(0, width - 1)

    if len(sep.join(NAV_STACK)) <= max_length:
        return sep.join(NAV_STACK)

    # Keep the deepest components that fit, prefixed with "… > " to show the
    # trail was cut. Walk outward from the last one.
    kept: list[str] = [NAV_STACK[-1]]
    for name in reversed(NAV_STACK[:-1]):
        if len("… > " + sep.join([name] + kept)) > max_length:
            break
        kept.insert(0, name)

    out = "… > " + sep.join(kept)
    if len(out) <= max_length:
        return out
    # Not even the deepest component fits beside the marker: truncate it from
    # its end, so what remains is the start of a real name rather than the tail
    # of one. truncate_text handles a width too small for the ellipsis itself.
    return truncate_text(NAV_STACK[-1], max_length)



def get_progress_bar(progress: float, width: int = 40, span: tuple | None = None) -> str:
    """
    A pip-style progress bar.
    [━━━━━━━━━━━━━━━━━━━━━━━━╸          ]
    `span`: a section to pick out (see progress_cells).
    """
    return f"{Colors.DIM}[{Colors.RESET}{progress_cells(progress, width, span)}{Colors.DIM}]{Colors.RESET}"


def progress_cells(progress: float, width: int, span: tuple | None = None, rest: str = " ") -> str:
    """A progress bar's `width` cells, coloured: what's played bright, the rest
    `rest` (dim). `span`: (start, end) fractions of one section (a chapter) to
    pick out: from its start to now in the accent colour, from now to its end dim."""
    progress = max(0, min(1, progress))

    filled_width = progress * width
    whole_blocks = int(filled_width)
    remainder = filled_width - whole_blocks

    bar = "━" * whole_blocks

    # half-cell tip
    if whole_blocks < width:
        if remainder > 0.6:
            bar += "━" # Almost full
        elif remainder > 0.2:
            bar += "╸" # Partial tip
        else:
            bar += " " # Not enough for a tip yet

    cells = [(Colors.PRIMARY, c) for c in bar.rstrip(" ")]
    cells += [(Colors.DIM, rest)] * (width - len(cells))
    if span:
        lo = max(0, min(width - 1, int(span[0] * width)))
        hi = max(lo + 1, min(width, round(span[1] * width)))
        cells[lo:hi] = [(Colors.ACCENT, c) if colour == Colors.PRIMARY else (Colors.DIM, "━")
                        for colour, c in cells[lo:hi]]
    return "".join(f"{colour}{''.join(c for _, c in group)}{Colors.RESET}"
                   for colour, group in groupby(cells, key=lambda cell: cell[0]))


C = Colors   # the short name every widget uses


# --- meters, boxes and live-view pieces (dashboards, progress, sizes) ---

SPIN = list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

PARTS = ["", "▏", "▎", "▍", "▌", "▋", "▊", "▉"]   # eighth-block fill steps

SPARK = "▁▂▃▄▅▆▇█"

def content_width(min_width: int = 1) -> int:
    """Terminal columns available for content, after the global left+right
    margin - the width every box/bar in a frame should be drawn against.
    No floor beyond `min_width`, so content is always sized against the real
    width, however narrow.
    """
    return max(min_width, get_terminal_width() - 2 * MARGIN_H)

def rule(n: int) -> str:
    return "─" * n

def bar(pct: int, width: int, color: str = "") -> str:
    color = color or Colors.TEAL
    pct = max(0, min(100, pct))
    eighths = pct * width * 8 // 100
    full, rem = divmod(eighths, 8)
    out = color + "█" * full
    if rem and full < width:
        out += PARTS[rem]
        full += 1
    out += Colors.MUTE + "─" * (width - full) + Colors.R
    return out

def rate_of_change(history: list, now: float, value: float, window: float = 20.0, max_len: int = 60):
    """Tracks `value` over time in `history` (a list of (ts, value) pairs,
    mutated in place and capped to `max_len` entries) and returns its rate
    of change per second, measured from the oldest sample within the last
    `window` seconds. None when there is no earlier sample in that window to
    measure from (the first call, or after a gap longer than `window`), so
    a caller can show "measuring" instead.
    """
    history.append((now, value))
    del history[:-max_len]
    base_ts = base_val = None
    for ts, v in history:
        if ts >= now - window:
            base_ts, base_val = ts, v
            break
    if base_ts is None or now - base_ts <= 0:
        return None
    return (value - base_val) / (now - base_ts)

def sparkline(rate_history: list, rate: float, max_len: int = 14) -> str:
    """Appends `rate` to `rate_history` (mutated in place, capped to
    `max_len`) and renders it as an 8-level sparkline (SPARK), scaled to the
    largest rate currently in the window.

    Deliberately a separate rendering from bar(): a bar is a fraction of a
    whole (how full), a sparkline is a trend over time (how fast, lately) -
    conflating the two by drawing both the same way reads as one number
    doubled, not two.
    """
    rate_history.append(rate)
    del rate_history[:-max_len]
    mx = max(rate_history, default=1) or 1
    return "".join(SPARK[max(0, min(7, int(r / mx * 7.99)))] for r in rate_history)

def header_box(left: str, right: str, cols: int, spin: str = "") -> list:
    """The 3-line rounded header frame (top rule, title row, bottom rule)
    for a live view - `cols` is total frame width, i.e. content_width()'s
    return value. The title row's padding is computed from the *actual*
    rendered pieces (left, right, spin), so the right border always lands
    exactly under the corners no matter how any of the three are sized -
    this single spot is the only place that math needs to be right.

    Truncates `left` (then `right`, if even that isn't enough) so the row
    never runs past `cols` regardless of terminal width - `right` (typically
    a short, fixed-format clock) is kept whole for as long as it can be;
    `left` (the variable, more compressible piece - a title/library name)
    gives way first.

    Bakes in its own MARGIN_H left indent (matching every hand-written
    widget line in backbone/prompt/ - e.g. confirm()'s
    f"  {message}") rather than relying on a wrapper to add it: a caller
    driving its view through _Widget.render() (prompt/core.py) gets no
    such wrapper, since _Widget only manages the vertical margin itself.
    """
    interior = cols - 2
    # Reserve the spinner plus one pad column *before* sizing left/right, so
    # truncating to fit `budget` always leaves room for pad >= 1 - flooring
    # pad afterward instead (max(1, ...)) can push the row a column past the
    # border once left+right already exactly fill the interior.
    budget = max(0, interior - len(spin) - 1)
    if visual_len(left) + visual_len(right) > budget:
        right = truncate_text(right, min(visual_len(right), budget))
        left = truncate_text(left, max(0, budget - visual_len(right)))
    pad = max(0, interior - visual_len(left) - visual_len(right) - len(spin))
    C = Colors
    hpad = " " * MARGIN_H
    return [
        f"{hpad}{C.FRAME}╭{rule(interior)}╮{C.R}",
        f"{hpad}{C.FRAME}│{C.B}{left}{C.R}{' ' * pad}{C.TXT}{right}{C.R}{spin}{C.FRAME}│{C.R}",
        f"{hpad}{C.FRAME}╰{rule(interior)}╯{C.R}",
    ]

def wrap_margins(lines: list, width: int = None) -> str:
    """Applies the global MARGIN_H/MARGIN_V inset plus per-line
    clear-to-end-of-line, ready for one `sys.stdout.write` - the standard
    back* frame render (pair with an `ESC[H` cursor-home beforehand).

    Joins with \\r\\n, not \\n: raw terminal mode (tty.setraw, used by
    backbone.prompt.core for key reading) clears OPOST, so the terminal
    stops translating a bare \\n into a carriage return - every line after
    the first would otherwise start wherever the previous one ended instead
    of column 1.

    `width` (typically content_width()'s return value), if given, clips
    every line to it first - a safety net so one field a caller forgot to
    size itself can't overflow the whole frame. Hand-tuned per-field
    truncation still reads better (an ellipsis where it makes sense, not a
    hard cut mid-word); this is the guarantee behind it, not a replacement.
    """
    if width is not None:
        lines = [clip_ansi(line, width) for line in lines]
    hpad = " " * MARGIN_H
    vpad = ["\033[K"] * MARGIN_V
    out = vpad + [hpad + line + "\033[K" for line in lines] + vpad
    return "\r\n".join(out)

def spinner(frame: int) -> str:
    return SPIN[frame % len(SPIN)]

def human_gb(kb: float) -> str:
    """`kb` (kilobytes) as a one-decimal GB string, e.g. "3.5"."""
    return f"{kb / 1048576:.1f}"

def dir_size_kb(path) -> int:
    """Total size of every file under `path`, in KB (0 if it doesn't exist)."""
    path = Path(path)
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total // 1024


_ANSI_DEMO = re.compile(r"\033\[[0-9;]*[a-zA-Z]")


def _demo() -> None:
    """Self-check for the pure logic here, header_box's alignment above all.
    Runs without a terminal: `python3 -m backbone.ui`.
    """
    for cols in (70, 100, 137):
        for left, right, spin in (("  SHORT", "12:00:00  ", "X"), ("", "", ""), ("a" * 20, "b", "Y")):
            top, mid, bot = header_box(left, right, cols, spin)
            widths = {len(strip_ansi(top)), len(strip_ansi(mid)), len(strip_ansi(bot))}
            assert len(widths) == 1, (cols, left, right, spin, widths)
    # A left piece far longer than the frame must truncate, not overflow -
    # the border still lines up at a width too narrow for it whole.
    for cols in (10, 20, 40):
        top, mid, bot = header_box("a" * 200, "12:00:00  ", cols, "X")
        widths = {len(strip_ansi(top)), len(strip_ansi(mid)), len(strip_ansi(bot))}
        assert len(widths) == 1, (cols, widths)
        assert len(strip_ansi(mid)) == cols + MARGIN_H, (cols, len(strip_ansi(mid)))
    assert visual_len("plain") == 5
    assert visual_len(f"{Colors.BOLD}x{Colors.RESET}") == 1
    assert truncate_text("abcdefgh", 4) == "abc…"
    assert plural(1, "disc") == "1 disc" and plural(2, "disc") == "2 discs"
    # Regression guard: wrap_margins must join with \r\n, not \n - under raw
    # terminal mode (OPOST cleared) a bare \n never returns to column 1, and
    # every line after the first starts wherever the previous one ended.
    assert "\r\n" in wrap_margins(["a", "b"])
    # wrap_margins(width=...) must clip an oversized line rather than let it
    # overflow - the safety net behind every tool's own per-field sizing.
    clipped = wrap_margins(["a" * 200], width=10).split("\r\n")[1]
    assert len(strip_ansi(clipped)) <= 10 + MARGIN_H, clipped
    assert human_gb(1048576) == "1.0"
    hist = []
    assert rate_of_change(hist, 0.0, 0) is None            # first sample - no window yet
    assert rate_of_change(hist, 10.0, 1024 * 10) == 1024.0  # 10240 KB over 10s = 1024 KB/s
    rh = []
    spark1 = sparkline(rh, 10.0)
    spark2 = sparkline(rh, 20.0)
    assert len(spark1) == 1 and len(spark2) == 2
    print("backbone.ui self-check OK")


if __name__ == "__main__":
    _demo()
