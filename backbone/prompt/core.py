"""Terminal primitives shared across prompt widgets."""
from __future__ import annotations
import re
import sys
import os
import math
import textwrap
import time
import select as _sel
from dataclasses import dataclass, field
from typing import Any

from backbone import keys, nav, ui
from backbone.log import log, enabled as _logging, quietly
C = ui.Colors

_IS_WINDOWS = os.name == "nt"

_COLUMNS_MAX_WIDTH = 160   # cap effective width for table layout even on ultra-wide terminals
_EDGE_MARGIN       = 2     # right-side padding for pinned columns
_MIN_COL_FLOOR     = 6     # a squeezed column shrinks to at most this before it stops giving up space
_MIN_PIN_GAP       = 2     # reserved breathing space between the left block and right-pinned columns

tty: Any
termios: Any
msvcrt: Any

if _IS_WINDOWS:
    import msvcrt
else:
    import tty
    import termios


def _get_term_attrs(fd: int):
    """Current termios attributes for fd, or None on Windows."""
    return None if _IS_WINDOWS else termios.tcgetattr(fd)


def _set_raw(fd: int) -> None:
    """Put the terminal into raw mode (no-op on Windows)."""
    if not _IS_WINDOWS:
        tty.setraw(fd)


def _restore_term_attrs(fd: int, old):
    """Restore terminal attributes captured before raw mode."""
    if not _IS_WINDOWS and old is not None:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


_footer_prev_h = [0]
_footer_last_draw = [0.0]
_status_prev_active = [False]            # was a background task shown last idle tick?

# Self-pipe so background threads can wake the menu poll to repaint the box the
# instant playback state changes; see ui.pulse_footer(). The
# poll's select() watches the read end alongside stdin; a pulse makes it return
# immediately and repaint, rather than waiting on the next keystroke or timeout.
try:
    _wake_r, _wake_w = os.pipe()
    os.set_blocking(_wake_r, False)
    os.set_blocking(_wake_w, False)
except OSError:                          # no pipes (e.g. odd sandbox): degrade gracefully
    _wake_r = _wake_w = -1


def _poke_footer_wake() -> None:
    """Write one byte to the wake pipe (coalesced by the reader; never blocks)."""
    if _wake_w >= 0:
        try:
            os.write(_wake_w, b'.')
        except (BlockingIOError, OSError):
            pass                          # pipe full already → a wake is pending anyway


if _wake_r >= 0:
    ui.set_footer_waker(_poke_footer_wake)


# ---------------------------------------------------------------------------
# Persistent screen model.
#
# One entry per screen row holding what is currently displayed there, shared by
# every writer (widget frames, the now-playing box, the status bar). A repaint
# writes only the rows whose content actually changed and never erases a row
# before rewriting it: erasing to the end of the screen and redrawing everything
# makes the screen flicker and the now-playing box blink on every keystroke. Rows are also written *absolutely*, with no newlines,
# so a line-buffered stdout cannot flush a half-drawn frame.
_screen: dict[int, str] = {}
# Each row's cells as last painted, (style, character) per column, so a changed
# row rewrites only the cells that changed: rewriting a whole row erases an
# image drawn over part of it (album art), which then flickers as it's resent.
_cells: dict[int, list] = {}
# The columns (first, last; 1-based) the last paint of each row wrote.
_painted: dict[int, list] = {}
# The terminal size the model was painted at. A resize reflows what is on
# screen, so the model no longer describes it: the next paint wipes the screen
# and repaints every row, whether or not the screen's own code thought to clear.
_screen_size: list = [None]


def screen_invalidate() -> None:
    """Forget what is on screen: after a full clear, a resize, or a write by
    something that doesn't go through here (the player view). A clear at a new
    size (a screen answering a resize) counts as the resize wipe too."""
    size = ui.get_terminal_size()
    if _screen_size[0] is not None and size != _screen_size[0]:
        _note_resize(_screen_size[0], size)
        _screen_size[0] = size
    _reserved.clear()                    # a frame after this reserves afresh
    _shown_pictures.clear()              # a clear took them with it
    _float.clear()
    _screen.clear()
    _cells.clear()


def _note_resize(was: tuple, size: tuple) -> None:
    """Log a resize: how long after the terminal reported it we're repainting,
    and which rows of the old frame were wider than the new window, the ones
    the terminal rewrapped before we could redraw."""
    if not _logging():
        return
    wide = sorted(r for r, key in _screen.items() if _row_width(key.split("\x00", 1)[0]) > size[0])
    log.debug("resize %sx%s -> %sx%s, repaint %.0f ms after the signal; %d old rows "
              "wider than the new width (rewrapped by the terminal): %s",
              was[0], was[1], size[0], size[1], ui.ms_since_resize_signal(),
              len(wide), wide[:20])


def _resize_wipe() -> str:
    """"" normally; after a terminal resize, a full clear (and a forgotten
    model), so the frame being painted repaints everything."""
    size = ui.get_terminal_size()
    if size == _screen_size[0]:
        return ""
    was, _screen_size[0] = _screen_size[0], size
    if was is not None:
        _note_resize(was, size)
    _screen.clear()
    _cells.clear()
    _shown_pictures.clear()
    return "\033[H\033[2J" if was is not None else ""


def _row_width(text: str) -> int:
    """Visible width of a painted row, ignoring colour codes."""
    return ui.visual_len(ui.strip_ansi(text))


def _register_screen_hooks() -> None:
    """Let `ui.clear_screen()` (and the alt-screen switch) drop the model."""
    ui.set_screen_invalidator(screen_invalidate)


_takeover_pending = [False]


def screen_takeover_next() -> None:
    """Take the screen over on the next frame *without* clearing it first.

    Called where a widget would otherwise clear on entry: the next paint
    overwrites the rows it needs and blanks whatever the previous screen left
    behind, in the same flush, so there is no blank flash between screens. A
    clear is only needed when the terminal reflowed (resize) or something
    painted outside this model.
    """
    _takeover_pending[0] = True


def _takeover_rows(frame: dict) -> dict:
    """Add blanks for rows the previous screen owned that `frame` doesn't."""
    if not _takeover_pending[0]:
        return frame
    _takeover_pending[0] = False
    out = dict(frame)
    rows = ui.get_terminal_height()
    footer = range(rows - _footer_prev_h[0], rows)           # the now-playing box: its own painter's
    for row in _screen:
        if row not in footer:
            out.setdefault(row, "")
    return out


def screen_rows() -> set:
    """Every row the painter currently believes it knows the content of."""
    return set(_screen)


def screen_forget_rows(first: int, last: int) -> None:
    """Forget rows `first`..`last` inclusive (another writer owns them now)."""
    for r in range(first, last + 1):
        _screen.pop(r, None)
        _cells.pop(r, None)


def screen_row_paint(row: int, text: str, extra: str = "") -> str:
    """The escape string that paints one row as `text` with `extra` layered on
    top, or "" when the row already reads exactly that way.

    `extra` is for absolute overlays that write a few columns of a row something
    else owns (the volume bar sits on the album art's rows). Both layers are part
    of the row's identity, so a row repaints when *either* changes, and a row
    whose overlay went away is erased rather than keeping stale glyphs. A blank
    row is content too: "" differs from anything previously drawn there.

    While an app runs as tabs, the top rows are the tab bar whatever is
    painted there: every screen leaves them as its top margin (top_margin()).
    """
    if row > ui.get_terminal_height():
        # Past the bottom (a frame laid out before the window shrank): the
        # terminal would write it on the last row, over what belongs there.
        _screen.pop(row, None); _cells.pop(row, None)
        return ""
    if nav.TABS and row <= ui.tab_rows():
        text, extra = ui.tab_bar_lines()[row - 1], ""
    notch = ui.tab_notch() if row == ui.tab_rows() + 1 else None
    wipe = _resize_wipe()
    key = f"{text}\x00{extra}\x00{notch}"
    if _screen.get(row) == key:
        _painted[row] = []
        return ""
    old = _cells.get(row) if row in _screen else None
    _screen[row] = key
    if _logging() and _row_width(text) > (_screen_size[0] or (0, 0))[0]:
        log.warning("row %d painted %d wide in a %d-column window: %r", row,
                    _row_width(text), _screen_size[0][0], ui.strip_ansi(text)[:80])
    new = _row_cells(text, extra)
    if notch and new is not None:
        new = _notched(new, notch)
    held = _reserved.get(row)
    if new is not None and held:
        # Someone else's cells on this row (the lyrics): left exactly as they
        # are, even on a row this paint doesn't know (then every other cell is
        # written, but never the whole row wiped).
        known = old if old is not None and not wipe else []
        old = known + [_UNKNOWN] * max(0, len(new) - len(known))
        width = max(len(new), len(old), max(b for _a, b in held))
        new = new + [_BLANK] * (width - len(new))
        for a, b in held:
            for i in range(a - 1, b):
                new[i] = old[i] if i < len(old) else _UNKNOWN
        _cells[row] = new
        out, _painted[row] = _cells_diff(row, old, new)
        return wipe + out
    if new is None or old is None or wipe:
        # A row not known cell by cell (first paint, after a clear, an escape
        # this doesn't follow): written whole, as it always was.
        _cells.pop(row, None) if new is None else _cells.__setitem__(row, new)
        _painted[row] = [(1, 10 ** 6)]
        if notch and new is not None and not _float_cols(row):   # drawn from its cells: the notch is in them
            return f"{wipe}\033[{row};1H\033[2K" + _cells_diff(row, [], new)[0]
        if new is not None and _float_cols(row):      # a floating box over it: every cell but its
            return wipe + _cells_diff(row, [_UNKNOWN] * max(len(new), ui.get_terminal_width()), new)[0]
        return f"{wipe}\033[{row};1H\033[2K{text}{extra}"
    _cells[row] = new
    out, _painted[row] = _cells_diff(row, old, new)
    return out


_EDGE = ("─", "┈")                    # a box's top border, plain or dotted


def _notched(cells: list, notch: tuple) -> list:
    """The row under the tab bar with the showing tab's outline run into it:
    where a box's top border lies under the tab, an opening cut in it (the
    tab's sides turning into the border, or carrying on as the box's own
    side at its corner), and that box's title moved to the right end of its
    border, clear of the tab (dropped when there's no room: the breadcrumb
    says it too). Over blank cells, the tab is closed underneath."""
    a, b = notch
    cells = list(cells) + [_BLANK] * max(0, b + 1 - len(cells))
    left = next((i for i in range(min(a, len(cells) - 1), -1, -1) if cells[i][1] == "╭"), None)
    right = next((i for i in range(left + 1, len(cells)) if cells[i][1] == "╮"), None) if left is not None else None
    if left is not None and right is not None and right >= b:
        _title_right(cells, left, right, b)
        # Border text under the tab (a subtitle, the toggle, in a narrow
        # window) gives way to it, a whole run between dashes at a time.
        lo, hi = max(a, left + 1), min(b, right - 1)
        if any(cells[i][1] not in _EDGE for i in range(lo, hi + 1)):
            while lo > left + 1 and cells[lo - 1][1] not in _EDGE:
                lo -= 1
            while hi < right - 1 and cells[hi + 1][1] not in _EDGE:
                hi += 1
            for i in range(lo, hi + 1):
                cells[i] = cells[left + 1]
        if all(cells[i][1] in _EDGE or i in (left, right) for i in range(a, b + 1)):
            style = cells[left][0]
            cells[a] = (style, "│" if a == left else "╯")
            cells[b] = (style, "│" if b == right else "╰")
            for i in range(a + 1, b):
                cells[i] = _BLANK
            return cells
    if all(c[1] == " " for c in cells[a:b + 1]):
        cells[a], cells[b] = (C.DIM, "╰"), (C.DIM, "╯")
        for i in range(a + 1, b):
            cells[i] = (C.DIM, "─")
    return cells


