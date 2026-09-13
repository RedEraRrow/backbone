"""ui.py - shared terminal-UI primitives for the back* suite (backtrack,
backcrack, ...): colors, sizing, ANSI-aware text measuring, the screen-diff
painter, background-task/status-bar tracking, and a generic "footer box"
hook (backtrack's now-playing bar, generalized - any tool can register a
callable(width) -> list[str] to have a persistent box drawn above the
status line; none is registered by default, so a tool that never calls
set_footer_provider() simply never sees one).

    NO_COLOR=1 - disables color everywhere that imports Colors from here.
"""
from __future__ import annotations
import os
import re
import shutil
import signal
import sys
import time as _time
import unicodedata
from pathlib import Path
from typing import Any

from .nav import NAV_STACK

USE_COLOR = not os.environ.get("NO_COLOR") and sys.stdout.isatty()


class Colors:
    """Semantic short aliases (R/B/...) alongside backtrack's original named
    palette (PRIMARY/ACCENT/...) - the prompt system ported from backtrack
    references the named set directly; a tool's own live view can use
    whichever reads better. Empty strings when USE_COLOR is False, so an
    f-string using these never needs an `if USE_COLOR` guard.
    """
    if USE_COLOR:
        PRIMARY = "\033[1;37m"   # bold white
        WHITE = "\033[37m"
        ACCENT = "\033[1;31m"    # red
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
        HIDE = "\033[?25l"
        SHOW = "\033[?25h"
        # backcrack-style semantic additions with no backtrack equivalent
        FRAME = "\033[38;5;239m"
        TEAL = "\033[38;5;43m"
        AMBER = "\033[38;5;179m"
        RED = "\033[38;5;167m"
        TXT = "\033[38;5;252m"
        MUTE = "\033[38;5;243m"
    else:
        PRIMARY = WHITE = ACCENT = CYAN = YELLOW = MAGENTA = GREEN = DIM = ""
        BOLD = ITALIC = UNDERLINE = RESET = BACK = INVERT = HIDE = SHOW = ""
        FRAME = TEAL = AMBER = RED = TXT = MUTE = ""
    # short aliases some back* tools use
    R = RESET
    B = BOLD


# Global content margins. All widgets and the status/footer bars read from
# here - change these two to tune the whole suite at once.
MARGIN_H = 2   # columns reserved on each horizontal side (left and right)
MARGIN_V = 1   # rows reserved on each vertical side (top and bottom)

SPIN = list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
PARTS = ["", "▏", "▎", "▍", "▌", "▋", "▊", "▉"]   # eighth-block fill steps
SPARK = "▁▂▃▄▅▆▇█"


def spinner(frame: int) -> str:
    return SPIN[frame % len(SPIN)]


