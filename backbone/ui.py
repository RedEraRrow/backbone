"""Terminal helpers: ANSI colours, sizing and resize tracking, the display-width
scanner, formatting, the status bar, the now-playing box registry, the alt
screen and the progress bar."""
from __future__ import annotations
import functools
import os
import sys
import shutil
import signal
import time as _time
from typing import Any
import re
import unicodedata
from itertools import groupby
from pathlib import Path

from backbone import nav
from backbone.log import quietly

# Status-bar messages: an ordinary one, and a warning or error worth reading.
STATUS_S = 3.0
STATUS_WARNING_S = 5.0

_resize_flag = False
_footer_layout_dirty = False

# Memoised terminal size. `get_terminal_size` was an ioctl per call and the render
# path calls it per *line* (clipping) as well as per frame; the size only changes
# on SIGWINCH, which clears this. Without SIGWINCH (Windows) it re-reads on a
# short TTL instead.
_size_cache: tuple[int, int] | None = None
_size_cache_at: float = 0.0
_SIZE_TTL = 0.25

_last_resize_signal = 0.0               # monotonic time of the last SIGWINCH
_cell_aspect_cache: float | None = None # see cell_aspect(); a resize clears it


def _sigwinch_handler(signum: int, frame: Any) -> None:
    """Mark that the terminal was resized; consume_resize() picks this up."""
    global _resize_flag, _size_cache, _cell_aspect_cache, _last_resize_signal
    _resize_flag = True
    _size_cache = None                   # the memoised size is now wrong
    _cell_aspect_cache = None            # and so may the cell size be (a font change)
    _last_resize_signal = _time.monotonic()


def request_relayout() -> None:
    """Have the next consume_resize() say yes: a screen that missed a resize
    (something over it took the signal) lays itself out again."""
    global _resize_flag
    _resize_flag = True


def last_resize_signal_at() -> float:
    """Monotonic time of the last resize report, to tell whether one arrived
    while something slow was under way."""
    return _last_resize_signal


def ms_since_resize_signal() -> float:
    """How long ago the terminal last reported a resize, in ms, for the
    diagnostics log, to show how far behind the resize a redraw landed."""
    return (_time.monotonic() - _last_resize_signal) * 1000

# SIGWINCH doesn't exist on Windows; guard so importing ui never raises there.
_HAS_SIGWINCH = hasattr(signal, "SIGWINCH")
if _HAS_SIGWINCH:
    signal.signal(signal.SIGWINCH, _sigwinch_handler)


def mark_footer_layout_dirty() -> None:
    """Signal that the now-playing box's height changed (it appeared or vanished,
    e.g. the player view opened in another window), so menus re-render and
    re-reserve rows for it via consume_resize()."""
    global _footer_layout_dirty
    _footer_layout_dirty = True


def consume_resize() -> bool:
    """True (and clears the flags) if the terminal was resized *or* the now-playing
    box changed height since last call: both need a full re-render/re-layout."""
    global _resize_flag, _footer_layout_dirty
    if _resize_flag or _footer_layout_dirty:
        _resize_flag = False
        _footer_layout_dirty = False
        return True
    return False

# Global content margins.  All widgets and the playback UI read from here:
# change these two values to tune the whole app at once.
MARGIN_H = 2   # columns reserved on each horizontal side (left and right)
MARGIN_V = 1   # rows reserved on each vertical side (top and bottom)

# Now-playing box transport geometry, shared so both places that depend on it
# can't drift apart: `now_playing_box.format_now_playing_bar` draws the glyphs here
# and `prompt.core.footer_click_action` maps a click back to the one under
# the pointer. (start column, width) of ⏸/⏵ and ⏭ in the box's content columns.
#
# No previous-track button: the box is an ambient reminder of what is playing,
# and stepping backwards from it is a rarer thing to want than the space a third
# control costs. ^B still works, and is still advertised in the hint bar.
FOOTER_GLYPH_COLS = ((0, 2), (4, 2))

