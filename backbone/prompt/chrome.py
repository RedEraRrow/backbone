"""What every prompt widget shares: the hint bar and help corner, the global
player and transport keys, mouse reporting, and the per-edit raw-text toggle."""
from __future__ import annotations
import sys
from backbone.prompt.core import (
    box_fits, boxed_frame, render_status_bar, C,
    _IS_WINDOWS, FOOTER_ACTIONS, _hint, add_hint_click_cells, footer_click_action, _hint_pin_target,
    screen_invalidate, HINTS_CLICK, is_hints_key, toggle_hints, place_help_toggle,
)
from backbone import keys, nav, ui


# Live on every screen: the background-audio transport (routed through
# registered callbacks so prompt need not import the playback layer), reopening
# the player, and the per-edit raw-text toggle.
keys.define("global", "Everywhere", [
    ("playpause", ("\x10",), "play / pause"),
    ("next", ("\x0e",), "next track"),
    ("prev", ("\x02",), "previous track"),
    ("player", ("\x0f",), "open the player"),
    ("raw_text", ("\x14",), "switch a value between its editor and raw text"),
], within=())
# The volume, on any screen that leaves these keys free (a list, the player,
# the miniplayer; not a text field, where they're typed). Its own group, so a
# key a screen binds for itself isn't taken from it.
keys.define("volume", "Volume", [
    ("up", ("+", "="), "volume up"),
    ("down", ("-", "_"), "volume down"),
], within=())

# The one key pair that moves a row up or down, wherever a list's order can be
# changed (select's on_move, list_edit).
keys.define("list", "Lists", [
    ("move_up", ("J",), "move the row up"),
    ("move_down", ("K",), "move the row down"),
])


def move_hint() -> tuple:
    return (keys.label("list.move_up", "list.move_down"), "move up/down")


# Per-edit "raw text ↔ smart widget" toggle. prompt_for_value enables the
# flag around a value edit; the value widgets then treat Ctrl-T as a request to
# switch modes by returning MODE_TOGGLE, and advertise it in their hint bar.
MODE_TOGGLE = object()


def _with_toggle_hint(pairs, label: str = 'raw text'):
    """Append the Ctrl-T hint to a widget's hint bar while the per-edit raw-text
    toggle is live.

    Every widget that *accepts* ^t advertises it through this, so the key is never
    silently available on one screen and absent from the bar on another.
    """
    return list(pairs) + [(keys.label("global.raw_text"), label)] if _value_toggle_enabled else list(pairs)


def is_mode_toggle(key: str) -> bool:
    """Whether `key` asks a value editor to switch to raw text (or back)."""
    return _value_toggle_enabled and keys.pressed(key, "global.raw_text")


_value_toggle_enabled = False


_toggle_hint_label = 'widget'      # what text()'s ^t hint calls the alternate mode


_toggle_carry: str | None = None   # in-progress text buffer handed across a Ctrl-T toggle


_player_opener = None


_transport_handler = None


def set_player_opener(fn) -> None:
    """Register a ``callable()`` that opens the background player's full view."""
    global _player_opener
    _player_opener = fn


_command_line = None


def set_command_line(fn) -> None:
    """Register a ``callable(around=None)`` that `:` opens, where a screen
    leaves it free. It returns whether it opened a screen of its own (the
    screen under it then needs drawing again), opening it inside `around()`
    when given."""
    global _command_line
    _command_line = fn


def open_command_line(around=None) -> bool | None:
    """Run the registered command line: whether it opened a screen of its own
    (inside the context `around()`, when given: the player steps out of view
    only for that); None when there is none to run here (none registered, or
    a screen that keeps you in it, nav.modal())."""
    if _command_line is None or nav.is_modal():
        return None
    return bool(_command_line(around=around) if around else _command_line())


# --- shared widget chrome -------------------------------------------------
# Every screen owes the user the same four things: a hint bar pinned above the
# now-playing box and status bar so its keys never move, those keys clickable, the
# background-audio transport keys listed whenever the now-playing box is up, and
# clicks on the now-playing box itself doing something. These two helpers are
# that contract in one place.

CHROME_HANDLED = object()      # the key was consumed; carry on with the loop
CHROME_REDRAW = object()       # consumed, and the caller should repaint fully