def _title_right(cells: list, left: int, right: int, after: int) -> None:
    """Move a box's top-border title (" Title ", just after its corner) to
    the right end of the border, before what's already there (a subtitle,
    the help toggle), past column `after`; drop it if it won't fit."""
    if left + 2 >= right or cells[left + 1][1] not in _EDGE or cells[left + 2][1] != " ":
        return
    end = next((i for i in range(left + 3, right) if cells[i][1] in _EDGE and cells[i - 1][1] == " "), None)
    if end is None:
        return
    title, dash = cells[left + 2:end], cells[left + 1]
    for i in range(left + 2, end):
        cells[i] = dash
    stop = next((i for i in range(end, right) if cells[i][1] not in _EDGE), right) - 1
    room = stop - (after + 2)                          # columns between the tab and what's at the right
    if len(title) > room >= 8:                         # shortened, rather than lost: " Tag ID (e.g. …"
        title = title[:room - 2] + [(title[room - 3][0], "…"), title[-1]]
        while title[-3][1] == "":                      # never half a wide character before the …
            title[-3:-2] = []
            title.insert(-2, (title[-2][0], " "))
    start = stop - len(title)
    if start > after + 1:
        cells[start:stop] = title


# Cells someone other than the row's painter draws (the player's lyrics, which
# move on their own clock): row → [(first, last) columns, 1-based]. A row paint
# leaves them be; their owner writes them with screen_span_paint.
_reserved: dict = {}


def screen_reserve(first_row: int, last_row: int, first_col: int, last_col: int) -> None:
    """Keep the rectangle's cells out of row paints (see _reserved)."""
    for r in range(first_row, last_row + 1):
        _reserved.setdefault(r, []).append((first_col, last_col))


def screen_release() -> None:
    """Drop every reservation: rows are their painters' whole again."""
    _reserved.clear()


def screen_span_paint(row: int, col: int, text: str) -> str:
    """The escape that writes `text` from (row, col) cell by cell: only the
    cells that changed, the rest of the row as it was (and still known), so
    nothing beside it (an image) is disturbed. For a part of a row with its
    own owner: the lyrics, the progress bar."""
    if row > ui.get_terminal_height():                # past the bottom: see screen_row_paint
        return ""
    span = _row_cells(text)
    if span is None:
        _screen.pop(row, None); _cells.pop(row, None)
        return f"\033[{row};{col}H{text}"
    old = _cells.get(row) if row in _screen else None
    known = old if old is not None else []
    new = known + [_UNKNOWN] * max(0, col - 1 + len(span) - len(known))
    before = list(new)
    if old is None:                                  # unknown row: the span's cells all written
        before = [_UNKNOWN] * len(new)
    new[col - 1:col - 1 + len(span)] = span
    _cells[row] = new
    _screen[row] = _screen.get(row, "") + "\x00span"          # the row's text no longer says it all
    out, spans = _cells_diff(row, before, new)
    _painted[row] = spans
    return out


_ESCAPE = re.compile(r'\x1b\[([0-9;]*)m|\x1b\[(\d+);(\d+)H|\x1b\[K|\x1b')
_BLANK = ("", " ")


def _row_cells(text: str, extra: str = "") -> list | None:
    """A row as cells, (style, character) per column: `text`, then `extra`'s
    overlays (each placed by its own cursor move) over it. A wide character's
    second cell has character "". None when the row holds an escape this
    doesn't follow (it is then written whole)."""
    cells: list = []
    style, col = "", 0

    def put(ch: str) -> None:
        nonlocal col
        w = ui.char_cols(ch)
        if w == 0:                                  # a combining mark: onto the last character
            if col and col - 1 < len(cells):
                st, prev = cells[col - 1]
                cells[col - 1] = (st, prev + ch)
            return
        while len(cells) < col + w:
            cells.append(_BLANK)
        cells[col] = (style, ch)
        if w == 2:
            cells[col + 1] = (style, "")
        col += w

    for part in (text, extra):
        pos = 0
        for m in _ESCAPE.finditer(part):
            for ch in part[pos:m.start()]:
                put(ch)
            pos = m.end()
            seq = m.group(0)
            if m.group(1) is not None:              # a colour or style
                style = "" if m.group(1) in ("", "0") else style + seq
            elif m.group(2) is not None:            # an overlay's own column
                col = int(m.group(3)) - 1
            elif seq == "\x1b[K":                   # erase the rest of the row
                del cells[col:]
            else:
                return None
        for ch in part[pos:]:
            put(ch)
        style = ""
    return cells


def _cells_diff(row: int, old: list, new: list) -> tuple[str, list]:
    """What writes row `row` from `old` cells to `new`: only the runs that
    changed (a run widened to keep wide characters whole), a shorter row's
    end erased; and those runs' columns (first, last; 1-based). Cells under
    a floating box (screen_float) aren't written: it's over them."""
    n = max(len(old), len(new))
    under = _float_cols(row)
    if under:                                        # no erasing to the end of the row past it: blanks
        new = list(new) + [_BLANK] * (n - len(new))
    cell = lambda cells, i: cells[i] if i < len(cells) else _BLANK      # noqa: E731
    changed = [i for i in range(n) if cell(old, i) != cell(new, i) and i not in under]
    if not changed:
        return "", []
    runs, start, last = [], changed[0], changed[0]
    for i in changed[1:]:
        if i > last + 1:
            runs.append((start, last))
            start = i
        last = i
    runs.append((start, last))
    out, spans = [], []
    for a, b in runs:
        while a > 0 and (cell(new, a)[1] == "" or cell(old, a)[1] == ""):   # the start of a wide character
            a -= 1
        while b + 1 < n and (cell(new, b + 1)[1] == "" or cell(old, b + 1)[1] == ""):
            b += 1
        spans.append((a + 1, b + 1))
        seg, cur = [f"\033[{row};{a + 1}H"], None
        if a >= len(new):                           # past the new row's end: erase the rest
            out.append(seg[0] + "\033[0m\033[K")
            continue
        for i in range(a, min(b + 1, len(new))):
            st, ch = new[i]
            if ch is None:                          # a cell someone else draws: step over it
                seg.append(f"\033[{row};{i + 2}H")
                continue
            if st != cur:
                seg.append("\033[0m" + st)
                cur = st
            if ch[:1] in ui.AMBIGUOUS_WIDE:
                # Counted two cells, drawn one or two depending on the font:
                # blank its second cell first, then place what follows by
                # column, so either way nothing shifts or is left behind.
                seg.append(f"\033[{row};{i + 2}H \033[{row};{i + 1}H{ch}\033[{row};{i + 3}H")
                continue
            seg.append(ch)
        seg.append("\033[0m")
        if b + 1 > len(new):                        # it runs off the new row's end
            seg.append("\033[K")
        out.append("".join(seg))
    return "".join(out), spans


_UNKNOWN = (None, None)               # a cell the painter no longer vouches for


def screen_forget_cells(first_row: int, last_row: int, first_col: int, last_col: int) -> None:
    """Forget the cells in rows first_row..last_row, columns first_col..last_col
    (1-based): something else drew over them (an image), so the next paint of
    those rows writes them again, and only them."""
    for r in range(first_row, last_row + 1):
        cells = _cells.get(r)
        if cells is None:
            continue
        while len(cells) < last_col:
            cells.append(_BLANK)
        for c in range(first_col - 1, last_col):
            cells[c] = _UNKNOWN
        _screen[r] = _screen.get(r, "") + "\x00forgotten"       # so the row isn't skipped as unchanged


_shown_pictures: list = []           # the last widget frame's pictures, for screen_restore


# A box floating over the screen for a moment (screen_float: the volume as it
# changes): its rows and columns, its lines, and when it goes. What's under
# it is kept in the model as ever, just not written, and put back after.
_float: dict = {}


def _float_cols(row: int) -> set:
    """The 0-based columns of `row` the floating box covers (none, mostly)."""
    if not _float or not _float['top'] <= row < _float['top'] + len(_float['lines']):
        return set()
    return set(range(_float['left'] - 1, _float['left'] - 1 + _float['w']))


