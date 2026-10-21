"""list_edit: the multi-column row editor (people, timestamps, pasted or imported rows)."""
from __future__ import annotations
import os
import re
import sys
import textwrap
from backbone.prompt.core import (
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress, _read_key, _cols, _Widget,
    FOOTER_ACTIONS, footer_click_action, screen_takeover_next, box_fits, Column,
    _render_cell_segments, _table_widths, edit_segments,
)
from backbone import keys, ui
from backbone import datetime_parse as dtp
from backbone.nav import QuitToTerminal
from backbone.prompt import chrome
from backbone.prompt.chrome import (
    CHROME_HANDLED, CHROME_REDRAW, boxed_chrome, chrome_room, consume_chrome, disable_mouse, enable_mouse,
    MODE_TOGGLE, move_hint,
)
from backbone.prompt.lists import confirm
from backbone.prompt.text import multiline, path
from backbone.prompt.core import C
from backbone.prompt.core import caret_spans, caret_window, edit_line, framed_col


# ---------------------------------------------------------------------------
# Timestamp cells: a split date/time field inside a list_edit table.
#
# The cell is a fixed mask.  Every slot that is still a placeholder letter shows
# dim, so the shape of what you are filling in is always on screen and only the
# digits have to be typed.  One caret walks the whole mask and steps over the
# separators, which is what makes the arrow keys carry from the end of one part
# straight into the next instead of needing Tab between them.
# ---------------------------------------------------------------------------
_TS_MASK  = "YYYY-MM-DD HH:MM:SS"
_TS_SLOTS = tuple(i for i, ch in enumerate(_TS_MASK) if ch.isalpha())


# The table editor's own keys; moving, saving (choose), back and quit are the
# shared list keys.
keys.define("table", "Table editors", [
    ("add", ("a",), "add a row"),
    ("edit", ("e",), "edit the row"),
    ("delete", ("d", "BACKSPACE", "DELETE"), "delete the row"),
    ("import_text", ("i",), "import rows typed in an editor"),
    ("import_file", ("f",), "import rows from a file"),
], within=("list", "global"))


def _ts_write(buf: list, start_slot: int, digits: str) -> None:
    """Write `digits` into consecutive mask slots from `start_slot`."""
    for k, ch in enumerate(digits):
        idx = start_slot + k
        if idx < len(_TS_SLOTS):
            buf[_TS_SLOTS[idx]] = ch


def _ts_buffer(value: str) -> list:
    """A mask buffer seeded from an existing cell value (blank where unknown)."""
    buf = list(_TS_MASK)
    parsed = dtp.parse_datetime(value) if value else None
    if parsed and parsed.date:
        d = parsed.date
        if parsed.precision == 'year':
            _ts_write(buf, 0, f"{d.year:04d}")
        elif parsed.precision == 'month':
            _ts_write(buf, 0, f"{d.year:04d}{d.month:02d}")
        else:
            _ts_write(buf, 0, f"{d.year:04d}{d.month:02d}{d.day:02d}")
            if parsed.time:
                _ts_write(buf, 8, parsed.time.replace(':', ''))
    return buf


def _ts_digits(buf: list) -> str:
    """The 14 mask slots as digits, with a space wherever one is still unfilled."""
    return ''.join(buf[i] if buf[i].isdigit() else ' ' for i in _TS_SLOTS)


def _ts_value(buf: list) -> str:
    """Assemble the cell's value at whatever precision has actually been filled.

    Leaving the time blank yields a plain date, which is a valid timestamp in its
    own right: the field never forces a time it was not given.
    """
    d = _ts_digits(buf)
    if ' ' in d[0:4]:
        return ''
    if ' ' in d[4:6]:
        return d[0:4]
    if ' ' in d[6:8]:
        return f"{d[0:4]}-{d[4:6]}"
    date = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    if ' ' in d[8:12]:
        return date
    if ' ' in d[12:14]:
        return f"{date} {d[8:10]}:{d[10:12]}"
    return f"{date} {d[8:10]}:{d[10:12]}:{d[12:14]}"


