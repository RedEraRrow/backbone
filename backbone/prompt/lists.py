"""Picking from lists: select (single, multi, with row actions), live_select
(a list driven by a query), and confirm."""
from __future__ import annotations
import re
import sys
import textwrap
import time
from typing import Any, Callable, Literal, overload
from backbone.prompt.core import (
    _COLUMNS_MAX_WIDTH, _EDGE_MARGIN, _get_term_attrs, _set_raw, _restore_term_attrs,
    _wait_for_keypress, _table_widths, _render_table_row, _clip_ansi, _norm, block_cursor,
    _read_key, _visible_rows, _cols, _Widget, _hint_pin_target, screen_takeover_next, screen_backdrop,
    Choice, Column, JumpTo, PanelTitle, Trail, border_right, box_fits, box_lines, boxed_frame, column_widths,
    COL_GAP, _COL_MAIN_MIN, _COL_PREVIEW, _LIST_ROWS_MIN, _SHAPE_SAMPLE, _STRIP_SPLIT, strip_rows,
    columns_shown, help_corner_text, panel_header, rounded_header, set_columns_shown, trail_lines,
)
from backbone import keys, ui
from backbone.nav import QuitToTerminal
from backbone.prompt import chrome
from backbone.prompt.chrome import (
    append_chrome, boxed_chrome, CHROME_HANDLED, chrome_hint_lines, CHROME_REDRAW, consume_chrome, disable_mouse, enable_mouse, move_hint, _plain,
)
from backbone.prompt.core import C
from backbone.prompt.core import edit_line


class ListPlace:
    """Where a list that is rebuilt each time round was left: the highlighted
    row's value, so a re-sort or an edit comes back to the same item, and its
    position for when that item has gone. Pass the same one to every select()
    of that list; reset() lands the next one on the first row."""
    def __init__(self) -> None:
        self.value: Any = None
        self.pos = 0

    def reset(self) -> None:
        self.value, self.pos = None, 0


@overload
def select(message: str, choices: list, *,
           header: list | None | Callable[[], list[str]] = ...,
           extra_hints: dict[str, str] | None = ...,
           index: int = ...,
           shortcuts: dict[str, str] | None = ...,
           columns: list | None = ...,
           multi: Literal[False] = ...,
           interlock_category_callback: Callable[[Any], str] | None = ...,
           on_inspect: Callable[[Any], None] | None = ...,
           inspect_key: str = ...,
           row_actions: dict[str, Callable[[Any], None]] | None = ...,
           row_action_hints: dict[str, str] | None = ...,
           allow_back: bool = ...,
           place: ListPlace | None = ...,
           actions: list[tuple[str, str, str]] | None = ...,
           on_move: Callable[[Any, int], bool] | None = ...,
           ) -> Any: ...


@overload
def select(message: str, choices: list, *,
           header: list | None | Callable[[], list[str]] = ...,
           extra_hints: dict[str, str] | None = ...,
           index: int = ...,
           shortcuts: dict[str, str] | None = ...,
           columns: list | None = ...,
           multi: Literal[True],
           interlock_category_callback: Callable[[Any], str] | None = ...,
           on_inspect: Callable[[Any], None] | None = ...,
           inspect_key: str = ...,
           row_actions: dict[str, Callable[[Any], None]] | None = ...,
           row_action_hints: dict[str, str] | None = ...,
           allow_back: bool = ...,
           place: ListPlace | None = ...,
           ) -> list[Any] | None: ...


keys.define("list", "Lists", [
    ("up", ("UP",), "previous row"),
    ("down", ("DOWN",), "next row"),
    ("page_up", ("PGUP",), "a page up"),
    ("page_down", ("PGDN",), "a page down"),
    ("top", ("HOME",), "first row"),
    ("bottom", ("END",), "last row"),
    ("choose", ("ENTER", "RIGHT"), "choose the row"),
    ("toggle", ("SPACE",), "tick the row, in lists with ticks"),
    ("toggle_all", ("a", "A"), "tick or clear every row, in lists with ticks"),
    ("back", ("ESC", "b", "LEFT"), "back"),
    ("quit", ("q", "Q"), "quit the app"),
    ("sections", ("/",), "a long list's sections, or the whole list"),
    ("row_options", ("o",), "everything you can do with the row"),
    ("list_options", ("O",), "everything you can do with the whole list"),
    ("columns", ("v",), "side columns on or off, in lists that have them"),
])
# The live search lists: typing goes into the query, so their keys are the
# ones that can't be typed.
keys.define("search", "Search lists", [
    ("up", ("UP",), "previous result"),
    ("down", ("DOWN",), "next result"),
    ("page_up", ("PGUP",), "five results up"),
    ("page_down", ("PGDN",), "five results down"),
    ("choose", ("ENTER",), "choose the result"),
    ("next_section", ("TAB",), "next section"),
    ("prev_section", ("BACKTAB",), "previous section"),
    ("back", ("ESC",), "back"),
])
keys.define("confirm", "Yes/no questions", [
    ("yes", ("y", "Y"), "yes"),
    ("no", ("n", "N"), "no"),
    ("default", ("ENTER",), "the default answer"),
    ("back", ("ESC",), "back, answering no"),
])
L = keys.label


def _scroll(viewport: int, cursor: int, vis: int, items: list) -> int:
    """The first row to show so the cursor is in view. A section heading (the
    run of disabled rows just above the cursor) comes into view with its first
    row, so the top of a list never reads "1 above" with only a heading there.
    Growing the window (or deleting rows) leaves the viewport further down than
    it needs to be, leaving "N above" with blank space below: it's pulled back
    so the last row of the list sits on the last visible row at most."""
    if cursor < viewport:
        viewport = cursor
    elif cursor >= viewport + vis:
        viewport = cursor - vis + 1
    top = cursor
    while top > 0 and items[top - 1].disabled:
        top -= 1
    if top < viewport and cursor - top < vis:
        viewport = top
    return max(0, min(viewport, len(items) - vis))


def options_menu(title: str, entries: list) -> Any:
    """A menu of what can be done, each with the key that does it directly:
    `entries` are (label, key text, value); returns the value picked, or None."""
    if not entries:
        return None
    choices = [Choice(title=label, value=i, cells=[label, key_text])
               for i, (label, key_text, _v) in enumerate(entries)]
    i = select("", choices, columns=[Column(style='primary'), Column(style='dynamic-dim', flex=True)],
               header=PanelTitle(title or "Options", "options"))
    return None if i is None else entries[i][2]


# A list in sections (separator() headings) this much longer than the screen
# opens as its section titles: ↵ opens one as its own list, / shows the whole.
_SECTION_SLACK = 1.2
_TO_WHOLE, _TO_SECTIONS = object(), object()
# Per sectioned list (keyed by its section titles): whether the whole list was
# asked for, and which section is open, so a caller that redraws its list after
# each change (Settings, Key bindings) comes back to the same section.
_section_memo: dict = {}


def _sections(items: list) -> list[tuple[str, list]] | None:
    """(title, rows) for each titled section, or None when the list isn't
    headed throughout. A blank separator stays inside its section; a greyed
    row (disabled, with cells) is a row, not a heading."""
    out: list[tuple[str, list]] = []
    for it in items:
        if it.disabled and not it.cells and str(it.title).strip():
            out.append((str(it.title), []))
        elif not out:
            return None                    # rows before the first heading
        else:
            out[-1][1].append(it)
    return out if len(out) > 1 else None


