"""Text entry: one line, a path, and a longer text of several lines (in a
box of its own, or the system editor)."""
from __future__ import annotations
import sys
import os
import tempfile
import subprocess
from backbone.prompt.core import (
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress, render_status_bar,
    block_cursor, block_cursor_width, _read_key, _cols, _wrap_bordered_input_lines,
    screen_paint, screen_invalidate, screen_takeover_next,
    box_lines, screen_restore, screen_save, screen_span_paint, box_fits, help_corner_text,
)
from backbone import keys, ui
from backbone.prompt import chrome
from backbone.prompt.chrome import CHROME_HANDLED, CHROME_REDRAW, MODE_TOGGLE, boxed_chrome, consume_chrome, disable_mouse, enable_mouse
from backbone.prompt.core import C
from backbone.prompt.core import _byte_ready, _hint_pin_target, edit_line


def text(message: str, default: str = "", suggest=None, check=None, allow=None,
         placeholder: str = "") -> str | None:
    """Free-text line editor with cursor movement and wrapping.

    `suggest(text)` → [(value, note)]: completions listed under the field as
    you type; ↑ ↓ pick one, Tab takes it (else the first), Enter on a picked
    one takes it too. `check(text)` → a problem (shown under the field;
    Enter won't take the text till it's put right) or None. `allow(text)`:
    whether typing may make the text this; a key that wouldn't is ignored
    (deleting always works). `placeholder`: an example shown dim in the
    empty field.

    Enter returns the buffer; Esc or Ctrl-C cancels (None); ^t returns
    MODE_TOGGLE when the toggle is live."""
    buf    = list(default)
    pos    = len(buf)
    picked = [-1]                    # the suggestion ↑ ↓ are on, or -1
    tried  = [False]                 # Enter was pressed on a text with a problem: say so louder
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    result = None
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    # Track how many physical lines were drawn to clear them later
    prev_lines = 0

    def _render():
        nonlocal prev_lines
        cols = _cols()
        content = "".join(buf)
        # Frame = '  '(mh) + '│ ' + content + ' │'; content = cols-4 makes the
        # frame span exactly the inter-margin width for an even 2/2 margin.
        content_width = max(1, cols - 4)

        wrapped_lines = _wrap_bordered_input_lines(content, content_width)
        pre_lines = _wrap_bordered_input_lines(content[:pos], content_width)
        cursor_row = max(0, len(pre_lines) - 1)
        cursor_col = len(pre_lines[-1]) if pre_lines else 0

        # This prompt owns the screen (it full-clears on entry), so it is laid
        # out absolutely like every other widget: message, input frame, then the
        # hint bar pinned above the now-playing box. The caret is drawn as a block on
        # the character, like every other field: a real terminal caret is at the
        # mercy of the terminal's own cursor style, and could be a thin bar or
        # invisible where the block always reads.
        # No "(^t widget)" suffix on the message: ^t is in the hint bar below,
        # like every other key. The title says what the screen is, not how to
        # leave it.
        body = []
        rows = (wrapped_lines if cursor_row < len(wrapped_lines)
                else wrapped_lines + [""])          # caret parked on a fresh wrap
        for i, line in enumerate(rows):
            body.append("  " + (block_cursor(line, cursor_col) if i == cursor_row else line))
        if placeholder and not content:
            body[0] = "  " + block_cursor(ui.truncate_text(placeholder, content_width), 0, base=C.DIM) + C.RESET
        problem = check(content) if check and content.strip() else None
        if problem:
            body.append(f"  {C.RED if tried[0] else C.DIM}✗ {problem}{C.RESET}")
        options = _options()
        if options:
            body.append("")
            width = max(ui.visual_len(v) for v, _n in options) + 3
            for k, (value, note) in enumerate(options):
                row = f"{value}{' ' * (width - ui.visual_len(value))}{C.DIM}{note}{C.RESET}"
                body.append("  " + (ui.on_bar(row, content_width) if k == picked[0] else row))

        pairs = [("↵", "save"), ("esc", "back")]
        if options:
            pairs = [("↑↓", "pick"), ("tab", "complete")] + pairs
        if chrome._value_toggle_enabled:
            pairs.append((keys.label("global.raw_text"), chrome._toggle_hint_label))
        # The field in a box its own size (growing as the text wraps), a blank
        # box under it to the hints holding any problem and the suggestions.
        field, below = body[:len(rows)], body[len(rows):]
        while below and not below[0]:
            below.pop(0)
        room = _hint_pin_target() - len(chrome.chrome_hint_lines(pairs)) - (len(field) + 2)
        if box_fits() and room >= 3:
            field_box = box_lines([ln[ui.MARGIN_H:] for ln in field], _cols(), len(field) + 2,
                                  message.strip().rstrip(":"), help_corner_text(False)[0])
            out, _dx = boxed_chrome(below, "", pairs, _hint_cells, header=field_box)
        else:
            out, _dx = boxed_chrome(body, message, pairs, _hint_cells)

        # One diffed frame (no full erase, no newlines): only the rows that
        # actually changed are written, so typing doesn't repaint the screen.
        frame = {i + 1: line for i, line in enumerate([""] * ui.top_margin() + out)}
        for r in range(len(frame) + 1, prev_lines + ui.top_margin() + 1):
            frame[r] = ""
        screen_paint(frame)          # the caret is drawn, not the terminal's own

        prev_lines = len(out)
        render_status_bar()

    def _options() -> list:
        """The completions for the text as it is (none without `suggest`)."""
        if not suggest:
            return []
        try:
            return list(suggest("".join(buf)))[:_SUGGESTIONS]
        except Exception:                                # a suggester's fault mustn't lose the field
            return []

    def _allowed(t: str) -> bool:
        try:
            return bool(allow(t))
        except Exception:                                # a filter's fault mustn't lock the field
            return True

    def _take(k: int) -> None:
        nonlocal buf, pos
        options = _options()
        if options:
            buf = list(options[max(0, k)][0])
            pos = len(buf)
        picked[0] = -1

    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()
        while True:
            if ui.consume_resize():
                ui.clear_screen()
                _render()
                continue
            if not _wait_for_keypress(0.05): continue
            key = _read_key(fd)

            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _render(); continue
            if _ch is not None:
                key = _ch

            if chrome.is_mode_toggle(key):
                chrome._toggle_carry = "".join(buf)
                return MODE_TOGGLE  # type: ignore[return-value]
            if   key == 'CTRL_C':             result = None;         break
            elif key == 'ESC':                result = None;         break
            elif key == 'ENTER' and picked[0] >= 0:
                _take(picked[0]); _render()
            elif key == 'ENTER':
                if check and "".join(buf).strip() and check("".join(buf)):
                    tried[0] = True; _render()               # not till it's right
                    continue
                result = "".join(buf); break
            elif key == 'TAB' and suggest:
                _take(picked[0]); _render()
            elif key in ('UP', 'DOWN') and _options():
                # Round the suggestions and back to the field (-1).
                n = len(_options())
                picked[0] = (picked[0] + 1 + (1 if key == 'DOWN' else -1)) % (n + 1) - 1
                _render()
            elif key == 'UP':
                pos = max(0, pos - _cols()); _render()
            elif key == 'DOWN':
                pos = min(len(buf), pos + _cols()); _render()
            else:
                was = list(buf)
                new_pos = edit_line(buf, pos, key)
                if new_pos is None:
                    continue
                if allow and len(buf) > len(was) and not _allowed("".join(buf)):
                    buf[:] = was                         # a key that can't lead anywhere: nothing happens
                    continue
                pos = new_pos; picked[0] = -1; tried[0] = False; _render()
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        ui.clear_screen()
        sys.stdout.write(C.HIDE)   # caret was ours; don't leave it blinking
        sys.stdout.flush()

    return result