def _ts_step(pos: int, delta: int) -> int:
    """Move the caret one editable slot, hopping the separators between parts."""
    slots = _TS_SLOTS
    if pos in slots:
        k = slots.index(pos)
    else:                                   # sitting on a separator: snap inward
        k = 0 if delta > 0 else len(slots) - 1
    k = max(0, min(len(slots) - 1, k + delta))
    return slots[k]


def _ts_segments(buf: list, pos: int = -1) -> list:
    """A timestamp cell as styled segments: the digits entered, the mask's
    unfilled slots dim, so the shape being filled in is always on screen;
    the caret on slot `pos` (-1: none)."""
    return [(ch, 'cursor' if i == pos else ('normal' if ch.isdigit() else 'dim')) for i, ch in enumerate(buf)]


def _row_values(item, num_cols: int) -> list:
    """A row's cells as num_cols strings (a one-column row is its value)."""
    vals = [str(v) for v in item] if isinstance(item, (list, tuple)) else [str(item)]
    return (vals + [""] * num_cols)[:num_cols]


def table_lines(headers: tuple, rows: list, width: int, *, cursor: int | None = None,
                mins: tuple | None = None, first: int = 0, count: int | None = None,
                layout: dict | None = None) -> list[str]:
    """`rows` (each a list of cells: text, or styled segments) as a ruled
    grid `width` wide: the headings over a rule, the columns divided, each
    as wide as what's in it (`mins`: at least) and the last taking the rest,
    row `cursor` on the bar. Shows rows[first:first + count], laid out by
    them all, so the columns hold still as it scrolls. What the table editor
    shows, and anything showing such a table (a tag's people). `layout`
    gets each column's 'starts' (its offset in a line) and 'widths'."""
    n = len(headers)
    mins = list(mins or ()) + [0] * n
    columns = [Column(style='primary', min_width=mins[j], gap=1 if j == 0 else 3, flex=j == n - 1)
               for j in range(n)]
    widths = [max(0, w) for w in _table_widths([list(headers)] + rows, columns, width, pointer_w=0, right_margin=1)]
    widths[-1] += max(0, width - sum(widths) - 2 * n - (n - 1))        # the last column to the edge
    div = f"{C.FRAME}│{C.RESET}"
    if layout is not None:
        layout.update(widths=widths, starts=[1 + sum(w + 3 for w in widths[:j]) for j in range(n)])

    def line(cells: list, current: bool = False) -> str:
        return " " + f" {div} ".join(
            _render_cell_segments(cells[j] if j < len(cells) else "", columns[j].style, current, widths[j], 'left')
            for j in range(n)) + " "

    out = [line([[(h, 'heading')] for h in headers]),
           C.FRAME + "┼".join("─" * (w + 2) for w in widths) + C.RESET]
    for i in range(first, len(rows) if count is None else min(len(rows), first + count)):
        out.append(ui.on_bar(line(rows[i], True), width) if i == cursor else line(rows[i]))
    return out