def select(message: str, choices: list, **kw) -> Any:
    """Arrow keys to navigate; Enter / → to confirm; ← / b / Esc → None; q
    quits the app. See _select_flat for every option.

    A single-choice list in sections that is much taller than the screen
    (_SECTION_SLACK) opens as its section titles; ↵ opens a section as its own
    list (Esc back to the titles) and list.sections (/) switches between that
    and the whole list, remembered per list."""
    items = _norm(choices)
    sections = None if kw.get('multi') else _sections(items)
    if not sections:
        return _select_flat(message, items, **kw)
    # 'whole': None until / is pressed: then the list's length decides, each time.
    memo = _section_memo.setdefault(tuple(t for t, _ in sections), {'whole': None, 'open': None})
    if memo['whole'] is None and len(items) <= _visible_rows() * _SECTION_SLACK:
        return _select_flat(message, items, **kw)
    # The row the caller wants to land on (where the list was left).
    place = kw.get('place')
    want = (place.value if place is not None and place.value is not None
            else items[max(0, min(kw.get('index', 0), len(items) - 1))].value)
    def _with_toggle(to, label) -> dict:
        return {**kw, 'shortcuts': {**(kw.get('shortcuts') or {}), 'list.sections': to},
                'extra_hints': {**(kw.get('extra_hints') or {}), 'list.sections': label}}

    while True:
        if memo['whole']:
            res = _select_flat(message, items, **_with_toggle(_TO_SECTIONS, "sections"))
            if res is _TO_SECTIONS:
                memo['whole'] = False
                continue
            return res
        titles = [t for t, _ in sections]
        if memo['open'] in titles:
            rows = sections[titles.index(memo['open'])][1]
            pos = next((i for i, it in enumerate(rows) if not it.disabled and it.value == want), 0)
            res = _select_flat(memo['open'] if not message else f"{message}  {memo['open']}", rows,
                               **{**_with_toggle(_TO_WHOLE, "whole list"), 'index': pos})
            if res is _TO_WHOLE:
                memo['whole'], memo['open'] = True, None
                continue
            if res is None:                     # back to the section titles
                want = memo['open']
                memo['open'] = None
                continue
            return res
        here = next((t for t, rows in sections if any(it.value == want for it in rows)), want)
        res = _select_flat(message, [Choice(t, value=t, cells=[t, f"{sum(not it.disabled for it in rows)}"])
                                     for t, rows in sections],
                           header=kw.get('header'), allow_back=kw.get('allow_back', True),
                           columns=[Column(style='primary'), Column(style='dynamic-dim', flex=True)],
                           index=titles.index(here) if here in titles else 0,
                           shortcuts={'list.sections': _TO_WHOLE}, extra_hints={'list.sections': "whole list"})
        if res is _TO_WHOLE:
            memo['whole'] = True
            continue
        if res is None:
            return None
        memo['open'] = res
        want = None


