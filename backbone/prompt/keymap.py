"""Settings → Key bindings: every action in backbone.keys, one row each, with
its keys. ↵ adds a key (press it), and the page's own keys remove one, reset
one, or reset everything. Changes save at once, so every hint follows."""
from __future__ import annotations
import sys

from backbone import keys, ui
from backbone.log import log
from backbone.prompt.core import Choice, Column, _read_key, rounded_header, separator
from backbone.ui import Colors as C

keys.define("keys_editor", "Key bindings page", [
    ("remove", ("d", "DELETE", "BACKSPACE"), "remove a key from the action"),
    ("reset", ("r",), "put the action back to its default keys"),
    ("reset_all", ("R",), "put every action back to its default keys"),
])

_COLUMNS = [
    Column(style='primary'),                        # what it does
    Column(),                                       # its keys: grey, or accent once changed
    Column(style='dynamic-dim', flex=True),         # default / changed
]


def _row(a: keys.Action) -> Choice:
    shown = keys.label(a.id) or "unbound"
    changed = keys.changed(a.id)
    return Choice(title=a.label, value=a.id,
                  cells=[a.label, [(shown, 'accent' if changed else 'dynamic-dim')],
                         "changed" if changed else ""])


def _choices() -> list:
    out: list = []
    for scope, title in keys.scopes():
        if scope == "keys_editor":
            continue                                 # listed last, below
        out.append(separator(title))
        out += [_row(a) for a in keys.actions(scope)]
    out.append(separator("This page"))
    out += [_row(a) for a in keys.actions("keys_editor")]
    return out


def _capture(what: str) -> str | None:
    """The next key pressed, for `what`; None on Esc."""
    from backbone.terminal_input import raw_mode
    ui.clear_screen()
    pad = " " * ui.MARGIN_H
    sys.stdout.write(f"\n{pad}{C.BOLD}Press the new key for: {what}{C.RESET}\n"
                     f"{pad}{C.DIM}esc cancels{C.RESET}\n")
    sys.stdout.flush()
    with raw_mode(sys.stdin):
        while True:
            key = _read_key(sys.stdin.fileno())
            if key == 'ESC':
                return None
            if keys.bindable(key):
                return key


def _title(aid: str) -> str:
    a = next(x for x in keys.actions() if x.id == aid)
    scope_title = dict(keys.scopes()).get(a.scope, a.scope)
    return f"{a.label} ({scope_title})"


def _add_key(aid: str) -> None:
    from backbone.prompt.lists import select
    key = _capture(_title(aid))
    if key is None:
        return
    shown = keys.glyph(key)
    if key in keys.of(aid):
        ui.show_status(f"{shown} is already one of its keys.")
        return
    others = keys.conflicts(aid, key)
    if others:
        names = "; ".join(_title(o) for o in others)
        pick = select(f"{shown} is already used for {names}.",
                      [Choice("Move it to this action", value="move"), Choice("Cancel", value=None)])
        if pick != "move":
            return
        for o in others:
            keys.bind(o, tuple(k for k in keys.of(o) if k != key))
    keys.bind(aid, keys.of(aid) + (key,))
    log.info("key binding %s += %r", aid, key)
    ui.show_status(f"{shown} now: {_title(aid)}")


def _remove_key(aid: str) -> None:
    from backbone.prompt.lists import select
    bound = keys.of(aid)
    if not bound:
        return
    key = bound[0] if len(bound) == 1 else select(
        f"Remove which key from {_title(aid)}?",
        [Choice(keys.glyph(k), value=k) for k in bound])
    if key is None:
        return
    keys.bind(aid, tuple(k for k in bound if k != key))
    ui.show_status(f"{keys.glyph(key)} removed" + ("" if len(bound) > 1 else ": the action has no key now"))


def keys_editor() -> None:
    """The Key bindings page. Returns when backed out of."""
    from backbone.prompt.lists import ListPlace, select
    place = ListPlace()
    while True:
        changed = sum(keys.changed(a.id) for a in keys.actions())
        done: dict = {}

        def _act(name):
            def run(aid):
                done['act'], done['aid'] = name, aid
                return aid                          # end the list; the loop redraws it
            return run

        row_actions, hints = {}, {}
        for name in ("remove", "reset", "reset_all"):
            aid = f"keys_editor.{name}"
            for k in keys.of(aid):
                row_actions[k] = _act(name)
            hints[keys.label(aid)] = {"remove": "remove a key", "reset": "reset",
                                      "reset_all": "reset all"}[name]

        choice = select("", _choices(), columns=_COLUMNS, place=place,
                        header=lambda: rounded_header("Key bindings", "",
                                                      f"{changed} changed" if changed else "all default"),
                        extra_hints={"↵": "add a key"},
                        row_actions=row_actions, row_action_hints=hints)
        if choice is None:
            return
        act = done.get('act')
        if act == "remove":
            _remove_key(choice)
        elif act == "reset":
            keys.reset(choice)
        elif act == "reset_all":
            keys.reset()
            ui.show_status("Every key is back to its default.")
        else:
            _add_key(choice)