class Colors:
    """What every screen paints with: the weights (DIM, BOLD, …), the cursor
    controls, and the theme's roles, each named for what it marks (see
    THEMES). set_theme fills the roles; all of them are empty when colour is
    off (NO_COLOR, or set_colour(False))."""
    # The theme's roles; the Terminal theme's, on a dark background, until set_theme.
    ACCENT = "\033[1;32m"     # the cursor ›, the picked thing, edits, what's playing, key letters
    ACCENT2 = "\033[1;36m"    # the focused box's title and border, the active tab
    STRONG = "\033[1m"        # bright text: the current row, values, a progress bar's played part
    OK = "\033[1;32m"         # ✔, done
    WARN = "\033[1;33m"       # ⚠
    ERROR = "\033[1;31m"      # a failure
    INFO = "\033[1;36m"       # two more hues, for screens that tell several kinds apart
    NOTE = "\033[1;35m"
    MUTED = "\033[2m"         # quiet marks that aren't text: map land
    FRAME = "\033[2m"         # every box's border, and the rules and separators inside one
    HEADING = "\033[1;2m"     # a minor title: a list's section headings
    GROUP = "\033[1m"         # a group's title among its rows: a disc's, a work's
    BAR = "\033[48;5;237m"    # behind the highlighted row of the list you're in
    BAR_DIM = "\033[48;5;235m"  # behind the row a column above opened, or the one playing
    # Weights and cursor controls: every theme's the same, but for a fixed
    # theme's DIM and BOLD (see theme_codes).
    DIM = "\033[2m"
    BOLD = "\033[1m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    RESET = "\033[0m"
    INVERT = "\033[7m"
    HIDE = "\033[?25l"
    SHOW = "\033[?25h"


ROLES = ('ACCENT', 'ACCENT2', 'STRONG', 'OK', 'WARN', 'ERROR', 'INFO', 'NOTE', 'MUTED', 'FRAME', 'HEADING',
         'GROUP', 'BAR', 'BAR_DIM')

# The styling half of Colors: everything that paints rather than moves the
# cursor. Suppressing colour must not suppress HIDE/SHOW, which are cursor
# control and still needed on a pipe.
_STYLE_NAMES = ROLES + ('DIM', 'BOLD', 'ITALIC', 'UNDERLINE', 'RESET', 'INVERT')
_STYLE_CODES = {name: getattr(Colors, name) for name in _STYLE_NAMES}


def on_bar(text: str, width: int, bar: str | None = None) -> str:
    """`text` on a highlight bar `width` columns wide (Colors.BAR unless
    `bar`), the bar kept up through any resets inside it."""
    bar = Colors.BAR if bar is None else bar
    if not bar:
        return text + " " * max(0, width - visual_len(text))
    body = text.replace(Colors.RESET, Colors.RESET + bar) if Colors.RESET else text
    return f"{bar}{body}{' ' * max(0, width - visual_len(text))}{Colors.RESET}"


def colour_enabled() -> bool:
    """Whether colour should be emitted: a terminal, and NO_COLOR unset.

    An empty NO_COLOR still counts as set: that is what the convention says,
    and `NO_COLOR=` in an environment is a deliberate act.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def set_colour(enabled: bool) -> None:
    """Turn every style code in `Colors` on or off, for the whole process.

    One switch rather than a check at each of the several hundred places a style
    is interpolated: the table renderer, the hint engine, the status bar and
    every screen already read their codes from here, so a pipe gets plain text
    without any of them knowing about it.
    """
    global _colour_on
    _colour_on = enabled
    for name, code in _STYLE_CODES.items():
        setattr(Colors, name, code if enabled else "")
    if _full['on'] or enabled:
        _paint_terminal()


_colour_on = True
# Whether the terminal's default colours are a theme's now, and whether it
# takes them at all (None: not tried yet). See _paint_terminal: a fixed theme
# sets them with OSC 10 / 11 on the app's screen and puts them back with OSC
# 110 / 111; whether they took is checked the first time by reading back.
_full = {'on': None, 'supported': None}   # on: the theme whose colours are set

# NO_COLOR turns colour off from the start; a host can also call set_colour.
USE_COLOR = os.environ.get("NO_COLOR") is None
if not USE_COLOR:
    set_colour(False)


# --- themes -----------------------------------------------------------------
# A theme fills every role in ROLES. Terminal is made of the terminal's own
# sixteen colours, so it follows the terminal's theme; only its highlight bars
# need to know whether the background is light or dark. The others are fixed
# colours ("#RRGGBB"), the whole app's: on the app's screen their TEXT and BG
# are the terminal's own default colours while it runs (see _paint_terminal),
# so every reset, blank and clear is the theme's too. Each is made for a dark
# background except those in _LIGHT_THEMES (Pocket), made for a light one.

_TERMINAL = {'ACCENT': "\033[1;32m", 'ACCENT2': "\033[1;36m", 'STRONG': "\033[1m",
             'OK': "\033[1;32m", 'WARN': "\033[1;33m", 'ERROR': "\033[1;31m",
             'INFO': "\033[1;36m", 'NOTE': "\033[1;35m", 'MUTED': "\033[2m", 'FRAME': "\033[2m",
             'HEADING': "\033[1;2m", 'GROUP': "\033[1m"}
_TERMINAL_BARS = {False: ("\033[48;5;237m", "\033[48;5;235m"),   # dark background
                  True: ("\033[48;5;253m", "\033[48;5;255m")}    # light background

# name -> (label, TEXT, BG and every role as "#RRGGBB"). Each is a classic
# look in its own few colours: the single-colour ones (Matrix, Amber, Pocket)
# tell OK, WARN and ERROR apart by brightness, as those screens did.
_FIXED = {
    'teletext': ("Teletext", dict(                     # a teletext index page, in its eight colours:
        TEXT="#FFFF00", ACCENT="#FFFF00", ACCENT2="#FFFF00", STRONG="#FFFF00",   # yellow the main colour,
        DIM="#FFFFFF", HEADING="#00FFFF", GROUP="#FFFFFF", BG="#000000",   # white what's secondary (a disc's title too), cyan the minor titles,
        FRAME="#0000FF", BAR="#0000FF", BAR_DIM="#00007F",       # blue bands round and across it
        OK="#00FF00", WARN="#FF00FF", ERROR="#FF0000", INFO="#00FFFF", NOTE="#FFFFFF",
        MUTED="#0000FF")),
    'matrix': ("Matrix", dict(                         # phosphor green on black
        TEXT="#00FF41", BG="#000000", ACCENT="#CCFFD8", ACCENT2="#39FF14", STRONG="#B6FFC5",
        OK="#00FF41", WARN="#9CFF57", ERROR="#F0FFF0", INFO="#00D936", NOTE="#7DFF9F",
        MUTED="#00802A", BAR="#00401A", BAR_DIM="#00230E")),
    'amber': ("Amber", dict(                           # an amber monochrome terminal
        TEXT="#FFB000", BG="#120A00", ACCENT="#FFE0A0", ACCENT2="#FFC640", STRONG="#FFD27F",
        OK="#FFB000", WARN="#FFCF40", ERROR="#FFF4E0", INFO="#E89B00", NOTE="#FFCC80",
        MUTED="#8C5E00", BAR="#4A2E00", BAR_DIM="#2A1A00")),
    'dos': ("DOS", dict(                               # yellow on CGA blue, the old IDEs
        TEXT="#FFFF55", BG="#0000AA", ACCENT="#55FFFF", ACCENT2="#FFFFFF", STRONG="#FFFFFF",
        OK="#55FF55", WARN="#FF55FF", ERROR="#FF5555", INFO="#55FFFF", NOTE="#AAAAAA",
        MUTED="#5555FF", BAR="#0000FF", BAR_DIM="#000077")),
    'basic': ("BASIC", dict(                           # a home micro's boot screen
        TEXT="#B4AAF5", BG="#40318D", ACCENT="#E2EE8C", ACCENT2="#8EE3EA", STRONG="#FFFFFF",
        OK="#A8F09C", WARN="#E2EE8C", ERROR="#FF9C94", INFO="#8EE3EA", NOTE="#D6D6D6",
        MUTED="#7F72D6", BAR="#2A1F6A", BAR_DIM="#35287C")),   # lighter than the original, to be read
    'pocket': ("Pocket", dict(                         # a pocket console's four greens
        TEXT="#0F380F", BG="#9BBC0F", ACCENT="#306230", ACCENT2="#0F380F", STRONG="#0F380F",
        OK="#306230", WARN="#0F380F", ERROR="#0F380F", INFO="#306230", NOTE="#306230",
        MUTED="#306230", BAR="#8BAC0F", BAR_DIM="#93B40F")),
    # Checked to WCAG AAA: every text colour 7:1 or more on the background,
    # the status colours apart under red-green and blue-yellow colour blindness
    # (OK is green turned to mint, away from red). Its own DIM: the terminal's
    # faint is anyone's guess. Its bars aren't backgrounds, which a coloured
    # line can't be read on at 7:1: the row you're on is underlined, the row
    # opened or playing bold.
    'contrast': ("High contrast", dict(
        TEXT="#FFFFFF", BG="#000000", DIM="#D9D9D9", ACCENT="#FFFF00", ACCENT2="#A6C8FF", STRONG="#FFFFFF",
        OK="#3DFFBE", WARN="#FF9E3D", ERROR="#FF6666", INFO="#00FFFF", NOTE="#C69CFF",
        MUTED="#D9D9D9", BAR="\033[4m", BAR_DIM="\033[1m")),
}
# Terminal's accents can be any of the terminal's own colours, plain or
# bright, so they still follow its palette: name -> (label, code).
TERMINAL_ACCENTS = {
    'green': ("Green", "\033[1;32m"), 'red': ("Red", "\033[1;31m"), 'yellow': ("Yellow", "\033[1;33m"),
    'blue': ("Blue", "\033[1;34m"), 'magenta': ("Magenta", "\033[1;35m"), 'cyan': ("Cyan", "\033[1;36m"),
    'bright green': ("Bright green", "\033[1;92m"), 'bright red': ("Bright red", "\033[1;91m"),
    'bright yellow': ("Bright yellow", "\033[1;93m"), 'bright blue': ("Bright blue", "\033[1;94m"),
    'bright magenta': ("Bright magenta", "\033[1;95m"), 'bright cyan': ("Bright cyan", "\033[1;96m"),
}
DEFAULT_ACCENTS = ('green', 'cyan')
_accents = list(DEFAULT_ACCENTS)     # Terminal's, as chosen (set_terminal_accents)


def set_terminal_accents(first: str | None, second: str | None) -> None:
    """Terminal's two accents from now on, TERMINAL_ACCENTS names (an unknown
    one is the default): a tool's setting, given once at startup and again when
    changed. Repaints at once when Terminal (or a theme built on it) is on."""
    _accents[:] = [n if n in TERMINAL_ACCENTS else d for n, d in zip((first, second), DEFAULT_ACCENTS)]
    if _theme[0] == DEFAULT_THEME:
        set_theme(_theme[0], _last_overrides[0])


def terminal_accent_names() -> tuple:
    """Terminal's two accents as chosen."""
    return tuple(_accents)


THEMES = {'terminal': "Terminal", **{name: label for name, (label, _roles) in _FIXED.items()}}
DEFAULT_THEME = 'terminal'


def parse_hex_colour(text: str) -> tuple[int, int, int] | None:
    """(r, g, b) from "#RRGGBB", "RRGGBB" or "#RGB", or None."""
    h = (text or '').strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def rgb_code(colour: str, background: bool = False, bold: bool = True) -> str:
    """The code for "#RRGGBB": text in that colour, bold unless not `bold`
    (or, `background`, the colour behind text), exact on a 24-bit terminal,
    else the nearest of the 256-colour palette's 6×6×6 cube or grey ramp."""
    r, g, b = parse_hex_colour(colour) or (128, 128, 128)
    lead = "48" if background else ("1;38" if bold else "38")
    if os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit'):
        return f"\033[{lead};2;{r};{g};{b}m"
    if max(r, g, b) - min(r, g, b) < 12:                       # a grey
        n = 232 + min(23, max(0, round((r - 8) / 10)))
    else:
        n = 16 + sum(round(v / 255 * 5) * m for v, m in ((r, 36), (g, 6), (b, 1)))
    return f"\033[{lead};5;{n}m"