_SUGGESTIONS = 8                 # completions a text field lists at most

_OVERLAY_W = 90                  # the overlay's widest; narrower windows get it less their margins


def _overlay_place(h: int, widest: int) -> dict:
    """Where a box `h` rows tall (borders included) and up to `widest` wide
    goes over the middle of the screen, and what it covers (saved, to put
    back). It takes the screen over at any size: inside the margins while
    they leave room, else edge to edge; as many rows as there are (h: the
    rows it gets); a single row (h: 1, no box) when there's no room for one."""
    cols, rows = ui.get_terminal_size()
    room = cols - 4 * ui.MARGIN_H if cols - 4 * ui.MARGIN_H >= 24 else cols
    w = max(1, min(room, widest))
    h = min(h, rows) if rows >= 3 and w >= 10 else 1
    if h == 1:
        w = cols
    top, left = max(1, (rows - h) // 2 + 1), max(1, (cols - w) // 2 + 1)
    return dict(top=top, left=left, w=w, h=h, saved=screen_save(top, top + h - 1, left, left + w - 1))


def token_completions(text: str, names: dict, open: str = "%", close: str = "%") -> list[tuple[str, str]]:
    """Completions for a token being typed at the end of `text`, for
    text(suggest=): `names` maps each name to a note, a name is written
    open + name + close."""
    i = text.rfind(open)
    typed = text[i + len(open):] if i >= 0 else ""
    if i < 0 or close in typed or (open == close and text.count(open) % 2 == 0):
        return []
    return [(text[:i] + open + n + close, note) for n, note in names.items() if n.startswith(typed.lower())]


def overlay_text(message: str, default: str = "", hint: str = "↵ run · esc",
                 placeholder: str = "") -> str | None:
    """A one-line text field in a small box over the middle of the screen,
    `message` its title, `hint` in its border: the screen under it stays, and
    is put back cell by cell when it closes. It takes the screen over at any
    size (a bare field on one row when there's no room for a box). The empty
    field says `placeholder` in dim, or the title where that doesn't show whole. Enter returns the text; Esc or Ctrl-C, None."""
    buf, pos = list(default), len(default)
    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    place: dict = {}

    def _open() -> None:
        place.update(_overlay_place(3, _OVERLAY_W))

    def _render() -> None:
        boxed = place['h'] == 3
        prefix = "" if boxed else f"{C.DIM}:{C.RESET} "
        inner = place['w'] - (4 if boxed else ui.visual_len(prefix) + 1)
        text = "".join(buf)
        start = max(0, pos - inner + 1)             # scrolled to keep the caret in view
        shown = block_cursor(text[start:start + inner], pos - start)
        if boxed:
            lines = [ln[ui.MARGIN_H:] for ln in box_lines([shown], place['w'], 3, message, f"{C.DIM}{hint}{C.RESET}")]
            titled = message.strip() in ui.strip_ansi(lines[0])
        else:
            titled = False
        if not text and (placeholder or not titled):  # an example, or what it's for when the title's cut
            shown = block_cursor(ui.truncate_text(placeholder or message, max(1, inner)), 0, base=C.DIM) + C.RESET
            if boxed:
                lines = [ln[ui.MARGIN_H:] for ln in box_lines([shown], place['w'], 3, message, f"{C.DIM}{hint}{C.RESET}")]
        if not boxed:                               # no room for a box: the field across one row
            lines = [prefix + shown + " " * max(0, inner - ui.visual_len(shown))]
        sys.stdout.write("".join(screen_span_paint(place['top'] + k, place['left'], ln)
                                 for k, ln in enumerate(lines)))
        sys.stdout.flush()

    result = None
    try:
        _set_raw(fd)
        _open()
        _render()
        while True:
            if ui.consume_resize():
                sys.stdout.write(screen_restore(place['saved']))
                _open()
                _render()
            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            if key == 'ENTER':
                result = "".join(buf)
                break
            if key in ('ESC', 'CTRL_C'):
                break
            new_pos = edit_line(buf, pos, key)
            if new_pos is not None:
                pos = new_pos
                _render()
    finally:
        sys.stdout.write(screen_restore(place['saved']) if place else "")
        sys.stdout.flush()
        _restore_term_attrs(fd, old)
    return result


def overlay_checklist(title: str, rows: list, checked: set, actions: list = ()) -> tuple[set, object] | None:
    """A small list in a box over the middle of the screen, as overlay_text's:
    `rows` (Choices; a disabled one is shown dim and passed over) each ticked
    or not (`checked`, their values), then any `actions` (Choices) under a
    rule. ↑ ↓ move; Space or Enter ticks a row, Enter on an action picks it;
    Esc closes. Returns (the values ticked, the action picked or None); the
    screen under it is put back cell by cell."""
    checked = set(checked)
    items = list(rows) + list(actions)
    live = [i for i, c in enumerate(items) if not c.disabled]
    if not live:
        return checked, None
    at = live[0]
    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    place: dict = {}
    hint = f"{C.DIM}space tick · ↵ · esc{C.RESET}"

    def _lines() -> list:
        out = []
        for i, c in enumerate(items):
            if i == len(rows) and actions:
                out.append(f"{C.DIM}{'─' * max(0, place['w'] - 4)}{C.RESET}")
            if i < len(rows):
                mark = f"{C.GREEN}✔{C.RESET}" if c.value in checked else f"{C.DIM}•{C.RESET}"
                text = f"{mark} {c.title}"
            else:
                text = f"  {c.title}"
            if c.disabled:
                text = f"{C.DIM}{ui.strip_ansi(text)}{C.RESET}"
            out.append(ui.on_bar(text, place['w'] - 4) if i == at else text)
        return out

    def _open() -> None:
        place.update(_overlay_place(len(items) + (1 if actions else 0) + 2,
                                    max(24, max(ui.visual_len(str(c.title)) for c in items) + 12)))

    def _render() -> None:
        body = _lines()
        if place['h'] == 1:                      # no room for a box: the row the cursor's on
            lines = [ui.clip_ansi(f"{C.DIM}{title}:{C.RESET} " + ui.strip_ansi(body[min(at + (at >= len(rows) and bool(actions)), len(body) - 1)]), place['w'])]
        else:
            room = place['h'] - 2
            line_at = at + (1 if actions and at >= len(rows) else 0)
            first = max(0, min(line_at - room + 1, len(body) - room)) if line_at >= room else 0
            lines = [ln[ui.MARGIN_H:] for ln in box_lines(body[first:first + room], place['w'], place['h'], title, hint)]
        lines = [ln + " " * max(0, place['w'] - ui.visual_len(ln)) for ln in lines]
        sys.stdout.write("".join(screen_span_paint(place['top'] + k, place['left'], ln)
                                 for k, ln in enumerate(lines)))
        sys.stdout.flush()

    picked = None
    try:
        _set_raw(fd)
        _open()
        _render()
        while True:
            if ui.consume_resize():
                sys.stdout.write(screen_restore(place['saved']))
                _open()
                _render()
            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            if key in ('ESC', 'CTRL_C'):
                break
            if key in ('UP', 'DOWN'):
                at = live[(live.index(at) + (-1 if key == 'UP' else 1)) % len(live)]
            elif key in ('SPACE', 'ENTER') and at < len(rows):
                checked ^= {items[at].value}
            elif key == 'ENTER':
                picked = items[at].value
                break
            else:
                continue
            _render()
    finally:
        sys.stdout.write(screen_restore(place['saved']) if place else "")
        sys.stdout.flush()
        _restore_term_attrs(fd, old)
    return checked, picked


def path(message: str, default: str = "") -> str | None:
    """Filesystem-path editor with Tab-cycling autocomplete against the current directory listing."""
    buf          = list(default)
    pos          = len(buf)
    fd           = sys.stdin.fileno()
    old          = _get_term_attrs(fd)
    result       = None
    _tab_matches : list = []
    _tab_index   = 0

    # Rows the previous frame used, so leftover tooltip rows are blanked.
    _last_rendered_lines = 1
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _completions(current: str) -> list:
        """List non-hidden entries in `current`'s directory matching its basename stub, for Tab completion."""
        try:
            expanded = os.path.expanduser(current)
            # Find the root lookup folder depending on whether the path target is a valid directory
            base     = expanded if os.path.isdir(expanded) else os.path.dirname(expanded) or "."
            stub     = "" if os.path.isdir(expanded) else os.path.basename(expanded)
            return sorted(
                os.path.join(base, e)
                for e in os.listdir(base)
                if e.startswith(stub) and not e.startswith('.')
            )
        except OSError:
            return []

    def _render():
        nonlocal _last_rendered_lines
        cols    = _cols()
        content = "".join(buf)
        # The field is the box's inside: cols-4, for "│ " and " │".
        max_w   = max(1, cols - 4)

        if pos >= max_w:
            # Scrolled: keep one column spare so the block has somewhere to sit
            # rather than straddling the frame's right border.
            display  = content[pos - max_w + 1: pos]
            disp_pos = len(display)
        else:
            display  = content[:max_w]
            disp_pos = pos

        cursor_col = disp_pos
        shown = block_cursor(display, disp_pos)

        render_stream = [f"  {shown}"]
        lines_count = 1

        # Do not output autocomplete options when at a subdirectory juncture or when empty
        should_show_hints = content and not content.endswith('/') and not content.endswith(os.path.sep)

        visible_matches = []
        if should_show_hints and _tab_matches:
            stub = os.path.basename(content)
            for m in _tab_matches:
                name = os.path.basename(m.rstrip('/'))
                if name.startswith(stub):
                    visible_matches.append(m)

        if visible_matches:
            render_stream.append("\r\n\033[K")
            lines_count += 1

            start_pad = min(cursor_col, max(0, max_w - 35))
            render_stream.append("  " + " " * start_pad)

            tooltip_parts = []
            current_len = start_pad

            for idx, match in enumerate(visible_matches[:5]):
                name = os.path.basename(match.rstrip('/'))
                if os.path.isdir(match):
                    name += "/"

                if idx == (_tab_index % len(visible_matches)):
                    item_str = f"{C.INVERT}{C.BOLD}{name}{C.RESET}"
                    visible_len = len(name)
                else:
                    item_str = f"{C.DIM}{name}{C.RESET}"
                    visible_len = len(name)

                if current_len + visible_len + 2 > max_w:
                    render_stream.append("  ".join(tooltip_parts) + "\r\n\033[K" + "  " + " " * start_pad)
                    lines_count += 1
                    tooltip_parts = [item_str]
                    current_len = start_pad + visible_len
                else:
                    tooltip_parts.append(item_str)
                    current_len += visible_len + 2

            if tooltip_parts:
                render_stream.append("  ".join(tooltip_parts))

            if len(visible_matches) > 5:
                render_stream.append(f" {C.DIM}(+{len(visible_matches)-5}){C.RESET}")

        _prev_rendered = _last_rendered_lines
        _last_rendered_lines = lines_count

        # Laid out absolutely, like every other screen: the completion tooltip
        # still rides just under the frame, but the hint bar is pinned above the
        # now-playing box instead of trailing whatever the tooltip left behind.
        body = [ln.replace("\033[K", "") for ln in "".join(render_stream).split("\r\n")]
        pairs = [("↵", "save"), ("tab/⇧tab", "complete"), ("esc", "back")]
        out, _dx = boxed_chrome(body, message, pairs, _hint_cells)

        # One diffed frame; see text() above.
        frame = {i + 1: line for i, line in enumerate([""] * ui.top_margin() + out)}
        for r in range(len(frame) + 1, _prev_rendered + ui.top_margin() + 2):
            frame[r] = ""
        screen_paint(frame)          # the caret is drawn, not the terminal's own
        render_status_bar()

    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _tab_matches = _completions("".join(buf))
        _render()

        while True:
            if ui.consume_resize(): _render()
            if not _wait_for_keypress(0.05): continue
            key = _read_key(fd)

            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _render(); continue
            if _ch is not None:
                key = _ch

            if key == 'CTRL_C':
                result = None; break
            elif key == 'ESC':
                result = None; break
            elif key == 'ENTER':
                result = "".join(buf); break

            elif key in ('TAB', 'BACKTAB'):
                current_text = "".join(buf)
                stub = os.path.basename(current_text) if (current_text and not current_text.endswith('/')) else ""

                visible_matches = [m for m in _tab_matches if os.path.basename(m.rstrip('/')).startswith(stub)] if stub else _tab_matches

                if visible_matches:
                    if key == 'BACKTAB':
                        _tab_index -= 2      # step back past the one just offered
                    completed = visible_matches[_tab_index % len(visible_matches)]
                    if os.path.isdir(completed) and not completed.endswith("/"):
                        completed += "/"

                    buf[:] = list(completed)
                    pos = len(buf)
                    _tab_index += 1

                    _tab_matches = _completions("".join(buf))
                _render()
                continue

            else:
                before = list(buf)
                new_pos = edit_line(buf, pos, key)
                if new_pos is not None:
                    pos = new_pos
                    if buf != before:              # the text changed: fresh completions
                        _tab_matches = _completions("".join(buf))
                        _tab_index = 0
                    _render()

    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        ui.clear_screen()
        sys.stdout.write(C.HIDE)   # caret was ours; don't leave it blinking
        sys.stdout.flush()

    return result


keys.define("multiline", "Text of several lines", [
    ("save", ("\x13",), "save"),
    ("find", ("\x06",), "find"),
    ("replace", ("\x12",), "find and replace"),
    ("external", ("\x05",), "open it in the system editor ($EDITOR)"),
])
# In the find box: plain text or a pattern (a regular expression), and
# replacing every match at once.
keys.define("multiline_find", "Text of several lines: find", [
    ("pattern", ("\x14",), "plain text / pattern (regular expression)"),
    ("all", ("\x01",), "replace every match"),
], within=("multiline",))

_MATCH = "\033[4m" + C.BAR                   # a match: underlined, on the bar colour
_CURRENT = C.INVERT                          # the match the caret is on


def _wrap_rows(lines: list[str], width: int) -> list[tuple[int, int, int]]:
    """Where each line breaks to fit `width` columns (wide characters counted
    as two): after the last space that fits, or mid-word for a word longer
    than a row. (line, start, end) per row on screen, every line at least one
    row."""
    out = []
    for n, line in enumerate(lines):
        start, used = 0, 0
        for i, ch in enumerate(line):
            w = ui.char_cols(ch)
            if used + w > width and i > start:
                space = line.rfind(" ", start, i)
                cut = space + 1 if space >= start and ch != " " else i
                out.append((n, start, cut))
                start, used = cut, ui.visual_len(line[cut:i])
            used += w
        out.append((n, start, len(line)))
    return out


def _row_of(rows: list, line: int, pos: int) -> int:
    """The screen row (index into `rows`) holding position `pos` of `line`:
    at a break, the row it starts."""
    for k, (n, a, b) in enumerate(rows):
        if n == line and a <= pos and (pos < b or pos == b and (k + 1 == len(rows) or rows[k + 1][0] != n)):
            return k
    return 0


def _pos_at(rows: list, k: int, cols: int, lines: list[str]) -> tuple[int, int]:
    """The (line, position) nearest `cols` columns into screen row `k`."""
    n, a, b = rows[k]
    used, i = 0, a
    while i < b and used + ui.char_cols(lines[n][i]) <= cols:
        used += ui.char_cols(lines[n][i])
        i += 1
    return n, i


def _text_width() -> int:
    """Columns a row of the text and its line number get: the box's inside,
    less one for the caret when it sits past a full row's end."""
    return max(4, _cols() - 5)                            # "│ " … " │" and the caret


def _find_pattern(query: str, regex: bool):
    """`query` compiled for finding: as typed (a regular expression) or
    literally, ignoring case unless it has a capital. Raises re.error for a
    pattern that won't compile."""
    import re
    flags = 0 if any(c.isupper() for c in query) else re.IGNORECASE
    return re.compile(query if regex else re.escape(query), flags)


def _matches(lines: list[str], pattern) -> list[tuple[int, int, int]]:
    """Every non-empty match, (line, start, end), in order. Within a line:
    a pattern doesn't reach across a line's end."""
    return [(n, m.start(), m.end()) for n, line in enumerate(lines)
            for m in pattern.finditer(line) if m.end() > m.start()]


def _styled(text: str, a: int, marks: dict, caret: int | None) -> str:
    """A row of the text (it starts at position `a` of its line) with its
    matches marked (`marks`: position → style) and the caret drawn on it."""
    out, cur = [], ""
    for i, ch in enumerate(text):
        st = C.INVERT + C.BOLD if a + i == caret else marks.get(a + i, "")
        if st != cur:
            out.append(C.RESET + st)
            cur = st
        out.append(ch)
    if caret is not None and caret >= a + len(text):
        out.append(f"{C.RESET}{C.BACK}█")
        cur = "x"
    return "".join(out) + (C.RESET if cur else "")


def multiline(message: str, default: str = "") -> str | None:
    """A text of several lines, edited in a box under `message`, numbered down
    its left: Enter starts a new line, the arrows (and a click) move through
    it, long lines wrap. Ctrl-F finds (Ctrl-R finds and replaces) in a box at
    the foot of it; Ctrl-S saves; Esc leaves, asking first when it was
    changed; Ctrl-E carries on in the system editor. The text, or None when
    cancelled or left empty."""
    import re
    lines = default.split("\n") if default else [""]
    line, pos, goal = len(lines) - 1, len(lines[-1]), None
    top = 0                                   # the first screen row shown
    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    cells: dict = {}
    place: dict = {}
    prev_h = [0]
    # The find box: None while closed. Its fields (what to find, and in
    # replace mode what with) and their carets; which field is being typed
    # in; plain text or a pattern; what it said last (a bad pattern).
    find: dict | None = None
    last = {'q': "", 'rep': "", 'regex': False}
    L = keys.label

    def _pairs() -> list:
        if find is None:
            return [(L("multiline.save"), "save"), ("esc", "back"), (L("multiline.find"), "find"),
                    (L("multiline.replace"), "replace"), (L("multiline.external"), "system editor")]
        pairs = [("↵ ↓", "next"), ("↑", "previous"),
                 (L("multiline_find.pattern"), "plain text" if find['regex'] else "pattern")]
        if find['mode'] == "replace":
            pairs += [("tab", "find / with"), (L("multiline_find.all"), "replace all")]
        return pairs + [("esc", "close")]

    def _found() -> tuple[list, str]:
        """The matches, and what the box says of them."""
        if find is None or not find['q']:
            return [], ""
        try:
            hits = _matches(lines, _find_pattern("".join(find['q']), find['regex']))
        except re.error:
            return [], "bad pattern"
        if not hits:
            return hits, "no matches"
        at = next((k for k, h in enumerate(hits) if h[:2] == (line, pos)), None)
        return hits, f"{at + 1} of {len(hits)}" if at is not None else f"{len(hits)} found"

    def _render() -> None:
        nonlocal top
        pairs = _pairs()
        gw = len(str(len(lines)))
        rows = _wrap_rows(lines, _text_width() - gw - 1)
        box = [] if find is None else _find_box(_cols() - 4)
        height = max(1, max(3, _hint_pin_target() - len(chrome.chrome_hint_lines(pairs))) - 2 - len(box))
        at = _row_of(rows, line, pos)
        top = min(max(top, at - height + 1), at, max(0, len(rows) - height))
        hits, _said = _found()
        marks: dict = {}
        for n, a, b in hits:
            for i in range(a, b):
                marks[(n, i)] = _CURRENT if (n, a) == (line, pos) else _MATCH
        body = []
        for k in range(top, min(len(rows), top + height)):
            n, a, b = rows[k]
            num = f"{C.DIM}{n + 1:>{gw}}{C.RESET} " if a == 0 else " " * (gw + 1)
            row_marks = {i: st for (m, i), st in marks.items() if m == n}
            body.append("  " + num + _styled(lines[n][a:b], a, row_marks, pos if k == at else None))
        body += [""] * (height - len(body)) + box
        title = message if len(lines) < 2 else f"{message} · line {line + 1} of {len(lines)}"
        out, dx = boxed_chrome(body, title, pairs, cells)
        place.update(rows=rows, first=ui.top_margin() + 2, left=ui.MARGIN_H + dx + 1 + gw + 1)
        frame = {i + 1: ln for i, ln in enumerate([""] * ui.top_margin() + out)}
        for r in range(len(frame) + 1, prev_h[0] + 1):
            frame[r] = ""
        prev_h[0] = len(frame)
        screen_paint(frame)
        render_status_bar()

    def _find_box(width: int) -> list[str]:
        """The find box's lines, as wide as the text's box is inside."""
        _hits, said = _found()
        fields = [("Find", 'q')] + ([("With", 'rep')] if find['mode'] == "replace" else [])
        shown = []
        for k, (name, f) in enumerate(fields):
            value = "".join(find[f])
            shown.append(f"{C.DIM}{name:<5}{C.RESET}"
                         + (block_cursor(value, find[f + '_pos']) if k == find['field'] else value))
        title = ("Find and replace" if find['mode'] == "replace" else "Find") + (" · pattern" if find['regex'] else "")
        return ["  " + ln[ui.MARGIN_H:] for ln in box_lines(shown, width, len(shown) + 2, title,
                                                             f"{C.DIM}{said}{C.RESET}" if said else "")]

    def _go(step: int) -> None:
        """To the next match after the caret (step 1), the one before it (-1),
        or the first at or after it (0); round from the end to the start."""
        nonlocal line, pos
        hits, _said = _found()
        if not hits:
            return
        here = (line, pos)
        if step < 0:
            before = [h for h in hits if h[:2] < here]
            line, pos = (before or hits)[-1][:2]
        else:
            after = [h for h in hits if (h[:2] > here if step else h[:2] >= here)]
            line, pos = (after or hits)[0][:2]

    def _replace(every: bool) -> None:
        """Replace the match the caret is on (then on to the next), or every
        match; in a pattern, \\1 and \\g<name> in the replacement are its groups."""
        nonlocal pos
        try:
            pattern = _find_pattern("".join(find['q']), find['regex'])
        except re.error:
            return
        rep = "".join(find['rep'])
        with_ = (lambda m: m.expand(rep)) if find['regex'] else (lambda m: rep)
        try:
            if every:
                count = 0
                for n, text_ in enumerate(lines):
                    def _one(m):
                        nonlocal count
                        if m.end() == m.start():
                            return m.group(0)
                        count += 1
                        return with_(m)
                    lines[n] = pattern.sub(_one, text_)
                pos = min(pos, len(lines[line]))
                ui.show_status(f"Replaced {ui.plural(count, 'match')}." if count else "Nothing to replace.")
                return
            m = pattern.search(lines[line], pos)
            if m and m.start() == pos and m.end() > pos:
                new = with_(m)
                lines[line] = lines[line][:pos] + new + lines[line][m.end():]
                pos += len(new)
        except (re.error, IndexError):
            ui.show_status("That replacement refers to a group the pattern doesn't have.")
            return
        _go(0)

    def _find_key(key: str) -> bool:
        """A key while the find box is open; False when it's not the box's."""
        nonlocal find
        act = keys.action(key, "multiline_find") or keys.action(key, "multiline")
        f = 'rep' if find['field'] else 'q'
        if key == "ESC":
            last.update(q="".join(find['q']), rep="".join(find['rep']), regex=find['regex'])
            find = None
        elif act in ("multiline.find", "multiline.replace"):
            find['mode'] = "replace" if act == "multiline.replace" else "find"
            find['field'] = 0
        elif act == "multiline_find.pattern":
            find['regex'] = not find['regex']
            _go(0)
        elif act == "multiline_find.all" and find['mode'] == "replace":
            _replace(True)
        elif key in ("TAB", "BACKTAB") and find['mode'] == "replace":
            find['field'] = 1 - find['field']
        elif key == "ENTER" and find['field']:
            _replace(False)
        elif key in ("ENTER", "DOWN"):
            _go(1)
        elif key == "UP":
            _go(-1)
        else:
            new = edit_line(find[f], find[f + '_pos'], key)
            if new is None:
                return False
            find[f + '_pos'] = new
            if f == 'q':
                line_, pos_ = find['origin']
                _go_from(line_, pos_)
        return True

    def _go_from(line_: int, pos_: int) -> None:
        """As typed: to the first match at or after where finding began."""
        nonlocal line, pos
        here = (line, pos)
        line, pos = line_, pos_
        _go(0)
        if not _found()[0]:
            line, pos = here

    def _open_find(mode: str) -> None:
        nonlocal find
        q, rep = list(last['q']), list(last['rep'])
        find = {'mode': mode, 'q': q, 'q_pos': len(q), 'rep': rep, 'rep_pos': len(rep),
                'field': 0, 'regex': last['regex'], 'origin': (line, pos)}

    def _key(key: str) -> str | None:
        """One key: None to carry on, or 'save' / 'leave'."""
        nonlocal line, pos, goal
        act = keys.action(key, "multiline")
        if act == "multiline.save":
            return "save"
        if act in ("multiline.find", "multiline.replace") and find is None:
            _open_find("replace" if act == "multiline.replace" else "find")
            _go(0)
            return None
        if key.startswith("MOUSE_CLICK:"):
            goal = None
            _b, r, c = (int(x) for x in key.split(":")[1:4])
            rows = place.get('rows') or []
            k = top + r - place.get('first', 0)
            if 0 <= k < len(rows) and c >= place['left']:
                line, pos = _pos_at(rows, k, c - place['left'], lines)
            return None
        if find is not None and _find_key(key):
            return None
        if key in ("ESC", "CTRL_C"):
            return "leave"
        rows = place.get('rows') or _wrap_rows(lines, _text_width())
        at = _row_of(rows, line, pos)
        vertical = key in ("UP", "DOWN", "PGUP", "PGDN", "SCROLL_UP", "SCROLL_DOWN")
        if vertical:
            if goal is None:
                n, a, _b = rows[at]
                goal = ui.visual_len(lines[n][a:pos])
            step = {"UP": -1, "DOWN": 1, "SCROLL_UP": -3, "SCROLL_DOWN": 3}.get(
                key, (-1 if key == "PGUP" else 1) * max(1, _hint_pin_target() - 4))
            line, pos = _pos_at(rows, max(0, min(len(rows) - 1, at + step)), goal, lines)
            return None
        goal = None
        if key == "ENTER":
            lines[line:line + 1] = [lines[line][:pos], lines[line][pos:]]
            line, pos = line + 1, 0
        elif key == "BACKSPACE" and pos == 0 and line > 0:
            pos = len(lines[line - 1])
            lines[line - 1:line + 1] = [lines[line - 1] + lines[line]]
            line -= 1
        elif key == "DELETE" and pos == len(lines[line]) and line + 1 < len(lines):
            lines[line:line + 2] = [lines[line] + lines[line + 1]]
        elif key == "LEFT" and pos == 0 and line > 0:
            line, pos = line - 1, len(lines[line - 1])
        elif key == "RIGHT" and pos == len(lines[line]) and line + 1 < len(lines):
            line, pos = line + 1, 0
        else:
            buf = list(lines[line])
            new = edit_line(buf, pos, key)
            if new is not None:
                lines[line], pos = "".join(buf), new
        return None

    result = None
    try:
        _set_raw(fd)
        enable_mouse()
        screen_takeover_next()
        _render()
        while True:
            if ui.consume_resize():
                ui.clear_screen()
                _render()
                continue
            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            _ch = consume_chrome(key, cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _render(); continue
            if _ch is not None:
                key = _ch
            if keys.action(key, "multiline") == "multiline.external":
                edited = system_editor_edit("\n".join(lines))
                _set_raw(fd)
                enable_mouse()
                if edited is not None:
                    lines = edited.split("\n")
                    line, pos = len(lines) - 1, len(lines[-1])
                ui.clear_screen()
                _render()
                continue
            done = _key(key)
            while done is None and _byte_ready(fd, 0):      # a paste: all of it, then one draw
                done = _key(_read_key(fd))
            text_ = "\n".join(lines).strip()
            if done == "save":
                result = text_ or None
                break
            if done == "leave":
                if text_ == default.strip():
                    break
                from backbone.prompt.lists import confirm
                if confirm("Leave without saving your changes?"):
                    break
                _set_raw(fd)
                enable_mouse()
                screen_takeover_next()
            _render()
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        ui.clear_screen()
        sys.stdout.write(C.HIDE)
        sys.stdout.flush()
    return result


_EDITOR_FALLBACKS = ['micro', 'nano', 'vim', 'vi', 'emacs']


def _find_editor() -> str | None:
    """Return the editor to use: $EDITOR if set and found, else first available fallback."""
    import shutil
    env_editor = os.environ.get('EDITOR', '').strip()
    if env_editor:
        cmd = env_editor.split()[0]
        if shutil.which(cmd):
            return env_editor
    for ed in _EDITOR_FALLBACKS:
        if shutil.which(ed):
            return ed
    return None


def system_editor_edit(initial_text: str) -> str | None:
    """Open system editor for long text."""
    with tempfile.NamedTemporaryFile(suffix=".txt", mode='w+', encoding='utf-8', delete=False) as tf:
        tf.write(initial_text)
        temp_path = tf.name
    try:
        editor = _find_editor() or 'nano'
        sys.stdout.write("\033[?7h"); sys.stdout.flush()     # the editor expects wrapping on
        try:
            subprocess.run(editor.split() + [temp_path], check=True)
        finally:
            sys.stdout.write("\033[?7l")                     # ours again (see enter_alt_screen)
        # The editor owned the screen and left its own cursor visible: forget what
        # we thought was on screen and hide the cursor again before the caller
        # repaints, so no caret is left blinking over our frame.
        screen_invalidate()
        sys.stdout.write(C.HIDE)
        sys.stdout.flush()
        with open(temp_path, 'r', encoding='utf-8') as f:
            result = f.read().strip()
        return result if result else None
    except (OSError, subprocess.CalledProcessError) as e:
        ui.show_error(f"couldn't open the editor: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