def content_width(min_width: int = 1) -> int:
    """Terminal columns available for content, after the global left+right
    margin - the width every box/bar in a frame should be drawn against.

    No artificial floor beyond `min_width` (default 1, i.e. effectively
    none) - matching backtrack's own _cols(): content is always sized
    against the TRUE current width, however narrow. A floor pitched above
    the real terminal width would size content for space that doesn't
    exist; a caller's later hard safety-clip against the true width (see
    watch.py's render()) would then cut that oversized content apart
    mid-render - border corners and all - instead of letting it degrade
    gracefully the way the per-field truncation is meant to.

    Routed through get_terminal_width() (below) - the one cached,
    SIGWINCH-invalidated source of truth for the terminal's size the whole
    suite reads from, same as every prompt widget. Safe for a live,
    tick-driven view specifically because such a view should be driven
    through run_dashboard() (prompt_core.py), which checks consume_resize()
    every tick and forces a hard clear-and-redraw the instant a resize
    lands - the same pattern every other screen in the suite uses, not a
    bespoke one-off.
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
    of change per second over the last `window` seconds - None until
    `window` seconds of history has accumulated (not yet measurable), so a
    caller can show "measuring" instead of a misleadingly noisy early rate.
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
    widget line in prompt.py/prompt_core.py - e.g. confirm()'s
    f"  {message}") rather than relying on a wrapper to add it: a caller
    driving its view through _Widget.render() (prompt_core.py) gets no
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
    backbone.prompt_core for key reading) clears OPOST, so the terminal
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


# ---------------------------------------------------------------------------
# Terminal size, cached and resize-aware.
# ---------------------------------------------------------------------------

_resize_flag = False
_footer_layout_dirty = False

# `get_terminal_size` is an ioctl per call and the render path calls it per
# *line* (clipping) as well as per frame; the size only changes on SIGWINCH,
# which clears this. Without SIGWINCH (Windows) it re-reads on a short TTL.
_size_cache: tuple | None = None
_size_cache_at: float = 0.0
_SIZE_TTL = 0.25


def _sigwinch_handler(signum: int, frame: Any) -> None:
    """Mark that the terminal was resized; consume_resize() picks this up."""
    global _resize_flag, _size_cache
    _resize_flag = True
    _size_cache = None


_HAS_SIGWINCH = hasattr(signal, "SIGWINCH")
if _HAS_SIGWINCH:
    signal.signal(signal.SIGWINCH, _sigwinch_handler)


def mark_footer_layout_dirty() -> None:
    """Signal that the footer box's height changed (it appeared, vanished, or
    a tool set a new provider), so menus re-render and re-reserve rows for
    it via consume_resize()."""
    global _footer_layout_dirty
    _footer_layout_dirty = True


def consume_resize() -> bool:
    """True (and clears the flags) if the terminal was resized *or* the
    footer box changed height since last call - both need a full
    re-render/re-layout."""
    global _resize_flag, _footer_layout_dirty
    if _resize_flag or _footer_layout_dirty:
        _resize_flag = False
        _footer_layout_dirty = False
        return True
    return False


def get_terminal_size(default: tuple = (80, 24)) -> tuple:
    """Terminal (columns, rows), falling back to `default` if the query
    fails. Memoised - see `_size_cache`."""
    global _size_cache, _size_cache_at
    if _size_cache is not None:
        if _HAS_SIGWINCH or (_time.monotonic() - _size_cache_at) < _SIZE_TTL:
            return _size_cache
    try:
        size = shutil.get_terminal_size()
    except OSError:
        return default
    _size_cache = (size.columns, size.lines)
    _size_cache_at = _time.monotonic()
    return _size_cache


def invalidate_terminal_size() -> None:
    """Drop the memoised terminal size (next query re-reads it)."""
    global _size_cache
    _size_cache = None


def get_terminal_width(default: int = 80) -> int:
    cols, _ = get_terminal_size((default, default))
    return cols


def get_terminal_height(default: int = 24) -> int:
    _, rows = get_terminal_size((default, default))
    return rows


# ---------------------------------------------------------------------------
# Screen clear / alt-screen.
# ---------------------------------------------------------------------------

_screen_invalidator = None


def set_screen_invalidator(fn) -> None:
    """Register the painter's "forget what's on screen" hook (prompt_core's
    screen_invalidate) so every clear_screen() keeps the diffed painter
    honest too."""
    global _screen_invalidator
    _screen_invalidator = fn


def _screen_cleared() -> None:
    if _screen_invalidator is not None:
        try:
            _screen_invalidator()
        except Exception:
            pass


def enter_alt_screen() -> None:
    """Switch to the terminal alternate screen buffer (no scrollback).
    Also enables focus in/out reporting (\\033[?1004h); unsupported
    terminals ignore it."""
    sys.stdout.write("\033[?1049h\033[?1004h\033[H\033[3J\033[J" + Colors.HIDE)
    sys.stdout.flush()
    _screen_cleared()


def exit_alt_screen() -> None:
    """Restore the main screen buffer, disable focus reporting, show cursor."""
    sys.stdout.write("\033[?25h\033[?1004l\033[?1049l")
    sys.stdout.flush()


def clear_screen() -> None:
    """Overwrite screen content from home without triggering scrollback
    save. Leaves the cursor hidden - a bare clear parks it at home, where it
    would otherwise blink in the top-left until the next frame hides it."""
    sys.stdout.write("\033[H\033[3J\033[J" + Colors.HIDE)
    sys.stdout.flush()
    _screen_cleared()


# ---------------------------------------------------------------------------
# Footer box - a persistent box drawn just above the status line (backtrack's
# now-playing bar, generalized). A tool registers set_footer_provider(fn),
# fn(width) -> list[str] | None; none registered means no box, ever.
# ---------------------------------------------------------------------------

FOOTER_GLYPH_COLS: tuple = ()   # (start, width) per clickable glyph, content-relative

_footer_provider = None
_footer_lines_cache: list = []
_footer_sig = None   # cheap identity of what's currently shown - keys the idle redraw
_footer_unboxed = False   # content is "live" but too narrow to draw a box for


def set_footer_provider(fn) -> None:
    """Register a ``callable(width) -> list[str] | None`` that renders the
    footer box."""
    global _footer_provider
    _footer_provider = fn


def set_footer_signature(sig) -> None:
    global _footer_sig
    _footer_sig = sig


def footer_signature():
    return _footer_sig


def footer_lines(width: int) -> list:
    """The footer box rows for `width` (empty list when nothing registered,
    or the provider itself has nothing to show)."""
    global _footer_lines_cache
    if _footer_provider is None:
        _footer_lines_cache = []
        return []
    try:
        lines = _footer_provider(width) or []
    except Exception:
        # A provider that raised tells us nothing about its state - keep the
        # box exactly as it was rather than blinking it out and back.
        return _footer_lines_cache
    _footer_lines_cache = list(lines)
    return _footer_lines_cache


def footer_active() -> bool:
    return bool(_footer_lines_cache)


def set_footer_unboxed(value: bool) -> None:
    """Content is live but the box couldn't be drawn (terminal too narrow) -
    the one state where the hint bar has to advertise its keys itself."""
    global _footer_unboxed
    _footer_unboxed = bool(value)


def footer_unboxed() -> bool:
    return _footer_unboxed


def footer_height() -> int:
    return len(_footer_lines_cache)


_footer_waker = None


def set_footer_waker(fn) -> None:
    """Register a ``callable()`` that nudges the menu poll to repaint the
    box immediately, instead of waiting for the next keystroke or tick."""
    global _footer_waker
    _footer_waker = fn


def pulse_footer() -> None:
    """Ask the active menu poll to repaint the footer box now (no-op if
    nothing is listening)."""
    if _footer_waker is not None:
        try:
            _footer_waker()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Status bar: breadcrumb + background-task pulse + one-shot toast.
# ---------------------------------------------------------------------------

BACKGROUND_TASKS: dict = {}

_toast_message: str = ""
_toast_expiry: float = 0.0

_PULSE_RAMP = (238, 243, 248, 253, 255, 253, 248, 243)   # dim -> white -> dim, 256-color


def pulse_circle() -> str:
    """A white ● whose brightness pulses over time - the beacon next to a
    running background task."""
    code = _PULSE_RAMP[int(_time.time() * 6) % len(_PULSE_RAMP)]
    return f"\033[38;5;{code}m●{Colors.RESET}"


def set_status(task_id: str, message) -> None:
    """Update or remove a background task status."""
    if message is None:
        BACKGROUND_TASKS.pop(task_id, None)
    else:
        BACKGROUND_TASKS[task_id] = message


def has_background_tasks() -> bool:
    return bool(BACKGROUND_TASKS)


def show_status(message: str, duration: float = 3.0) -> None:
    """Flash a one-shot message in the status bar for `duration` seconds."""
    global _toast_message, _toast_expiry
    _toast_message = message
    _toast_expiry = _time.time() + duration


def _get_breadcrumb_str(width: int) -> str:
    """Render NAV_STACK as a '>'-joined breadcrumb that fits `width`.

    Over-long trails shed whole path components from the front, keeping the
    deepest ones - those say where you are. The last component is kept
    whatever it costs, truncated at its own end if even it doesn't fit.
    """
    if width <= 1 or not NAV_STACK:
        return ""
    sep = " > "
    max_length = max(0, width - 1)
    if len(sep.join(NAV_STACK)) <= max_length:
        return sep.join(NAV_STACK)
    kept: list = [NAV_STACK[-1]]
    for name in reversed(NAV_STACK[:-1]):
        if len("… > " + sep.join([name] + kept)) > max_length:
            break
        kept.insert(0, name)
    out = "… > " + sep.join(kept)
    if len(out) <= max_length:
        return out
    return truncate_text(NAV_STACK[-1], max_length)


def get_status_line() -> str:
    """The current status bar content (breadcrumb + tasks + toast)."""
    global _toast_message, _toast_expiry
    cols = get_terminal_width()
    if cols <= 0:
        return ""
    if _toast_message and _time.time() > _toast_expiry:
        _toast_message = ""

    sep = f"  {Colors.DIM}·{Colors.RESET}  "
    right_parts = []
    for msg in BACKGROUND_TASKS.values():
        right_parts.append(f"{pulse_circle()} {Colors.DIM}{msg}{Colors.RESET}")
    if _toast_message:
        right_parts.append(f"{Colors.DIM}{_toast_message}{Colors.RESET}")
    right = sep.join(right_parts)

    crumb = _get_breadcrumb_str(cols // 2) if NAV_STACK else ""
    left = f"  {Colors.DIM}{crumb}{Colors.RESET}" if crumb else ""

    if left and right:
        gap = max(2, cols - visual_len(left) - visual_len(right) - 2)
        status = left + " " * gap + right + "  "
    elif left:
        status = left
    elif right:
        status = "  " + right + "  "
    else:
        status = ""

    if visual_len(status) > cols:
        status = clip_ansi(status, cols)
    return status


# ---------------------------------------------------------------------------
# Display width: one ANSI scanner, one column table, for the whole suite.
# Every widget that measures, clips or pads a styled line goes through this.
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(
    r'(\x1b\[[0-9;?]*[ -/]*[@-~])'
    r'|(\x1b_G[^\x1b]*\x1b\\)'
    r'|(\x1b\][^\x1b\x07]*(?:\x1b\\|\x07))'
    r'|(\x1b[PX^_].*?\x1b\\)'
    r'|(\x1b.)'
)


def _scan(s: str):
    """Yield (is_escape, chunk) across `s` - one chunk per escape sequence,
    one per printable character."""
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
    if '\x1b' not in s:
        return s
    return _ANSI_RE.sub('', s)


_ZERO_WIDTH_SET = frozenset(('︎', '️', '​', '‌', '‍'))
_WIDE_SET = frozenset('⏮⏭⏸⏵⏪⏩⏫⏬⏯⏱⏲⏰')
_cols_cache: dict = {}


def char_cols(ch: str) -> int:
    """How many terminal columns `ch` occupies: 0, 1 or 2."""
    w = _cols_cache.get(ch)
    if w is None:
        if ch in _ZERO_WIDTH_SET or unicodedata.combining(ch):
            w = 0
        elif ch in _WIDE_SET or unicodedata.east_asian_width(ch) in ('W', 'F'):
            w = 2
        else:
            w = 1
        _cols_cache[ch] = w
    return w


def display_text(s: str) -> str:
    """`s` with ANSI escapes and zero-width codepoints removed."""
    out = strip_ansi(s)
    if out.isascii():
        return out
    return ''.join(ch for ch in out if char_cols(ch) != 0)


def visual_len(s: str) -> int:
    """Columns `s` occupies on screen, ignoring ANSI escapes - not the same
    as len(): escapes/combining marks take no column, a wide glyph takes
    two."""
    if not s:
        return 0
    if s.isascii() and '\x1b' not in s:
        return len(s)
    return sum(char_cols(ch) for esc, ch in _scan(s) if not esc)


def clip_ansi(text: str, max_cols: int, reset: bool = True) -> str:
    """`text` truncated to `max_cols` visible columns, escapes preserved and
    never splitting a two-cell glyph across the boundary."""
    if max_cols <= 0:
        return ""
    out: list = []
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


def truncate_text(text: str, max_width: int, placeholder: str = "…", front: bool = False) -> str:
    """Truncate `text` to `max_width` columns, replacing the cut end (or
    start, if `front`) with `placeholder`."""
    if text is None:
        return ""
    if visual_len(text) <= max_width:
        return text
    ph_w = visual_len(placeholder)
    if max_width <= ph_w:
        return clip_ansi(text, max_width, reset=False)
    keep = max_width - ph_w
    if front:
        taken: list = []
        used = 0
        for ch in reversed(text):
            w = char_cols(ch)
            if w and used + w > keep:
                break
            taken.append(ch)
            used += w
        return placeholder + "".join(reversed(taken))
    return clip_ansi(text, keep, reset=False) + placeholder


def plural(n: int, singular: str, many: str = None) -> str:
    """``"1 result"`` / ``"156 results"`` - the count and its noun, agreeing."""
    return f"{n} {singular if abs(n) == 1 else (many or singular + 's')}"


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
    """ponytail: self-check for header_box's alignment math - the one thing
    in this module that has broken silently before (an off-by-one in a
    hand-written pad formula). Run directly: `python3 -m backbone.ui`.
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