def theme_codes(name: str, overrides: dict | None = None) -> dict:
    """Every role's code in the theme `name` (Terminal when unknown), and its
    DIM (the terminal's faint, unless the theme has its own), with
    `overrides` ({role: "#RRGGBB"}) laid over it."""
    if name in _FIXED:
        t = _FIXED[name][1]
        # FRAME, when a theme doesn't give one, is its DIM: borders drawn as
        # quietly as the rest of what's secondary;
        # HEADING its DIM in bold, as Terminal's are; and GROUP its TEXT in bold.
        frame, heading = t.get('FRAME') or t.get('DIM'), t.get('HEADING') or t.get('DIM')
        codes = {role: _role_code(role, t[role]) for role in ROLES if role not in ('FRAME', 'HEADING', 'GROUP')}
        codes['FRAME'] = _role_code('FRAME', frame) if frame else "\033[2m"
        codes['HEADING'] = rgb_code(heading) if heading else "\033[1;2m"
        codes['GROUP'] = rgb_code(t.get('GROUP') or t['TEXT'])
    else:
        codes = dict(_TERMINAL)
        codes['ACCENT'], codes['ACCENT2'] = (TERMINAL_ACCENTS[n][1] for n in _accents)
        codes['BAR'], codes['BAR_DIM'] = _TERMINAL_BARS[background_is_light()]
    own_dim = _FIXED[name][1].get('DIM') if name in _FIXED else None     # a theme that won't trust the faint
    codes['DIM'] = _role_code('DIM', own_dim) if own_dim else "\033[2m"
    # A fixed theme's bold is in its own TEXT colour: bold in the terminal's
    # default colour is drawn in the terminal's bold colour (iTerm's "Bold
    # Color"), which the theme can't set. So BOLD goes before a colour, not after.
    codes['BOLD'] = rgb_code(_FIXED[name][1]['TEXT']) if name in _FIXED else "\033[1m"
    for role, colour in (overrides or {}).items():
        codes[role] = _role_code(role, colour)
    return codes


def _role_code(role: str, colour: str) -> str:
    """A role's code for "#RRGGBB": the bars are behind text, MUTED, DIM and
    FRAME are quiet (not bold), the rest bold text. A value that's already a code
    (a bar that's an underline) is used as it is."""
    if colour.startswith("\033["):
        return colour
    return rgb_code(colour, background=role.startswith('BAR'), bold=role not in ('MUTED', 'DIM', 'FRAME'))


_theme = [DEFAULT_THEME]
_last_overrides = [None]              # the overrides set_theme was last given


def set_theme(name: str, overrides: dict | None = None) -> None:
    """Paint with the theme `name` (a THEMES key; Terminal when unknown) from
    now on, `overrides` ({role: "#RRGGBB" or a code}) laid over it: a host's
    own variation, like colours taken from album art. (Terminal's chosen
    accents are its own: set_terminal_accents.) When that changes any
    colour, the screen that's up lays itself out again (as after a resize),
    so it's all in the new colours at once, whichever thread set them."""
    _theme[0] = name if isinstance(name, str) and name in THEMES else DEFAULT_THEME
    _last_overrides[0] = overrides
    codes = theme_codes(_theme[0], overrides)
    changed = any(_STYLE_CODES.get(role) != code for role, code in codes.items())
    for role, code in codes.items():
        _STYLE_CODES[role] = code
        if _colour_on:
            setattr(Colors, role, code)
    _paint_terminal()
    if changed:
        request_relayout()


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio of two "#RRGGBB" colours (1 to 21)."""
    def lum(colour):
        f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4   # noqa: E731
        r, g, b = (f(v / 255) for v in parse_hex_colour(colour) or (0, 0, 0))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def readable(colour: str, against: str, ratio: float = 4.5) -> str:
    """`colour` itself when it reads against `against` at `ratio`, else the
    nearest that does with the same hue and saturation: only its lightness
    moves, away from the background's, and only as far as it has to."""
    import colorsys
    if contrast_ratio(colour, against) >= ratio:
        return colour
    h, l, s = colorsys.rgb_to_hls(*(v / 255 for v in parse_hex_colour(colour)))
    lighter = contrast_ratio("#FFFFFF", against) >= contrast_ratio("#000000", against)
    for step in range(1, 101):
        tone = min(1.0, l + step / 100) if lighter else max(0.0, l - step / 100)
        out = "#%02X%02X%02X" % tuple(round(v * 255) for v in colorsys.hls_to_rgb(h, tone, s))
        if contrast_ratio(out, against) >= ratio:
            return out
    return "#FFFFFF" if lighter else "#000000"


def terminal_background() -> str:
    """The terminal's own background as "#RRGGBB": as it said when asked, else
    white or black by background_is_light."""
    light = background_is_light()
    return _own['BG'] or ("#FFFFFF" if light else "#000000")


def theme_text(name: str) -> str:
    """The code for the theme `name`'s ordinary text, to show it as it would
    look whatever theme is on now: a fixed theme's own, or for Terminal the
    terminal's own (as asked before any theme changed it)."""
    colour = _FIXED[name][1]['TEXT'] if name in _FIXED else _own['TEXT']
    return rgb_code(colour, bold=False) if colour else "\033[39m"


def theme_background(name: str) -> str:
    """The code for the theme `name`'s background, the same way as theme_text."""
    background_is_light()                            # the terminal's own asked, if not yet
    colour = _FIXED[name][1]['BG'] if name in _FIXED else _own['BG']
    return rgb_code(colour, background=True) if colour else "\033[49m"


# While the app's screen is up with a fixed theme, the terminal's own default
# text and background colours are the theme's (_full, above), put back when it
# isn't.


def _osc_rgb(colour: str) -> str:
    r, g, b = parse_hex_colour(colour) or (0, 0, 0)
    return f"rgb:{r:02x}/{g:02x}/{b:02x}"


def _paint_terminal() -> None:
    """Make the terminal's default colours the theme's, or put its own back,
    whichever the moment calls for. Not while output is being captured (a
    command run from the app's `:` line): the screen stays as it is."""
    if sys.stdout is not sys.__stdout__:
        return
    want = (_theme[0] in _FIXED and _in_app[0] and _colour_on and _full['supported'] is not False
            and sys.stdout.isatty())
    if want and _full['on'] == _theme[0]:
        return                                       # its colours are set already: nothing to write
    if want:
        background_is_light()                        # the terminal's own, read before it changes
        roles = _FIXED[_theme[0]][1]
        sys.stdout.write(f"\033]10;{_osc_rgb(roles['TEXT'])}\033\\\033]11;{_osc_rgb(roles['BG'])}\033\\")
        sys.stdout.flush()
        _full['on'] = _theme[0]
        if _full['supported'] is None:
            got = _rgb_from_reply(_ask_terminal(b"\033]11;?\033\\"))
            wanted = [v / 255 for v in parse_hex_colour(roles['BG'])]
            _full['supported'] = got is not None and all(abs(a - b) < 0.02 for a, b in zip(got, wanted))
            if not _full['supported']:
                _paint_terminal()                    # put back whatever half took
    elif _full['on']:
        sys.stdout.write("\033]110\033\\\033]111\033\\")
        sys.stdout.flush()
        _full['on'] = None


def full_themes_supported() -> bool | None:
    """Whether this terminal takes a theme's own background: None until a
    fixed theme has been shown on the app's screen."""
    return _full['supported']


def _put_terminal_back() -> None:
    """At exit, however it comes: the terminal's own colours back."""
    if _full['on']:
        _in_app[0] = False
        _paint_terminal()


import atexit as _atexit  # noqa: E402
_atexit.register(_put_terminal_back)