def chrome_hint_pairs(pairs) -> list:
    """A widget's hint pairs plus the transport keys, while audio is playing.

    Only keys that will actually do something are advertised: the transport trio
    needs a handler installed and ^O needs a player to reopen. `unboxed` covers
    a terminal too narrow to draw the now-playing box: the keys are still live,
    so they are still listed.
    """
    items = list(pairs.items()) if isinstance(pairs, dict) else [tuple(p) for p in pairs]
    if ui.footer_active() or ui.footer_unboxed():
        if _transport_handler is not None:
            items += [(keys.label("global.playpause"), "play/pause"),
                      (keys.label("global.next", "global.prev"), "next/prev")]
        if _player_opener is not None:
            items += [(keys.label("global.player"), "player")]
    return items


def chrome_hint_lines(pairs, *, extra: str = "") -> list:
    """The hint bar as rendered lines: widgets that size a viewport need the
    row count before they lay their content out."""
    return _hint(*chrome_hint_pairs(pairs), extra=extra).splitlines()


def chrome_room(pairs, *, extra: str = "") -> int:
    """The rows a widget's frame has above its hint bar (append_chrome), for a
    screen laying out its own boxes."""
    return _hint_pin_target() - len(chrome_hint_lines(pairs, extra=extra))


def append_chrome(out: list, pairs, cells: dict, *, extra: str = "",
                  pin: bool = True, help_key: bool = False) -> list:
    """Append the hint bar to a widget's rendered `out` lines, in place.

    Pads down to :func:`_hint_pin_target` so the bar sits just above the
    now-playing box and status bar and its keys keep the same screen position across
    redraws; otherwise a repeated click chases the bar as the content changes
    height. Records each bright key's screen cell in `cells` for
    :func:`consume_chrome` to look up.

    The bar is empty unless hints are switched on; either way the top line
    (`out[0]`) carries the corner toggle. `help_key`: this screen leaves `?` free,
    so `?` toggles and the corner says so; otherwise it names Ctrl-/ (or Ctrl-G).
    """
    items = chrome_hint_pairs(pairs)
    hint_lines = _hint(*items, extra=extra).splitlines()
    if pin:
        filler = _hint_pin_target() - len(out) - len(hint_lines)
        if filler > 0:
            out.extend([""] * filler)
    out.extend(f"{' ' * ui.MARGIN_H}{h}" for h in hint_lines)

    cells.clear()
    if hint_lines:
        start = len(out) - len(hint_lines)
        for k in range(len(hint_lines)):
            # `_Widget.render` lays line j at terminal row anchor(1) + top_margin() + j.
            add_hint_click_cells(cells, out[start + k],
                                 1 + ui.top_margin() + (start + k), items)
    place_help_toggle(out, 1 + ui.top_margin(), cells, help_key)
    return out


def inner_rule() -> str:
    """A divider across the inside of a widget's box (boxed_chrome)."""
    return f"  {C.DIM}{'─' * max(1, ui.get_terminal_width() - 2 * ui.MARGIN_H - 4)}{C.RESET}"


def boxed_chrome(body: list, title: str, pairs, cells: dict, *, extra: str = "",
                 help_key: bool = False, header: list | None = None) -> tuple[list, int]:
    """A widget's whole frame: any `header` lines, then `body` (lines with the
    usual left margin) in a box titled `title` that reaches down to the hint
    bar, then the bar (append_chrome). In a window too small for a box, `title`
    is a line above the body instead. Either way the body starts one line under
    the header; returns the lines and how many columns right the body moved (2
    inside the box, for "│ " where the margin was), for a widget that maps
    clicks on its body."""
    title, header = title.strip().rstrip(":"), list(header or [])
    if not box_fits():
        out = header + [f"  {C.DIM}{title}{C.RESET}"] + list(body)
        append_chrome(out, pairs, cells, extra=extra, help_key=help_key)
        return out, 0
    box_h = max(3, chrome_room(pairs, extra=extra) - len(header))
    out = boxed_frame(header, list(body), title, box_h, help_key and not header)
    append_chrome(out, pairs, cells, extra=extra, help_key=help_key)
    return out, 2


