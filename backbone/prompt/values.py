"""Small value editors: fraction pairs, times, numbers and ratings."""
from __future__ import annotations
import sys
from backbone.prompt.core import (
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress, block_cursor,
    _read_key, _Widget, screen_takeover_next,
)
from backbone import keys, ui
from backbone.nav import QuitToTerminal
from backbone.prompt import chrome
from backbone.prompt.chrome import CHROME_HANDLED, CHROME_REDRAW, MODE_TOGGLE, _with_toggle_hint, boxed_chrome, consume_chrome, disable_mouse, enable_mouse
from backbone.prompt.core import C, clicked_field, edit_line, field_row_spans, framed_col
from backbone.prompt.text import field_frame


def _frac_result(buffers: dict, varies: set) -> dict:
    """Collect the fraction editor's buffers, mapping an untouched *varying*
    field to None so the caller keeps each file's existing value for it."""
    out = {}
    for field in ('current', 'total'):
        text = "".join(buffers[field])
        out[field] = None if (not text and field in varies) else text
    return out


def fraction_edit(message: str = "Edit metadata pair:",
                    tag: str = "TRCK", value: str = "",
                    varies: object = (), note: str = "") -> dict | None:
    """A number and its total (track 3 of 12, disc 1 of 2), as a typed field:
    the pair in a box their own size, Tab or Shift-Tab between them, and
    under it what guides: `note` (what's being set), what a blank field
    keeps, and a number past its total. Either half takes any text (a vinyl
    side's A1, a half-track's 3.5).

    ``varies`` names the halves ('current' / 'total') that differ across a
    bulk selection. Those open blank, say so, and come back as ``None`` if
    left blank (each file keeps its own), so the half the files share can be
    set without flattening the half they don't. Typing into one makes it a
    value for every file.

    Returns {'current', 'total'} (either None as above), or None if cancelled.
    """
    varies = {str(v) for v in (varies or ())}
    label = {"TRCK": "Track", "TPOS": "Disc", "MVIN": "Movement"}.get(tag.upper(), "Number")
    text = str(value)
    cur, _sep, tot = text.partition('/') if '/' in text else text.partition('⁄')
    buffers = {'current': [] if 'current' in varies else list(cur.strip()),
               'total': [] if 'total' in varies else list(tot.strip())}
    positions = {k: len(v) for k, v in buffers.items()}
    order = ['current', 'total']
    at = 0

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _shown(field: str) -> str:
        """A half as drawn: the caret in the one being typed in, a dim
        stand-in for a blank one."""
        val = "".join(buffers[field])
        if field == order[at]:
            return block_cursor(val, positions[field])
        if not val:
            return f"{C.DIM}{ui.stand_in('varies' if field in varies else 'none')}{C.RESET}"
        return val

    def _guide() -> list:
        """What's under the field: the note, what a blank varying half keeps,
        and a number past its total (a slip, most likely; still taken)."""
        lines = [f"  {C.DIM}{note}{C.RESET}"] if note else []
        for field, name in (('current', label.lower()), ('total', 'total')):
            if field in varies and not buffers[field]:
                lines.append(f"  {C.DIM}Left blank, each file keeps its own {name}.{C.RESET}")
        c, t = "".join(buffers['current']), "".join(buffers['total'])
        if c.isdigit() and t.isdigit() and int(t) and int(c) > int(t):
            lines.append(f"  {C.WARN}{label} {c} is past the total, {t}.{C.RESET}")
        return lines

    def _render():
        pieces = [(None, f"{C.DIM}{label}{C.RESET} "), (buffers['current'], _shown('current')),
                  (None, f"   {C.DIM}of{C.RESET} "), (buffers['total'], _shown('total'))]
        field = ["  " + "".join(drawn for _b, drawn in pieces)]
        field_row_spans((w.row or 1) + ui.top_margin() + 1, framed_col(), pieces)   # field_frame's second line
        # No q: these fields take text, so q is a letter here.
        pairs = _with_toggle_hint([(keys.label("field.next", "field.prev"), "field"), (keys.label("field.submit"), "save"),
                                   (keys.label("field.cancel"), "back")])
        w.render(field_frame(message, field, _guide(), pairs, _hint_cells))

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui.consume_resize():
                ui.clear_screen()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the now-playing box, or a click on one of our
            # own hint keys, handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if chrome.is_mode_toggle(key):
                return MODE_TOGGLE  # type: ignore[return-value]
            field = order[at]
            if keys.pressed(key, 'field.submit'):
                result = _frac_result(buffers, varies)
                break
            elif key == 'CTRL_C' or keys.pressed(key, 'field.cancel'):   # Ctrl-C cancels, as in every widget
                break
            elif keys.pressed(key, 'field.next') or keys.pressed(key, 'field.prev'):
                at = (at + (-1 if keys.pressed(key, 'field.prev') else 1)) % len(order)
            elif hit := clicked_field(key, {f: buffers[f] for f in order}):
                at, positions[hit[0]] = order.index(hit[0]), hit[1]
            elif (new_pos := edit_line(buffers[field], positions[field], key)) is not None:
                positions[field] = new_pos

            _render()

    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def time_edit(message: str = "Edit time:", initial: str = "00:00:00") -> str | None:
    """
    In-place editor for time input (HH:MM:SS).
    Supports milliseconds and auto-validation.

    Args:
        message: Prompt label
        initial: Initial time (HH:MM:SS or HH:MM:SS.mmm)

    Returns:
        Formatted time string or None if cancelled
    """
    parts = initial.split(':')
    hours = parts[0] if parts and parts[0] else "00"
    minutes = parts[1] if len(parts) > 1 and parts[1] else "00"

    if len(parts) > 2:
        sec_parts = parts[2].split('.')
        seconds = sec_parts[0] if sec_parts else "00"
        millis = sec_parts[1] if len(sec_parts) > 1 else "000"
    else:
        seconds = "00"
        millis = "000"

    fields = {
        'hours': list(hours[-2:].zfill(2)),
        'minutes': list(minutes[-2:].zfill(2)),
        'seconds': list(seconds[-2:].zfill(2)),
        'millis': list(millis[-3:].zfill(3)),
    }

    field_order = ['hours', 'minutes', 'seconds', 'millis']
    field_maxlen = {
        'hours': 2,
        'minutes': 2,
        'seconds': 2,
        'millis': 3,
    }

    cursor_field = 0
    positions = {k: len(fields[k]) for k in field_order}

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _validate_time() -> bool:
        """True if the current H/M/S fields form a valid time."""
        try:
            h = int("".join(fields['hours']) or "0")
            m = int("".join(fields['minutes']) or "0")
            s = int("".join(fields['seconds']) or "0")
            return 0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60
        except ValueError:
            return False

    def _render():
        lines = []

        pieces = []
        for i, field in enumerate(field_order):
            value = "".join(fields[field])
            pos = positions[field]
            display = block_cursor(value, pos) if i == cursor_field else value.ljust(field_maxlen[field], '0')
            pieces += [(fields[field], display), (None, ":" if i < 2 else "." if i == 2 else "")]
        lines.append("  " + "".join(drawn for _b, drawn in pieces))
        field_row_spans((w.row or 1) + ui.top_margin() + 1, framed_col(), pieces)   # boxed_chrome's second line
        lines, _dx = boxed_chrome(lines, message, _with_toggle_hint(
            [(keys.label('field.submit'), 'save'), (keys.label('field.next', 'field.prev'), 'field'),
             (keys.label('field.cancel'), 'back'), (keys.label('list.quit'), 'quit app')]), _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui.consume_resize():
                ui.clear_screen()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the now-playing box, or a click on one of our
            # own hint keys, handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if chrome.is_mode_toggle(key):
                return MODE_TOGGLE  # type: ignore[return-value]
            current_field = field_order[cursor_field]
            buf = fields[current_field]
            pos = positions[current_field]
            max_len = field_maxlen[current_field]

            if keys.pressed(key, 'field.submit'):
                if _validate_time():
                    h = "".join(fields['hours']).zfill(2)
                    m = "".join(fields['minutes']).zfill(2)
                    s = "".join(fields['seconds']).zfill(2)
                    ms = "".join(fields['millis']).zfill(3)
                    result = f"{h}:{m}:{s}.{ms}"
                    break
                else:
                    ui.show_status("Hours go up to 23, minutes and seconds up to 59.")
            elif key == 'CTRL_C' or keys.pressed(key, 'field.cancel'):   # Ctrl-C cancels, as in every widget
                break
            elif keys.pressed(key, 'list.quit'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif keys.pressed(key, 'field.next') or keys.pressed(key, 'field.prev'):
                _step = -1 if keys.pressed(key, 'field.prev') else 1
                cursor_field = (cursor_field + _step) % len(field_order)
            elif hit := clicked_field(key, fields):
                cursor_field, positions[hit[0]] = field_order.index(hit[0]), hit[1]
            elif keys.action(key, 'text'):                   # the caret and deleting: edit_line's
                positions[current_field] = edit_line(buf, pos, key)
            elif key.isdigit() and len(buf) < max_len:
                buf.insert(pos, key)
                positions[current_field] = pos + 1

            _render()

    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def number_edit(message: str = "Edit number:", *, value: int = 0,
                minimum: int = 0, maximum: int | None = None, unit: str = ""):
    """Bounded-integer spinner (TBPM / TLEN / TDLY / play counts, …).

    ↑/↓ step ±1, PgUp/PgDn ±10, type digits to enter a value directly,
    Backspace deletes a digit, Home clamps to the minimum. Returns the chosen
    int, ``None`` on cancel, or :data:`MODE_TOGGLE` when Ctrl-T flips to the raw
    text field (only when the caller enabled the toggle).
    """
    def _clamp(n: int) -> int:
        n = max(minimum, n)
        if maximum is not None:
            n = min(maximum, n)
        return n

    try:
        value = _clamp(int(str(value).strip() or minimum))
    except ValueError:
        value = minimum
    buf: list[str] = []                 # typed digits; empty → show `value`

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _cur() -> int:
        return _clamp(int("".join(buf))) if buf else value

    def _render():
        # While digits are being typed the block marks the live field; the
        # resting value carries no caret, so the screen isn't blinking at you.
        shown = block_cursor("".join(buf), len(buf)) if buf else str(value)
        unit_s = f" {unit}" if unit else ""
        bounds = f"min {minimum}" + ("" if maximum is None else f", max {maximum}")
        lines = [
            f"  {C.ACCENT}›{C.RESET} {C.BOLD}{shown}{C.RESET}{C.DIM}{unit_s}{C.RESET}   {C.DIM}({bounds}){C.RESET}",
        ]
        lines, _dx = boxed_chrome(lines, message, _with_toggle_hint(
            [(keys.label("field.up", "field.down"), "±1"), (keys.label("field.page_up", "field.page_down"), "±10"),
             (keys.label("field.submit"), "save"), (keys.label("field.cancel"), "back"),
             (keys.label("list.quit"), "quit app")]),
                      _hint_cells, help_key=True)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()
        while True:
            if ui.consume_resize():
                ui.clear_screen()
                w.anchor_reset()
                _render()
                continue
            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the now-playing box, or a click on one of our
            # own hint keys, handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if chrome.is_mode_toggle(key):
                return MODE_TOGGLE
            if key == 'CTRL_C':
                result = None; break
            elif keys.pressed(key, 'field.submit'):
                result = _cur(); break
            elif keys.pressed(key, 'field.cancel'):
                result = None; break
            elif keys.pressed(key, 'list.quit'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif key.isdigit():
                if len("".join(buf)) < 12:
                    buf.append(key); _render()
            elif keys.pressed(key, 'text.backspace'):
                if buf:
                    buf.pop(); _render()
            elif keys.pressed(key, 'field.up'):
                value = _clamp(_cur() + 1); buf.clear(); _render()
            elif keys.pressed(key, 'field.down'):
                value = _clamp(_cur() - 1); buf.clear(); _render()
            elif keys.pressed(key, 'field.page_up'):
                value = _clamp(_cur() + 10); buf.clear(); _render()
            elif keys.pressed(key, 'field.page_down'):
                value = _clamp(_cur() - 10); buf.clear(); _render()
            elif keys.pressed(key, 'field.lowest'):
                value = minimum; buf.clear(); _render()
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def _more(key: str) -> bool:
    """A star or a count up: a value up, or the caret's right."""
    return keys.pressed(key, 'field.up') or keys.pressed(key, 'text.right')


def _less(key: str) -> bool:
    return keys.pressed(key, 'field.down') or keys.pressed(key, 'text.left')


def rating_edit(message: str = "Rating:", *, stars: int = 0, count: int = 0,
                email: str = "") -> dict | None:
    """POPM rating form: a 0-5 star rating, a play count, and the rater email.

    Tab moves between the three fields. On Rating: ←/→ (or 0-5) set the stars.
    On Plays: ↑/↓ ±1, PgUp/PgDn ±10, or type digits. On Rater: type the
    identifier text. Returns ``{'stars', 'count', 'email'}`` or ``None`` on
    cancel. A binary/asset frame, so there is no raw-text toggle.
    """
    stars = max(0, min(5, int(stars)))
    count = max(0, int(count))
    email = str(email or "")
    field = 0                            # 0 = rating, 1 = plays, 2 = rater
    cbuf: list[str] = []                 # typed play-count digits

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _count() -> int:
        return int("".join(cbuf)) if cbuf else count

    def _render():
        filled = f"{C.ACCENT}{'★' * stars}{C.RESET}"
        empty = f"{C.DIM}{'☆' * (5 - stars)}{C.RESET}"
        rlabel = ui.stand_in("unrated") if stars == 0 else f"{stars}/5"
        # Plays and Rater take typing but drew nothing to type against: the
        # block shows which field has the keyboard and where the next character
        # lands. Both append at the end, so the block rides there.
        cshown = "".join(cbuf) if cbuf else str(count)
        if field == 1:
            cshown = block_cursor(cshown, len(cshown))
        if field == 2:
            rater = block_cursor(email, len(email))
        elif email:
            rater = email
        else:
            rater = f"{C.DIM}{ui.stand_in('default')}{C.RESET}"

        def _mark(i: int) -> str:
            return f"{C.ACCENT}›{C.RESET}" if field == i else " "

        def _lab(i: int, text: str) -> str:
            return f"{C.BOLD}{text}{C.RESET}" if field == i else f"{C.DIM}{text}{C.RESET}"

        lines = [
            f"  {_mark(0)} {_lab(0, 'Rating')}   {filled}{empty}  {C.DIM}{rlabel}{C.RESET}",
            f"  {_mark(1)} {_lab(1, 'Plays ')}   {cshown}",
            f"  {_mark(2)} {_lab(2, 'Rater ')}   {rater}",
        ]
        # The keys of the focused row: stars, the play count, or (the Rater's
        # e-mail) just typing, where q is a letter, not quit.
        pairs = [(keys.label("field.next", "field.prev"), "field")]
        pairs += {0: [(keys.label("text.left", "text.right"), "stars")],
                  1: [(keys.label("text.left", "text.right"), "±1"),
                      (keys.label("field.page_up", "field.page_down"), "±10")]}.get(field, [])
        pairs += [(keys.label("field.submit"), "save"), (keys.label("field.cancel"), "back")]
        pairs += [(keys.label("list.quit"), "quit app")] if field != 2 else []
        lines, _dx = boxed_chrome(lines, message, pairs, _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()
        while True:
            if ui.consume_resize():
                ui.clear_screen()
                w.anchor_reset()
                _render()
                continue
            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the now-playing box, or a click on one of our
            # own hint keys, handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if key == 'CTRL_C':
                result = None; break
            elif keys.pressed(key, 'field.submit'):
                result = {'stars': stars, 'count': _count(), 'email': email}; break
            elif keys.pressed(key, 'field.cancel'):
                result = None; break
            elif keys.pressed(key, 'field.next') or keys.pressed(key, 'field.prev'):
                count = _count(); cbuf.clear()
                field = (field + (-1 if keys.pressed(key, 'field.prev') else 1)) % 3
                _render()
            # 'q' quits only outside the free-text Rater field (an email may contain 'q').
            elif keys.pressed(key, 'list.quit') and field != 2:   # field 2 is the e-mail text
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif field == 0:
                if _less(key):
                    stars = max(0, stars - 1); _render()
                elif _more(key):
                    stars = min(5, stars + 1); _render()
                elif key.isdigit() and 0 <= int(key) <= 5:
                    stars = int(key); _render()
            elif field == 1:
                if _more(key):
                    count = _count() + 1; cbuf.clear(); _render()
                elif _less(key):
                    count = max(0, _count() - 1); cbuf.clear(); _render()
                elif keys.pressed(key, 'field.page_up'):
                    count = _count() + 10; cbuf.clear(); _render()
                elif keys.pressed(key, 'field.page_down'):
                    count = max(0, _count() - 10); cbuf.clear(); _render()
                elif key.isdigit():
                    if len("".join(cbuf)) < 12:
                        cbuf.append(key); _render()
                elif keys.pressed(key, 'text.backspace'):
                    if cbuf:
                        cbuf.pop(); _render()
            elif field == 2:
                if keys.pressed(key, 'text.backspace'):
                    email = email[:-1]; _render()
                elif len(key) == 1 and key.isprintable():
                    email += key; _render()
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result