def current_theme() -> str:
    """The THEMES key last set."""
    return _theme[0]


_LIGHT_THEMES = {'pocket'}


def theme_is_light() -> bool:
    """Whether the theme is drawn for a light background: Terminal's is the
    terminal's own; a fixed theme's is the one it was made for."""
    return background_is_light() if _theme[0] == 'terminal' else _theme[0] in _LIGHT_THEMES


# --- the terminal's background ----------------------------------------------

_light = [None]
_own = {'TEXT': None, 'BG': None}   # the terminal's own colours, "#RRGGBB", once asked (before any theme's)


def _ask_background() -> bool | None:
    """Ask the terminal its text and background colours (OSC 10, 11),
    keeping both, and whether the background is light, or None when it
    won't say."""
    reply = _ask_terminal(b"\033]10;?\033\\\033]11;?\033\\")
    for role, code in (('TEXT', b"10"), ('BG', b"11")):
        rgb = _rgb_from_reply(reply, code)
        if rgb is not None:
            _own[role] = "#%02X%02X%02X" % tuple(round(v * 255) for v in rgb)
    return _light_from_reply(reply)


def _ask_terminal(request: bytes) -> bytes:
    """Send `request` and return what the terminal answers (b"" when it isn't
    a terminal). A device-attributes request goes after it, which every
    terminal answers: once that answer is in, any reply to the first has come,
    so a terminal that ignores it costs no wait and leaves nothing behind to be
    read as a keypress."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return b""
    try:
        import termios
        import select as _select
    except ImportError:
        return b""
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return b""
    reply = b""
    try:
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(fd, termios.TCSANOW, new)
        sys.stdout.flush()
        os.write(sys.stdout.fileno(), request + b"\033[c")
        end = _time.monotonic() + 0.5
        while _time.monotonic() < end and not re.search(rb"\033\[\?[0-9;]*c", reply):
            ready, _, _ = _select.select([fd], [], [], max(0.0, end - _time.monotonic()))
            if not ready:
                break
            reply += os.read(fd, 1024)
    except OSError:
        return b""
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)
    return reply


def _rgb_from_reply(reply: bytes, code: bytes = b"11") -> tuple | None:
    """The colour (r, g, b, each 0-1) in an OSC 11 (or `code`) reply
    ("…]11;rgb:RRRR/GGGG/BBBB…"), or None when there's no such reply in it."""
    m = re.search(rb"\]" + code + rb";rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", reply)
    return tuple(int(x, 16) / (16 ** len(x) - 1) for x in m.groups()) if m else None


def _light_from_reply(reply: bytes) -> bool | None:
    """Whether an OSC 11 reply names a light colour, or None when there's no
    such reply in it."""
    rgb = _rgb_from_reply(reply)
    return None if rgb is None else 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] > 0.5


def _background_from_env() -> bool | None:
    """COLORFGBG ("15;0": foreground;background palette numbers), which some
    terminals set: a light background is 7 or 15."""
    bg = os.environ.get("COLORFGBG", "").split(";")[-1]
    return bg in ("7", "15") if bg.isdigit() else None


def background_is_light() -> bool:
    """Whether the terminal's background is light: asked once, then COLORFGBG,
    else taken as dark."""
    if _light[0] is None:
        found = _ask_background()
        if found is None:
            found = _background_from_env()
        _light[0] = bool(found)
    return _light[0]


_screen_invalidator = None


def set_screen_invalidator(fn) -> None:
    """Register the painter's "forget what's on screen" hook.

    Registered by `prompt.core` (which can't be imported here, as it imports this
    module), so every existing `clear_screen()` keeps meaning "the screen is now
    blank" for the diffed painter as well.
    """
    global _screen_invalidator
    _screen_invalidator = fn


def _screen_cleared() -> None:
    """Tell the painter the screen was wiped outside its own frame writes."""
    if _screen_invalidator is not None:
        with quietly():
            _screen_invalidator()


MOUSE_ON = "\033[?1000h\033[?1006h"     # click and wheel reporting, SGR-encoded
MOUSE_OFF = "\033[?1000l\033[?1006l"


def enter_alt_screen() -> None:
    """Switch to the terminal alternate screen buffer (no scrollback).

    Also enables focus in/out reporting (\\033[?1004h) so editors can show a
    hollow cursor when the window loses focus; unsupported terminals ignore it.
    And mouse reporting (clicks and the wheel), for as long as the app's
    screen is up: one switch for the whole terminal, so it's this screen's,
    not each widget's (a widget turning it off on its way out left the
    screen it went back to deaf to the mouse).
    And turns auto-wrap off (\\033[?7l): every row is placed explicitly, so a
    row too wide for the window should be cut at the edge, not run onto the
    next, which is what a write still sized for the old width does in the
    moment between a resize and the redraw that answers it.
    """
    sys.stdout.write("\033[?1049h\033[?1004h\033[?7l\033[H\033[3J\033[J" + MOUSE_ON + Colors.HIDE)
    sys.stdout.flush()
    _screen_cleared()
    _in_app[0] = True
    _paint_terminal()


def exit_alt_screen() -> None:
    """Restore the main screen buffer, auto-wrap and focus reporting, turn
    mouse reporting off, and show the cursor."""
    sys.stdout.write(MOUSE_OFF + "\033[?25h\033[?7h\033[?1004l\033[?1049l")
    sys.stdout.flush()
    if _screen_invalidator is not None:          # the app's screen is gone: the painter forgets it
        with quietly():
            _screen_invalidator()
    _in_app[0] = False
    _paint_terminal()


_in_app = [False]   # on the alternate screen, where everything is laid out in boxes


def on_app_screen() -> bool:
    """Writing to the app's screen: in the app, and not to output being
    captured (a command run from its `:` line)."""
    return _in_app[0] and sys.stdout is sys.__stdout__


def clear_screen() -> None:
    """Overwrite screen content from home without triggering scrollback save.

    Leaves the cursor **hidden**: a bare clear parks it at home, where it blinks
    in the top-left corner until the next frame happens to hide it; during a
    library build or any slow step, that's a visible flashing caret.
    """
    sys.stdout.write("\033[H\033[3J\033[J" + Colors.HIDE)
    sys.stdout.flush()
    _screen_cleared()

BACKGROUND_TASKS: dict[str, str] = {}

_toast_message: str = ""
_toast_expiry: float = 0.0

# Now-playing box: the playback layer registers a provider so the widget
# layer can draw a background-audio box without importing playback (keeps the
# dependency flowing one way). provider(width) -> list[str] | None (styled rows,
# top to bottom; drawn just above the status line).
_footer_provider = None
_footer_lines_cache: list[str] = []
# A cheap identity of the currently-shown track (file/generation/paused/index …).
# The idle-tick redraw keys off this so a background track change always repaints
# the box, even in the rare case two tracks render to byte-identical rows.
_footer_sig: tuple | None = None


def set_footer_provider(fn) -> None:
    """Register a ``callable(width) -> list[str] | None`` that renders the now-playing box."""
    global _footer_provider
    _footer_provider = fn


# The now-playing box's clock, as (first, last) column on its content row, when
# a click on it does something (set by the box provider; None otherwise).
_footer_time_cols: tuple | None = None


def set_footer_time_cols(cols: tuple | None) -> None:
    global _footer_time_cols
    _footer_time_cols = cols


def footer_time_cols() -> tuple | None:
    return _footer_time_cols


def set_footer_signature(sig: tuple | None) -> None:
    """Record the identity of the track the box provider just rendered."""
    global _footer_sig
    _footer_sig = sig


# Event-driven repaint: background threads (a joined window's snapshot
# receiver, the host's auto-advance tick) call pulse_footer() when the
# now-playing state changes so the menu poll repaints the box *immediately*
# instead of only on the next keystroke. The waker is registered by the input
# layer (a self-pipe that wakes its select); this hook keeps the playback/IPC
# threads free of any input-layer import.
_footer_waker = None


def set_footer_waker(fn) -> None:
    """Register a ``callable()`` that nudges the menu poll to repaint the box."""
    global _footer_waker
    _footer_waker = fn