def _select_flat(message: str, choices: list, *,
           header: list | None | Callable[[], list[str]] = None,
           extra_hints: dict[str, str] | None = None,
           index: int = 0,
           shortcuts: dict[str, str] | None = None,
           columns: list | None = None,
           multi: bool = False,
           interlock_category_callback: Callable[[Any], str] | None = None,
           on_inspect: Callable[[Any], None] | None = None,
           inspect_key: str = 'd',
           row_actions: dict[str, Callable[[Any], None]] | None = None,
           row_action_hints: dict[str, str] | None = None,
           row_edit: Callable[[Any], list] | None = None,
           row_edit_commit: Callable[[Any, str], None] | None = None,
           row_edit_col: int = 1,
           row_edit_key: str = 'e',
           allow_back: bool = True,
           actions: list[tuple[str, str, str]] | None = None,
           on_move: Callable[[Any, int], bool] | None = None,
           place: ListPlace | None = None,
           row_action_applies: Callable[[str, Any], bool] | None = None,
           list_actions: dict[str, Callable[[], Any]] | None = None,
           list_action_hints: dict[str, str] | None = None,
           choose_label: str | None = None,
           trail: list | None = None,
           preview: Callable[[Any], Any] | None = None,
           ) -> Any:
    """Arrow keys to navigate; Enter / → to confirm; ← / b / Esc → None; q quits the app.

    When multi=True, Space toggles the current item and Enter returns a list of
    all checked values (possibly empty).  Otherwise returns the single selected
    value, or None if cancelled.

    Args:
        message:    Prompt label shown above the list.
        choices:    Items: str, dict, or Choice objects.
        header:     Optional lines rendered above the prompt: a callable
                    returning them, rebuilt every frame, for anything sized to
                    the window (a boxed header), or it keeps its first width.
        extra_hints: Extra key→action bindings merged into the hint bar.
        index:      Initial cursor position.
        place:      A ListPlace to start from and record where the list was left
                    (instead of index), for a list rebuilt each time round.
        shortcuts:  Optional key→return-value map (single-select only).
        columns:    Column layout descriptors (see Column dataclass).
        multi:      Enable multi-select mode (Space to toggle, Enter returns list).
        interlock_category_callback: When set, only one category can be checked
            at a time (multi=True only).
        on_inspect: Called with the current row's value when `inspect_key` is
            pressed; runs its own view and returns, leaving selection/checkbox
            state intact (the list redraws afterwards). If it returns something
            other than None, select() returns that instead, for a caller that
            must rebuild the list after the view changed what it shows.
        inspect_key: Key that triggers `on_inspect` (default 'd').
        row_actions: key→callback(current row value) map. Pressing the key runs
            the callback against the highlighted row and stays in the list (like
            on_inspect, but any number of keys), e.g. queue the current track.
            A callback that returns something other than None ends the list
            with that as the result, for a caller that must rebuild it.
        row_action_hints: key→label map surfaced in the hint bar for row_actions.
        row_edit:   row value → the values `row_edit_key` cycles that row through,
            its current one first. Each press steps to the next, and one step past
            the last is an inline text field seeded from where you left off, so a
            value that isn't on the list can just be typed. ↑↓ cycle too (a way
            back out of the field), ↵ commits, Esc abandons. Columns-only, since
            the edit happens inside a cell.
        row_edit_commit: called with (row value, chosen text) on ↵.
        row_edit_col: which cell of the row the editing happens in.
        row_edit_key: the key that opens the cycle and advances it (default 'e').
        allow_back: when False, the cancel keys (←/b/Esc) are ignored so the
            list can only move forward (Enter) or quit (q), used for top-level
            menus that have nowhere to go back to.
        actions:    (key, label, value) list-wide actions (play all, shuffle…),
            kept out of the rows so the cursor only moves through the list
            itself: each is listed first in the hint bar, and its key (or a
            click on it there) returns value.
        on_move:    (row value, -1 up / +1 down) → whether the caller moved it.
            The list.move_up / list.move_down keys call it for the highlighted row, and on
            True the row swaps with its neighbour on screen and the cursor
            follows. Never called past a separator or the ends of the list.
        row_action_applies: (row action key or id, row value) → whether that
            action means something for the row; the others are left out of
            the row's options menu (and do nothing on it).
        list_actions: key or id → callback() acting on the whole list and
            staying in it, like row_actions without a row; labels in
            list_action_hints. A callback returning something other than None
            ends the list with that.
        choose_label: what ↵ does to a row ("Play", "Open"), for the top of
            the row's options menu.
        trail:      the levels above this list (Trail, oldest first): with
            preview, the list becomes a column browser, its box holding them
            as columns left of the list, as many as fit. A click on a row of
            one returns JumpTo(its depth, the row's value).
        preview:    highlighted row's value → None or a callable (width,
            height) → lines (or a Pane, with pictures) for the last column:
            what the row holds, or its details. column_widths sizes the
            columns; list.columns (v) turns the browser off and on.

    Options menus: list.row_options (o) lists everything that can be done
    with the highlighted row (↵, on_inspect, the row actions that apply),
    list.list_options (O) everything for the whole list (actions, shortcuts,
    list_actions), each with its key, so actions without a key are
    reachable too. Picking one does exactly what its key does.
    """
    items = _norm(choices)
    # The list's options menu (O) leads with its actions, then its shortcuts.
    _list_specs = [k for k, _label, _v in (actions or [])] + [k for k in (shortcuts or {})
                                                                  if k not in {a[0] for a in actions or []}]
    # Any key below may be given as an action id (backbone.keys): every key
    # bound to it works, and its hint shows them.
    if actions:
        shortcuts   = {**(shortcuts or {}), **{k: v for k, _label, v in actions}}
        extra_hints = {**{k: label for k, label, _v in actions}, **(extra_hints or {})}
    # The options menus list these as given (an action may have no key yet),
    # with the caller's labels; an id also works as its own key, for the menu.
    _labels = {**(extra_hints or {}), **(row_action_hints or {}), **(list_action_hints or {})}
    _row_specs = list(row_actions or {})
    _list_act_specs = list(list_actions or {})
    shortcuts = {**keys.expand(shortcuts), **(shortcuts or {})}
    row_actions = {**keys.expand(row_actions), **(row_actions or {})}
    list_actions = {**keys.expand(list_actions), **(list_actions or {})}
    extra_hints = {keys.hint_for(k): v for k, v in (extra_hints or {}).items()}
    row_action_hints = {keys.hint_for(k): v for k, v in (row_action_hints or {}).items()}
    row_action_hints.update({keys.hint_for(k): v for k, v in (list_action_hints or {}).items()})
    inspect_keys = keys.keys_for(inspect_key) + (inspect_key,)
    row_edit_keys = keys.keys_for(row_edit_key)
    row_edit_key = keys.hint_for(row_edit_key)          # for the hints
    if not items:
        return None

    selectable = [i for i, it in enumerate(items) if not it.disabled]
    if not selectable:
        return None

    def _step(cur: int, direction: int) -> int:
        """Move to the next selectable row, skipping disabled separators."""
        n = len(items)
        nxt = (cur + direction) % n
        steps = 0
        while items[nxt].disabled and steps < n:
            nxt = (nxt + direction) % n
            steps += 1
        return nxt

    def _nearest_selectable(idx: int) -> int:
        """Closest selectable row to idx (used after page jumps / clamps)."""
        return min(selectable, key=lambda s: abs(s - idx))

    if place is not None:
        index = next((i for i, it in enumerate(items)
                      if place.value is not None and not it.disabled and it.value == place.value),
                     place.pos)
    cursor   = max(0, min(index, len(items) - 1))
    if items[cursor].disabled:
        cursor = _step(cursor, 1)
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = _get_term_attrs(fd)
    w        = _Widget(fd)

    # Interlock state for multi-select: track which category is locked
    _locked_category: list[str | None] = [None]

    def _update_interlock() -> None:
        """Lock selection to the category of the first checked item, disabling every non-matching row."""
        if not multi or interlock_category_callback is None:
            return
        checked = [it for it in items if it.checked]
        if not checked:
            _locked_category[0] = None
            for it in items:
                it.disabled = False
            return
        _locked_category[0] = interlock_category_callback(checked[0].value)
        for it in items:
            if not it.checked:
                it.disabled = (interlock_category_callback(it.value) != _locked_category[0])

    _update_interlock()

    base_hints: dict[str, str]
    # Toggle-all ('a') is offered only where it can't misbehave: multi-select with
    # no category interlock and no caller shortcut already bound to 'a'.
    _toggle_all_ok = multi and interlock_category_callback is None and not (
        shortcuts and any(k in shortcuts for k in keys.of("list.toggle_all")))
    _back_hint = {L("list.back", most=2): "back"} if allow_back else {}
    _move = {L("list.up", "list.down"): "move"}
    _end = {L("list.quit"): "quit app", L("list.choose", most=1): "confirm"}
    if multi:
        base_hints = {**_move, L("list.toggle"): "toggle", **_back_hint, **_end}
        if _toggle_all_ok:
            base_hints = {**_move, L("list.toggle"): "toggle", L("list.toggle_all"): "all",
                          **_back_hint, **_end}
    else:
        base_hints = {**_move, **_back_hint, **_end}

    if extra_hints:
        combined_hints = {**extra_hints, **base_hints}
    else:
        combined_hints = base_hints
    # Row-action keys (e.g. queue the current track) sit with the other action
    # hints, before the navigation keys.
    if row_action_hints:
        combined_hints = {**{k: v for k, v in combined_hints.items() if k not in base_hints},
                          **row_action_hints, **base_hints}
    if on_move is not None:
        combined_hints = {**{k: v for k, v in combined_hints.items() if k not in base_hints},
                          move_hint()[0]: move_hint()[1], **base_hints}
    _opt_hints = {}
    if on_inspect is not None or _row_specs or choose_label:
        _opt_hints[L("list.row_options")] = "options"
    if _list_specs or _list_act_specs:
        _opt_hints[L("list.list_options")] = "list options"
    if trail or preview is not None:
        _opt_hints[L("list.columns")] = "columns"
    combined_hints = {**{k: v for k, v in combined_hints.items() if k not in base_hints},
                      **_opt_hints, **base_hints}

    def _name(spec) -> str:
        """A menu label for a key or action id: what the action does (the hint
        text is too terse for a menu), else what the caller calls the key."""
        text = keys.describe(spec) or _labels.get(spec) or str(spec)
        return text[:1].upper() + text[1:]

    def _row_menu() -> list:
        it = items[cursor]
        if it.disabled:
            return []
        out = []
        if choose_label and not multi:
            out.append((choose_label, L("list.choose", most=1), ("choose", None)))
        if on_inspect is not None:
            out.append((_name(inspect_key), keys.hint_for(inspect_key), ("key", inspect_key)))
        for spec in _row_specs:
            if row_action_applies is None or row_action_applies(spec, it.value):
                out.append((_name(spec), keys.hint_for(spec), ("key", spec)))
        return out

    def _list_menu() -> list:
        return ([(_name(s), keys.hint_for(s), ("key", s)) for s in _list_specs]
                + [(_name(s), keys.hint_for(s), ("list", s)) for s in _list_act_specs])

    # Inline row edit (opt-in, see row_edit): the cycle sits at _edit_i over
    # _edit_opts, with one position past the end being the text field. While
    # typing, every key belongs to the buffer, including 'q' and the cycle key
    # itself, which is why leaving the field is ↑↓/↵/Esc and nothing else.
    _edit_on    = False
    _edit_opts: list = []
    _edit_i     = 0
    _edit_buf: list = []
    _edit_pos   = 0
    _edit_hints = {row_edit_key: "next", "↑↓": "cycle",
                   "↵": "set", "esc": "cancel"}
    if row_edit is not None:
        combined_hints = {row_edit_key: "edit",
                          **{k: v for k, v in combined_hints.items() if k != row_edit_key}}

    _last_hlen = [0]
    _col_geo: list = [None]  # the column browser's last layout, for clicks (see _lines)
    _scrolling = [False]          # the highlighted row is cut off and scrolls: redraw it as it moves
    _scrolled_at = [0.0]
    # Maps a visible item index → its ANSI-stripped rendered text, so a mouse
    # click can tell whether it landed on a printed character or blank space.
    _row_plain: dict[int, str] = {}
    # Maps an absolute (row, col) on a hint line → the key that clicking that
    # bright glyph should replay through the normal key handling below.
    _hint_cells: dict[tuple[int, int], str] = {}


    def _help_key_free() -> bool:
        """Whether `?` can toggle the hints here: not bound by this list, and
        not being typed into a cell."""
        def bound(k):
            return (k in shortcuts or k in row_actions
                    or (on_inspect is not None and k in inspect_keys)
                    or (row_edit is not None and k in row_edit_keys))
        return not (_edit_on or any(bound(k) for k in keys.of("global.help")))

    def _editing_text() -> bool:
        """Whether the cycle has stepped past its options into the text field."""
        return _edit_i >= len(_edit_opts)

    def _edit_cell() -> list:
        """The cell under edit, as styled segments: a cycled option, or the live
        text field. Segments rather than raw ANSI: the table measures a cell by
        the length of its text, so escape codes inside one would be counted as
        visible and the cell truncated to nothing."""
        if not _editing_text():
            # ▾ marks a value being stepped through rather than one already set.
            return [("▾ ", 'accent'), (_edit_opts[_edit_i], 'primary')]
        text = "".join(_edit_buf)
        head = [("✎ ", 'accent')]
        if _edit_pos >= len(text):
            return head + [(text, 'primary'), (" ", 'cursor')]
        return head + [(text[:_edit_pos], 'primary'), (text[_edit_pos], 'cursor'),
                       (text[_edit_pos + 1:], 'primary')]

    def _details(pv, width: int, height: int) -> list:
        """The preview's details drawn `width` × at most `height`."""
        return pv.details(width, height) if pv.details is not None and height > 0 and width > 0 else []

    def _place_pictures(lines, line0: int, col0: int) -> None:
        """Put `lines`' pictures on screen: lines[k] on out[line0 + k] (render()
        lays out[j] at screen row 1 + top_margin() + j), its column 0 at col0."""
        for line, col, rows, key, esc, *size in getattr(lines, 'pictures', ()):
            w.pictures.append((1 + ui.top_margin() + line0 + line, col0 + col, rows, key, esc, *size))

    _shape: list = [None]

    def _level_shape() -> tuple[int, int, int]:
        """(the preview width, the contents rows, the list's own width) its
        rows would like at most: worked out once for the list (its longest
        rows sampled), so the columns hold still as you move through it and
        change only at another level."""
        if _shape[0] is None:
            live = [it for it in items if not it.disabled]
            step = max(1, len(live) // _SHAPE_SAMPLE)    # ponytail: a sample of a huge list, not all of it
            sample = live[::step]
            pvs = [p for p in (preview(it.value) for it in sample) if p is not None] if preview else []
            wide = max((sum(ui.visual_len(str(c)) for c in it.cells) + COL_GAP * (len(it.cells) - 1)
                        if it.cells else ui.visual_len(ui.strip_ansi(str(it.title))) for it in sample), default=0)
            _shape[0] = (max((p.want for p in pvs), default=0), max((len(p.contents) for p in pvs), default=0), wide)
        return _shape[0]

    def _clear_of_tab(pw: int, trails_w: int) -> int:
        """The preview's width moved a few columns, if need be, so the edge
        between the browser's box and the preview's isn't under the showing
        tab (whose outline opens into the box under it: ui.tab_notch); the
        shorter way, as far as both keep their least. Else as it was."""
        notch = ui.tab_notch() if pw else None
        if not notch:
            return pw
        a, b = notch
        mh = ui.MARGIN_H
        right = mh + _cols() - pw - mh - 1                   # the browser box's right corner, 0-based
        if b <= right or a >= right + mh + 1:
            return pw
        fits = []
        if pw - (b - right) >= _COL_PREVIEW[0]:              # the edge right of the tab: the browser's
            fits.append(pw - (b - right))
        move = right - (a - mh - 1)                          # the preview's corner at the tab's left side
        if _cols() - pw - move - mh - 4 - trails_w >= _COL_MAIN_MIN[0]:
            fits.append(pw + move)
        return min(fits, key=lambda w: abs(w - pw)) if fits else pw

    def _contents_box(pv, width: int, height: int, title: bool = True) -> list:
        """What the highlighted row holds, in a box `width` × `height`."""
        room = height - 2
        rows = [f"{C.DIM}{ui.truncate_text(str(x), width - 4)}{C.RESET}" for x in pv.contents[:room]]
        if len(pv.contents) > room > 0:
            rows[-1] = f"{C.DIM}… {len(pv.contents) - room + 1} more{C.RESET}"
        return box_lines(rows, width, height, pv.contents_title if title else "", focused=False)

    def _preview_column(pv, pw: int, height: int, browser_w: int, first: int) -> list:
        """The preview beside the browser: its details in one box over what
        it holds in another, together `height` rows."""
        inner, col0 = pw - 4, 1 + ui.MARGIN_H + browser_w + ui.MARGIN_H + 2
        # What the rows hold gets the rows the most any of them needs, up to
        # half: the same for every row, so the picture above doesn't resize.
        most = _level_shape()[1]
        held = max(5, min(most + 2, height // 2)) if most else 0
        if most and pv.details_rows is not None:      # the details' picture as wide as the box first
            held = max(5, height - (pv.details_rows(inner) + 2))
        det = _details(pv, inner, height - 2 - held)
        det_h = len(det) + 2 if det else 0
        if not most or height - det_h < 3:
            det, det_h = (_details(pv, inner, height - 2) if not det else det), height
        _place_pictures(det, first, col0)
        out = box_lines(det, pw, det_h, focused=False) if det_h else []
        if det_h < height:
            out += _contents_box(pv, pw, height - det_h)
        return out

    def _preview_strip(pv, height: int, line0: int) -> list:
        """The preview in a narrow window: its details in a strip under the
        browser, `height` rows, its first line out[line0]."""
        total = _cols()
        # What it holds beside the details, when there's room for both.
        cw = (total - ui.MARGIN_H) * 2 // 5 if pv.contents and total >= _STRIP_SPLIT else 0
        dw = total - (cw + ui.MARGIN_H if cw else 0)
        det = _details(pv, dw - 4, height - 2)
        _place_pictures(det, line0 + 1, 1 + ui.MARGIN_H + 2)
        left = box_lines(det, dw, height, focused=False)
        if not cw:
            return left
        return [a + b for a, b in zip(left, _contents_box(pv, cw, height))]

    def _browser_rows(body, shown, main_in, height, first, lead) -> list:
        """The column browser's rows inside its box: the levels above that fit
        (`shown`: (trail, width)), then the list (`body`, its rows' margin
        dropped), the levels' rows from the top of the box (`lead` lines down:
        the message), not moved by the list's "N above".
        Records where each column landed (_col_geo), for clicks."""
        sep = f" {C.DIM}│{C.RESET} "
        x = 1 + ui.MARGIN_H + 2                  # the screen column inside "│ "
        trails, columns = [], []
        for t, tw in shown:
            lines, top = trail_lines(t, tw, height - lead)
            trails.append((x, x + tw - 1, top - lead, t.values, t.depth, lead))
            columns.append(([""] * lead + lines, tw))
            x += tw + 3
        main = (x, x + main_in - 1)
        columns.append(([ln[ui.MARGIN_H:] if ln.startswith(" " * ui.MARGIN_H) else ln for ln in body], main_in))
        _col_geo[0] = {'first': first, 'main': main, 'trails': trails}

        def cell(lines, k, width):
            text = _clip_ansi(lines[k], width) if k < len(lines) else ""
            return text + C.RESET + " " * max(0, width - ui.visual_len(text))
        return [sep.join(cell(lines, k, width) for lines, width in columns) for k in range(height)]

    def _lines():
        nonlocal viewport
        # Boxed when the window has room: a PanelTitle header becomes the box's
        # title, any other header stays above the box.
        boxed = box_fits()
        ptitle, h_lines = panel_header(header)
        _scrolling[0] = False
        # A column browser, when there's room: the levels above and the preview
        # share the box with the list, which gets what's left.
        shown, pw, strip, pview, strip_h = [], 0, False, None, 0
        if boxed and (trail or preview is not None) and columns_shown() and items:
            pview = preview(items[cursor].value) if preview is not None else None
            shape = _level_shape()
            shown, pw, strip = column_widths(_cols(), trail or [], pview,
                                             shape[0] if preview is not None else None, shape[2])
        pw = _clear_of_tab(pw, sum(tw + 3 for _t, tw in shown))
        browser_w = _cols() - (pw + ui.MARGIN_H if pw else 0)       # the browser's box
        main_in = browser_w - 4 - sum(tw + 3 for _t, tw in shown)   # the list's own column
        cols    = main_in + 2 if boxed else _cols()  # rows lose their 2-space margin inside the box
        # Refresh the now-playing box height up front so this frame's row budget
        # (vis) and hint pinning match the box that render() will actually draw;
        # otherwise a just-appeared box paints over the pinned hints until the
        # next redraw (hints missing until you click/navigate).
        ui.footer_lines(ui.get_terminal_width())
        _row_plain.clear()

        max_header_w = 0
        for hl in h_lines:
            plain_hl = ui.strip_ansi(hl)
            plain_hl = re.sub(r'[╭─│╰╮╯┌┐└┘├┤┬┴┼═║╔╗╚╝]', '', plain_hl).strip()
            max_header_w = max(max_header_w, ui.visual_len(plain_hl))

        layout_constraint = " " * max_header_w if (0 < max_header_w < cols - 20) else ""

        # The transport keys are surfaced here whenever background audio is
        # playing (recomputed each render so they appear/vanish live); see
        # `chrome_hint_pairs`.
        # One source for both the row budget below and the bar actually painted
        # at the end of this function: they must agree or the list mis-sizes.
        hints_now  = _edit_hints if _edit_on else combined_hints
        hint_lines = chrome_hint_lines(hints_now, extra=layout_constraint)
        # A header over the list (a file's details, a picture: more than a
        # title's one line) goes in a window too short to keep it and
        # _LIST_ROWS_MIN rows of the list; it's back when the window grows.
        if len(h_lines) > 1 and (_hint_pin_target() - len(h_lines) - len(hint_lines) - (4 if boxed else 2)
                                 < min(len(items), _LIST_ROWS_MIN)):
            h_lines = []

        # Lines inside the box (or under the header) before the rows: a panel's
        # subtitle, then the message, which a panel only shows when it says something.
        # Boxed, the panel's title (else the message) and its subtitle are in
        # the box's top border; unboxed, the message is a line over the rows.
        pre = [] if boxed or not message.strip() else [f"  {C.DIM}{message}{C.RESET}"]
        box_title = ptitle.title if ptitle else message.strip().rstrip(":")
        # Under a panel's title, a message that says more than a label
        # ("Albums:") joins its subtitle in the border: a section's name.
        said = message.strip() if ptitle and message.strip() and not message.strip().endswith(":") else ""
        box_right = border_right(" · ".join(x for x in ((ptitle.subtitle if ptitle else None), said) if x),
                                 _help_key_free() if not h_lines else None)
        if boxed:
            # The box runs from under any header down to the hint bar.
            box_h = max(4, _hint_pin_target() - len(h_lines) - len(hint_lines))
            if strip:                                           # the preview's strip, under the box
                inner = _cols() - 4
                det_rows = pview.details_rows(inner) if pview.details_rows else len(_details(pview, inner, box_h))
                strip_h = strip_rows(box_h, len(items) + len(pre), max(det_rows, min(len(pview.contents), 12)) + 2)
            box_h -= strip_h
            vis = max(2, box_h - 2 - len(pre) - 2)
        else:
            # Non-item lines this widget emits: header + message + the two
            # above/below indicator rows (always present) + hints.
            fixed_overhead = len(h_lines) + len(hint_lines) + 3
            vis     = max(2, _visible_rows() - fixed_overhead)

        n       = len(items)
        # The "N above" row only once the list has scrolled: till then the
        # first row sits right under the border (or the message).
        viewport = _scroll(viewport, cursor, vis + 1, items)
        if viewport > 0:
            viewport = _scroll(viewport, cursor, vis, items)
        else:
            vis += 1

        out = pre[:]
        if viewport > 0:
            out.append(f"  {C.DIM}╵ {viewport} above{C.RESET}")
        # A row of a list whose rows open something carries a › at its right.
        opens = choose_label == "Open"
        row_room = cols - ui.MARGIN_H - (2 if opens else 0)

        def _row(text: str, i: int, current: bool) -> str:
            """A row (`text`, its margin already off) fitted to the list's
            width: the › of a row that opens, and the highlight bar on the
            highlighted one."""
            text = _clip_ansi(text, row_room)
            text += " " * max(0, row_room - ui.visual_len(text))
            if opens:
                text += f" {C.DIM}›{C.RESET}" if not items[i].disabled else "  "
            return " " * ui.MARGIN_H + (ui.on_bar(text, row_room + (2 if opens else 0)) if current else text)
        row_at: dict[int, int] = {}              # item index → its line in `out`

        # Structured columns: compute table widths once from each item's cells.
        # Rows without cells (headings/separators) fall back to plain rendering.
        eff: int = 0
        col_widths: list[int] = []

        def _cells_of(i: int) -> list:
            """A row's cells, with the edited one swapped in while it is live,
            and the highlighted row's scrolling cell (Column.scroll, else the
            first) moving through its text when it's cut off."""
            cells = items[i].cells
            at = next((k for k, c in enumerate(columns or []) if c.scroll), 0)
            if _edit_on and i == cursor and cells and 0 <= row_edit_col < len(cells):
                cells = list(cells)
                cells[row_edit_col] = _edit_cell()
            elif i == cursor and cells and len(col_widths) > at and len(cells) > at \
                    and isinstance(cells[at], str) and ui.visual_len(cells[at]) > col_widths[at] > 0:
                cells = list(cells)
                cells[at] = ui.marquee(cells[at], col_widths[at], time.monotonic())
                _scrolling[0] = True
            return cells

        if columns:
            eff = min(ui.MARGIN_H + row_room, _COLUMNS_MAX_WIDTH)
            rows_cells = [_cells_of(i) for i in range(len(items)) if items[i].cells]
            vis_cells = [_cells_of(i) for i in range(viewport, min(viewport + vis, len(items)))
                         if items[i].cells]
            col_widths = _table_widths(rows_cells, columns, eff,
                                       pointer_w=6 if multi else 4, right_margin=_EDGE_MARGIN,
                                       visible_cells=vis_cells)

        for i in range(viewport, min(viewport + vis, n)):
            current = i == cursor and not items[i].disabled
            if columns and items[i].cells:
                line = _render_table_row(
                    _cells_of(i), columns, i == cursor, col_widths, eff, _EDGE_MARGIN,
                    is_checked=items[i].checked if multi else None,
                    disabled=items[i].disabled)
                out.append(_row(line[ui.MARGIN_H:], i, current))
                row_at[i] = len(out) - 1
                continue

            _ct = items[i].cursor_title
            label = str(_ct if (_ct is not None and i == cursor) else items[i].title)
            max_w = row_room - (5 if multi else 2)
            if ui.visual_len(label) > max_w:             # measured as shown: a label may carry styles
                if i == cursor:                          # the highlighted row scrolls through it
                    label = ui.marquee(ui.strip_ansi(label), max_w, time.monotonic())
                    _scrolling[0] = True
                else:
                    label = _clip_ansi(label, max_w - 1) + f"{C.RESET}…"
            if multi:
                glyph = (f"{C.GREEN}✔{C.RESET}" if items[i].checked else f"{C.DIM}•{C.RESET}")
                if items[i].disabled and not items[i].checked:
                    text = f"  {C.DIM}• {label}{C.RESET}"        # interlocked: dimmed, not selectable
                else:
                    text = f" {glyph} " + (f"{C.PRIMARY}{C.BOLD}{label}{C.RESET}" if i == cursor else label)
                out.append(_row(text, i, i == cursor))
            elif items[i].disabled:
                # Section heading / separator: dim, slightly outdented.
                out.append(_row(f"{C.DIM}{C.BOLD}{label}{C.RESET}" if label else "", i, False))
            elif current:
                out.append(_row(f" {C.PRIMARY}{C.BOLD}{label}{C.RESET}", i, True))
            else:
                out.append(_row(f" {label}", i, False))
            row_at[i] = len(out) - 1
        remaining = n - viewport - vis
        out.append(f"  {C.DIM}╷ {remaining} below{C.RESET}" if remaining > 0 else "")
        first = len(h_lines) + (1 if boxed else 0)       # where `out` starts on screen
        w.pictures = []
        _col_geo[0] = None
        # A header's pictures (an image in it): its line k is on screen row 1 + top_margin() + k.
        for line, col, rows, key, esc, *size in getattr(h_lines, 'pictures', ()):
            w.pictures.append((1 + ui.top_margin() + line, col + 1, rows, key, esc, *size))
        if boxed and (shown or pw or strip_h):
            browser = box_lines(_browser_rows(out, shown, main_in, box_h - 2, first, len(pre)),
                                browser_w, box_h, box_title, box_right)
            if pw:
                browser = [b + p for b, p in zip(browser, _preview_column(pview, pw, box_h, browser_w, first))]
            out = h_lines + browser + (_preview_strip(pview, strip_h, len(h_lines) + box_h) if strip_h else [])
        elif boxed:
            out = h_lines + box_lines([ln[ui.MARGIN_H:] if ln.startswith(" " * ui.MARGIN_H) else ln for ln in out],
                                      _cols(), box_h, box_title, box_right)
        else:
            out = h_lines + out
        # Where the rows landed, for clicks: the first row's line, and each row's
        # plain text, to tell a click on a character from one on blank space.
        _last_hlen[0] = first + len(pre) + (1 if viewport > 0 else 0)
        for i, k in row_at.items():
            if first + k < len(out):                 # a row the box had no room for has no text to click
                _row_plain[i] = _plain(out[first + k])
        # Inset the hint block by the left margin so it never hugs an edge; _hint
        # centres within _cols() (= width-2*MARGIN_H), so this makes it symmetric.
        # Pin the hint bar to the bottom (just above the miniplayer + status) so
        # its keys keep a fixed screen position across redraws / list sizes.
        append_chrome(out, hints_now, _hint_cells, extra=layout_constraint, help_key=_help_key_free())
        # Hard guarantee: no rendered line ever exceeds the terminal width, so
        # the list can never wrap no matter how narrow the window is.
        _w = ui.get_terminal_width()          # once per frame, not per line
        return [_clip_ansi(line, _w) for line in out]

    result = None
    _sel_last_click: int | None = None
    _drop_backdrop = lambda: None      # noqa: E731 (none until the loop's first frame)
    try:
        _set_raw(fd)
        enable_mouse()
        screen_takeover_next()   # paint over the previous screen, no flash
        w.render(_lines())

        def _relaid() -> None:
            ui.clear_screen()
            w.anchor_reset()
            w.render(_lines())
        _drop_backdrop = screen_backdrop(_relaid)

        while True:
            if ui.consume_resize():
                _relaid()
                continue

            if not _wait_for_keypress(0.05):
                if _scrolling[0] and time.monotonic() - _scrolled_at[0] >= ui.MARQUEE_STEP_S:
                    _scrolled_at[0] = time.monotonic()
                    w.render(_lines())
                continue

            key = _read_key(fd)
            # Transport keys, clicks on the now-playing box, and clicks on our own
            # hint glyphs are all handled once, here, before the switch below:
            # box → transport/open, hint → replay its key.
            _ch = consume_chrome(key, _hint_cells, free_keys=not _edit_on)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _sel_last_click = None; w.anchor_reset(); w.render(_lines()); continue
            if _ch is not None:
                key = _ch                # replay the hint's key through the switch
            if _edit_on:
                # Editing owns every key: nothing here may fall through to the
                # list's own navigation, and while the text field is live that
                # includes 'q' and the cycle key (an artist name may contain
                # either). ↑↓ step the cycle, which is also the way back out of
                # the field and on to the next option.
                if key == 'ESC':
                    _edit_on = False
                elif key == 'ENTER':
                    chosen = ("".join(_edit_buf) if _editing_text()
                              else _edit_opts[_edit_i]).strip()
                    if chosen and row_edit_commit is not None:
                        row_edit_commit(items[cursor].value, chosen)
                    _edit_on = False
                elif key in ('UP', 'DOWN') or (key in row_edit_keys and not _editing_text()):
                    step = -1 if key == 'UP' else 1
                    was = (_edit_opts[_edit_i] if not _editing_text()
                           else "".join(_edit_buf))
                    _edit_i = (_edit_i + step) % (len(_edit_opts) + 1)
                    if _editing_text():
                        # Seed the field from wherever the cycle left off, so it
                        # opens on something to amend rather than empty.
                        _edit_buf = list(was)
                        _edit_pos = len(_edit_buf)
                elif _editing_text() and (new_pos := edit_line(_edit_buf, _edit_pos, key)) is not None:
                    _edit_pos = new_pos
                _sel_last_click = None
                w.render(_lines())
                continue

            act = keys.action(key, "list")
            if act in ("list.row_options", "list.list_options"):
                # Everything for the row (o) or the whole list (O): picking
                # an entry does what its key does, from here on.
                entries = _row_menu() if act == "list.row_options" else _list_menu()
                title = (str(items[cursor].title) if act == "list.row_options" else message) or "This list"
                picked = options_menu(title, entries) if entries else None
                enable_mouse()
                sys.stdout.flush()
                _sel_last_click = None
                w.anchor_reset()
                if picked is None:
                    w.render(_lines()); continue
                kind, spec = picked
                if kind == "choose":
                    result = items[cursor].value; break
                if kind == "list":
                    _ret = list_actions[spec]()
                    if _ret is not None:
                        result = _ret; break
                    w.render(_lines()); continue
                key, act = spec, None           # as if its key were pressed
            if   key == 'CTRL_C':                break
            elif key in row_edit_keys and row_edit is not None and not items[cursor].disabled:
                # Open the cycle on the row's current value; a second press steps
                # to the next option (see the edit block above).
                _edit_opts = [str(o) for o in (row_edit(items[cursor].value) or []) if str(o)]
                _edit_i = 0
                _edit_buf = list(_edit_opts[0]) if _edit_opts else []
                _edit_pos = len(_edit_buf)
                _edit_on = True
                _sel_last_click = None
                w.render(_lines())
            elif act == 'list.up':               cursor = _step(cursor, -1);          _sel_last_click = None; w.render(_lines())
            elif act == 'list.down':             cursor = _step(cursor, 1);           _sel_last_click = None; w.render(_lines())
            elif act == 'list.top':              cursor = selectable[0];              _sel_last_click = None; w.render(_lines())
            elif act == 'list.bottom':           cursor = selectable[-1];             _sel_last_click = None; w.render(_lines())
            elif act == 'list.page_up':          cursor = _nearest_selectable(max(0, cursor - _visible_rows())); _sel_last_click = None; w.render(_lines())
            elif act == 'list.page_down':        cursor = _nearest_selectable(min(len(items) - 1, cursor + _visible_rows())); _sel_last_click = None; w.render(_lines())
            elif act == 'list.toggle' and multi:
                it = items[cursor]
                if not it.disabled or it.checked:
                    if interlock_category_callback and _locked_category[0] and not it.checked:
                        cat = interlock_category_callback(it.value)
                        if cat != _locked_category[0]:
                            sys.stdout.write("\a"); sys.stdout.flush(); continue
                    it.checked = not it.checked
                    _update_interlock()
                    selectable[:] = [i for i, x in enumerate(items) if not x.disabled or x.checked]
                    w.render(_lines())
            elif act == 'list.toggle_all' and _toggle_all_ok:
                # Toggle every selectable row at once: check all, or clear all if
                # everything is already checked.
                targets = [it for it in items if not it.disabled]
                make_checked = any(not it.checked for it in targets)
                for it in targets:
                    it.checked = make_checked
                selectable[:] = [i for i, x in enumerate(items) if not x.disabled or x.checked]
                _sel_last_click = None
                w.render(_lines())
            elif act == 'list.choose':
                if multi:
                    result = [it.value for it in items if it.checked]; break
                elif not items[cursor].disabled:
                    result = items[cursor].value; break
            elif act == 'list.back':
                if allow_back:
                    result = None; break
                # Top-level menu: no back/cancel, only forward or quit.
            elif act == 'list.quit':             raise QuitToTerminal()
            elif on_inspect is not None and key in inspect_keys and not items[cursor].disabled:
                # Inspect the current row (e.g. a full detail view) without
                # ending selection or losing checkbox state. The callback runs
                # its own full-screen prompt, so re-arm mouse reporting and force
                # a full redraw when it returns.
                _ret = on_inspect(items[cursor].value)
                if _ret is not None:
                    result = _ret; break
                enable_mouse()
                sys.stdout.flush()
                _sel_last_click = None
                w.anchor_reset()
                w.render(_lines())
            elif list_actions and key in list_actions:
                _ret = list_actions[key]()
                if _ret is not None:
                    result = _ret; break
                enable_mouse()
                sys.stdout.flush()
                w.anchor_reset()
                w.render(_lines())
            elif (row_actions and key in row_actions and not items[cursor].disabled
                  and (row_action_applies is None
                       or row_action_applies(next((s for s in _row_specs if key in keys.keys_for(s)), key),
                                             items[cursor].value))):
                # Act on the highlighted row (e.g. queue this track) and stay in
                # the list; the callback shows its own status; we just redraw.
                _ret = row_actions[key](items[cursor].value)
                if _ret is not None:
                    result = _ret; break
                # The callback may have opened its own screen (a menu): take the
                # mouse back and repaint in full, as after on_inspect.
                enable_mouse()
                sys.stdout.flush()
                _sel_last_click = None
                w.anchor_reset()
                w.render(_lines())
            elif (on_move is not None and keys.action(key, "list") in ("list.move_up", "list.move_down")
                  and not items[cursor].disabled):
                delta = -1 if keys.pressed(key, "list.move_up") else 1
                j = cursor + delta
                if 0 <= j < len(items) and not items[j].disabled and on_move(items[cursor].value, delta):
                    items[cursor], items[j] = items[j], items[cursor]
                    cursor = j
                _sel_last_click = None
                w.render(_lines())
            elif act == 'list.columns' and (trail or preview is not None):
                set_columns_shown(not columns_shown())
                _sel_last_click = None; w.anchor_reset(); w.render(_lines())
            elif shortcuts and key in shortcuts:  result = shortcuts[key]; break
            elif key == 'SCROLL_UP':             cursor = _step(cursor, -1); _sel_last_click = None; w.render(_lines())
            elif key == 'SCROLL_DOWN':           cursor = _step(cursor, 1); _sel_last_click = None; w.render(_lines())
            elif key.startswith('MOUSE_CLICK:'):
                parts = key.split(':')
                r, col = int(parts[2]), int(parts[3]) if len(parts) > 3 else 1
                # Click on the status-bar row's pulsing ● beacon → open the
                # activity centre (only while something is actually running).
                if (chrome._activity_opener is not None
                        and r >= ui.get_terminal_height()
                        and ui.has_background_tasks()):
                    chrome._activity_opener()
                    enable_mouse()
                    sys.stdout.flush()
                    _sel_last_click = None
                    w.anchor_reset()
                    w.render(_lines())
                    continue
                if w.row is None:
                    continue
                # render() prepends top_margin() blank rows before lines[0], and
                # _last_hlen holds how many lines come before item[viewport]
                # (header, box top, subtitle, message, the "above" row).
                geo = _col_geo[0]
                if geo and not geo['main'][0] <= col <= geo['main'][1]:
                    # The column browser: a row of a level above goes back to
                    # it; the preview column does nothing.
                    b = r - w.row - ui.top_margin() - geo['first']
                    hit = next(((top + b, values, depth) for x0, x1, top, values, depth, lead in geo['trails']
                                if x0 <= col <= x1 and b >= lead and top + b < len(values)), None)
                    if hit is None:
                        continue
                    result = JumpTo(hit[2], hit[1][hit[0]])
                    break
                i = r - w.row - ui.top_margin() - _last_hlen[0]
                idx = viewport + i
                if not (0 <= idx < len(items)):
                    continue
                clickable = not items[idx].disabled

                # A click only confirms/toggles when it lands on a printed
                # character; clicking the blank space anywhere in a row (trailing
                # padding, gaps between table columns, the empty left margin) just
                # moves the highlight; it never enters.
                row_plain = _row_plain.get(idx, "")
                on_char = 0 < col <= len(row_plain) and row_plain[col - 1] != ' '
                if not on_char:
                    if clickable or (multi and items[idx].checked):
                        cursor = idx
                    _sel_last_click = None
                    w.render(_lines())
                    continue

                if multi and (clickable or items[idx].checked):
                    cursor = idx
                    it = items[cursor]
                    if not (interlock_category_callback and _locked_category[0]
                            and not it.checked
                            and interlock_category_callback(it.value) != _locked_category[0]):
                        it.checked = not it.checked
                        _update_interlock()
                        selectable[:] = [i for i, x in enumerate(items) if not x.disabled or x.checked]
                    w.render(_lines())
                elif not multi and clickable:
                    if idx == cursor or _sel_last_click == idx:
                        # Already on this item (keyboard or prior click): confirm
                        cursor = idx
                        result = items[cursor].value
                        break
                    else:
                        _sel_last_click = idx
                        cursor = idx
                        w.render(_lines())
                elif not multi:
                    # Disabled/heading row: move cursor, reset click state
                    _sel_last_click = None
                    cursor = idx
                    w.render(_lines())

    finally:
        _drop_backdrop()
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()
        if place is not None and items:
            place.value, place.pos = items[cursor].value, cursor

    return result


def live_select(message: str, provider: Callable[[str], list], *,
                count_of: Callable[[], int] | None = None,
                header: list | None | Callable[[], list[str]] = None,
                columns: list | None = None,
                extra_hints: dict[str, str] | None = None,
                on_cycle: Callable[[int], None] | None = None,
                cycle_key: str | None = None,
                section_nav: bool = False,
                row_actions: dict[str, Callable[[Any], None]] | None = None,
                placeholder: str = "type to search…",
                initial_query: str = "") -> Any:
    """Incremental "search box + live results" widget.

    `provider(query)` is called on each query change and returns the ranked list
    of Choice to display (cells already built, including any highlight segments).
    Letters/digits type into the query; ← → move the query caret; ↑ ↓ (and the
    scroll wheel) move through results; Enter selects the highlighted row; Esc
    cancels. Returns the chosen Choice.value, or None.

    `row_actions`: key -> callback(current row value), same shape as
    `select`'s: the only per-row hotkey mechanism available here, since every
    other key types into the query. Bind non-printable keys only (e.g. a
    Ctrl-combo); the callback runs and the list stays open, redrawing after.

    `count_of()` gives the number shown as "N results" when the list holds
    more than the matches (e.g. section headings); by default it is len(list).

    `on_cycle(step)` is called when `cycle_key` is pressed (e.g. to change the
    search scope), then the results are recomputed.

    `section_nav` makes Tab / Shift-Tab jump between section headings, and dims
    the rows outside the section under the cursor.

    `initial_query` pre-fills the query, caret at its end.

    `placeholder` is greyed out inside the empty field, behind the caret, and
    goes as soon as there is a query to show in its place.
    """
    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w   = _Widget(fd)

    query: list[str] = list(initial_query)
    qpos             = len(query)
    items: list      = list(provider("".join(query)))
    cursor           = 0
    viewport         = 0
    _sel_last_click: int | None = None
    # Maps a visible item index → its ANSI-stripped rendered text, so a mouse
    # click can tell whether it landed on a printed character or blank space
    # (shared hit-test convention with `select`).
    _row_plain: dict[int, str] = {}
    _fixed_rows = [0]   # header + query + count + above-indicator lines, this frame

    base_hints = {L("search.up", "search.down"): "results", L("search.back"): "back",
                  L("search.choose"): "confirm"}
    if section_nav:
        base_hints[L("search.next_section", first=True)] = "section"
    # Keys given as action ids (backbone.keys), as select() takes them.
    row_actions = keys.expand(row_actions)
    cycle_keys = keys.keys_for(cycle_key) if cycle_key is not None else ()
    hints = {**{keys.hint_for(k): v for k, v in (extra_hints or {}).items()}, **base_hints}
    # Maps an absolute (row, col) on a hint line → the key clicking it replays.
    _hint_cells: dict[tuple[int, int], str] = {}


    def _selectable() -> list[int]:
        return [i for i, it in enumerate(items) if not it.disabled]

    def _step(cur: int, direction: int) -> int:
        sel = _selectable()
        if not sel:
            return cur
        if cur in sel:
            idx = sel.index(cur)
            return sel[(idx + direction) % len(sel)]
        return sel[0] if direction > 0 else sel[-1]

    def _headings() -> list[int]:
        """Indices of the section heading rows (disabled rows carrying a title)."""
        return [i for i, it in enumerate(items) if it.disabled and it.title]

    def _owners() -> list[int]:
        """Per row, the index of the heading that owns it (-1 above the first).

        Built in one pass and reused for the whole frame; resolving each row
        against the heading list separately is quadratic, and this runs on every
        keystroke of a live search.
        """
        out: list[int] = []
        cur = -1
        for i, it in enumerate(items):
            if it.disabled and it.title:
                cur = i
            out.append(cur)
        return out

    def _section_of(idx: int) -> int:
        """Index of the heading that owns row `idx`, or -1 above the first one."""
        owners = _owners()
        return owners[idx] if 0 <= idx < len(owners) else -1

    def _jump_section(direction: int) -> int:
        """First selectable row of the next/previous section, wrapping around."""
        heads = _headings()
        if not heads:
            return cursor
        here = _section_of(cursor)
        order = [-1] + heads if _section_of(0) == -1 and heads[0] > 0 else heads
        try:
            pos = order.index(here)
        except ValueError:
            pos = 0
        target = order[(pos + direction) % len(order)]
        start = 0 if target == -1 else target + 1
        for i in range(start, len(items)):
            if items[i].disabled:
                if i in heads and i != target:
                    break
                continue
            return i
        return cursor

    def _recompute() -> None:
        """Re-run the provider for the current query and reset cursor/viewport onto the new results."""
        nonlocal items, cursor, viewport, _sel_last_click
        # Called for the empty query too: the provider owns what a query yields,
        # including "nothing", and anything it reported for the previous one
        # (result counts, section tallies) has to be cleared rather than left
        # standing over an empty box.
        try:
            items = list(provider("".join(query)))
        except Exception:
            items = []
        cursor = _step(-1, 1) if items else 0
        viewport = 0
        _sel_last_click = None

    def _lines() -> list:
        nonlocal viewport
        width = ui.get_terminal_width()
        boxed = box_fits()
        ptitle, h_lines = panel_header(header)
        cols  = _cols() - (2 if boxed else 0)       # rows lose their 2-space margin inside the box
        ui.footer_lines(width)   # refresh box height (see select._lines)
        out = []
        if boxed and ptitle and ptitle.subtitle:
            out.append(f"  {C.DIM}{ptitle.subtitle}{C.RESET}")

        qtext = "".join(query)
        # An empty message means the header already names the screen, so the query
        # then starts at the normal margin rather than behind a stray space.
        _label = f"{C.DIM}{message}{C.RESET} " if message else ""
        # A block cursor sitting on the character, not a bar drawn between two:
        # the query stays still as the caret walks it. Empty, the block sits on
        # the placeholder's first letter (where typing will start) with the
        # rest of the hint dimmed behind it.
        if qtext:
            _field = block_cursor(qtext, qpos)
        elif placeholder:
            _field = block_cursor(placeholder, 0, base=C.DIM) + C.RESET
        else:
            _field = block_cursor("", 0)
        out.append(f"  {_label}{_field}")
        count = ("" if not qtext else
                 ui.plural(len(items) if count_of is None else count_of(), "result"))
        out.append(f"  {C.DIM}{count}{C.RESET}" if count else "")

        hint_lines = chrome_hint_lines(hints)
        if boxed:
            # The box runs from under any header down to the hint bar.
            box_h = max(4, _hint_pin_target() - len(h_lines) - len(hint_lines))
            vis = max(2, box_h - 2 - len(out) - 2)
        else:
            # header + message + count; +2 for the above/below rows.
            overhead = len(h_lines) + len(out) + len(hint_lines) + 2
            vis = max(2, _visible_rows() - overhead)

        n = len(items)
        viewport = _scroll(viewport, cursor, vis + 1, items)          # the "above" row only once scrolled
        if viewport > 0:
            viewport = _scroll(viewport, cursor, vis, items)
            out.append(f"  {C.DIM}╵ {viewport} above{C.RESET}")
        else:
            vis += 1
        first = len(h_lines) + (1 if boxed else 0)      # where `out` starts on screen
        _fixed_rows[0] = first + len(out)   # rows before the first item: the click-math offset
        _row_plain.clear()
        row_at: dict[int, int] = {}

        eff = min(cols, _COLUMNS_MAX_WIDTH)
        col_widths: list = []
        if columns:
            rows_cells = [it.cells for it in items if it.cells]
            if rows_cells:
                vis_cells = [it.cells for it in items[viewport:viewport + vis] if it.cells]
                col_widths = _table_widths(rows_cells, columns, eff,
                                           pointer_w=4, right_margin=_EDGE_MARGIN,
                                           visible_cells=vis_cells)

        owners = _owners() if section_nav else []
        focus = (owners[cursor] if section_nav and 0 <= cursor < len(owners) else None)
        for i in range(viewport, min(viewport + vis, n)):
            it = items[i]
            if columns and it.cells:
                line = _render_table_row(it.cells, columns, i == cursor,
                                         col_widths, eff, _EDGE_MARGIN,
                                         dim=section_nav and owners[i] != focus)
                # The highlighted result on the bar, like every list's.
                out.append(line[:ui.MARGIN_H] + ui.on_bar(line[ui.MARGIN_H:], cols - ui.MARGIN_H)
                           if i == cursor else line)
            elif it.disabled:
                out.append(f"  {C.DIM}{C.BOLD}{it.title}{C.RESET}" if it.title else "")
            elif i == cursor:
                out.append("  " + ui.on_bar(f" {C.PRIMARY}{C.BOLD}{it.title}{C.RESET}", cols - ui.MARGIN_H))
            else:
                out.append(f"   {it.title}")
            row_at[i] = len(out) - 1

        remaining = n - viewport - vis
        out.append(f"  {C.DIM}╷ {remaining} below{C.RESET}" if remaining > 0 else "")
        out = (boxed_frame(h_lines, out, ptitle.title if ptitle else "", box_h, False) if boxed
               else h_lines + out)
        for i, k in row_at.items():
            if first + k < len(out):                 # a row the box had no room for has no text to click
                _row_plain[i] = _plain(out[first + k])
        # Inset the hint block by the left margin so it never hugs an edge; _hint
        # centres within _cols() (= width-2*MARGIN_H), so this makes it symmetric.
        _filler = _hint_pin_target() - len(out) - len(hint_lines)
        if _filler > 0:
            out.extend([""] * _filler)
        append_chrome(out, hints, _hint_cells, pin=False)
        return [_clip_ansi(line, width) for line in out]

    result = None
    try:
        _set_raw(fd)
        enable_mouse()
        screen_takeover_next()   # paint over the previous screen, no flash
        w.render(_lines())

        def _relaid() -> None:
            ui.clear_screen()
            w.anchor_reset()
            w.render(_lines())
        _drop_backdrop = screen_backdrop(_relaid)

        while True:
            if ui.consume_resize():
                _relaid()
                continue
            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)

            # Transport keys, clicks on the now-playing box, and clicks on our own
            # hint glyphs, handled once here, before the switch below.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); w.render(_lines()); continue
            if _ch is not None:
                key = _ch                # replay the hint's key through the switch

            act = keys.action(key, "search")
            if key == 'CTRL_C':
                raise QuitToTerminal()
            elif act == 'search.back':
                result = None
                break
            elif row_actions and isinstance(key, str) and key in row_actions and items:
                row_actions[key](items[cursor].value)
                enable_mouse()                   # it may have opened its own screen
                sys.stdout.flush()
                w.anchor_reset()
                w.render(_lines())
            elif act in ('search.next_section', 'search.prev_section') and section_nav:
                cursor = _jump_section(-1 if act == 'search.prev_section' else 1)
                w.render(_lines())
            elif key in cycle_keys and on_cycle is not None:
                on_cycle(1)                      # on_cycle(step): step through the scopes
                _recompute()
                w.render(_lines())
            elif act == 'search.choose':
                if items and not items[cursor].disabled:
                    result = items[cursor].value
                    break
            elif act == 'search.up':
                cursor = _step(cursor, -1); _sel_last_click = None; w.render(_lines())
            elif act == 'search.down':
                cursor = _step(cursor, 1); _sel_last_click = None; w.render(_lines())
            elif key == 'SCROLL_UP':
                cursor = _step(cursor, -1); _sel_last_click = None; w.render(_lines())
            elif key == 'SCROLL_DOWN':
                cursor = _step(cursor, 1); _sel_last_click = None; w.render(_lines())
            elif key.startswith('MOUSE_CLICK:') and w.row is not None:
                # Same two-click convention as `select`: a click on a row not
                # already highlighted moves the cursor there; clicking it again
                # (or a row already under the cursor) confirms, so one click can't
                # accidentally jump straight into a result.
                parts = key.split(':')
                r = int(parts[2]) if len(parts) > 2 else 0
                col = int(parts[3]) if len(parts) > 3 else 1
                i = r - w.row - ui.top_margin() - _fixed_rows[0]
                idx = viewport + i
                if 0 <= idx < len(items):
                    clickable = not items[idx].disabled
                    row_plain = _row_plain.get(idx, "")
                    on_char = 0 < col <= len(row_plain) and row_plain[col - 1] != ' '
                    if not on_char:
                        if clickable:
                            cursor = idx
                        _sel_last_click = None
                        w.render(_lines())
                    elif clickable:
                        if idx == cursor or _sel_last_click == idx:
                            cursor = idx
                            result = items[cursor].value
                            break
                        _sel_last_click = idx
                        cursor = idx
                        w.render(_lines())
                    else:
                        _sel_last_click = None
                        cursor = idx
                        w.render(_lines())
            elif act == 'search.page_up':
                sel = _selectable()
                if sel:
                    cursor = max(sel[0], cursor - 5)
                    if items[cursor].disabled:
                        cursor = _step(cursor, -1)
                _sel_last_click = None
                w.render(_lines())
            elif act == 'search.page_down':
                sel = _selectable()
                if sel:
                    cursor = min(sel[-1], cursor + 5)
                    if items[cursor].disabled:
                        cursor = _step(cursor, 1)
                _sel_last_click = None
                w.render(_lines())
            elif key == 'LEFT':
                qpos = max(0, qpos - 1); w.render(_lines())
            elif key == 'RIGHT':
                qpos = min(len(query), qpos + 1); w.render(_lines())
            elif key == 'HOME':
                qpos = 0; w.render(_lines())
            elif key == 'END':
                qpos = len(query); w.render(_lines())
            elif key == 'BACKSPACE':
                if qpos > 0:
                    query.pop(qpos - 1); qpos -= 1
                    _recompute(); w.render(_lines())
            elif key == 'SPACE':
                query.insert(qpos, ' '); qpos += 1
                _recompute(); w.render(_lines())
            elif len(key) == 1 and key.isprintable():
                query.insert(qpos, key); qpos += 1
                _recompute(); w.render(_lines())
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def confirm(message: str, default: bool = False) -> bool:
    """Yes/no prompt; y/n or Enter (accepting `default`) answers, Ctrl-C answers no.
    The y / n / ↵ hints are clickable."""
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    w      = _Widget(fd)
    result = default
    _hint_cells: dict[tuple[int, int], str] = {}

    def _render():
        dflt = "yes" if default else "no"
        pairs = [(L("confirm.yes"), "yes"), (L("confirm.no"), "no"),
                 (L("confirm.default"), f"default ({dflt})"), (L("confirm.back"), "back")]
        # The question itself, wrapped inside the box (a long one would be cut
        # short as the box's title).
        lines = [f"  {C.BOLD}{part}{C.RESET}" for part in
                 textwrap.wrap(message, max(10, ui.get_terminal_width() - 2 * ui.MARGIN_H - 4)) or [""]]
        lines, _dx = boxed_chrome(lines, "", pairs, _hint_cells, help_key=True)
        w.render(lines)

    try:
        _set_raw(fd)
        enable_mouse()
        _render()
        while True:
            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if key.startswith('MOUSE_CLICK:'):
                _mp = key.split(':')
                _mr = int(_mp[2]); _mc = int(_mp[3]) if len(_mp) > 3 else 1
                _hk = _hint_cells.get((_mr, _mc))
                if _hk is None:
                    continue             # modal: ignore clicks off the y/n/↵ hints
                key = _hk                # replay the hint's key
            act = keys.action(key, "confirm")
            if   key == 'CTRL_C':            result = False; break
            # Esc backs out of every other screen, so it must do something here
            # too: cancelling a yes/no question means "no".
            elif act == 'confirm.back':      result = False; break
            elif act == 'confirm.default':   result = default; break
            elif act == 'confirm.yes':       result = True;  break
            elif act == 'confirm.no':        result = False; break
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result