def _build_list_edit_lines(message: str, items: list, headers: tuple, cursor: int, viewport: int,
                           editing: tuple | None, pairs: list, cells: dict, note: str = "",
                           col_mins: tuple | None = None, col_types: dict | None = None, can_add: bool = True,
                           layout: dict | None = None):
    """The table editor's frame: the rows as a ruled grid (table_lines), the
    highlighted one on the bar, in a box titled `message` down to the hint
    bar. `editing` is the highlighted row's cell under edit, (column, its
    segments), or None. `note`, dim at the top of the box, says what to put
    in the rows. Returns (lines, viewport, rows shown, the line the first
    row is on)."""
    num_cols = len(headers)
    types = col_types or {}
    mins = [max(m, len(_TS_MASK) if types.get(j) == 'timestamp' else 0)
            for j, m in enumerate(list(col_mins or ()) + [0] * num_cols)][:num_cols]
    boxed = box_fits()
    inner = _cols() - (4 if boxed else 0)            # inside the box's "│ " and " │"

    def shown(i: int) -> list:
        vals = _row_values(items[i], num_cols)
        out: list = [_ts_segments(_ts_buffer(v)) if types.get(j) == 'timestamp' else v
                     for j, v in enumerate(vals)]
        if editing is not None and i == cursor:
            out[editing[0]] = editing[1]
        return out

    top = [f"   {C.DIM}{ln}{C.RESET}" for ln in textwrap.wrap(note, max(10, inner - 2))] + ([""] if note else [])
    vis = max(1, chrome_room(pairs) - (2 if boxed else 1) - len(top) - 2)    # the headings and their rule
    n = len(items)
    if cursor < viewport:
        viewport = cursor
    elif cursor >= viewport + vis:
        viewport = cursor - vis + 1
    viewport = max(0, min(viewport, n - vis))   # see select(): no scrolled-off rows once they fit

    grid = table_lines(headers, [shown(i) for i in range(n)], inner, cursor=cursor, mins=tuple(mins),
                       first=viewport, count=vis, layout=layout)
    body = top + [" " * ui.MARGIN_H + ln for ln in grid]
    if not n:
        body.append(f"   {C.DIM}No rows yet · {keys.label('table.add')} adds one{C.RESET}" if can_add
                    else f"   {C.DIM}No rows{C.RESET}")
    out, _dx = boxed_chrome(body, message, pairs, cells, help_key=editing is None)
    return out, viewport, vis, 1 + len(top) + 2


