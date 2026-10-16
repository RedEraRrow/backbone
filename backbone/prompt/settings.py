"""What every back* Settings screen is built from: the two-column row (name,
then its state), the on/off glyphs and space to flip them, a pick from fixed
values, and the theme picker."""
from __future__ import annotations

from backbone import ui
from backbone.prompt.core import Choice, Column, PanelTitle, separator
from backbone.ui import Colors as C

SETTINGS_COLUMNS = [
    Column(style='primary'),                 # name, sized to its content
    Column(style='dynamic-dim', flex=True),  # state, left-aligned just after
]

# The on/off pair: filled and hollow, not a tick and a cross: ✘ reads as
# *invalid* rather than *off*. ● and ○ differ only in fill, which is exactly
# the difference.
ON_GLYPH, OFF_GLYPH = "●", "○"


def state_glyph(value) -> str:
    """● or ○ for an on/off setting's current state."""
    return ON_GLYPH if value else OFF_GLYPH


def space_toggles(values) -> dict:
    """select() kwargs making space flip the rows in `values` (the on/off
    ones): select() returns ("__space__", row), and space does nothing on any
    other row."""
    return {"on_inspect": lambda v: ("__space__", v) if v in values else None,
            "inspect_key": "list.toggle"}


def index_of(choices: list, value, default: int = 0) -> int:
    """Index of the choice whose value == `value` (for putting the cursor back
    on the row just acted on)."""
    for i, c in enumerate(choices):
        if (c.value if isinstance(c, Choice) else c) == value:
            return i
    return default


def pick_option(question: str, options: dict, current, title: str):
    """One of a setting's fixed values, `options` being value → label: the
    one picked, or None when backed out of. The current one is marked."""
    from backbone.prompt.lists import select
    return select(question,
                  choices=[Choice(title=label, value=value, cells=[label, ON_GLYPH if value == current else ""])
                           for value, label in options.items()],
                  columns=SETTINGS_COLUMNS, index=index_of(list(options), current),
                  header=PanelTitle(title))


def theme_sample(name: str, overrides: dict | None = None) -> list:
    """The theme `name` (with `overrides`, as set_theme takes them) in
    miniature, as it looks whatever theme is on now: on its own background,
    between its borders, a title, its text, its secondary text and a minor
    title, the cursor, and a line on its highlight band."""
    if not C.RESET:                                   # colour is off
        return ["│Aa Aa Aa Aa › Aa │"]
    c, bg, text = ui.theme_codes(name, overrides), ui.theme_background(name), ui.theme_text(name)
    on = lambda back, code, s: f"{C.RESET}{back}{text}{code}{s}"          # noqa: E731  (a weight alone: on its text)
    return [(on(bg, c['FRAME'], "│") + on(bg, c['ACCENT2'], "Aa ") + on(bg, "", "Aa ") + on(bg, c['DIM'], "Aa ")
             + on(bg, c['HEADING'], "Aa ") + on(bg, c['ACCENT'], "›") + on(bg + c['BAR'], "", " Aa ")
             + on(bg, c['FRAME'], "│") + C.RESET, 'normal')]


def theme_name(name: str, label: str, overrides: dict | None = None):
    """The theme's name, written in its own accent."""
    return [(f"{ui.theme_codes(name, overrides)['ACCENT']}{label}{C.RESET}", 'normal')] if C.RESET else label


THEME_COLUMNS = [
    Column(style='normal'),                  # name, in its own accent
    Column(style='normal'),                  # its colours
    Column(style='dynamic-dim', flex=True),  # ● on the one in use
]


def pick_terminal_accent(title: str, current: str, on_pick) -> None:
    """One of Terminal's accents, from the terminal's own colours
    (ui.TERMINAL_ACCENTS), each written in itself. Picking one calls
    `on_pick(name)`, which applies it, so the screen around the list is the
    preview; it returns when backed out of."""
    from backbone.prompt.lists import ListPlace, select
    place = ListPlace()
    place.value = current
    while True:
        choices = [Choice(title=label, value=name,
                          cells=[[(f"{code}{label}{C.RESET}", 'normal')] if C.RESET else label,
                                 ON_GLYPH if name == current else ""])
                   for name, (label, code) in ui.TERMINAL_ACCENTS.items()]
        choice = select("", choices=choices, columns=SETTINGS_COLUMNS, place=place,
                        header=PanelTitle(title, ui.TERMINAL_ACCENTS.get(current, ("",))[0]))
        if not choice:
            return
        current = choice
        on_pick(choice)
        ui.show_status(f"{title}: {ui.TERMINAL_ACCENTS[choice][0]}")


def pick_theme(current: str, on_pick, extra: dict | None = None) -> None:
    """The theme picker: every theme in ui.THEMES, and `extra`, a host's own
    as {name: (label, overrides)}: Terminal with those colours over it (see
    set_theme; or a callable giving them), each by name in its own colours. Picking one calls
    `on_pick(name)`, which applies it, so the screen around the list is the
    preview; it returns when backed out of."""
    from backbone.prompt.lists import ListPlace, select
    themes = {**{name: (label, None) for name, label in ui.THEMES.items()}, **(extra or {})}
    place = ListPlace()
    place.value = current
    while True:
        # A host theme's colours can be a callable, worked out each time the
        # list is drawn: Album art's change with the track, list open or not.
        live = lambda over: over() if callable(over) else over                  # noqa: E731
        choices = [Choice(title=label, value=name,
                          cells=[lambda n=name, l=label, o=over: theme_name(n, l, live(o)),
                                 lambda n=name, o=over: theme_sample(n, live(o)),
                                 ON_GLYPH if name == current else ""])
                   for name, (label, over) in themes.items()]
        note = " · this terminal keeps its own background" if ui.full_themes_supported() is False else ""
        choice = select("", choices=choices, columns=THEME_COLUMNS, place=place,
                        header=PanelTitle("Theme", themes.get(current, ("",))[0] + note))
        if not choice:
            return
        current = choice
        on_pick(choice)
        ui.show_status(f"Theme: {themes[choice][0]}")
