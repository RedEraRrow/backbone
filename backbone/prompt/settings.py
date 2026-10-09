"""What every back* Settings screen is built from: the two-column row (name,
then its state), the on/off glyphs and space to flip them, a pick from fixed
values, and the accent colour picker."""
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


def accent_swatch(value) -> list:
    """A block of the colour itself, or a hatched gap when there is none."""
    code = ui.accent_code(value)
    return [(f"{code}████{C.RESET}", 'normal')] if code else [("░░░░", 'dim')]


def accent_name(value, name: str):
    """The colour's name, written in that colour."""
    code = ui.accent_code(value)
    return [(f"{code}{name}{C.RESET}", 'normal')] if code else name


ACCENT_COLUMNS = [
    Column(style='normal'),                  # swatch
    Column(style='normal'),                  # name, in its own colour
    Column(style='dynamic-dim', flex=True),  # ● on the one in use
]


def pick_accent(title: str, current, on_pick) -> None:
    """An accent colour picker: every colour shown in itself, the terminal's
    own first, then fixed ones and a custom #RRGGBB. Picking one calls
    `on_pick(value)`, which applies it, so the screen around the list is the
    preview; it returns when backed out of."""
    from backbone.prompt.lists import ListPlace, select
    from backbone.prompt.text import text
    place = ListPlace()
    place.value = '__custom__' if str(current).startswith('#') else current
    while True:
        custom = current if str(current).startswith('#') else None

        def _row(value, name: str, colour) -> Choice:
            in_use = value == current or (value == '__custom__' and custom)
            return Choice(title=name, value=value,
                          cells=[accent_swatch(colour), accent_name(colour, name), ON_GLYPH if in_use else ""])

        choices: list = [separator("Your terminal's colours")]
        for i, (key, name, _colour) in enumerate(ui.ACCENT_PRESETS):
            if i == 6:
                choices.append(separator("Fixed colours"))
            choices.append(_row(key, name, key))
        choices += [separator(), _row('__custom__', 'Custom colour…', custom)]

        choice = select("", choices=choices, columns=ACCENT_COLUMNS, place=place,
                        header=PanelTitle(title, ui.accent_label(current)))
        if not choice:
            return
        if choice == '__custom__':
            typed = text("Hex colour:", default=custom or "", placeholder="#4FC3F7")
            if typed is None:
                continue
            rgb = ui.parse_hex_colour(typed)
            if rgb is None:
                ui.show_status("That isn't a colour. Use #RRGGBB, like #4FC3F7.")
                continue
            choice = "#%02X%02X%02X" % rgb
        current = choice
        on_pick(choice)
        ui.show_status(f"{title}: {ui.accent_label(choice)}")