def consume_chrome(key: str, cells: dict, free_keys: bool = False):
    """Handle a transport key, a now-playing box click, a click on a hint key,
    or a click on the tab bar. With `free_keys` (a screen that has no other use
    for them), also Tab / Shift-Tab and the digits for the tabs, `:` for the
    command line, and the volume (+ / -).

    Returns :data:`CHROME_HANDLED` when the key is fully dealt with,
    :data:`CHROME_REDRAW` when the caller should also repaint, the synthesised
    key string when a hint was clicked (replay it through the widget's own
    switch), or None when the key is not ours.
    """
    if key == 'FOCUS_IN':
        # Back in focus: the terminal may not have painted us meanwhile (or a
        # background track change went by), so repaint everything.
        screen_invalidate()
        return CHROME_REDRAW
    if free_keys and key == ':' and (opened := open_command_line()) is not None:
        if not opened:                       # just the overlay, put back: only the toast is new
            render_status_bar()
            return CHROME_HANDLED
        screen_invalidate()
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")
        return CHROME_REDRAW
    tab = nav.tab_for(key, free_keys)
    if tab is not None:
        nav.switch_to(tab)              # back here once this tab shows again
        screen_invalidate()
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")   # the other tab may have had the mouse off
        return CHROME_REDRAW
    if is_hints_key(key, bool(cells.get('__help_key__'))):
        toggle_hints()
        return CHROME_REDRAW            # the bar appeared or went: re-lay the screen
    act = keys.action(key, "global") if isinstance(key, str) else None
    if act == "global.player" and _player_opener is not None:
        _player_opener()
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")   # the player took the mouse
        sys.stdout.flush()
        return CHROME_REDRAW
    if act in ("global.playpause", "global.next", "global.prev") and _transport_handler is not None:
        _transport_handler(act.split(".")[1])
        return CHROME_HANDLED
    vol = keys.action(key, "volume") if free_keys and isinstance(key, str) else None
    if vol in ("volume.up", "volume.down") and _transport_handler is not None:
        _transport_handler("vol_up" if vol == "volume.up" else "vol_down")
        return CHROME_HANDLED

    if isinstance(key, str) and key.startswith('MOUSE_CLICK:'):
        parts = key.split(':')
        try:
            row = int(parts[2])
            col = int(parts[3]) if len(parts) > 3 else 1
        except (IndexError, ValueError):
            return None
        act = footer_click_action(row, col)
        if act == 'open' and _player_opener is not None:
            _player_opener()
            if not _IS_WINDOWS:
                sys.stdout.write("\033[?1000h\033[?1006h")
            sys.stdout.flush()
            return CHROME_REDRAW
        if act in FOOTER_ACTIONS and _transport_handler is not None:
            _transport_handler(act)
            return CHROME_HANDLED
        hit = cells.get((row, col))
        if hit == HINTS_CLICK or keys.action(hit or "", "global") in (
                "global.player", "global.playpause", "global.next", "global.prev"):
            return consume_chrome(hit, cells)   # a transport hint: act on it here
        if hit is not None:
            return hit                      # replay the clicked hint's key
    return None


def enable_mouse() -> None:
    """Turn on click + scroll reporting for a widget that wants clickable hints."""
    if not _IS_WINDOWS:
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()


def disable_mouse() -> None:
    """Turn click reporting back off on the way out."""
    if not _IS_WINDOWS:
        sys.stdout.write("\033[?1000l\033[?1006l")
        sys.stdout.flush()


def set_transport_handler(fn) -> None:
    """Register ``callable(action)`` for the global transport hotkeys and clicks
    on the now-playing box, where action is one of FOOTER_ACTIONS ('time': its
    clock was clicked). Kept as a registered callback so
    prompt need not import the playback layer (mirrors set_player_opener)."""
    global _transport_handler
    _transport_handler = fn


_activity_opener = None


def set_activity_opener(fn) -> None:
    """Register a ``callable()`` that opens the activity centre,
    invoked when the status-bar ● beacon is clicked."""
    global _activity_opener
    _activity_opener = fn


def _plain(s: str) -> str:
    """The row's printed characters, one entry per *terminal column*.

    A two-cell glyph is repeated so that an index into the result is the
    column it sits in, which is what a click hit-test assumes when it asks
    whether column `col` holds a character or blank padding. Shared by
    `select` and `live_select`, so the two widgets' click behaviour can't drift.
    """
    return "".join(ch * ui.char_cols(ch) for ch in ui.display_text(s))
