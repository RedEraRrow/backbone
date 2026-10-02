"""Key bindings: every rebindable key, named by what it does.

A screen defines its actions where it uses them, then asks which action a key
is rather than comparing literal keys, and builds its hints from the same
table, so a rebound key works and is shown everywhere at once:

    keys.define("player", "Player", [
        ("next", ("]",), "next track"),
        ("prev", ("[",), "previous track"),
    ])
    if keys.action(key, "player") == "player.next": ...
    hint pair: (keys.label("player.prev", "player.next"), "prev/next")

Key names are what `prompt.core._read_key` returns ('UP', 'SPACE', 'ENTER',
'\\x10' for Ctrl-P, 'b', ...). An action may have several keys (aliases such
as b/B/Esc). The user's changes live in <config folder>/keys.json, apart from
the host's own config for the reason the hints switch is: a screen holding a
loaded config would save it back over a change made meanwhile.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass

from backbone.log import log


@dataclass(frozen=True)
class Action:
    id: str                  # "scope.name"
    scope: str
    label: str               # what it does, for the Key bindings page
    default: tuple


_titles: dict[str, str] = {}             # scope → its heading, in definition order
_within: dict[str, tuple] = {}           # scope → the scopes active alongside it
_actions: dict[str, Action] = {}         # id → Action, in definition order

# Never rebindable: Ctrl-C always quits, and pointer/focus events aren't keys.
_FIXED = ('CTRL_C',)
_EVENT_PREFIXES = ('MOUSE_', 'SCROLL_', 'FOCUS_')


def define(scope: str, title: str, actions: list, within: tuple = ("global",)) -> None:
    """Register a scope's actions: (name, default keys, label) each. `within`:
    the scopes also live on that screen (their keys are found after its own,
    and a key can't mean two things across them)."""
    _titles.setdefault(scope, title)
    _within[scope] = tuple(s for s in within if s != scope)
    for name, default, label in actions:
        aid = f"{scope}.{name}"
        _actions[aid] = Action(aid, scope, label, tuple(default))


def actions(scope: str | None = None) -> list[Action]:
    """Every action, or one scope's, in definition order."""
    return [a for a in _actions.values() if scope is None or a.scope == scope]


def scopes() -> list[tuple[str, str]]:
    """(scope, title) for every scope that has actions, in definition order."""
    return [(s, t) for s, t in _titles.items() if any(a.scope == s for a in _actions.values())]


# --- the user's bindings: keys.json -------------------------------------------

_saved: dict = {'map': None, 'mtime': None, 'checked': 0.0}
_RECHECK_S = 1.0         # how often to look for a change made by another window


def _path():
    from backbone import app
    return app.config_dir / "keys.json"


def _overrides() -> dict[str, tuple]:
    """The user's bindings, re-read when the file changes (another window may
    have rebound a key). A broken file means the defaults, and a log line."""
    now = time.monotonic()
    if _saved['map'] is not None and now - _saved['checked'] < _RECHECK_S:
        return _saved['map']
    _saved['checked'] = now
    try:
        mtime = _path().stat().st_mtime
    except OSError:
        mtime = None
    if _saved['map'] is not None and mtime == _saved['mtime']:
        return _saved['map']
    _saved['mtime'] = mtime
    found: dict[str, tuple] = {}
    if mtime is not None:
        try:
            raw = json.loads(_path().read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            log.warning("keys.json unreadable, using the default keys: %s", exc)
            raw = {}
        for aid, ks in raw.items():
            if isinstance(ks, list) and all(isinstance(k, str) and k for k in ks):
                found[aid] = tuple(ks)
            else:
                log.warning("keys.json: ignoring %r (not a list of key names)", aid)
    _saved['map'] = found
    return found


def _write(overrides: dict[str, tuple]) -> None:
    """Save the bindings atomically. Entries for actions this build doesn't
    define are kept: another tool or version may still use them."""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: list(v) for k, v in overrides.items()}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("couldn't save keys.json: %s", exc)
    _saved.update(map=dict(overrides), mtime=None, checked=0.0)


def of(aid: str) -> tuple:
    """The keys bound to an action: the user's, or else its default."""
    return _overrides().get(aid, _actions[aid].default)


def changed(aid: str) -> bool:
    return of(aid) != _actions[aid].default


def bind(aid: str, keys: tuple) -> None:
    """Set an action's keys (an empty tuple leaves it unbound)."""
    overrides = dict(_overrides())
    if tuple(keys) == _actions[aid].default:
        overrides.pop(aid, None)
    else:
        overrides[aid] = tuple(keys)
        if not keys:
            log.info("key binding %s left without a key", aid)
    _write(overrides)


def reset(aid: str | None = None) -> None:
    """Back to the default keys: one action, or every one."""
    if aid is None:
        _write({k: v for k, v in _overrides().items() if k not in _actions})
    else:
        overrides = dict(_overrides())
        overrides.pop(aid, None)
        _write(overrides)


def bindable(key: str) -> bool:
    """Whether a key can be bound at all."""
    return bool(key) and key not in _FIXED and not key.startswith(_EVENT_PREFIXES)


# --- lookups -------------------------------------------------------------------

def _chain(scope: str) -> tuple:
    return (scope, *_within.get(scope, ()))


def action(key: str, scope: str) -> str | None:
    """Which action `key` is on a screen of `scope`: its own actions first,
    then the scopes live alongside it. None when the key is unbound there."""
    for s in _chain(scope):
        for a in _actions.values():
            if a.scope == s and key in of(a.id):
                return a.id
    return None


def keys_for(spec: str) -> tuple:
    """A widget parameter that takes a key may be given an action id instead:
    its bound keys, or the plain key itself."""
    if spec in _actions:
        return of(spec)
    if len(spec) > 2 and "." in spec[1:-1] and spec.replace(".", "").replace("_", "").isalnum():
        log.warning("key spec %r looks like an action id but none is defined", spec)
    return (spec,)


def hint_for(spec: str) -> str:
    """The hint text for such a parameter: the action's keys, or the key as given."""
    return label(spec) if spec in _actions else spec


def describe(spec: str) -> str:
    """What an action does, for a menu ("" for a plain key)."""
    a = _actions.get(spec)
    return a.label if a else ""


def expand(mapping: dict | None) -> dict:
    """{key or action id: value} → {key: value}, every bound key of an action."""
    return {k: v for spec, v in (mapping or {}).items() for k in keys_for(spec)}


def pressed(key: str, aid: str) -> bool:
    """Whether `key` is one of the action's keys."""
    return key in of(aid)


def conflicts(aid: str, key: str) -> list[str]:
    """Other actions already using `key` somewhere this action is live: its own
    scope, the scopes live alongside it, and the scopes it is live alongside."""
    scope = _actions[aid].scope
    related = set(_chain(scope)) | {s for s, w in _within.items() if scope in w}
    return [a.id for a in _actions.values()
            if a.id != aid and a.scope in related and key in of(a.id)]


# --- how keys are shown ---------------------------------------------------------

_GLYPHS = {
    'UP': '↑', 'DOWN': '↓', 'LEFT': '←', 'RIGHT': '→', 'PGUP': '⇞', 'PGDN': '⇟',
    'SPACE': 'space', 'ENTER': '↵', 'ESC': 'esc', 'TAB': 'tab', 'BACKTAB': '⇧tab',
    'HOME': 'home', 'END': 'end', 'BACKSPACE': '⌫', 'DELETE': 'del', 'INSERT': 'ins',
    '\x1f': '^/', '\x1b': 'esc',
}
_ARROWS = set('↑↓←→⇞⇟')


def glyph(key: str) -> str:
    """How one key is written in hints and on the Key bindings page."""
    if key in _GLYPHS:
        return _GLYPHS[key]
    if len(key) == 1 and 1 <= ord(key) <= 26:
        return '^' + chr(ord(key) + 96)              # '\x10' → '^p'
    return key


class HintKey(str):
    """A hint's key text that also knows the real key behind each glyph, so a
    click on it replays the bound key whatever it looks like ('/', '[', '^/')."""
    tokens: list                                     # (offset, glyph length, key)


def _display_keys(aids: tuple) -> list[str]:
    """The keys to show for these actions: each once, and a letter's other
    case left out when both are bound (b/B is one key to the reader)."""
    shown: list[str] = []
    for aid in aids:
        for k in of(aid):
            if k in shown or (len(k) == 1 and k.isalpha() and k.swapcase() in shown):
                continue
            shown.append(k)
    return shown


def label(*aids: str, first: bool = False, most: int | None = None) -> HintKey:
    """The hint text for these actions' keys ("[/]", "↑↓", "space/p"), for a
    hint short on room: `first` shows each action's first key only, `most`
    caps how many keys are shown in all."""
    shown = _display_keys(aids) if not first else [
        ks[0] for ks in (_display_keys((a,)) for a in aids) if ks]
    shown = shown[:most] if most else shown
    text, tokens = "", []
    for k in shown:
        g = glyph(k)
        if text and not (g in _ARROWS and text[-1] in _ARROWS):
            text += "/"                              # arrows run together: ↑↓, ←→
        tokens.append((len(text), len(g), k))
        text += g
    out = HintKey(text)
    out.tokens = tokens
    return out