def screen_float(lines: list, seconds: float) -> None:
    """Float `lines` (a small box, all one width) over the middle of the
    screen for `seconds`, then put back what's under it. Showing again while
    it's up replaces it and starts its time again. Nothing else stops for it:
    the screen goes on painting round it (see float_tick)."""
    cols, rows = ui.get_terminal_size()
    lines = [ui.clip_ansi(ln, cols) for ln in lines[:rows]]
    w = max((ui.visual_len(ln) for ln in lines), default=0)
    place = dict(w=w, top=max(1, (rows - len(lines)) // 2 + 1), left=max(1, (cols - w) // 2 + 1))
    if _float and (len(_float['lines']), _float['w'], _float['top'], _float['left']) != (
            len(lines), place['w'], place['top'], place['left']):
        _float_gone()                # a new shape: put back what the old one covered first
    _float.update(lines=lines, until=time.time() + seconds, **place)
    _float_draw()


def screen_float_close() -> None:
    """Take a floating box away now, putting back what it covered."""
    if _float:
        _float_gone()


_PROGRESS_W = 64                     # the progress box's widest


def progress_float(message: str, progress: float) -> None:
    """A blocking job's progress (`progress` 0-1, `message` what it's on) in a
    box over the middle of the screen, the same width throughout so it holds
    still as the names change; screen_float_close takes it away."""
    cols, rows = ui.get_terminal_size()
    w = min(cols, _PROGRESS_W)
    boxed = rows >= 3 and w >= 16
    room = w - 4 if boxed else cols
    bar = ui.get_progress_bar(progress, max(4, min(24, room // 3)))
    head = f"{ui.pulse_circle()} {bar} "
    text = head + C.DIM + ui.truncate_text(message, max(1, room - ui.visual_len(head))) + C.RESET
    text += " " * max(0, room - ui.visual_len(text))
    lines = [ln[ui.MARGIN_H:] for ln in box_lines([text], w, 3)] if boxed else [text]
    screen_float(lines, 24 * 3600)   # up till it's closed


def _float_draw() -> None:
    sys.stdout.write("\0337" + "".join(f"\033[{_float['top'] + k};{_float['left']}H{C.RESET}{ln}{C.RESET}"
                                       for k, ln in enumerate(_float['lines'])) + "\0338")
    sys.stdout.flush()


def _float_gone() -> None:
    """Put back what the floating box covered, from the model, and any
    picture of the screen it was over."""
    top, left, w, h = _float['top'], _float['left'], _float['w'], len(_float['lines'])
    _float.clear()
    out = []
    for r in range(top, top + h):
        cells = list(_cells.get(r) or [])
        cells += [_BLANK] * max(0, left - 1 + w - len(cells))
        before = cells[:left - 1] + [_UNKNOWN] * w + cells[left - 1 + w:]
        out.append(_cells_diff(r, before, cells)[0])
    for r, c, n, _k, esc, *size in _shown_pictures:
        last = c + (size[0] if size else 10 ** 6) - 1
        if r < top + h and r + n > top and c < left + w and last >= left:
            out.append(f"\0337\033[{r};{c}H{esc}\0338")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def float_tick() -> bool:
    """Keep a floating box on top while it's up (a picture sent over it is
    put under it again), and take it away when its time is up. True when it
    just went: a screen with a picture of its own under it redraws that."""
    if not _float:
        return False
    if time.time() < _float['until']:
        _float_draw()
        return False
    _float_gone()
    return True


def screen_forget_pictures() -> None:
    """For a screen that draws without _Widget (a player view): forget the
    cells under the pictures the last widget drew, so this screen's paint
    writes them all and no picture of that screen is left showing through."""
    for r, c, n, _k, _esc, *size in _shown_pictures:
        if size:
            screen_forget_cells(r, r + n - 1, c, c + size[0] - 1)
        else:
            screen_forget_rows(r, r + n - 1)
    _shown_pictures.clear()


def screen_save(first_row: int, last_row: int, first_col: int, last_col: int) -> dict:
    """What the painter holds in a rectangle, to put back with screen_restore
    once something drawn over it (an overlay) is gone."""
    return {'rect': (first_row, last_row, first_col, last_col),
            'cells': {r: [(_cells.get(r) or [])[c] if c < len(_cells.get(r) or []) else _BLANK
                          for c in range(first_col - 1, last_col)]
                      for r in range(first_row, last_row + 1)}}


def screen_restore(saved: dict) -> str:
    """The escape that puts a saved rectangle back, cell by cell, and redraws
    any picture (an image) it overlapped, which writing over it erased."""
    r0, r1, c0, c1 = saved['rect']
    out = []
    for r, cells in saved['cells'].items():
        text, cur = [], None
        for st, ch in cells:
            if ch is None:
                st, ch = "", " "                      # a cell nothing vouched for: blank
            if st != cur:
                text.append(C.RESET + st)
                cur = st
            text.append(ch)
        out.append(screen_span_paint(r, c0, "".join(text) + C.RESET))
    for r, c, n, _k, esc, *size in _shown_pictures:
        last = c + (size[0] if size else 10 ** 6) - 1
        if r <= r1 and r + n - 1 >= r0 and c <= c1 and last >= c0:
            out.append(f"\0337\033[{r};{c}H{esc}\0338")
    return "".join(out)


def screen_painted(row: int, first: int, last: int) -> bool:
    """Whether the last paint of `row` wrote any of columns first..last
    (1-based): whether an image over them was erased and needs drawing again."""
    return any(a <= last and b >= first for a, b in _painted.get(row, ()))


def screen_row_segment(row: int, text: str) -> str:
    """Paint one row of plain text (no overlay); see `screen_row_paint`."""
    return screen_row_paint(row, text)


def screen_paint(rows: dict, *, cursor: tuple | None = None,
                 hide_cursor: bool = True, save_cursor: bool = False) -> None:
    """Paint `rows` ({1-based row: text}) as one buffered, single-syscall frame.

    Unchanged rows cost nothing. `cursor` places the caret and shows it (text
    inputs); `save_cursor` wraps the frame in DEC save/restore so a caret
    elsewhere is left alone (background repaints).
    """
    rows = _takeover_rows(rows)
    parts: list[str] = []
    for row in sorted(rows):
        seg = screen_row_segment(row, rows[row])
        if seg:
            parts.append(seg)
    if not parts:
        return                             # nothing changed: draw nothing at all
    body = "".join(parts)
    if save_cursor:
        out = "\0337" + body + "\0338"
    else:
        out = (C.HIDE if hide_cursor else "") + body
        if cursor is not None:
            out += f"\033[{cursor[0]};{cursor[1]}H" + C.SHOW
    sys.stdout.write(out)
    sys.stdout.flush()


def _footer_box_str(rows: int, lines: list) -> str:
    """Escape string that draws the now-playing box in the rows just above the
    breadcrumb, clearing any band a taller previous box left behind, through
    the screen model (unchanged rows write nothing). Keeps the box's height
    for the layout (footer_height_for_layout)."""
    if rows <= 1:
        _footer_prev_h[0] = 0
        return ""

    max_box_rows = max(0, rows - 1)
    lines = lines[-max_box_rows:]
    h = len(lines)
    band = max(_footer_prev_h[0], h)
    # Rows the box no longer covers are blanked; rows it does are painted, both
    # through the shared screen model, so an unchanged box emits nothing at all
    # and a shrinking one clears exactly the rows it gave up.
    wanted: dict[int, str] = {}
    for k in range(band):
        row = rows - band + k
        if 1 <= row < rows:
            wanted[row] = ""
    for k in range(h):
        row = rows - h + k
        if 1 <= row < rows:
            wanted[row] = lines[k]
    parts: list[str] = []
    for row in sorted(wanted):
        seg = screen_row_segment(row, wanted[row])
        if seg:
            parts.append(seg)
    _footer_prev_h[0] = h
    return "".join(parts)


def footer_height_for_layout() -> int:
    """Rows the now-playing box occupies, as the frame layout should assume."""
    return max(_footer_prev_h[0], ui.footer_height())


def footer_box_segment() -> str:
    """The box draw-string for embedding in a widget's own atomic flush (so
    navigation redraws it alongside the list instead of leaving it flashed out)."""
    rows = ui.get_terminal_height()
    if rows <= 1:
        return ""
    cols = ui.get_terminal_width()
    return _footer_box_str(rows, ui.footer_lines(cols))


def invalidate_footer_box() -> None:
    """Forget the box's rows so the next idle tick repaints them. Used on
    focus-in: while a window is unfocused the terminal may not paint our box
    writes, yet the model advances as if it had."""
    rows = ui.get_terminal_height()
    screen_forget_rows(rows - _footer_prev_h[0], rows - 1)
    _footer_last_draw[0] = 0.0              # let the next poll repaint immediately


def _render_footer_bar() -> None:
    """Idle-tick refresh so the clock/progress advance (and a background track
    change lands) when nothing else redraws. Repaints when either the track
    identity or the styled rows changed, so an idle screen never flickers yet a
    new song is never missed."""
    rows = ui.get_terminal_height()
    cols = ui.get_terminal_width()
    lines = ui.footer_lines(cols)
    if len(lines) != _footer_prev_h[0]:
        # The box appeared or vanished: the menu must re-reserve rows for it.
        ui.mark_footer_layout_dirty()
    # Every tick, through the screen model: rows that already read this way
    # cost nothing, and any a screen change wiped are put back at once.
    seg = _footer_box_str(rows, lines)
    if seg:                       # unchanged rows produce nothing to write
        sys.stdout.write("\0337" + seg + "\0338")
        sys.stdout.flush()


def _wait_for_keypress(timeout: float = 0.05) -> bool:
    """Block up to `timeout` seconds for a keypress; return whether one arrived.

    Also refreshes the now-playing box at ~4 Hz so background-audio status
    stays live on every widget/menu without each one needing its own tick."""
    now = time.time()
    if now - _footer_last_draw[0] >= 0.12:
        _footer_last_draw[0] = now
        with quietly():
            _render_footer_bar()
            float_tick()
        # Keep the background-activity notice live: while a task is running the
        # status bar is re-stamped each tick so it stays up for the whole job and
        # its cyan ● pulses; one extra redraw after the last task clears the bar.
        active = ui.has_background_tasks()
        if active or _status_prev_active[0]:
            with quietly():
                render_status_bar()
        _status_prev_active[0] = active
    if _IS_WINDOWS:
        end = time.time() + timeout
        while time.time() < end:
            if msvcrt.kbhit():
                return True
            time.sleep(0.01)
        return False
    watch = [sys.stdin, _wake_r] if _wake_r >= 0 else [sys.stdin]
    ready = _sel.select(watch, [], [], timeout)[0]
    if _wake_r >= 0 and _wake_r in ready:
        try:
            os.read(_wake_r, 4096)        # drain all coalesced pulses
        except OSError:
            pass
        _footer_last_draw[0] = time.time()    # this pulse counts as the tick
        with quietly():
            _render_footer_bar()     # repaint immediately on a state change
    return sys.stdin in ready             # a wake alone is not a keypress



def _cols() -> int:
    """Usable terminal width after subtracting the horizontal margins."""
    return max(1, ui.get_terminal_width() - 2 * ui.MARGIN_H)



# --- Hint bar visibility ------------------------------------------------------
# One switch for every screen's hint bar, off until turned on (`?`, Ctrl-/ where
# `?` is typed, or a click on the corner toggle each screen shows on its top
# line), remembered between runs.
# Kept in its own small file rather than config.json: screens hold a loaded
# config and save it back later, which would quietly undo a toggle made meanwhile.
HINTS_CLICK = '\x00hints'     # the key a click on the corner toggle replays
keys.define("global", "Everywhere", [
    # Wherever it isn't typed or bound; Ctrl-/ (the same key with Ctrl) is the
    # way where it is, and works everywhere.
    ("help", ("?",), "show or hide the key hints"),
    ("help_typed", ("\x1f",), "show or hide the key hints, in a text field too"),
], within=())
_hints_on: list = [None]      # None until first read


def _hints_file():
    from backbone import app
    return app.config_dir / "hints_on"


def hints_visible() -> bool:
    """Whether hint bars are shown."""
    if _hints_on[0] is None:
        try:
            _hints_on[0] = _hints_file().exists()
        except Exception:
            _hints_on[0] = False
    return _hints_on[0]


def toggle_hints() -> None:
    """Show or hide every hint bar, and remember it."""
    _hints_on[0] = not hints_visible()
    try:
        f = _hints_file()
        if _hints_on[0]:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.touch()
        else:
            f.unlink(missing_ok=True)
    except OSError:
        pass


_toggle_shown: list = [None]  # None until first read


def _toggle_hidden_file():
    from backbone import app
    return app.config_dir / "help_toggle_hidden"


def help_toggle_shown() -> bool:
    """Whether the `[?] help` toggle is drawn. Off, `?` still shows the hints:
    you just have to know it's there."""
    if _toggle_shown[0] is None:
        try:
            _toggle_shown[0] = not _toggle_hidden_file().exists()
        except Exception:
            _toggle_shown[0] = True
    return _toggle_shown[0]


def set_help_toggle_shown(shown: bool) -> None:
    """Draw the `[?] help` toggle or not, and remember it (kept beside the
    hints switch, for the same reason)."""
    _toggle_shown[0] = shown
    try:
        f = _toggle_hidden_file()
        if shown:
            f.unlink(missing_ok=True)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.touch()
    except OSError:
        pass


def is_hints_key(key: str, key_free: bool) -> bool:
    """Whether `key` toggles the hints: a click on the corner, Ctrl-/, or `?`
    where the screen leaves `?` free (not typed or bound)."""
    return (key == HINTS_CLICK or keys.pressed(key, "global.help_typed")
            or (key_free and keys.pressed(key, "global.help")))


def help_corner_text(help_key: bool = True, shown: bool | None = None) -> tuple[str, int]:
    """The header toggle, styled, and its width: `[?] help` / `[?] hide help`,
    or `[^/] …` where `?` is typed (a text field) and Ctrl-/ is the key.
    `shown`: the hints' state to describe (default: as they are now).
    Nothing, when the toggle is switched off (help_toggle_shown)."""
    if not help_toggle_shown():
        return "", 0
    key = keys.label("global.help" if help_key else "global.help_typed", first=True) or " "
    label = "hide help" if (hints_visible() if shown is None else shown) else "help"
    return (f"{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{key}{C.RESET}{C.DIM}] {label}{C.RESET}",
            3 + len(key) + len(label))


def _toggle_variants() -> list[tuple[str, int]]:
    """Every form the toggle can take: either key, either state."""
    return [help_corner_text(k, shown) for k in (True, False) for shown in (True, False)]


def help_toggle_width() -> int:
    """The room a header keeps for the toggle: its widest form, so swapping in
    another (the hints switched, or the key that works here) never moves the row."""
    return max(w for _t, w in _toggle_variants())


def add_help_corner(line: str, row: int, cells: dict, help_key: bool = False) -> str:
    """`line` (a screen's top line, drawn on screen row `row`) with the toggle
    right-aligned on it, clipping the line if the two would meet. Only the key
    is clickable (a click replays HINTS_CLICK). `help_key`: pressing `?` toggles
    here; elsewhere `?` is typed or bound, and the toggle names Ctrl-/."""
    text, width = help_corner_text(help_key)
    if not width:                                    # the toggle is switched off
        if help_key:
            cells['__help_key__'] = True
        return line
    col = max(1, ui.get_terminal_width() - ui.MARGIN_H - width + 1)
    room = col - 2                                   # keep one blank column before it
    body = line if ui.visual_len(ui.strip_ansi(line)) <= room else _clip_ansi(line, room)
    pad = max(1, col - 1 - ui.visual_len(ui.strip_ansi(body)))
    cells[(row, col + 1)] = HINTS_CLICK              # the key inside "[…]"
    if help_key:
        cells['__help_key__'] = True                 # consume_chrome: `?` toggles here
    return f"{body}{C.RESET}{' ' * pad}{text}"


# A list draws inside a rounded box when the window has room for one; smaller
# than this, it draws as plain lines (the narrow-window rule, in one place).
_BOX_MIN_WIDTH = 9                   # a box round a column of text: boxed at any width that holds one
_BOX_MIN_HEIGHT = 10                 # a shorter window gives up the boxes first (its rows go to the rows)


def box_fits() -> bool:
    """Whether panels are boxed: at any width that holds a box, in a window
    tall enough that its borders don't cost the list its rows."""
    return ui.get_terminal_width() >= _BOX_MIN_WIDTH and ui.get_terminal_height() >= _BOX_MIN_HEIGHT


class PanelTitle:
    """A list's header that names the panel: drawn in its box's top border (the
    subtitle as its first line), or as a title line when the window is too
    short for a box. Pass one as select(header=…)."""

    def __init__(self, title: str, subtitle: str | None = None):
        self.title, self.subtitle = title, subtitle or ""

    def __call__(self) -> list[str]:                 # the unboxed form
        sub = f"  {C.DIM}{self.subtitle}{C.RESET}" if self.subtitle else ""
        return [f"  {C.BOLD}{self.title}{C.RESET}{sub}"]


def box_lines(lines: list[str], width: int, height: int, title: str = "", right: str = "",
              focused: bool = True, dotted: bool = False) -> list[str]:
    """`lines` inside a rounded box `width` columns wide and `height` rows tall
    (borders included), inset by the left margin: `title` in the top border
    (in the second accent when `focused`), `right` at its far end, each line
    clipped or padded to the room inside, blank rows below them. `dotted`: its
    edges perforated (a tool of its own, like the lyrics editor)."""
    h, v = ("┈", "┊") if dotted else ("─", "│")
    inner = max(1, width - 4)                         # "│ " + content + " │"
    pad = " " * ui.MARGIN_H
    name = f" {title} " if title else ""
    # Short of room, the title and the right end (a subtitle, the help
    # toggle) each keep up to half the border, the longer giving way first.
    avail = width - 4 - 3
    ln, lr = ui.visual_len(name), ui.visual_len(right)
    if right and ln + lr > avail:
        fit = avail - min(ln, max(avail // 2, avail - lr))
        if lr > fit:
            # The help toggle at its end stays whole (it's clicked, and found
            # there by place_help_toggle); what's before it shortens.
            toggle = next((t for t, _w in _toggle_variants() if t and right.endswith(t)), "")
            head, tw = right[:len(right) - len(toggle)], ui.visual_len(toggle)
            room_h = fit - tw
            head = (_clip_ansi(head, room_h - 2) + f"{C.DIM}… {C.RESET}") if room_h > 4 else ""
            right = head + toggle if tw <= fit else ""
    tail = f" {right} ─" if right else ""
    room = width - 4 - ui.visual_len(tail)
    if ui.visual_len(name) > room:
        name = (_clip_ansi(name, max(0, room - 2)) + "… ") if room > 2 else ""
    rule = max(1, width - 3 - ui.visual_len(name) - ui.visual_len(tail))
    shade = C.PRIMARY if focused else C.DIM
    tail = f"{C.DIM} {C.RESET}{right}{C.DIM} ─{C.RESET}" if right else ""
    if dotted and tail:
        tail = tail.replace(" ─", f" {h}")
    top = (f"{pad}{C.DIM}╭{h}{C.RESET}{shade}{C.BOLD}{name}{C.RESET}{C.DIM}{h * rule}{C.RESET}"
           f"{tail}{C.DIM}╮{C.RESET}")
    body = [_clip_ansi(ln, inner) for ln in lines[:max(0, height - 2)]]
    body += [""] * (max(0, height - 2) - len(body))
    mid = [f"{pad}{C.DIM}{v}{C.RESET} {ln}{C.RESET}{' ' * (inner - ui.visual_len(ln))} {C.DIM}{v}{C.RESET}" for ln in body]
    return [top, *mid, f"{pad}{C.DIM}╰{h * (width - 2)}╯{C.RESET}"]


def border_right(subtitle: str | None, help_key: bool | None) -> str:
    """The right end of a box's top border: a dim `subtitle`, then the hints
    toggle (`help_key` None: none; else as help_corner_text has it)."""
    parts = [f"{C.DIM}{subtitle}{C.RESET}"] if subtitle else []
    toggle = help_corner_text(help_key)[0] if help_key is not None else ""
    if toggle:
        parts.append(toggle)
    return f"{C.DIM} ─ {C.RESET}".join(parts)


def wrap_path(path: str, width: int) -> list[str]:
    """`path` in lines of at most `width` columns, broken after a slash where
    it can be (inside a name only when the name alone is too long)."""
    width = max(1, width)
    lines, line = [], ""
    for part in re.findall(r'[^/]*/|[^/]+$', path) or [""]:
        while ui.visual_len(line + part) > width:
            if line:
                lines.append(line); line = ""
            else:
                lines.append(part[:width]); part = part[width:]
        line += part
    return lines + [line] if line or not lines else lines


def path_box(path: str, title: str) -> list[str]:
    """A box the window's width holding `path` (wrapped at its slashes),
    `title` in its border: a list's header, for a file's path shown as itself
    rather than squeezed into a title."""
    rows = wrap_path(path, _cols() - 4)
    return box_lines(rows, _cols(), len(rows) + 2, title, help_corner_text()[0])


def boxed_frame(h_lines: list[str], body: list[str], title: str, box_h: int,
                help_key: bool = False, width: int | None = None) -> list[str]:
    """A list widget's frame with its body in a box under any header lines:
    each body line drops its own left margin (the border is the margin now),
    and with no header the box's top border carries the `[?] help` toggle.
    The box is `width` wide (all of it when None)."""
    right = help_corner_text(help_key)[0] if not h_lines else ""
    inner = [ln[ui.MARGIN_H:] if ln.startswith(" " * ui.MARGIN_H) else ln for ln in body]
    return h_lines + box_lines(inner, width or _cols(), box_h, title, right)


# The column browser (select(trail=…, preview=…)): one box holding the levels
# above the list as columns, oldest first, then the list; beside it the preview
# of the highlighted row (its details in one box, what it holds in another), or
# in a window too narrow for that, a strip of its details under the list.
_COL_SEP = 3                         # " │ " between columns
_COL_MAIN_MIN = (20, 36)             # the list's own column: least, and as much as it keeps before others get room
_COL_PREVIEW = (30, 40)              # the preview's width: least, and most as a share of the window (%)
_COL_TRAIL = (10, 28)                # each column of a level above
_STRIP_ROWS = 4                      # the narrow layout's strip: least
_LIST_ROWS_MIN = 5                   # the list keeps this many rows (three of its own) beside a strip
_STRIP_SPLIT = 60                    # a strip this wide shows what the row holds beside its details
_SHAPE_SAMPLE = 400                  # rows of a long list measured for its column sizes


@dataclass
class Trail:
    """A level above a list, as a column left of it: its rows' labels and
    values, the one that was opened, and the level's `depth`, which a click on
    one of its rows hands back (JumpTo)."""
    labels: list
    values: list
    current: Any
    depth: int


@dataclass
class JumpTo:
    """select()'s result for a click on a row of a trail column: go back to
    level `depth` with `value` highlighted."""
    depth: int
    value: Any


@dataclass
class Preview:
    """What select(preview=…) shows for the highlighted row: `details`, a
    callable (width, height) → lines (or a Pane, with pictures) of its picture
    and facts, which lays itself out across a wide, short space; `contents`,
    what opening the row lists, under `contents_title`; `want`, the width its
    text would like, which the preview's width follows."""
    details: Any = None
    contents_title: str = ""
    contents: list = field(default_factory=list)
    want: int = 0
    # The rows the details would like at a width (its picture as wide as the
    # box): given them first, what it holds getting the rest.
    details_rows: Any = None


def column_widths(total: int, trails: list, preview: Preview | None,
                  want: int | None = None, list_want: int | None = None) -> tuple[list, int, bool]:
    """A column browser's layout in `total` columns: (the trail levels that
    fit with their widths, newest kept, as (trail, width) pairs; the preview's
    width beside the browser, 0 for none; whether the preview is a strip under
    it instead). Each is sized to its content: a level by its labels, the
    preview by `want` (the widest any row of the list would like, so it holds
    still as you move; else this preview's), the list by `list_want` (its
    widest row). The list keeps what it needs; the preview goes under it
    before anything is dropped; levels narrow before the oldest go. Room to
    spare is shared: the preview gets what it'd like, then the two split the
    rest evenly, the preview up to its share of the window."""
    natural = (list_want + 6) if list_want else _COL_MAIN_MIN[1]       # its margin and ›
    need = max(_COL_MAIN_MIN[0], min(_COL_MAIN_MIN[1], natural))
    pw = 0
    if preview is not None and total - _COL_PREVIEW[0] - ui.MARGIN_H - 4 >= need:
        pw = _COL_PREVIEW[0]
    room = total - (pw + ui.MARGIN_H if pw else 0) - 4 - need
    shown = []
    for t in reversed(trails):
        tw = min(max(max((ui.visual_len(str(x)) for x in t.labels), default=0) + 3, _COL_TRAIL[0]), _COL_TRAIL[1])
        if tw + _COL_SEP > room:
            tw = _COL_TRAIL[0]                    # narrowed, rather than gone
        if tw + _COL_SEP > room:
            break
        shown.insert(0, (t, tw))
        room -= tw + _COL_SEP
    if pw:
        grow = min(room, max(0, (preview.want if want is None else want) + 4 - pw))
        pw, room = pw + grow, room - grow
        # The rest split evenly, a half the list doesn't need (its widest row
        # fits) going to the preview, up to the preview's share of the window.
        pw += min(max(room // 2, room - max(0, natural - need)), max(0, total * _COL_PREVIEW[1] // 100 - pw))
    return shown, pw, preview is not None and preview.details is not None and not pw


def strip_rows(box_h: int, list_rows: int, useful: int) -> int:
    """The narrow layout's strip height out of `box_h` rows: the rows the list
    (`list_rows` long) leaves, else a third, up to what the strip can use; 0
    when the list would be left fewer than _LIST_ROWS_MIN."""
    spare = box_h - (list_rows + 4)                   # the list's box: borders, a blank, a "more" line
    rows = min(max(_STRIP_ROWS, spare if spare > box_h // 3 else box_h // 3), useful)
    rows = min(max(rows, _STRIP_ROWS), box_h - _LIST_ROWS_MIN)
    return rows if rows >= _STRIP_ROWS else 0


def trail_lines(trail: Trail, width: int, height: int) -> tuple[list, int]:
    """A trail column's lines, scrolled so the opened row is in view, and the
    index of its first row (for clicks)."""
    at = next((n for n, v in enumerate(trail.values) if v == trail.current), 0)
    top = max(0, min(at - height // 2, len(trail.labels) - height))
    out = []
    for n, label in enumerate(trail.labels[top:top + height], top):
        text = ui.truncate_text(str(label), max(1, width - 2))
        out.append(ui.on_bar(f" {text}", width, C.BAR_DIM) if n == at else f" {C.DIM}{text}{C.RESET}")
    return out, top


class Pane(list):
    """A preview column's lines, with pictures to lay over them: (line, col,
    rows, key, escape, columns), line and col counted from the column's own
    first line and character, `escape` drawing a picture `rows` × `columns`
    (an iTerm2 image) with the cursor there. `key` names it: a new key redraws
    it, as does a write to a cell under it."""

    def __init__(self, lines=(), pictures=()):
        super().__init__(lines)
        self.pictures = list(pictures)


_columns_on: list = [None]  # None until first read


def _columns_off_file():
    from backbone import app
    return app.config_dir / "columns_off"


def columns_shown() -> bool:
    """Whether lists that can be a column browser are one (list.columns, v)."""
    if _columns_on[0] is None:
        try:
            _columns_on[0] = not _columns_off_file().exists()
        except Exception:
            _columns_on[0] = True
    return _columns_on[0]


def set_columns_shown(shown: bool) -> None:
    """Column browser or the list alone, and remember it."""
    _columns_on[0] = shown
    try:
        f = _columns_off_file()
        if shown:
            f.unlink(missing_ok=True)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.touch()
    except OSError:
        pass


def panel_header(header) -> tuple:
    """A list's header resolved for this frame: (PanelTitle or None, header lines).
    `header` may be lines, a callable giving lines, or a PanelTitle (or a
    callable giving one, for a title that changes). With room for a box, a
    PanelTitle goes in its border and leaves no lines; without, its own lines."""
    h = header() if callable(header) and not isinstance(header, PanelTitle) else header
    if isinstance(h, PanelTitle):
        return (h, []) if box_fits() else (None, h())
    return None, h if isinstance(h, Pane) else list(h or [])    # a Pane keeps its pictures


def rounded_header(title: str, detail: str = "", right: str = "",
                   subtitle: str | None = None) -> list[str]:
    """The app's boxed header for a file (or a set of them): bold `title`, dim
    `detail` after it (" · artist"), dim facts `right`-aligned, and the hints
    toggle inline at the far right of the same row, then an optional dim
    `subtitle` row and a blank row. When space runs out the detail is trimmed
    first, then the facts dropped, then the title trimmed; the toggle stays."""
    vl = ui.visual_len
    mh = ui.MARGIN_H
    inner = max(12, ui.get_terminal_width() - 2 * mh - 4)
    # Room for the toggle's widest form, so place_help_toggle can swap in the
    # one that fits now without the row growing (see help_toggle_width).
    toggle, tw = help_corner_text()
    wide = help_toggle_width()
    toggle, tw = " " * (wide - tw) + toggle, wide

    def _fit(text: str, n: int) -> str:
        return text if vl(text) <= n else (text[:max(0, n - 1)] + "…" if n > 1 else "")

    tail = tw + (vl(right) + 2 if right else 0)
    if right and vl(title) + tail + 1 > inner:
        right, tail = "", tw                         # no room for the facts
    avail = max(1, inner - tail - 1)
    title = _fit(title, avail)
    detail = _fit(detail, avail - vl(title)) if avail - vl(title) > 5 else ""
    left = f"{C.BOLD}{title}{C.RESET}{C.DIM}{detail}{C.RESET}"
    gap = max(1, inner - vl(title) - vl(detail) - tail)
    facts = f"{C.DIM}{right}{C.RESET}  " if right else ""
    edge = f"{' ' * mh}{C.DIM}"
    lines = [f"{edge}╭{'─' * (inner + 2)}╮{C.RESET}",
             f"{edge}│{C.RESET} {left}{' ' * gap}{facts}{toggle} {C.DIM}│{C.RESET}"]
    if subtitle:
        sub = _fit(subtitle, inner)
        lines.append(f"{edge}│{C.RESET} {C.DIM}{sub}{' ' * (inner - vl(sub))}{C.RESET} {C.DIM}│{C.RESET}")
    lines += [f"{edge}╰{'─' * (inner + 2)}╯{C.RESET}", ""]
    return lines


def place_help_toggle(out: list, first_row: int, cells: dict, help_key: bool = False) -> None:
    """Make the hints toggle on a screen clickable: a header that already
    carries it (rounded_header) gets it brought up to date where it is (the
    hints may have been switched since the header was built, and it says
    Ctrl-/ when `?` isn't free); otherwise it is added to the top line,
    `out[0]`. `out[k]` is drawn on row first_row + k."""
    if not help_toggle_shown():
        if help_key:
            cells['__help_key__'] = True             # `?` still toggles here
        return
    wide = help_toggle_width()
    now, now_w = help_corner_text(help_key)
    for k, line in enumerate(out[:4]):
        if now and now in line:                      # drawn as it is now (a box's border): just make it clickable
            plain = ui.strip_ansi(line)
            cells[(first_row + k, ui.visual_len(plain[:plain.find(ui.strip_ansi(now))]) + 2)] = HINTS_CLICK
            if help_key:
                cells['__help_key__'] = True
            return
    for k, line in enumerate(out[:4]):
        was = next((" " * (wide - w) + t for t, w in _toggle_variants() if " " * (wide - w) + t in line), None)
        if was is None:
            continue
        line = out[k] = line.replace(was, " " * (wide - now_w) + now, 1)
        plain = ui.strip_ansi(line)
        at = plain.find(ui.strip_ansi(help_corner_text(help_key)[0]))
        if at >= 0:
            cells[(first_row + k, ui.visual_len(plain[:at]) + 2)] = HINTS_CLICK
            if help_key:
                cells['__help_key__'] = True
            return
    if out:
        out[0] = add_help_corner(out[0], first_row, cells, help_key)


def _hint(*pairs, extra="", always: bool = False) -> str:
    """
    Lays out the hint bar, falling back through: one line → pyramid → grid →
    aligned stack → split stack.
    Every hint bar comes through here, so hiding them (hints_visible) is one
    check; `always` draws regardless (the player's own `[i] help` stays in its bar).
    """
    if (not pairs and not extra) or not (always or hints_visible()):
        return ""

    cols = _cols()

    # Parse items into structured tuples: (key, value, raw_string_for_math).
    # An action left without a key has nothing to show.
    parsed_items = []
    for k, v in pairs:
        if isinstance(k, keys.HintKey) and not k:
            continue
        parsed_items.append((k, v, f"[{k}] {v}"))

    if extra:
        plain_extra = ui.strip_ansi(extra).strip()
        if plain_extra:
            m = re.match(r'\[(.*?)\]\s*(.*)', plain_extra)
            if m:
                parsed_items.append((m.group(1), m.group(2), f"[{m.group(1)}] {m.group(2)}"))
            else:
                parsed_items.append(("", plain_extra, plain_extra))

    total_items = len(parsed_items)

    def render_inline(k, v):
        """Render one [key] value pair inline, dimmed with a bold key."""
        if not k: return f"{C.DIM}{v}{C.RESET}"
        return f"{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}] {v}{C.RESET}"

    # Interpunct (·): the one separator used everywhere: hint bars, player
    # details, bulk headers, multi-value fields.
    sep = f"{C.DIM} · {C.RESET}"
    raw_sep_len = len(' · ')

    # LAYOUT 1: Centred Long Line
    raw_len = sum(len(raw) for _, _, raw in parsed_items) + raw_sep_len * (total_items - 1)
    if raw_len <= cols:
        line = sep.join(render_inline(k, v) for k, v, _ in parsed_items)
        pad = max(0, cols - raw_len) // 2
        return (" " * pad) + line

    # LAYOUT 2: Upside-Down Pyramid
    def get_pyramid_distribution(n):
        """Row sizes for an upside-down pyramid: start near sqrt(2n) and shrink
        by one each row until all n items are placed."""
        rows = []
        current_row_size = math.ceil(math.sqrt(2 * n))
        while n > 0:
            take = min(current_row_size, n)
            rows.append(take)
            n -= take
            current_row_size = max(1, current_row_size - 1)
        return rows

    pyr_dist = get_pyramid_distribution(total_items)
    if len(pyr_dist) > 1 and pyr_dist[0] > pyr_dist[-1]:
        fits = True
        pyr_lines = []
        idx = 0
        for r in pyr_dist:
            row_items = parsed_items[idx:idx+r]
            r_raw_len = sum(len(raw) for _, _, raw in row_items) + raw_sep_len * (len(row_items) - 1)

            if r_raw_len > cols:
                fits = False
                break

            line = sep.join(render_inline(k, v) for k, v, _ in row_items)
            pad = max(0, cols - r_raw_len) // 2
            pyr_lines.append((" " * pad) + line)
            idx += r

        if fits:
            return "\n".join(pyr_lines)

    # LAYOUT 3: Grid (Side-by-side uniform columns)
    max_item_len = max((len(raw) for _, _, raw in parsed_items), default=0)
    gutter = 4
    col_width = max_item_len + gutter
    possible_cols = max(1, cols // col_width)

    if possible_cols >= 2:
        grid_lines = []
        for i in range(0, total_items, possible_cols):
            row = parsed_items[i:i+possible_cols]
            raw_row_len = sum(col_width for _ in row) - gutter
            formatted_parts = []
            for k, v, raw in row:
                space_padding = " " * (col_width - len(raw))
                formatted_parts.append(render_inline(k, v) + space_padding)

            row_str = "".join(formatted_parts).rstrip()
            pad = max(0, cols - raw_row_len) // 2
            grid_lines.append((" " * pad) + row_str)
        return "\n".join(grid_lines)

    # LAYOUT 4: Aligned Vertical Stack
    # Center aligned: Keys right-aligned to spine, values left-aligned from spine
    max_k_len = max((len(f"[{k}]") for k, _, _ in parsed_items if k), default=0)
    max_v_len = max((len(v) for _, v, _ in parsed_items), default=0)
    total_stack_w = max_k_len + 1 + max_v_len # key + space + value

    if total_stack_w <= cols:
        stack_lines = []
        global_pad = max(0, cols - total_stack_w) // 2

        for k, v, _ in parsed_items:
            if k:
                k_raw = f"[{k}]"
                k_space_pad = " " * (max_k_len - len(k_raw))
                left_side = f"{k_space_pad}{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}]{C.RESET}"
            else:
                left_side = " " * max_k_len

            right_side = f"{C.DIM}{v}{C.RESET}"
            stack_lines.append(f"{' ' * global_pad}{left_side} {right_side}")
        return "\n".join(stack_lines)

    # LAYOUT 5: Split Vertical Stack (narrowest fallback)
    # Key on row 1, value on row 2, dot separator between pairs.
    split_lines = []
    for i, (k, v, _) in enumerate(parsed_items):
        if k:
            k_raw = f"[{k}]"
            k_pad = max(0, cols - len(k_raw)) // 2
            split_lines.append(f"{' ' * k_pad}{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}]{C.RESET}")

        v_pad = max(0, cols - len(v)) // 2
        split_lines.append(f"{' ' * v_pad}{C.DIM}{v}{C.RESET}")

        # Add centered separator dot between discrete blocks
        if i < total_items - 1:
            dot_pad = max(0, cols - 1) // 2
            split_lines.append(f"{' ' * dot_pad}{C.DIM}⋅{C.RESET}")

    return "\n".join(split_lines)

# --- Clickable hints & now-playing box hit-testing -------------------------
# Hint keys render as ``[key] label`` with only ``key`` bold/bright; a click is
# actionable only when it lands on those bright glyphs. Multi-key labels split
# into separate buttons: a '/' between keys is a non-clickable separator, and an
# adjacent arrow cluster (``↑↓``, ``←→``) is one button per arrow. Each button
# maps to the SAME synthesised key the keyboard produces, so the widgets need no
# extra per-key logic: a click just replays that key through their normal switch.

_HINT_ARROWS = {'↑': 'UP', '↓': 'DOWN', '←': 'LEFT', '→': 'RIGHT', '⇞': 'PGUP', '⇟': 'PGDN'}
_HINT_WORDS = {
    'space': 'SPACE', 'spc': 'SPACE', 'esc': 'ESC', 'tab': 'TAB', '↵': 'ENTER',
    'pgup': 'PGUP', 'pgdn': 'PGDN', '⇧tab': 'BACKTAB', 'home': 'HOME', 'end': 'END',
}


def _hint_key_tokens(key: str) -> list[tuple[int, int, str]]:
    """Split a hint key label into clickable ``(offset, glyph_len, synth_key)``
    buttons. ``offset`` is 0-based within the key text; the '/' joiners it skips
    over are left non-clickable."""
    if isinstance(key, keys.HintKey):              # built from the keymap: it knows
        return list(key.tokens)                    # the real key behind each glyph
    segs = key.split('/') if (key and key != '/' and '/' in key) else [key]
    tokens: list[tuple[int, int, str]] = []
    off = 0
    for si, seg in enumerate(segs):
        if si > 0:
            off += 1                       # the '/' separator column (not clickable)
        if not seg:
            continue
        if all(c in _HINT_ARROWS for c in seg):     # e.g. "↑↓" → one button per arrow
            for c in seg:
                tokens.append((off, 1, _HINT_ARROWS[c]))
                off += 1
        elif seg.lower() in _HINT_WORDS:            # "space"/"esc"/"tab"/"↵"/"PgUp"…
            tokens.append((off, len(seg), _HINT_WORDS[seg.lower()]))
            off += len(seg)
        elif len(seg) == 2 and seg[0] == '^':       # "^N" → Ctrl-N control char
            tokens.append((off, 2, chr(ord(seg[1].upper()) - 64)))
            off += 2
        else:                                       # a single glyph / plain letter
            tokens.append((off, len(seg), _HINT_WORDS.get(seg.lower(), seg)))
            off += len(seg)
    return tokens


def add_hint_click_cells_auto(cells: dict, line: str, base_row: int,
                              left_inset: int = 0) -> None:
    """Like add_hint_click_cells but auto-detects ``[key]`` groups in the plain
    text (no pairs needed). Use only on lines known to be a hint bar; arbitrary
    bracketed text (e.g. a lyric ``[Chorus]``) would be picked up as a key."""
    plain = ui.display_text(line)
    for m in re.finditer(r'\[([^\[\]]+)\]', plain):
        key_col0 = m.start() + 1
        for off, glen, synth in _hint_key_tokens(m.group(1)):
            for c in range(glen):
                cells[(base_row, left_inset + key_col0 + off + c + 1)] = synth


def add_hint_click_cells(cells: dict, line: str, base_row: int, pairs,
                         left_inset: int = 0) -> None:
    """Populate ``cells`` (a ``{(row, col): synth_key}`` map) with the clickable
    bright-key glyphs found on one rendered hint ``line`` at absolute ``base_row``.
    ``pairs`` is the (key, label) sequence that produced the hint bar."""
    plain = ui.display_text(line)
    for k, _v in pairs:
        if not k:
            continue
        idx = plain.find(f"[{k}]")
        if idx < 0:
            continue
        key_col0 = idx + 1                 # 0-based index of key[0] (just past '[')
        for off, glen, synth in _hint_key_tokens(k):
            for c in range(glen):
                col = left_inset + key_col0 + off + c + 1   # 1-based screen column
                cells[(base_row, col)] = synth


def _hint_pin_target() -> int:
    """The flowed-line count after which a widget's hint bar sits pinned at the
    bottom, directly above the now-playing box and status bar, so its keys keep the
    same screen position across redraws (repeated clicks don't chase the bar)."""
    rows = ui.get_terminal_height()
    return rows - 1 - ui.top_margin() - max(ui.footer_height(), ui.MARGIN_V)


# Now-playing box transport-icon columns, derived from the one place the glyph
# layout is defined (ui.FOOTER_GLYPH_COLS) rather than restated here: the
# box is inset by MARGIN_H, then "│ " precedes the content, so a glyph at content
# offset `o` lands on 1-based column MARGIN_H + 3 + o. Each glyph claims its own
# column plus the space after it, so a click just to the right still lands.
def _footer_glyph_cols() -> list[tuple[str, int, int]]:
    """(action, first_col, last_col) for each transport glyph in the box."""
    base = ui.MARGIN_H + 3
    actions = ('playpause', 'next')
    # A glyph claims its own cells plus the space after it, so a click just to
    # the right of a narrow glyph still lands on it.
    return [(a, base + start, base + start + width)
            for a, (start, width) in zip(actions, ui.FOOTER_GLYPH_COLS)]


# What a click on the now-playing box can ask the transport handler for (see
# prompt.set_transport_handler); anywhere else in the box is 'open'.
FOOTER_ACTIONS = ('playpause', 'next', 'prev', 'time')


def footer_click_action(row: int, col: int) -> str | None:
    """Classify a click against the now-playing box: ``'prev'`` / ``'playpause'``
    / ``'next'`` on the transport glyphs, ``'time'`` on the clock when the box
    says it's clickable, ``'open'`` anywhere else in the box, or ``None`` when
    the click misses it (or no box is shown)."""
    h = _footer_prev_h[0]
    if h <= 0 or not ui.footer_active():
        return None
    rows = ui.get_terminal_height()
    top = rows - h
    if not (top <= row <= rows - 1):
        return None
    if row == top + 1:                     # the content row that carries the icons
        for action, lo, hi in _footer_glyph_cols():
            if lo <= col <= hi:
                return action
        time_cols = ui.footer_time_cols()
        if time_cols and time_cols[0] <= col <= time_cols[1]:
            return 'time'
    return 'open'


def render_status_bar():
    """Redraw the bottom status bar in place, saving/restoring the cursor so
    the text input caret doesn't move."""
    rows = ui.get_terminal_height()
    float_tick()
    if rows <= 0 or not ui.chrome_fits():             # too short a window: its rows are the screen's
        return
    status = ui.get_status_line()
    # \0337 / \0338 (via save_cursor) keep the caret where the text input left
    # it rather than jumping to the status row; an unchanged bar writes nothing.
    screen_paint({rows: status}, save_cursor=True)


class Choice:
    __slots__ = ('title', 'value', 'checked', 'disabled', 'cells', 'cursor_title')

    def __init__(self, title: str, value: object = None, checked: bool = False,
                 disabled: bool = False, cells: list | None = None,
                 cursor_title: str | None = None) -> None:
        """Build a selectable/checkable row for select(), defaulting value to title."""
        self.title        = title
        self.value        = value if value is not None else title
        self.checked      = checked
        self.disabled     = disabled  # non-selectable separator / section heading
        self.cells        = cells    # structured column data for columns= mode
        self.cursor_title = cursor_title  # alternate label shown when cursor is on this row


# The single inter-column gap for every list in the app, so columns line up the
# same way in every list. Narrow terminals are handled by column `priority` (columns drop)
# and by the dynamically computed pin gap, not by varying this.
COL_GAP = 3


class Column:
    """A column spec for a structured select() table (no string parsing).

    style    : 'primary' | 'static-dim' | 'dynamic-dim' | 'accent' | 'normal'
    align    : 'left' | 'right'
    flex     : absorbs leftover width, truncates (use for the title column)
    pin      : laid against the right edge (e.g. duration)
    max_frac : clamp column to this fraction of total width (0.0-1.0)
    gap      : leading gap before this column (defaults to `COL_GAP`, the one
               value every list uses; the pin block's separation from the left
               block is computed per render, so this is only the minimum)
    scroll   : the column whose text scrolls on the highlighted row when it's
               cut off (the first, when none says): a number or time before
               it then holds still
    priority : drop-order when the row is too narrow to show every column
               readably. None (default) = essential, never dropped. A number
               marks the column droppable; the lowest-priority droppable column
               is dropped first, so give the least important columns the lowest
               numbers (e.g. 1 = first to go).
    """
    __slots__ = ('style', 'align', 'flex', 'pin', 'min_width', 'max_width', 'max_frac',
                 'gap', 'priority', 'scroll')

    def __init__(self, style: str = 'normal', align: str = 'left', flex: bool = False,
                 pin: bool = False, min_width: int = 0, max_width: int | None = None,
                 max_frac: float | None = None, gap: int = COL_GAP,
                 priority: float | None = None, scroll: bool = False) -> None:
        """Build a column spec for a structured select() table."""
        self.style     = style
        self.align     = align
        self.flex      = flex
        self.pin       = pin
        self.min_width = min_width
        self.max_width = max_width
        self.max_frac  = max_frac
        self.gap       = gap
        self.priority  = priority
        self.scroll    = scroll


def _cell_text(cell) -> tuple[str, str | None]:
    """Return (plain_text, style_override) for a str, (str, style) tuple, or list of segments."""
    if isinstance(cell, list):
        return "".join(str(seg[0]) if isinstance(seg, tuple) else str(seg) for seg in cell), None
    if isinstance(cell, tuple):
        return str(cell[0]), (cell[1] if len(cell) > 1 else None)
    return str(cell), None


def _style_cell(text: str, style: str, is_current: bool) -> str:
    """Apply a named cell style (dim/dynamic-dim/accent/primary/normal) to text."""
    if not text:
        return ""
    if style in ('dim', 'static-dim'):
        return f"{C.DIM}{text}{C.RESET}"
    if style == 'dynamic-dim':
        return f"{C.BOLD}{C.DIM}{text}{C.RESET}" if is_current else f"{C.DIM}{text}{C.RESET}"
    if style == 'accent':
        return f"{C.ACCENT}{text}{C.RESET}"
    if style == 'primary':
        return f"{C.BOLD}{text}{C.RESET}" if is_current else text
    if style == 'cursor':
        # The block cursor as a cell segment; see block_cursor(), which does the
        # same thing where a whole line rather than a table cell is being drawn.
        return f"{C.INVERT}{C.BOLD}{text}{C.RESET}"
    return text  # 'normal'


def _render_cell_segments(cell, style: str, is_current: bool, width: int, align: str,
                          force_dim: bool = False) -> str:
    """Render a cell (plain text or list of styled segments), truncated/padded
    to `width` and aligned."""
    if isinstance(cell, list):
        parts = []
        raw_len = 0
        remaining = width
        for seg in cell:
            t, s = (str(seg[0]), seg[1]) if isinstance(seg, tuple) else (str(seg), style)
            if force_dim:
                s = 'dynamic-dim'
            if remaining <= 0:
                t = ""
            else:
                t = ui.truncate_text(t, remaining)
            seg_w = ui.visual_len(t)
            raw_len += seg_w
            remaining -= seg_w
            parts.append(_style_cell(t, s, is_current))
        text = "".join(parts)
        pad = " " * max(0, width - raw_len)
    else:
        raw_text, override = _cell_text(cell)
        raw_text = ui.truncate_text(raw_text, width)
        raw_len = ui.visual_len(raw_text)
        text = _style_cell(raw_text, override or style, is_current)
        pad = " " * max(0, width - raw_len)
    return (pad + text) if align == 'right' else (text + pad)


def _table_widths(rows_cells: list, columns: list, eff: int,
                  pointer_w: int, right_margin: int,
                  visible_cells: list | None = None) -> list[int]:
    """Compute per-column widths that fit the effective width `eff`.

    Content sets each column's natural width, scanned across *all* rows, so a
    wide entry far down the list is accounted for and the layout stays stable
    while scrolling. Natural widths are clamped by max_frac / max_width and
    floored by min_width. Then space is reconciled with the terminal:

      * blank → a droppable column with nothing to show in the *visible* window
                is dropped outright. Its natural width comes from all rows, so
                an off-screen entry would otherwise reserve a wide column that
                renders as empty space on every row you can actually see (a
                genre far down the search results, a featured artist nobody in
                view has). Width it can't use is width the title column needs;
      * drop  → if not every column can fit even at its comfortable minimum,
                drop the lowest-priority droppable column (Column.priority) and
                retry; essential columns (priority=None) are never dropped;
      * fits  → flex column(s) share the leftover evenly (each respecting its
                own max_frac / max_width cap **and its own content**) so pinned
                columns sit flush right. A flex column never grows past what it
                has to show: padding a left column out to the full width just
                buries the row's right-hand block behind a field of blanks. Any
                surplus is left unallocated and the renderer spends it as the
                single gap between the left block and the pinned block;
      * tight → the widest kept column gives up space first, one unit at a time,
                never below a readable floor, so narrow columns (durations,
                counts) stay intact and only the widest columns truncate.

    `visible_cells` is the window of rows actually on screen (defaults to
    `rows_cells`); only the blank pass uses it, so widths stay scroll-stable.

    Returns a width per column; a dropped column's width is -1 (skipped by the
    renderer, which also drops its gap).
    """
    ncol = len(columns)
    content = [0] * ncol
    for cells in rows_cells:
        for i in range(min(ncol, len(cells))):
            content[i] = max(content[i], ui.visual_len(_cell_text(cells[i])[0]))

    # What each column actually has to show in the window on screen.
    shown = [0] * ncol
    for cells in (rows_cells if visible_cells is None else visible_cells):
        for i in range(min(ncol, len(cells))):
            shown[i] = max(shown[i], ui.visual_len(_cell_text(cells[i])[0]))

    def _cap(col) -> int | None:
        """The hard upper bound a column may reach (max_frac / max_width), or None."""
        cap = int(eff * col.max_frac) if col.max_frac is not None else None
        if col.max_width is not None:
            cap = col.max_width if cap is None else min(cap, col.max_width)
        return cap

    # Natural (capped, min-floored) width each column would like.
    natural = [0] * ncol
    for i, col in enumerate(columns):
        w = content[i]
        cap = _cap(col)
        if cap is not None:
            w = min(w, cap)
        natural[i] = max(w, col.min_width)

    def _comfort(i: int) -> int:
        """Smallest width column i still reads at (its content, if that's smaller)."""
        return max(columns[i].min_width, min(natural[i], _MIN_COL_FLOOR))

    def _overhead(ks: list) -> int:
        """Fixed, non-content width for a kept set: pointer, gaps, margin, pin gap."""
        pin = any(columns[i].pin for i in ks)
        return (pointer_w + right_margin + sum(columns[i].gap for i in ks)
                + (_MIN_PIN_GAP if pin else 0))

    kept = list(range(ncol))

    # Blank pass: a droppable column with nothing to show in the visible window
    # reserves width that renders as empty space on every row on screen. Drop it
    # and give the space to the columns that do have something to say; it comes
    # back when you scroll to rows that fill it. Never drops the last column.
    blank = [i for i in kept
             if columns[i].priority is not None and shown[i] == 0 and columns[i].min_width == 0]
    if len(blank) < len(kept):
        for i in blank:
            kept.remove(i)

    # Drop pass: while even everyone's comfortable minimum can't fit, shed the
    # lowest-priority droppable column (ties: the rightmost goes first).
    while len(kept) > 1 and _overhead(kept) + sum(_comfort(i) for i in kept) > eff:
        droppable = [i for i in kept if columns[i].priority is not None]
        if not droppable:
            break
        victim = min(droppable, key=lambda i: (columns[i].priority, -i))
        kept.remove(victim)

    widths = [-1] * ncol                 # -1 = dropped (renderer skips it and its gap)
    for i in kept:
        widths[i] = natural[i]

    flex_idxs = [i for i in kept if columns[i].flex]
    has_pin = any(columns[i].pin for i in kept)
    gaps = sum(columns[i].gap for i in kept)
    budget = eff - pointer_w - gaps - right_margin
    if has_pin:
        budget -= _MIN_PIN_GAP           # reserve the left/right inter-block gap
    budget = max(0, budget)

    total = sum(widths[i] for i in kept)
    if total < budget and flex_idxs:
        # Surplus: round-robin one unit at a time into the flex columns, each
        # stopping at its own cap *or its own content*, whichever comes first:
        # growing a column past what it has to show only pads it with blanks and
        # pushes the pinned block away from the text it belongs to. Leftover is
        # deliberately unspent: _render_table_row turns it into the one gap
        # between the left block and the right-pinned block.
        surplus = budget - total
        caps = {}
        for i in flex_idxs:
            cap_i = _cap(columns[i])
            need_i = max(content[i], columns[i].min_width)
            caps[i] = need_i if cap_i is None else min(cap_i, need_i)
        progressed = True
        while surplus > 0 and progressed:
            progressed = False
            for i in flex_idxs:
                if surplus == 0:
                    break
                cap_i = caps[i]
                if cap_i is None or widths[i] < cap_i:
                    widths[i] += 1
                    surplus -= 1
                    progressed = True
    elif total > budget:
        # Over budget: shave the widest kept column repeatedly until it fits,
        # never below its floor (min_width, a readable minimum, or its own
        # content if that is already smaller). The readable minimum eases toward
        # the fair per-column share when a many-column row is genuinely cramped,
        # so the layout still fits. n and the deficit are both small.
        floor_cap = min(_MIN_COL_FLOOR, max(1, budget // len(kept)))
        floors = {i: min(widths[i], max(columns[i].min_width, floor_cap)) for i in kept}
        deficit = total - budget
        while deficit > 0:
            widest = -1
            for i in kept:
                if widths[i] > floors[i] and (widest < 0 or widths[i] > widths[widest]):
                    widest = i
            if widest < 0:
                break                    # everything at its floor; clip guard handles the rest
            widths[widest] -= 1
            deficit -= 1
    return widths


def _render_table_row(cells: list, columns: list, is_current: bool,
                      widths: list[int], eff: int, right_margin: int,
                      is_checked: bool | None = None,
                      disabled: bool = False, dim: bool = False) -> str:
    """Render one table row, laying out left-aligned and right-pinned columns
    and applying pointer/check/disabled styling.

    `dim` greys a row that is selectable but not in focus, used by the sectioned
    search to quiet every section except the one the cursor is in. Unlike
    `disabled` it keeps the normal row prefix, so columns stay aligned with the
    focused section above and below it.
    """
    if disabled:
        # Match enabled non-current prefix exactly so columns stay aligned.
        if is_checked is not None:
            left = f"    {C.DIM}•{C.RESET}"   # 4 spaces + dim bullet = same as "    •"
        else:
            left = "   "                        # 3 spaces, same as single-select non-current
        for i, col in enumerate(columns):
            if not col.pin and widths[i] >= 0:
                left += " " * col.gap + _render_cell_segments(
                    cells[i] if i < len(cells) else "", 'dynamic-dim', False, widths[i], col.align,
                    force_dim=True)
        right = ""
        for i, col in enumerate(columns):
            if col.pin and widths[i] >= 0:
                right += " " * col.gap + _render_cell_segments(
                    cells[i] if i < len(cells) else "", 'dynamic-dim', False, widths[i], col.align,
                    force_dim=True)
        if right:
            gap = max(2, (eff - right_margin) - ui.visual_len(left) - ui.visual_len(right))
            return left + " " * gap + right + " " * right_margin
        return left

    pointer = " "                       # the highlighted row is a bar (select), not a pointer
    if is_checked is None:
        left = f"  {pointer}"
    else:
        glyph = f"{C.GREEN}✔{C.RESET}" if is_checked else f"{C.DIM}•{C.RESET}"
        left = f"  {pointer} {glyph}"
    for i, col in enumerate(columns):
        if not col.pin and widths[i] >= 0:
            left += " " * col.gap + _render_cell_segments(
                cells[i] if i < len(cells) else "", col.style, is_current, widths[i], col.align,
                force_dim=dim)

    right = ""
    for i, col in enumerate(columns):
        if col.pin and widths[i] >= 0:
            right += " " * col.gap + _render_cell_segments(
                cells[i] if i < len(cells) else "", col.style, is_current, widths[i], col.align,
                force_dim=dim)

    if right:
        gap = max(2, (eff - right_margin) - ui.visual_len(left) - ui.visual_len(right))
        return left + " " * gap + right + " " * right_margin
    return left


def edit_line(buf: list, pos: int, key: str) -> int | None:
    """Apply one line-editing key to `buf` (a list of characters) in place
    (typing, space, backspace, delete, ←/→, Home/End) and return the caret's new
    position, or None when `key` isn't one of them. The one editor every text
    field uses, so a key works the same in each."""
    if key == 'BACKSPACE':
        if pos > 0:
            del buf[pos - 1]; pos -= 1
    elif key == 'DELETE':
        if pos < len(buf):
            del buf[pos]
    elif key == 'LEFT':
        pos = max(0, pos - 1)
    elif key == 'RIGHT':
        pos = min(len(buf), pos + 1)
    elif key == 'HOME':
        pos = 0
    elif key == 'END':
        pos = len(buf)
    elif key == 'SPACE' or (len(key) == 1 and key.isprintable()):
        buf.insert(pos, ' ' if key == 'SPACE' else key); pos += 1
    else:
        return None
    return pos


def block_cursor(text: str, pos: int, base: str = '') -> str:
    """`text` with a white block cursor sitting *on* the character at `pos`.

    Reverse video on the character itself, never a bar drawn between two of
    them: a drawn bar occupies a column of its own, so every keystroke and every
    arrow press shifts the rest of the line sideways under the reader's eye.
    Past the end of the text the block sits on a space: the one place it does
    add a column, and there is nothing to its right to shift.

    `base` is re-asserted after the block so a caller's row styling survives the
    RESET that closes it.
    """
    close = f"{C.RESET}{base}"
    if pos >= len(text):
        return f"{text}{C.BACK}█{close}"
    return f"{text[:pos]}{C.INVERT}{C.BOLD}{text[pos]}{close}{text[pos + 1:]}"


def block_cursor_width(text: str, pos: int) -> int:
    """Columns `block_cursor` will occupy: one more than the text at its end."""
    return len(text) + (1 if pos >= len(text) else 0)


def separator(title: str = "") -> Choice:
    """A non-selectable heading/divider row for grouping a select() list."""
    return Choice(title, value=None, disabled=True)



def _clip_ansi(s: str, width: int) -> str:
    """Truncate a string to `width` visible columns, preserving ANSI escape
    sequences (they don't count toward width). Guarantees the line never wraps."""
    return ui.clip_ansi(s, width)



def _norm(choices: list) -> list:
    """Normalize a mixed list of Choice/str/dict/choice-like objects into Choice instances."""
    out = []
    for c in choices:
        if isinstance(c, Choice):
            out.append(c)
        elif isinstance(c, str):
            out.append(Choice(c, c))
        elif isinstance(c, dict):
            out.append(Choice(
                title    = c.get('name', c.get('title', str(c))),
                value    = c.get('value', c.get('name', str(c))),
                checked  = c.get('checked', False),
                disabled = c.get('disabled', False),
            ))
        elif hasattr(c, 'title') and hasattr(c, 'value'):
            out.append(Choice(c.title, c.value, getattr(c, 'checked', False),
                              getattr(c, 'disabled', False)))
        else:
            s = str(c)
            out.append(Choice(s, s))
    return out


def _read_key(fd: int) -> str:
    """Read one key, discarding focus-out events. Focus-in is passed on: the
    terminal may not have painted us while unfocused, so every widget repaints
    on it (consume_chrome answers it with a full redraw)."""
    while True:
        key = _read_key_raw(fd)
        if key != 'FOCUS_OUT':
            return key


# How long to wait for the rest of an escape sequence before deciding the Esc
# was pressed on its own. A real sequence's bytes arrive in the same burst, so
# this only ever elapses for a genuine bare Esc; small enough that Esc still
# feels instant, large enough to survive a slow link.
_ESC_SEQ_TIMEOUT = 0.05


def _byte_ready(fd: int, timeout: float) -> bool:
    """True if another byte can be read from `fd` within `timeout` seconds."""
    try:
        return bool(_sel.select([fd], [], [], timeout)[0])
    except (OSError, ValueError):
        return False


# Function keys: F1-F4 as ESC O P..S (or ESC [ 1;… P..S with a modifier),
# F1-F12 as ESC [ n ~ (11-14 on some terminals for F1-F4).
_SS3_FKEYS = {'P': 'F1', 'Q': 'F2', 'R': 'F3', 'S': 'F4'}
_CSI_FKEYS = {'11': 'F1', '12': 'F2', '13': 'F3', '14': 'F4', '15': 'F5', '17': 'F6', '18': 'F7',
              '19': 'F8', '20': 'F9', '21': 'F10', '23': 'F11', '24': 'F12'}


def _read_key_raw(fd: int) -> str:
    """Read and decode one raw keypress, including escape sequences and mouse
    events, into a named key string."""
    if _IS_WINDOWS:
        ch = msvcrt.getwch()
        if ch in ('\x00', '\xe0'):
            ext = msvcrt.getwch()
            return {
                'H': 'UP', 'P': 'DOWN', 'K': 'LEFT', 'M': 'RIGHT',
                'G': 'HOME', 'O': 'END', 'I': 'PGUP', 'Q': 'PGDN', 'S': 'DELETE',
                'R': 'INSERT',
                **{chr(59 + n): f'F{n + 1}' for n in range(10)},   # F1-F10: ; < = … D
                '\x85': 'F11', '\x86': 'F12',
            }.get(ext, '')
        if ch == '\r': return 'ENTER'
        if ch == '\x08': return 'BACKSPACE'
        if ch == '\x03': return 'CTRL_C'
        if ch == '\t': return 'TAB'
        if ch == ' ': return 'SPACE'
        return ch

    ch = os.read(fd, 1)
    if ch == b'\x1b':
        try:
            # A lone Esc is just this byte; an arrow/function key sends more in
            # the same burst. Raw mode's read blocks while nothing is pending, so
            # peek first, or Esc looks dead until the *next* keypress
            # arrives to unblock the read, and that keypress is then swallowed
            # as part of the sequence, so Esc would only work on a second press.
            if not _byte_ready(fd, _ESC_SEQ_TIMEOUT):
                return 'ESC'
            ch2 = os.read(fd, 1)
            if ch2 == b'O':
                # SS3: some terminals send ESC O A for the arrows while in
                # application-cursor mode.
                ss3 = os.read(fd, 1).decode('utf-8', errors='replace')
                return {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                        'H': 'HOME', 'F': 'END', **_SS3_FKEYS}.get(ss3, 'ESC')
            if ch2 == b'[':
                ch3 = os.read(fd, 1)
                seq = ch3.decode('utf-8', errors='replace')
                if seq == '<':
                    # SGR mouse event: \033[<btn;col;row{M|m}
                    buf = ''
                    while len(buf) < 24:
                        # Same guard as the bare Esc above: a truncated mouse
                        # report would otherwise block the whole UI until the
                        # next keypress arrived.
                        if not _byte_ready(fd, _ESC_SEQ_TIMEOUT):
                            return 'ESC'
                        c = os.read(fd, 1).decode('utf-8', errors='replace')
                        if c in ('M', 'm'):
                            parts = buf.split(';')
                            if len(parts) == 3:
                                try:
                                    btn, col, row = int(parts[0]), int(parts[1]), int(parts[2])
                                    if c == 'm':
                                        return f'MOUSE_RELEASE:{btn}:{row}:{col}'
                                    if btn == 64: return 'SCROLL_UP'
                                    if btn == 65: return 'SCROLL_DOWN'
                                    if btn in (0, 1, 2): return f'MOUSE_CLICK:{btn}:{row}:{col}'
                                except ValueError:
                                    pass
                            return 'ESC'
                        buf += c
                    return 'ESC'
                if seq.isdigit():
                    # ESC [ <number> ~ : page/home/end/delete/insert. Read the
                    # whole number, or PgUp and PgDn would come through as 'ESC'.
                    num, term = seq, ''
                    while len(num) < 4 and _byte_ready(fd, _ESC_SEQ_TIMEOUT):
                        c = os.read(fd, 1).decode('utf-8', errors='replace')
                        if c.isdigit():
                            num += c
                            continue
                        term = c
                        break
                    if term == ';':
                        # Modified form (ESC [ 1;5A = Ctrl-Up): drain to the
                        # final letter and treat it as the unmodified key.
                        while _byte_ready(fd, _ESC_SEQ_TIMEOUT):
                            c = os.read(fd, 1).decode('utf-8', errors='replace')
                            if c.isalpha():
                                term = c
                                break
                    if term.isalpha():
                        return {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                                'H': 'HOME', 'F': 'END', **_SS3_FKEYS}.get(term, 'ESC')
                    return {'1': 'HOME', '2': 'INSERT', '3': 'DELETE', '4': 'END',
                            '5': 'PGUP', '6': 'PGDN', '7': 'HOME', '8': 'END',
                            **_CSI_FKEYS}.get(num, 'ESC')
                mapped = {
                    'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                    'H': 'HOME', 'F': 'END',
                    'Z': 'BACKTAB',                       # Shift+Tab
                    'I': 'FOCUS_IN', 'O': 'FOCUS_OUT',
                }.get(seq, 'ESC')
                if mapped in ('FOCUS_IN', 'FOCUS_OUT'):
                    ui.set_window_focused(mapped == 'FOCUS_IN')
                if mapped == 'FOCUS_IN':
                    # Regained focus: force the now-playing box to repaint (it may
                    # be stale from a background change while we were unfocused).
                    invalidate_footer_box()
                return mapped
            return 'ESC'
        except (OSError, EOFError):
            return 'ESC'
    # A non-ASCII character (e.g. an accented letter) is 2-4 bytes in UTF-8, but
    # os.read(fd, 1) only grabbed the lead byte. Pull the continuation bytes so
    # the whole codepoint decodes to one character instead of several U+FFFD.
    b0 = ch[0]
    if b0 >= 0x80:
        if   b0 >= 0xF0: n_cont = 3
        elif b0 >= 0xE0: n_cont = 2
        elif b0 >= 0xC0: n_cont = 1
        else:            n_cont = 0   # stray continuation byte; nothing to gather
        for _ in range(n_cont):
            ch += os.read(fd, 1)
    decoded = ch.decode('utf-8', errors='replace')
    if decoded in ('\r', '\n'): return 'ENTER'
    if decoded in ('\x7f', '\x08'): return 'BACKSPACE'
    if decoded == ' ':    return 'SPACE'
    if decoded == '\x03': return 'CTRL_C'
    if decoded == '\t':   return 'TAB'
    return decoded

def _visible_rows() -> int:
    """Total lines a list widget may emit: the full terminal height minus the
    status bar (1) and the top+bottom vertical margins. Callers subtract their
    OWN chrome (header, message, indicators, hints); do not double-count it
    here, or lists show a premature "N more"."""
    _, rows = ui.get_terminal_size()
    # Reserve the status-bar row, plus the now-playing box's rows whenever
    # background audio is active, so lists never collide with it.
    reserve = 1 + ui.footer_height()
    return max(4, rows - reserve - ui.top_margin() - ui.MARGIN_V)


def _rows() -> int:
    """Terminal height in rows."""
    return ui.get_terminal_height()


def _hint_lines(*pairs, extra="") -> list[str]:
    """The hint bar rendered as a list of lines rather than one newline-joined string."""
    return _hint(*pairs, extra=extra).splitlines()


def _wrap_bordered_input_lines(text: str, content_width: int) -> list[str]:
    """Word-wrap text to `content_width`, preserving blank lines as empty entries."""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        if raw_line == "":
            lines.append("")
        else:
            wrapped = textwrap.wrap(raw_line, width=content_width, drop_whitespace=False) or [""]
            lines.extend(wrapped)
    return lines


class _Widget:
    """
    Paints a widget's lines from row 1 through the shared screen model. The
    first frame takes the screen over without a clear; after a resize
    (anchor_reset) it clears and repaints.
    """

    def __init__(self, fd: int) -> None:
        """No frame painted yet."""
        self.fd      = fd
        self.row     = None   # anchor row, 1-based
        self.last_h  = 0
        self._full   = False  # whether we own the full screen
        # Pictures laid over the frame (Pane.pictures): (row, col, rows, key,
        # escape) on screen, and the ones last drawn.
        self.pictures: list = []

    def anchor_reset(self) -> None:
        """Called on resize (or after another view owned the screen): clears and
        redraws from scratch next render."""
        self.row   = None
        self._full = True

    def render(self, lines: list) -> None:
        """Paint `lines` from row 1, diffed against what is already on screen.

        Only rows whose content changed are written, in one buffered frame with
        no newlines and no erase-to-end-of-screen, so the frame can't be flushed
        half-drawn, and the rows this widget doesn't own (the now-playing box, the
        status bar) are left exactly as they are instead of being wiped and
        restamped on every keystroke.
        """
        mv   = ui.MARGIN_V
        top  = ui.top_margin()
        rows = ui.get_terminal_height()

        # Wrap content with vertical margins: mv blank rows on top, mv reserved
        # rows before the status bar at the bottom. The box's band is excluded so
        # the two writers never own the same row (a shrinking box would otherwise
        # blank rows this diff believes it still owns).
        padded: list[str] = [''] * top + list(lines)
        limit = rows - 1 - max(mv, footer_height_for_layout())
        padded = padded[:max(0, limit)]

        if self._full or self.row is None:
            if self._full and _screen:
                # A real clear only for a resize (or after another view owned the
                # screen): the terminal reflowed, so nothing on it can be trusted.
                sys.stdout.write("\033[H\033[2J" + C.HIDE)
                screen_invalidate()
            else:
                # First frame of a new widget: take the screen over in the paint
                # itself, so moving between screens never shows a blank one.
                screen_takeover_next()
            self.row   = 1
            self._full = False

        frame: dict[int, str] = {i + 1: line for i, line in enumerate(padded)}
        # Blank any rows a previous, taller frame left behind.
        for i in range(len(padded), self.last_h):
            frame[i + 1] = ""
        self.last_h = len(padded)

        # The status bar and the box join the same frame, so everything lands in
        # one flush, but each row still only costs anything if it changed.
        frame[rows] = ui.get_status_line()
        frame = _takeover_rows(frame)
        # A picture that changed or went (this screen's, or the one before
        # it, which this one took over): its cells repaint, which wipes it.
        drawn = [(p[0], p[1], p[2], p[3], *p[5:]) for p in self.pictures]
        shown = [(p[0], p[1], p[2], p[3], *p[5:]) for p in _shown_pictures]
        if drawn != shown:
            for r, c, n, _k, *size in shown + drawn:
                if size:
                    screen_forget_cells(r, r + n - 1, c, c + size[0] - 1)
                else:
                    screen_forget_rows(r, r + n - 1)
        parts = [C.HIDE]
        painted = set()
        for row in sorted(frame):
            seg = screen_row_segment(row, frame[row])
            if seg:
                painted.add(row)
            parts.append(seg)
        # Then the pictures, where cells under them were just written (that
        # erases them) or they changed.
        for r, c, n, _k, esc, *size in self.pictures:
            last = c + (size[0] if size else 10 ** 6) - 1
            if any(screen_painted(row, c, last) for row in range(r, r + n) if row in painted):
                parts.append(f"\0337\033[{r};{c}H{esc}\0338")
        _shown_pictures[:] = self.pictures
        parts.append(footer_box_segment())
        out = "".join(p for p in parts if p)
        if out != C.HIDE:
            sys.stdout.write(out)
            sys.stdout.flush()

    def clear(self) -> None:
        """Clear the screen and reset anchor state, cursor still hidden.

        The cursor is only ever shown for a text caret, or by
        `ui.exit_alt_screen()` when the app hands the terminal back.
        """
        sys.stdout.write("\033[H\033[3J\033[J" + C.HIDE)
        sys.stdout.flush()
        screen_invalidate()
        self.last_h = 0
        self.row    = None
        self._full  = True


_register_screen_hooks()
ui.set_tab_keys_shown(lambda: hints_visible())


hint = _hint   # the public name for the hint bar


def run_dashboard(render, interval: float = 1.0, quit_action: str = "list.quit", on_quit=None,
                  poll: float = 0.05, on_key=None) -> None:
    """Runs a live, tick-driven view through a _Widget, so it resizes and
    paints like every other widget here. Returns when a key of the
    `quit_action` binding (see backbone.keys) is pressed.

    `render()` takes no arguments and returns the whole frame as a list of
    lines, each carrying its own left-margin indent (see header_box()). It
    runs once every `interval` seconds; keypresses and resizes are checked
    every `poll` seconds regardless, and a resize renders at once.

    `on_key(key)`, if given, is called for any other keypress (e.g. "s" for
    a settings screen). It may open select()/text()/confirm() itself; the
    view is cleared and redrawn when it returns.

    `on_quit()`, if given, runs after the terminal is restored (cursor
    back, raw mode undone) - the place for a "stop the background work
    too?" confirm().

    When stdin is not a terminal, keys can't be read: it renders on a plain
    time.sleep(interval) loop and never checks for the quit key, so the
    caller has to be stopped from outside.
    """
    fd = sys.stdin.fileno()
    is_tty = sys.stdin.isatty()
    old_settings = _get_term_attrs(fd) if is_tty else None
    if is_tty:
        _set_raw(fd)

    w = _Widget(fd)
    screen_takeover_next()
    last_render = 0.0
    try:
        while True:
            resized = ui.consume_resize()
            if resized:
                ui.clear_screen()
                w.anchor_reset()
            now = time.monotonic()
            if resized or now - last_render >= interval:
                w.render(render())
                last_render = now
            if is_tty:
                if _wait_for_keypress(poll):
                    key = _read_key(fd)
                    if keys.pressed(key, quit_action):
                        break
                    if on_key is not None:
                        on_key(key)
                        ui.clear_screen()
                        w.anchor_reset()
            else:
                time.sleep(interval)
    finally:
        if is_tty:
            _restore_term_attrs(fd, old_settings)
        sys.stdout.write("\033[?25h\n")

    if on_quit is not None:
        on_quit()


def _demo() -> None:
    """Self-check for the pure (non-interactive) logic in this module, the
    parts that run without a terminal. Doesn't touch raw mode, key reading,
    or screen painting (those need a real tty).
    Run directly: `python3 -m backbone.prompt.core`.
    """
    assert _norm(["a", "b"])[0].title == "a"
    assert _norm([{"name": "x", "value": 1}])[0].value == 1
    c = Choice("t", value=5, checked=True)
    assert c.value == 5 and c.checked

    cols = [Column(flex=True, min_width=4), Column(pin=True, min_width=3)]
    rows = [["short", "1"], ["a much longer title here", "22"]]
    widths = _table_widths(rows, cols, eff=40, pointer_w=4, right_margin=0)
    assert all(w >= 0 for w in widths), widths

    assert block_cursor_width("abc", 1) == 3
    assert block_cursor_width("abc", 5) == 4

    tokens = _hint_key_tokens("↑↓")
    assert [t[2] for t in tokens] == ["UP", "DOWN"]
    assert _hint_key_tokens("^N") == [(0, 2, "\x0e")]

    print("backbone.prompt.core self-check OK")


if __name__ == "__main__":
    _demo()