def pulse_footer() -> None:
    """Ask the active menu poll to repaint the now-playing box now (no-op if no
    poll is listening, e.g. the full player view drives its own redraws)."""
    if _footer_waker is not None:
        with quietly():
            _footer_waker()


def footer_signature() -> tuple | None:
    """The identity of the currently-shown track (see :func:`set_footer_signature`)."""
    return _footer_sig


def footer_lines(width: int) -> list[str]:
    """The now-playing box rows for ``width`` (empty list when nothing is playing)."""
    global _footer_lines_cache
    if _footer_provider is None:
        _footer_lines_cache = []
        return []
    try:
        lines = _footer_provider(width) or []
    except Exception:
        # A provider that raised tells us nothing about what's playing; keep the
        # box exactly as it was rather than blinking it out and back next tick.
        return _footer_lines_cache
    _footer_lines_cache = list(lines)
    return _footer_lines_cache


def footer_active() -> bool:
    """Whether a now-playing box is currently shown (cached from the last draw)."""
    return bool(_footer_lines_cache)


# Audio is playing but the box could not be drawn: the terminal is too narrow
# for it. The box normally advertises the transport keys in its own top border,
# so this is the one state where the hint bar has to advertise them instead
# (see `prompt.chrome_hint_pairs`). Deliberately *not* set when the full player
# view owns the display: that view shows its own transport.
_footer_unboxed: bool = False


def set_footer_unboxed(value: bool) -> None:
    """Record whether transport is live with no now-playing box to advertise it."""
    global _footer_unboxed
    _footer_unboxed = bool(value)


def footer_unboxed() -> bool:
    """Whether transport is live but no box is drawn to show its keys."""
    return _footer_unboxed


def footer_height() -> int:
    """How many rows the now-playing box currently occupies (0 when inactive)."""
    return len(_footer_lines_cache)


def set_status(task_id: str, message: str | None) -> None:
    """Update or remove a background task status."""
    if message is None:
        BACKGROUND_TASKS.pop(task_id, None)
    else:
        BACKGROUND_TASKS[task_id] = message


def has_background_tasks() -> bool:
    """Whether any background activity is currently running (drives the live,
    pulsing status indicator so the notice stays up until the work is done)."""
    return bool(BACKGROUND_TASKS)


# 256-colour greys for the pulsing beacon, from faint to the most it stands out
# and back: towards white on a dark background, towards black on a light one.
_PULSE_RAMP = {False: (238, 243, 248, 253, 255, 253, 248, 243),
               True: (252, 248, 244, 240, 236, 240, 244, 248)}


def pulse_circle() -> str:
    """A ● whose brightness pulses over time: the beacon next to a running
    background activity. The status bar is re-rendered ~8 Hz while a task is
    active (see the menu idle tick), which animates this."""
    if not _colour_on:
        return "●"
    ramp = _PULSE_RAMP[theme_is_light()]
    return f"\033[38;5;{ramp[int(_time.time() * 6) % len(ramp)]}m●{Colors.RESET}"