def _parse_import_rows(text: str, headers: tuple[str, ...]) -> list:
    """Parse pasted/imported text into list_edit rows, auto-detecting the layout.

    Handles CSV, TSV, and other separators (``;`` ``|`` ``:`` `` - ``) plus
    run-of-2+-spaces columns. Comment (`#`) and blank lines are skipped, and a
    leading header row that just repeats the column names is dropped."""
    num_cols = len(headers)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith('#')]
    if not lines:
        return []
    if num_cols <= 1:
        return list(lines)

    # Delimiter present in a majority of lines wins (tab/comma preferred).
    delim = None
    for cand in ('\t', ',', ';', '|', ' - ', ':'):
        if sum(1 for ln in lines if cand in ln) >= max(1, (len(lines) + 1) // 2):
            delim = cand
            break

    def _fit(fields: list) -> tuple:
        """Pad/collapse a split row to exactly num_cols fields, folding overflow into the last column."""
        fields = [f.strip() for f in fields]
        if len(fields) > num_cols:                  # overflow → last column keeps the rest
            tail = (delim or ' ').join(fields[num_cols - 1:]).strip()
            fields = fields[:num_cols - 1] + [tail]
        fields += [''] * (num_cols - len(fields))
        return tuple(fields[:num_cols])

    rows: list = []
    if delim == ',':
        import csv
        import io
        for fields in csv.reader(io.StringIO('\n'.join(lines))):
            rows.append(_fit(fields))
    elif delim:
        for ln in lines:
            rows.append(_fit(ln.split(delim, num_cols - 1)))
    else:
        for ln in lines:                            # no delimiter → split on runs of spaces
            parts = re.split(r'\s{2,}', ln)
            rows.append(_fit(parts if len(parts) >= num_cols else [ln]))

    if rows and tuple(str(c).lower() for c in rows[0]) == tuple(h.lower() for h in headers):
        rows = rows[1:]                             # drop a repeated-header row
    return rows


def list_edit(message: str, initial_items: list | None = None, headers: tuple[str, ...] = ("ROLE", "NAME"),
              fixed_rows: bool = False, locked_cols: set | None = None, col_hints: object = None,
              col_mins: tuple | None = None, col_types: dict | None = None, note: str = "") -> list | None:
    """A table of rows to edit: ↑↓ move, 'a' adds a row, 'e' edits the
    highlighted one in place (Tab, Shift-Tab between its cells), 'd' deletes,
    Enter saves; 'i' imports rows typed in the system editor, 'f' from a file.

    `message` is the box's title: what the rows are. `note`: what to put in
    them, dim at the top of the box. fixed_rows: rows can be edited, not
    added or removed. locked_cols: column indices that can't be edited.
    col_hints:  callable(col, row) -> candidate values for a cell. With two or
                more, and the cell's value among them, the cell opens as a
                choice that ↑↓ step through instead of free text.
    col_mins:   per-column minimum widths: what keeps a fixed-shape cell
                readable when the table is narrow.
    col_types:  {column index: type} for cells that edit as something other than
                free text. ``'timestamp'`` gives a split date/time field masked
                as ``YYYY-MM-DD HH:MM:SS``: you type only the digits, and the
                left/right arrows run past the end of one part straight into the
                next rather than needing Tab. Leaving the time blank keeps the
                value a plain date.

    Returns the rows (Esc with changes asks whether to keep them), or None
    when left unchanged."""
    items    = list(initial_items) if initial_items else []
    cursor   = 0
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = _get_term_attrs(fd)
    w        = _Widget(fd)
    num_cols = len(headers)
    locked   = set(locked_cols or ())

    edit_mode   = False
    edit_col    = 0
    edit_buf: list[str] = []
    edit_pos    = 0
    edit_backup = None
    barrel_mode  = False
    barrel_idx   = 0
    barrel_hints: list[str] = []

    def _is_ts(col: int) -> bool:
        """True if `col` edits as a split timestamp rather than free text."""
        return (col_types or {}).get(col) == 'timestamp'

    def _cell_hints(col: int) -> list[str]:
        """Candidate values for the highlighted row's cell `col`, from `col_hints`."""
        if col_hints is None or not items or _is_ts(col):
            return []
        try:
            return [str(v) for v in col_hints(col, _row_values(items[cursor], num_cols))]  # type: ignore[operator]
        except Exception:
            return []

    def _open_cell(col: int) -> None:
        """Start editing the highlighted row's cell `col`: a timestamp's mask,
        a choice where the value is one of its hints, else the text."""
        nonlocal edit_col, edit_buf, edit_pos, barrel_mode, barrel_idx, barrel_hints
        edit_col = col
        value = _row_values(items[cursor], num_cols)[col]
        if _is_ts(col):
            edit_buf, edit_pos = _ts_buffer(value), _TS_SLOTS[0]
        else:
            edit_buf, edit_pos = list(value), len(value)
        barrel_hints = _cell_hints(col)
        barrel_mode = len(barrel_hints) >= 2 and value in barrel_hints
        barrel_idx = barrel_hints.index(value) if barrel_mode else 0

    def _next_col(step: int) -> int:
        """The next unlocked column from the one being edited, either way."""
        col = edit_col
        for _ in range(num_cols):
            col = (col + step) % num_cols
            if col not in locked:
                return col
        return edit_col

    def _commit() -> None:
        """Write the cell being edited back into its row."""
        if barrel_mode and barrel_hints:
            val = barrel_hints[barrel_idx]
        else:
            val = _ts_value(edit_buf) if _is_ts(edit_col) else "".join(edit_buf)
        if num_cols > 1:
            row = _row_values(items[cursor], num_cols)
            row[edit_col] = val
            items[cursor] = tuple(row)
        else:
            items[cursor] = val

    def _editing() -> tuple | None:
        """The cell under edit as (column, its segments), or None."""
        if not edit_mode:
            return None
        if barrel_mode:
            return edit_col, edit_segments(barrel_hints[barrel_idx], 0, cycling=True)
        if _is_ts(edit_col):
            return edit_col, _ts_segments(edit_buf, edit_pos)
        return edit_col, edit_segments("".join(edit_buf), edit_pos)

    def _pairs() -> list:
        L = keys.label
        if edit_mode:
            cell = (L("field.next", "field.prev"), "cell")
            if barrel_mode:
                pairs = [(L("field.up", "field.down"), "choose"), cell]
            elif _is_ts(edit_col):
                pairs = [(L("text.left", "text.right"), "move"), cell]
            else:
                pairs = [cell]
            return pairs + [(L("field.submit"), "set"), (L("field.cancel"), "cancel")]
        pairs = [(L("list.up", "list.down"), "move")]
        if not fixed_rows:
            pairs.append((L("table.add"), "add"))
        pairs.append((L("table.edit"), "edit"))
        if not fixed_rows:
            pairs += [(L("table.delete", most=1), "delete"), move_hint()]
        pairs += [(L("table.import_text"), "import text"), (L("table.import_file"), "from file"),
                  (L("list.choose", most=1), "save"), (L("list.back", most=1), "back"),
                  (L("list.quit"), "quit app")]
        if chrome._value_toggle_enabled:
            pairs.append((L("global.raw_text"), "raw text"))
        return pairs

    _vis = 1
    _first_line = 1
    _hint_cells: dict[tuple[int, int], str] = {}

    def _render():
        nonlocal viewport, _vis, _first_line
        layout: dict = {}
        lines, viewport, _vis, _first_line = _build_list_edit_lines(
            message, items, headers, cursor, viewport, _editing(), _pairs(), _hint_cells,
            note, col_mins, col_types, can_add=not fixed_rows, layout=layout)
        w.render(lines)
        if edit_mode and not barrel_mode and not _is_ts(edit_col) and layout:
            # Where the cell's text is, as far as it shows (scrolled to its caret).
            width = layout['widths'][edit_col]
            text = "".join(edit_buf)
            start = caret_window(len(text) + (edit_pos >= len(text)), edit_pos, width)
            caret_spans(edit_buf, [(ui.top_margin() + 1 + _first_line + cursor - viewport,
                                    framed_col() + layout['starts'][edit_col], start, text[start:start + width])])

    result = None
    _last_click: int | None = None
    try:
        _set_raw(fd)
        enable_mouse()
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui.consume_resize():
                ui.clear_screen()
                w.anchor_reset()
                _last_click = None
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)

            # Transport keys and miniplayer/hint clicks work in every mode of
            # this widget, so they are consumed before the mode switches below.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch

            # After the replay, so a clicked ^t toggles as the typed one does.
            if chrome.is_mode_toggle(key) and not edit_mode:
                return MODE_TOGGLE  # type: ignore[return-value]

            if edit_mode:
                act, typing = keys.action(key, 'field'), keys.action(key, 'text')
                if act == 'field.cancel':
                    items[cursor] = edit_backup
                    edit_mode = False
                elif act == 'field.submit':
                    _commit()
                    edit_mode = False
                elif act in ('field.next', 'field.prev'):
                    if num_cols > 1:
                        _commit()
                        _open_cell(_next_col(-1 if act == 'field.prev' else 1))
                elif barrel_mode:
                    if act in ('field.up', 'field.down'):
                        barrel_idx = (barrel_idx + (-1 if act == 'field.up' else 1)) % len(barrel_hints)
                    elif len(key) == 1 and key.isprintable():
                        barrel_mode = False                  # typing: the cell's a text field again
                        edit_buf, edit_pos = [key], 1
                elif _is_ts(edit_col):
                    # Only digits go in, and the caret walks the whole mask, so
                    # running off the end of one part lands in the next. Anything
                    # else isn't taken: the mask has every separator already.
                    if typing in ('text.left', 'text.right'):
                        edit_pos = _ts_step(edit_pos, -1 if typing == 'text.left' else 1)
                    elif typing in ('text.home', 'text.end'):
                        edit_pos = _TS_SLOTS[0 if typing == 'text.home' else -1]
                    elif typing == 'text.backspace':
                        # Clear the slot behind the caret and sit on it, so holding
                        # backspace rubs the stamp out right to left.
                        prev = _ts_step(edit_pos, -1)
                        edit_buf[prev] = _TS_MASK[prev]
                        edit_pos = prev
                    elif typing == 'text.delete':
                        edit_buf[edit_pos] = _TS_MASK[edit_pos]
                    elif len(key) == 1 and key.isdigit():
                        edit_buf[edit_pos] = key
                        edit_pos = _ts_step(edit_pos, 1)
                elif typing == 'text.backspace' and edit_pos == 0 and len(barrel_hints) >= 2:
                    barrel_mode, barrel_idx = True, 0      # back to the choices
                elif (new_pos := edit_line(edit_buf, edit_pos, key)) is not None:
                    edit_pos = new_pos
                _render()
                continue

            if key.startswith('MOUSE_CLICK:'):
                # Now-playing box / hint-glyph clicks first; a plain row click
                # falls through to the row-selection handler below.
                _mp = key.split(':')
                _mr = int(_mp[2]); _mc = int(_mp[3]) if len(_mp) > 3 else 1
                _act = footer_click_action(_mr, _mc)
                if _act == 'open' and chrome._player_opener is not None:
                    chrome._player_opener()
                    enable_mouse()
                    sys.stdout.flush()
                    w.anchor_reset(); _last_click = None; _render(); continue
                if _act in FOOTER_ACTIONS and chrome._transport_handler is not None:
                    chrome._transport_handler(_act); continue

            act = keys.action(key, "table")
            if key == 'CTRL_C':
                break
            elif key in ('SCROLL_UP', 'SCROLL_DOWN') or act in ('list.up', 'list.down'):
                if items:
                    cursor = (cursor + (-1 if key == 'SCROLL_UP' or act == 'list.up' else 1)) % len(items)
                _last_click = None
            elif key.startswith('MOUSE_CLICK:'):
                _parts = key.split(':')
                offset = int(_parts[2]) - 1 - ui.top_margin() - _first_line
                if int(_parts[1]) == 0 and 0 <= offset < _vis and viewport + offset < len(items):
                    cursor = viewport + offset
                    if _last_click == cursor:            # a second click on the row: save
                        result = items
                        break
                    _last_click = cursor
            elif act == 'table.add' and not fixed_rows:
                items.append(tuple("" for _ in range(num_cols)) if num_cols > 1 else "")
                cursor = len(items) - 1
                edit_backup = items[cursor]
                if len(locked) < num_cols:
                    edit_mode = True
                    _open_cell(next(c for c in range(num_cols) if c not in locked))
            elif act == 'table.edit' and items and len(locked) < num_cols:
                edit_backup = items[cursor]
                edit_mode = True
                _open_cell(next(c for c in range(num_cols) if c not in locked))
            elif act == 'table.delete' and items and not fixed_rows:
                items.pop(cursor)
                cursor = min(cursor, max(0, len(items) - 1))
            elif keys.pressed(key, "list.move_up") and not fixed_rows and cursor > 0:
                items[cursor - 1], items[cursor] = items[cursor], items[cursor - 1]
                cursor -= 1
                _last_click = None
            elif keys.pressed(key, "list.move_down") and not fixed_rows and cursor < len(items) - 1:
                items[cursor + 1], items[cursor] = items[cursor], items[cursor + 1]
                cursor += 1
                _last_click = None
            elif act == 'list.choose':
                result = items
                break
            elif act in ('table.import_text', 'table.import_file'):
                _restore_term_attrs(fd, old)
                if act == 'table.import_text':
                    template = ("# One entry per line: " + " : ".join(h.lower() for h in headers)
                                + "\n# Example:\n" + " : ".join(h.lower() for h in headers) + "\n"
                                if num_cols > 1 else "# One entry per line\n")
                    text_input = multiline(message, template)
                else:
                    file_path = path("Import from file:")      # with completion; the format is worked out
                    text_input = None
                    if file_path:
                        try:
                            with open(os.path.expanduser(file_path), encoding='utf-8') as _fp:
                                text_input = _fp.read()
                        except OSError:
                            ui.show_error(f"couldn't read {file_path}")
                _set_raw(fd)
                enable_mouse()   # re-arm mouse
                screen_takeover_next()   # paint over the previous screen, no flash
                w.anchor_reset()
                if text_input:
                    items.extend(_parse_import_rows(text_input, headers))
                    cursor = len(items) - 1 if items else 0
            elif act == 'list.quit':
                raise QuitToTerminal()   # q quits the app; never a way out of a widget
            elif act == 'list.back':
                if [_row_values(r, num_cols) for r in items] == [_row_values(r, num_cols) for r in initial_items or []]:
                    break                                # nothing changed: nothing to ask
                # "Discard changes?" → yes = drop the edits, no = keep them.
                result = None if confirm("Discard changes?", default=False) else items
                break
            else:
                continue
            _render()

    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result