def print_inline_progress(message: str, progress: float) -> None:
    """Redraw a single in-place line for a blocking, no-other-redraw loop
    (a per-track ffmpeg pass): pulsing beacon + bar so a slow scan still
    looks alive instead of a hung terminal, inset by MARGIN_H and centred
    like the rest of the chrome. Call `clear_inline_progress()` once the
    loop finishes. In the app (the alternate screen) it's a box over the
    middle of the screen instead, not a line wherever the cursor is."""
    if on_app_screen():
        from backbone.prompt.core import progress_float
        progress_float(message, progress)
        return
    bar = get_progress_bar(progress, 24)
    width = get_terminal_width()
    avail = max(1, width - 2 * MARGIN_H)
    prefix_len = visual_len(f"{pulse_circle()} {bar} ")
    if prefix_len + len(message) > avail:
        message = message[:max(0, avail - prefix_len - 1)] + "…"
    content = f"{pulse_circle()} {bar} {Colors.DIM}{message}{Colors.RESET}"
    pad = max(MARGIN_H, (width - visual_len(content)) // 2)
    sys.stdout.write(f"\r{' ' * pad}{content}\033[K")
    sys.stdout.flush()


def progress_iter(items, message: str, name=None, after: float = 0.25):
    """Each of `items` in turn, with a progress box over the screen
    (print_inline_progress) once going through them has taken `after`
    seconds: a long read or write is never a screen that just sits there,
    and a quick one never flashes a box. `name(item)` adds what it's on to
    the message (os.path.basename, for paths). Only in the app; elsewhere
    the items pass through untouched."""
    items = items if hasattr(items, '__len__') else list(items)
    total = len(items)
    start = shown_at = _time.monotonic()
    shown = False
    try:
        for i, item in enumerate(items):
            now = _time.monotonic()
            if total > 1 and now - start >= after and (not shown or now - shown_at >= 0.05) and on_app_screen():
                print_inline_progress(f"{message} · {name(item)}" if name else message, i / total)
                shown, shown_at = True, now
            yield item
    finally:
        if shown:
            clear_inline_progress()


def clear_inline_progress() -> None:
    """Erase the line (or the box) left by `print_inline_progress()`."""
    if on_app_screen():
        from backbone.prompt.core import screen_float_close
        screen_float_close()
        return
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def show_status(message: str, duration: float = STATUS_S) -> None:
    """Flash a one-shot message in the status bar for `duration` seconds."""
    global _toast_message, _toast_expiry
    _toast_message = message
    _toast_expiry = _time.time() + duration


def show_error(message: str) -> None:
    """Flash "Error: message" in the status bar, held longer, and log it."""
    from backbone.log import log
    log.warning("%s", message)
    show_status(f"Error: {message}", STATUS_WARNING_S)


def show_loading(message: str) -> None:
    """Clear the screen and display a greyed loading message during long operations."""
    clear_screen()
    sys.stdout.write(f"\n  {Colors.DIM}{message}{Colors.RESET}\n")
    sys.stdout.flush()


def get_status_line() -> str:
    """Return the current status bar content (background tasks + toast), on the right."""
    global _toast_message
    cols = get_terminal_width()

    if cols <= 0:
        return ""

    if _toast_message and _time.time() > _toast_expiry:
        _toast_message = ""

    sep = f"  {Colors.DIM}·{Colors.RESET}  "

    right_parts: list[str] = []
    for msg in BACKGROUND_TASKS.values():
        right_parts.append(f"{pulse_circle()} {Colors.DIM}{msg}{Colors.RESET}")
    if _toast_message:
        right_parts.append(f"{Colors.DIM}{_toast_message}{Colors.RESET}")
    right = sep.join(right_parts)
    status = " " * max(2, cols - visual_len(right) - 2) + right + "  " if right else ""

    if visual_len(status) > cols:
        status = clip_ansi(status, cols)
    return status

_tab_spans: list = []   # (first col, last col, tab) of each label in the bar last drawn


def tab_bar_lines() -> list[str]:
    """The top rows while an app runs as tabs ([] otherwise): the tabs' names
    along one row, the one showing outlined like a folder's tab over it, its
    outline running down into the screen's top border (tab_notch: the painter
    cuts the opening there). Their keys (F1, F2…) show with the hints (the
    `?` toggle), in the accent, as hint keys are; in a window too narrow for
    every name only the showing tab keeps its name, the others their keys."""
    _tab_spans.clear()
    _notch[0] = None
    names = [name for name, _run in nav.TABS]
    if not names:
        return []
    on = nav.active_tab()
    total = get_terminal_width() - 2 * MARGIN_H
    keys_on = _tab_keys_shown()
    modal = nav.is_modal()

    def labels(full: bool) -> list:
        """(key, name) per tab: the key shown with the hints, or in place of
        the name when the tabs don't all fit."""
        return [(f"F{i + 1}" if keys_on or not (full or i == on) else "",
                 n if full or i == on else "") for i, n in enumerate(names)]

    def width(ls) -> int:
        return sum(len(k) + len(n) + (1 if k and n else 0) for k, n in ls) + _TAB_GAP * (len(ls) - 1) + 2

    # The other tabs give their names way first, then (very narrow) the showing one is cut.
    for full in (True, False):
        ls = labels(full)
        if width(ls) <= total:
            break
    else:
        ls = [(k, clip_ansi(n, max(1, total - 6))) if i == on else ("", "") for i, (k, n) in enumerate(labels(False))]

    text = " " * (MARGIN_H + 2)                         # the first name two in from the screen's left border
    col = MARGIN_H + 2                                  # 0-based column the next name starts at
    for i, (k, n) in enumerate(ls):
        if not (k or n):
            continue
        if col > MARGIN_H + 2:
            text += " " * _TAB_GAP
            col += _TAB_GAP
        plain = f"{k} {n}" if k and n else k or n
        key = f"{Colors.ACCENT if keys_on else Colors.DIM}{k}{Colors.RESET}" if k else ""
        if i == on and not modal:
            name = f"{Colors.ACCENT2}{n}{Colors.RESET}"
        else:
            name = f"{Colors.DIM}{n}{Colors.RESET}"
        text += (key + " " + name) if k and n else (key or name)
        _tab_spans.append((col, col + 1 + len(plain), i))      # 1-based, with a space either side
        if i == on:
            _notch[0] = (col - 2, col + len(plain) + 1)         # 0-based: its outline's two sides
        col += len(plain)
    mid = text
    top = ""
    if _notch[0]:
        a, b = _notch[0]
        top = " " * a + f"{Colors.FRAME}╭{'─' * (b - a - 1)}╮{Colors.RESET}"
        mid += " " * max(0, b + 1 - visual_len(mid))          # out to its right side (the last tab's too)
        mid = _overlay(mid, a, f"{Colors.FRAME}│{Colors.RESET}")
        mid = _overlay(mid, b, f"{Colors.FRAME}│{Colors.RESET}")
    return [top, mid]


def _overlay(line: str, col: int, glyph: str) -> str:
    """`line` with the one cell at 0-based column `col` (a space) swapped for `glyph`."""
    out, at, i = [], 0, 0
    while i < len(line):
        m = _ANSI_AT.match(line, i)
        if m:
            out.append(m.group(0)); i = m.end(); continue
        out.append(glyph if at == col else line[i])
        at += char_cols(line[i]); i += 1
    return "".join(out)


_ANSI_AT = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_TAB_GAP = 4                       # between two tabs' names: room for the outline's sides
_notch: list = [None]              # the showing tab's outline, (left, right) 0-based columns


def tab_notch() -> tuple | None:
    """Where the showing tab's outline meets the screen under the bar, laid
    out for the window as it is now (a layout asking before the bar's next
    paint, just after a resize, mustn't get where it was)."""
    if not (nav.TABS and tab_rows()):
        return None
    tab_bar_lines()
    return _notch[0]


_tab_keys_shown = lambda: False      # noqa: E731 (the hints' switch, set by prompt: set_tab_keys_shown)


def set_tab_keys_shown(fn) -> None:
    """Whether the tabs show their keys: the hints' switch (prompt.core)."""
    global _tab_keys_shown
    _tab_keys_shown = fn


_tabs_hidden = [False]


def set_tabs_hidden(hidden: bool) -> bool:
    """Leave the tab bar off the screen (a full-screen view that offers it,
    like a player); its keys still switch tabs. Returns what it was."""
    was, _tabs_hidden[0] = _tabs_hidden[0], hidden
    return was


_focused = [True]                  # the terminal's window has focus (its focus reports, when it sends them)


def set_window_focused(focused: bool) -> None:
    _focused[0] = focused


def window_focused() -> bool:
    """Whether the window has focus, as the terminal last reported (True if
    it never says). A terminal may read a window's output slowly while it's
    in the background: heavy writes (a full-size image) can wait for focus."""
    return _focused[0]


_CHROME_MIN_ROWS = 6               # a window shorter than this has no room for the tab bar or status line


def chrome_fits() -> bool:
    """Whether the window has room for the tab bar and the status line: a
    shorter one gives every row to the screen itself."""
    return get_terminal_height() >= _CHROME_MIN_ROWS


def tab_rows() -> int:
    """Rows the tab bar takes at the top: 2 (the showing tab's outline, then
    the names) while an app runs as tabs, unless it's hidden or the window is
    too short for it. The row under them is the screen's top border, the
    tab's outline running into it."""
    return 2 if nav.TABS and not _tabs_hidden[0] and chrome_fits() else 0


def top_margin() -> int:
    """Rows above every screen's content: the margin, or the tab bar."""
    return max(MARGIN_V, tab_rows())


def tab_at(col: int) -> int | None:
    """The tab whose label is at column `col` of the bar."""
    return next((i for first, last, i in _tab_spans if first <= col <= last), None)


def _tty_size() -> os.terminal_size:
    """Ask the terminal itself. shutil.get_terminal_size() prefers exported
    COLUMNS/LINES, which some shells and terminals export once at startup,
    and that froze the size for good. Those only count when no stream is a tty."""
    for stream in (sys.__stdout__, sys.__stdin__, sys.__stderr__):
        try:
            return os.get_terminal_size(stream.fileno())
        except (AttributeError, ValueError, OSError):
            continue
    return shutil.get_terminal_size()


def get_terminal_size(default: tuple = (80, 24)) -> tuple:
    """Terminal (columns, rows), falling back to `default` if the query fails.

    Memoised; see `_size_cache`; a resize (SIGWINCH) clears it.
    """
    global _size_cache, _size_cache_at
    if _size_cache is not None:
        if _HAS_SIGWINCH or (_time.monotonic() - _size_cache_at) < _SIZE_TTL:
            return _size_cache
    try:
        size = _tty_size()
    except OSError:
        return default
    _size_cache = (size.columns, size.lines)
    _size_cache_at = _time.monotonic()
    return _size_cache



def cell_aspect(default: float = 2.0) -> float:
    """How many times taller than wide one character cell is on screen, from
    the pixel size the terminal reports. Anything drawn in cells (half-block
    art, an inline image's box) needs it to keep an image's proportions;
    `default` when the terminal doesn't report pixels. Re-read after a resize,
    which is also what a font change sends."""
    global _cell_aspect_cache
    if _cell_aspect_cache is None:
        _cell_aspect_cache = default
        try:
            import fcntl, struct, termios
            for stream in (sys.__stdout__, sys.__stdin__, sys.__stderr__):
                try:
                    r, c, xpx, ypx = struct.unpack(
                        "HHHH", fcntl.ioctl(stream.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
                except (AttributeError, ValueError, OSError):
                    continue
                if r and c and xpx and ypx:
                    _cell_aspect_cache = (ypx / r) / (xpx / c)
                break
        except ImportError:                          # Windows: no ioctl
            pass
    return _cell_aspect_cache


def get_terminal_width(default: int = 80) -> int:
    """Terminal width in columns."""
    cols, _ = get_terminal_size((default, default))
    return cols


def get_terminal_height(default: int = 24) -> int:
    """Terminal height in rows."""
    _, rows = get_terminal_size((default, default))
    return rows


# ---------------------------------------------------------------------------
# Display width: one ANSI scanner, one column table, for the whole app.
#
# Every module that measures, clips or pads a styled line goes through this
# block.
# ---------------------------------------------------------------------------

# The escape sequences a terminal consumes without drawing anything. In order:
# CSI (including private `?` parameters and intermediate bytes, so `\033[?25l`
# and `\033[3J` are recognised, not just SGR); the Kitty graphics protocol used
# by the album-art renderer; OSC, terminated by either ST or BEL; the remaining
# string-introducers (DCS/SOS/PM/APC); and a bare two-byte escape as a backstop.
_ANSI_RE = re.compile(
    r'(\x1b\[[0-9;?]*[ -/]*[@-~])'
    r'|(\x1b_G[^\x1b]*\x1b\\)'
    r'|(\x1b\][^\x1b\x07]*(?:\x1b\\|\x07))'
    r'|(\x1b[PX^_].*?\x1b\\)'
    r'|(\x1b.)'
)


def _scan(s: str):
    """Yield ``(is_escape, chunk)`` across `s`.

    One chunk per escape sequence, one per printable character: the single
    walk every width routine below is built on, so measuring and clipping can
    never disagree about where an escape starts or ends.
    """
    i = 0
    n = len(s)
    while i < n:
        if s[i] == '\x1b':
            m = _ANSI_RE.match(s, i)
            if m:
                yield True, m.group(0)
                i = m.end()
                continue
        yield False, s[i]
        i += 1


_LINE_BREAK_RE = re.compile(r'\r\n|[\r\n\v\f\x85\u2028\u2029]')
_CONTROL_RE = re.compile(r'[\x00-\x08\x0e-\x1a\x1c-\x1f\x7f]')


def one_line(text: str, mark: str = " ") -> str:
    """`text` as one line for a row: each line break (Windows' \\r\\n, an old
    Mac's \\r, \\n…) as `mark`, a tab as a space, and other control characters
    dropped. A carriage return left in a row sends the cursor back to the
    start of the line, and the rest of the text is drawn over the row's start.
    Escapes are kept: they're styling, not text."""
    if text.isprintable():
        return text
    return _CONTROL_RE.sub("", _LINE_BREAK_RE.sub(lambda _m: mark, text).replace("\t", " "))


def strip_ansi(s: str) -> str:
    """`s` with every ANSI escape sequence removed."""
    if '\x1b' not in s:
        return s
    return _ANSI_RE.sub('', s)


# Codepoints that occupy no column of their own: the variation selectors that
# pick a glyph's text/emoji presentation, the zero-width joiner family, and
# combining marks that stack onto the character before them.
_ZERO_WIDTH = ('︎', '️', '​', '‌', '‍')

_ZERO_WIDTH_SET = frozenset(_ZERO_WIDTH)

# Codepoints a terminal draws two cells wide. Two sources: East Asian Wide and
# Fullwidth (handled below via unicodedata), and emoji-presentation characters,
# which UTR#51 says to render wide and which every modern terminal does. The
# media-control glyphs are the app's own case, and their true width is not
# knowable from Unicode alone. U+23EE ⏮ and U+23ED ⏭ are emoji-by-default; U+23F8
# ⏸ and U+23F5 ⏵ are text-by-default and "should" be one cell, but no monospace
# font on a stock macOS box carries any of them, so they are drawn by whichever
# fallback font does, and Apple Color Emoji (which has ⏮ ⏸ ⏭) draws two cells
# wide whatever the default presentation says. All four report east-asian-width
# N, so nothing available here can tell them apart.
#
# They are listed as wide deliberately, as an over-estimate. Reserving two cells
# and getting one leaves a small gap; reserving one and getting two overruns
# whatever sits to the right, and that is the now-playing box's closing border.
AMBIGUOUS_WIDE = frozenset('⏮⏭⏸⏵⏪⏩⏫⏬⏯⏱⏲⏰')

_cols_cache: dict = {}


def char_cols(ch: str) -> int:
    """How many terminal columns `ch` occupies: 0, 1 or 2."""
    w = _cols_cache.get(ch)
    if w is None:
        if ch in _ZERO_WIDTH_SET or unicodedata.combining(ch):
            w = 0
        elif ch in AMBIGUOUS_WIDE or unicodedata.east_asian_width(ch) in ('W', 'F'):
            w = 2
        else:
            w = 1
        _cols_cache[ch] = w
    return w


def display_text(s: str) -> str:
    """`s` with ANSI escapes and zero-width codepoints removed.

    Every remaining character occupies at least one column, but *not* always
    exactly one: a wide glyph still takes two, so `visual_len` is what you
    want for width maths. This is for callers that need the plain characters
    themselves (cursor hit-testing, writing a styled report out as text).
    """
    out = strip_ansi(s)
    if out.isascii():
        return out
    return ''.join(ch for ch in out if char_cols(ch) != 0)


def visual_len(s: str) -> int:
    """Columns `s` occupies on screen, ignoring ANSI escapes.

    Not the same as `len`: escapes and combining marks take no column, and an
    emoji-presentation or East-Asian-wide character takes two. The ASCII fast
    path keeps the common case a plain length, as this is called per line in the
    render path.
    """
    if not s:
        return 0
    if s.isascii() and '\x1b' not in s:
        return len(s)
    return _visual_len(s)


@functools.lru_cache(maxsize=8192)
def _visual_len(s: str) -> int:
    """visual_len past its fast path: kept, as a frame's rows mostly repeat
    the last frame's (box edges make nearly every row non-ASCII)."""
    plain = strip_ansi(s)
    if plain.isascii():
        return len(plain)
    return sum(char_cols(ch) for ch in plain)


def clip_ansi(text: str, max_cols: int, reset: bool = True) -> str:
    """`text` truncated to `max_cols` visible columns, escapes preserved.

    Escapes ride along without being counted, so the styling that survives the
    cut still closes properly; a zero-width mark rides along with the glyph it
    belongs to; and a two-cell glyph is never split across the boundary: it is
    dropped whole, leaving a one-column gap, because half of one renders as a
    stray cell that pushes everything after it out of line.

    `reset` appends a reset when the string was actually cut, so the clipped
    line can't bleed colour into whatever is drawn after it. Callers that go on
    to concatenate more styled content onto the result pass False.
    """
    if max_cols <= 0:
        return ""
    if text.isascii() and '\x1b' not in text:
        if len(text) <= max_cols:
            return text
        return text[:max_cols] + (Colors.RESET if reset else "")
    return _clip_ansi(text, max_cols, reset, Colors.RESET)


@functools.lru_cache(maxsize=8192)
def _clip_ansi(text: str, max_cols: int, reset: bool, _reset_code: str) -> str:
    """clip_ansi past its fast path, kept as _visual_len is (the reset code is
    part of the key: a theme can turn it off)."""
    out: list[str] = []
    visible = 0
    truncated = False
    # Text between escapes a run at a time: a run of plain ASCII is one slice,
    # only other text is measured a character at a time.
    pos = 0
    for m in (*_ANSI_RE.finditer(text), None):
        run = text[pos:m.start()] if m else text[pos:]
        if run.isascii():
            if visible + len(run) > max_cols:
                out.append(run[:max_cols - visible])
                truncated = True
                break
            out.append(run)
            visible += len(run)
        else:
            for ch in run:
                w = char_cols(ch)
                if w and visible + w > max_cols:
                    truncated = True
                    break
                out.append(ch)
                visible += w
            if truncated:
                break
        if m is None:
            break
        out.append(m.group(0))
        pos = m.end()
    res = "".join(out)
    if truncated and reset and not res.endswith(Colors.RESET):
        res += Colors.RESET
    return res


# How often something scrolling through a marquee needs redrawing.
MARQUEE_BPM = 100                    # scrolling text moves a column per beat: unhurried
MARQUEE_STEP_S = 60 / MARQUEE_BPM


def set_marquee_bpm(bpm: float) -> None:
    """How fast scrolling text moves: a column per beat at `bpm`."""
    global MARQUEE_BPM, MARQUEE_STEP_S
    MARQUEE_BPM = max(1.0, float(bpm))
    MARQUEE_STEP_S = 60 / MARQUEE_BPM


def marquee(text: str, width: int, t: float, speed: float | None = None, pause: float = 2.0) -> str:
    """Plain `text` fitted to `width` columns at time `t` (seconds): as it is
    when it fits, else scrolling through it `speed` columns a second (a column
    a beat at MARQUEE_BPM unless given), held for
    `pause` seconds at each end before starting over."""
    over = visual_len(text) - width
    if over <= 0:
        return text
    speed = speed or MARQUEE_BPM / 60
    run = over / speed
    p = t % (2 * pause + run)
    skip = 0 if p < pause else over if p >= pause + run else int((p - pause) * speed)
    for i, ch in enumerate(text):                  # drop `skip` columns from the front
        if skip <= 0:
            text = text[i:]
            break
        skip -= char_cols(ch)
    return clip_ansi(text, width, reset=False)


def truncate_text(text: str, max_width: int, placeholder: str = "…", front: bool = False) -> str:
    """Truncate `text` to `max_width` columns, replacing the cut end (or start,
    if `front`) with `placeholder`.

    Measured in columns rather than codepoints, so a CJK or emoji run is cut
    where it actually reaches the edge instead of a character count that
    overruns it by up to 2×.
    """
    if text is None:
        return ""
    if visual_len(text) <= max_width:
        return text
    ph_w = visual_len(placeholder)
    if max_width <= ph_w:
        return clip_ansi(text, max_width, reset=False)

    keep = max_width - ph_w
    if front:
        # Walk from the right, taking whole glyphs until `keep` columns are full.
        taken: list[str] = []
        used = 0
        for ch in reversed(text):
            w = char_cols(ch)
            if w and used + w > keep:
                break
            taken.append(ch)
            used += w
        return placeholder + "".join(reversed(taken))
    return clip_ansi(text, keep, reset=False) + placeholder


def stand_in(word: str) -> str:
    """`word` standing in for a value it isn't (none, varies, default, an
    image): bracketed, so it never reads as the value itself."""
    return f"‹{word}›"


def plural(n: int, singular: str, many: str | None = None) -> str:
    """``"1 result"`` / ``"156 results"``, ``"2 matches"``: the count and its noun, agreeing."""
    if abs(n) == 1:
        return f"{n} {singular}"
    return f"{n} {many or singular + ('es' if singular.endswith(('s', 'x', 'z', 'ch', 'sh')) else 's')}"


def divider(width: int | None = None, char: str = "─") -> str:
    """A horizontal rule of `char` spanning `width` (or the terminal width)."""
    width = width or get_terminal_width()
    return char * width
def format_time(seconds: int | float) -> str:
    """Convert seconds (may be float) to a compact time string.

    Preserves sub-second precision by appending centiseconds when the
    input contains a fractional portion.
    """
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        total = 0.0

    int_sec = int(total)
    frac_cs = int(round((total - int_sec) * 100))  # centiseconds (0-99)

    intervals = [31536000, 2592000, 86400, 3600, 60, 1]
    parts = []
    rem = int_sec
    for unit in intervals:
        parts.append(rem // unit)
        rem %= unit

    start = max(0, next((i for i, p in enumerate(parts[:-2]) if p > 0), len(parts) - 2))
    start = min(start, len(parts) - 2)

    result = [str(parts[start])]
    for p in parts[start + 1:]:
        result.append(str(p).zfill(2))

    base = ":".join(result)
    if frac_cs:
        return f"{base}.{frac_cs:02d}"
    return base


def progress_caps(caps: bool) -> tuple[str, str]:
    """A progress bar's end caps: brackets on a bare bar, none on one in a box
    (the box already frames it)."""
    return ("[", "]") if caps else ("", "")


def progress_caps_width(caps: bool) -> int:
    """Columns the caps add around a progress bar's cells."""
    return sum(map(len, progress_caps(caps)))


def get_progress_bar(progress: float, width: int = 40, span: tuple | None = None, caps: bool = True) -> str:
    """
    A pip-style progress bar.
    [━━━━━━━━━━━━━━━━━━━━━━━━━──────────]
    `span`: a section to pick out (see progress_cells). `caps`: see progress_caps.
    """
    left, right = progress_caps(caps)
    return f"{Colors.DIM}{left}{Colors.RESET}{progress_cells(progress, width, span)}{Colors.DIM}{right}{Colors.RESET}"


def progress_cells(progress: float, width: int, span: tuple | None = None) -> str:
    """A progress bar's `width` cells: what's played heavy and bright, whole
    cells only, the rest thin and dim. `span`: (start, end) fractions of one
    section (a chapter) to pick out: from its start to now in the accent
    colour, from now to its end dim."""
    played = round(max(0, min(1, progress)) * width)
    cells = [(Colors.STRONG, "━")] * played + [(Colors.DIM, "─")] * (width - played)
    if span:
        lo = max(0, min(width - 1, int(span[0] * width)))
        hi = max(lo + 1, min(width, round(span[1] * width)))
        cells[lo:hi] = [(Colors.ACCENT, c) if i < played else (Colors.DIM, "━")
                        for i, (_, c) in enumerate(cells[lo:hi], lo)]
    return "".join(f"{colour}{''.join(c for _, c in group)}{Colors.RESET}"
                   for colour, group in groupby(cells, key=lambda cell: cell[0]))


C = Colors   # the short name every widget uses


# --- meters, boxes and live-view pieces (dashboards, progress, sizes) ---

SPIN = list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

SPARK = "▁▂▃▄▅▆▇█"

def content_width(min_width: int = 1) -> int:
    """Terminal columns available for content, after the global left+right
    margin - the width every box/bar in a frame should be drawn against.
    No floor beyond `min_width`, so content is always sized against the real
    width, however narrow.
    """
    return max(min_width, get_terminal_width() - 2 * MARGIN_H)

def rate_of_change(history: list, now: float, value: float, window: float = 20.0, max_len: int = 60):
    """Tracks `value` over time in `history` (a list of (ts, value) pairs,
    mutated in place and capped to `max_len` entries) and returns its rate
    of change per second, measured from the oldest sample within the last
    `window` seconds. None when there is no earlier sample in that window to
    measure from (the first call, or after a gap longer than `window`), so
    a caller can show "measuring" instead.
    """
    history.append((now, value))
    del history[:-max_len]
    base_ts = base_val = None
    for ts, v in history:
        if ts >= now - window:
            base_ts, base_val = ts, v
            break
    if base_ts is None or now - base_ts <= 0:
        return None
    return (value - base_val) / (now - base_ts)

def sparkline(rate_history: list, rate: float, max_len: int = 14) -> str:
    """Appends `rate` to `rate_history` (mutated in place, capped to
    `max_len`) and renders it as an 8-level sparkline (SPARK), scaled to the
    largest rate currently in the window.

    Deliberately a separate rendering from bar(): a bar is a fraction of a
    whole (how full), a sparkline is a trend over time (how fast, lately) -
    conflating the two by drawing both the same way reads as one number
    doubled, not two.
    """
    rate_history.append(rate)
    del rate_history[:-max_len]
    mx = max(rate_history, default=1) or 1
    return "".join(SPARK[max(0, min(7, int(r / mx * 7.99)))] for r in rate_history)

def spinner(frame: int) -> str:
    return SPIN[frame % len(SPIN)]

def human_gb(kb: float) -> str:
    """`kb` (kilobytes) as a one-decimal GB string, e.g. "3.5"."""
    return f"{kb / 1048576:.1f}"

def dir_size_kb(path) -> int:
    """Total size of every file under `path`, in KB (0 if it doesn't exist)."""
    path = Path(path)
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total // 1024


_ANSI_DEMO = re.compile(r"\033\[[0-9;]*[a-zA-Z]")


def _demo() -> None:
    """Self-check for the pure logic here.
    Runs without a terminal: `python3 -m backbone.ui`.
    """
    assert visual_len("plain") == 5
    assert visual_len(f"{Colors.BOLD}x{Colors.RESET}") == 1
    assert truncate_text("abcdefgh", 4) == "abc…"
    assert plural(1, "disc") == "1 disc" and plural(2, "disc") == "2 discs"
    assert human_gb(1048576) == "1.0"
    hist = []
    assert rate_of_change(hist, 0.0, 0) is None            # first sample - no window yet
    assert rate_of_change(hist, 10.0, 1024 * 10) == 1024.0  # 10240 KB over 10s = 1024 KB/s
    rh = []
    spark1 = sparkline(rh, 10.0)
    spark2 = sparkline(rh, 20.0)
    assert len(spark1) == 1 and len(spark2) == 2
    print("backbone.ui self-check OK")


if __name__ == "__main__":
    _demo()
