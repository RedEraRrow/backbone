"""Themes: every theme fills every role, set_theme paints with it (and keeps
colour off when it's off), and the terminal's background is read right."""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from backbone import ui


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _ratio(a: str, b: str) -> float:
    """WCAG contrast ratio of two "#RRGGBB" colours."""
    def lum(h):
        r, g, b = (_linear(c / 255) for c in ui.parse_hex_colour(h))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# Colour blindness at full severity (Machado, Oliveira and Fernandes, 2009),
# in linear RGB; None is ordinary sight.
_CVD = {"normal": None,
        "protanopia": [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
        "deuteranopia": [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
        "tritanopia": [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.303900]]}


def _delta_e(a: str, b: str, matrix) -> float:
    """How far apart two colours look (CIE76 ΔE) to someone seeing through `matrix`."""
    import math

    def lab(h):
        lin = [_linear(c / 255) for c in ui.parse_hex_colour(h)]
        if matrix:
            lin = [max(0.0, min(1.0, sum(matrix[i][j] * lin[j] for j in range(3)))) for i in range(3)]
        x = (0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]) / 0.95047
        y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        z = (0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]) / 1.08883
        f = lambda v: v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116   # noqa: E731
        return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))
    return math.dist(lab(a), lab(b))


class ThemeTest(unittest.TestCase):
    def tearDown(self):
        ui.set_theme(ui.DEFAULT_THEME)

    def test_every_theme_fills_every_role(self):
        for name in ui.THEMES:
            with self.subTest(name):
                codes = ui.theme_codes(name)
                self.assertEqual(set(codes), set(ui.ROLES) | {'DIM', 'BOLD'})
                self.assertTrue(all(c.startswith("\033[") for c in codes.values()))

    def test_set_theme_paints_with_it(self):
        ui.set_theme("pocket")
        self.assertEqual(ui.Colors.ERROR, ui.theme_codes("pocket")["ERROR"])
        self.assertEqual(ui.current_theme(), "pocket")
        self.assertTrue(ui.theme_is_light())
        for bad in ("no such theme", {"accent": "x"}, None):
            ui.set_theme(bad)
            self.assertEqual(ui.current_theme(), ui.DEFAULT_THEME)

    def test_overrides_lie_over_the_theme(self):
        ui.set_theme("teletext", {"ACCENT": "#102030"})
        self.assertIn("16;32;48" if "38;2" in ui.Colors.ACCENT else "38;5;", ui.Colors.ACCENT)
        self.assertEqual(ui.Colors.OK, ui.theme_codes("teletext")["OK"])

    def test_colour_off_stays_off_and_comes_back_themed(self):
        ui.set_colour(False)
        try:
            ui.set_theme("matrix")
            self.assertEqual(ui.Colors.ACCENT, "")
        finally:
            ui.set_colour(True)
        self.assertEqual(ui.Colors.ACCENT, ui.theme_codes("matrix")["ACCENT"])

    def test_every_fixed_theme_can_be_read(self):
        """Text 4.5:1 against its background and its highlight bar, the
        accents and ERROR 3:1 (WCAG's measures): a classic look, but legible."""
        for name, (_label, t) in ui._FIXED.items():
            with self.subTest(name):
                bar = t["BAR"] if t["BAR"].startswith("#") else t["BG"]      # an underline: on the background
                self.assertGreaterEqual(_ratio(t["TEXT"], t["BG"]), 4.5)
                self.assertGreaterEqual(_ratio(t["TEXT"], bar), 4.5)
                for role in ("ACCENT", "ACCENT2", "STRONG", "ERROR"):
                    self.assertGreaterEqual(_ratio(t[role], t["BG"]), 3, role)

    def test_high_contrast_is_aaa_and_colour_blind_safe(self):
        """WCAG AAA, 7:1, for every colour text is drawn in, on whatever it can
        be drawn on (its bars aren't backgrounds); OK, WARN and ERROR, and the
        accents against them, apart (ΔE 20) for each colour blindness."""
        t = ui._FIXED["contrast"][1]
        backs = [t["BG"]] + [t[b] for b in ("BAR", "BAR_DIM") if t[b].startswith("#")]
        for role in ("TEXT", "DIM", "STRONG", "ACCENT", "ACCENT2", "OK", "WARN", "ERROR", "INFO", "NOTE", "MUTED"):
            for back in backs:
                self.assertGreaterEqual(_ratio(t[role], back), 7, (role, back))
        for a, b in (("OK", "WARN"), ("OK", "ERROR"), ("WARN", "ERROR"),
                     ("ACCENT", "OK"), ("ACCENT", "WARN"), ("ACCENT", "ERROR"), ("ACCENT", "ACCENT2")):
            for view, matrix in _CVD.items():
                self.assertGreaterEqual(_delta_e(t[a], t[b], matrix), 20, (a, b, view))

    def test_muted_is_quiet_and_bars_are_behind_text(self):
        codes = ui.theme_codes("teletext")
        self.assertNotIn("1;", codes["MUTED"])
        self.assertTrue(codes["BAR"].startswith("\033[48;"))


class TerminalAccentTest(unittest.TestCase):
    """Terminal's accents are chosen from the terminal's own colours; the
    fixed themes keep theirs."""

    def tearDown(self):
        ui.set_terminal_accents(*ui.DEFAULT_ACCENTS)
        ui.set_theme(ui.DEFAULT_THEME)

    def test_chosen_accents_paint_terminal_at_once(self):
        ui.set_theme("terminal")
        ui.set_terminal_accents("bright blue", "magenta")
        self.assertEqual((ui.Colors.ACCENT, ui.Colors.ACCENT2), ("\033[1;94m", "\033[1;35m"))
        self.assertEqual(ui.theme_codes("terminal")["ACCENT"], "\033[1;94m")    # its swatch too

    def test_a_fixed_theme_keeps_its_own(self):
        ui.set_terminal_accents("red", "red")
        ui.set_theme("dos")
        self.assertEqual(ui.Colors.ACCENT, ui.theme_codes("dos")["ACCENT"])
        self.assertNotEqual(ui.Colors.ACCENT, "\033[1;31m")

    def test_an_unknown_name_is_the_default(self):
        ui.set_terminal_accents("rose", None)
        self.assertEqual(ui.terminal_accent_names(), ui.DEFAULT_ACCENTS)

    def test_a_hosts_overrides_still_lie_over_them(self):
        ui.set_terminal_accents("blue", "yellow")
        ui.set_theme("terminal", {"ACCENT": "#123456"})
        self.assertIn("18;52;86", ui.Colors.ACCENT if "38;2" in ui.Colors.ACCENT else "18;52;86")
        self.assertEqual(ui.Colors.ACCENT2, "\033[1;33m")


class ChangeTest(unittest.TestCase):
    """A change of colours repaints the screen that's up; the same colours
    again don't, and don't rewrite the terminal's."""

    def tearDown(self):
        ui.set_theme(ui.DEFAULT_THEME)
        ui.consume_resize()

    def test_a_new_colour_lays_the_screen_out_again(self):
        ui.set_theme("matrix")
        ui.consume_resize()
        ui.set_theme("matrix")
        self.assertFalse(ui.consume_resize())                      # nothing changed
        ui.set_theme("terminal", {"ACCENT": "#123456"})
        self.assertTrue(ui.consume_resize())                       # a new track's cover: repaint

    def test_the_terminals_colours_are_written_once(self):
        import io
        out = io.StringIO()
        out.isatty = lambda: True
        with patch.object(ui.sys, "stdout", out), patch.object(ui.sys, "__stdout__", out), \
             patch.dict(ui._full, {'on': None, 'supported': True}), patch.object(ui, "_in_app", [True]), \
             patch.object(ui, "_light", [False]):
            ui.set_theme("teletext")
            once = out.getvalue()
            ui.set_theme("teletext")                               # a track change, the same theme
            self.assertEqual(out.getvalue(), once)
            ui._in_app[0] = False
            ui._paint_terminal()

    def test_a_cell_can_be_worked_out_as_its_drawn(self):
        from backbone.prompt.core import _cell_text, _render_cell_segments
        state = ["first"]
        cell = lambda: [(state[0], 'normal')]                     # noqa: E731
        self.assertEqual(_cell_text(cell)[0], "first")
        state[0] = "second"
        self.assertIn("second", _render_cell_segments(cell, 'normal', False, 10, 'left'))


class SwatchTest(unittest.TestCase):
    """The theme picker's swatches are each theme as it looks: its own
    background, text, secondary text, minor title, borders, cursor and band,
    whichever theme is on while the list is shown."""

    def tearDown(self):
        ui.set_theme(ui.DEFAULT_THEME)

    def test_each_swatch_is_drawn_in_its_own_colours(self):
        from backbone.prompt.settings import theme_sample
        ui.set_theme("teletext")                                    # another theme on: no difference
        for name in ui.THEMES:
            with self.subTest(name):
                sample = theme_sample(name)[0][0]
                c = ui.theme_codes(name)
                for code in (ui.theme_background(name), ui.theme_text(name), c["FRAME"], c["ACCENT2"],
                             c["DIM"], c["HEADING"], c["ACCENT"], c["BAR"]):
                    self.assertIn(code, sample)

    def test_a_hosts_theme_is_shown_with_its_own_colours(self):
        from backbone.prompt.settings import theme_sample
        sample = theme_sample("album_art", {"ACCENT": "#123456", "ACCENT2": "#654321"})[0][0]
        self.assertIn(ui.rgb_code("#123456"), sample)
        self.assertIn(ui.rgb_code("#654321"), sample)

    def test_readable_moves_only_lightness_and_only_as_far_as_it_must(self):
        import colorsys
        self.assertEqual(ui.readable("#FFB000", "#000000"), "#FFB000")          # reads already: itself
        navy = ui.readable("#1A2A6C", "#000000")
        self.assertGreaterEqual(ui.contrast_ratio(navy, "#000000"), 4.5)
        self.assertLess(ui.contrast_ratio(navy, "#000000"), 4.9)                # no further than it must
        h0, _l, s0 = colorsys.rgb_to_hls(*(v / 255 for v in ui.parse_hex_colour("#1A2A6C")))
        h1, _l, s1 = colorsys.rgb_to_hls(*(v / 255 for v in ui.parse_hex_colour(navy)))
        self.assertAlmostEqual(h0, h1, delta=0.01)
        self.assertAlmostEqual(s0, s1, delta=0.03)
        self.assertGreaterEqual(ui.contrast_ratio(ui.readable("#F0E68C", "#FFFFFF"), "#FFFFFF"), 4.5)   # darker on light

    def test_terminals_swatch_is_the_terminals_own_colours(self):
        with patch.dict(ui._own, {"TEXT": "#C0C0C0", "BG": "#101010"}), patch.object(ui, "_light", [False]):
            ui.set_theme("matrix")
            self.assertEqual(ui.theme_text("terminal"), ui.rgb_code("#C0C0C0", bold=False))
            self.assertEqual(ui.theme_background("terminal"), ui.rgb_code("#101010", background=True))
        with patch.dict(ui._own, {"TEXT": None, "BG": None}), patch.object(ui, "_light", [False]):
            self.assertEqual(ui.theme_background("terminal"), "\033[49m")     # never said: its default

    def test_both_colours_are_kept_from_one_reply(self):
        reply = b"\033]10;rgb:c0c0/c0c0/c0c0\033\\\033]11;rgb:1010/1010/1010\033\\\033[?62c"
        with patch.dict(ui._own, {"TEXT": None, "BG": None}), patch.object(ui, "_ask_terminal", lambda req: reply):
            self.assertFalse(ui._ask_background())
            self.assertEqual((ui._own["TEXT"], ui._own["BG"]), ("#C0C0C0", "#101010"))


class BackgroundTest(unittest.TestCase):
    def test_reply(self):
        self.assertTrue(ui._light_from_reply(b"\033]11;rgb:ffff/fefe/f0f0\033\\\033[?62;22c"))
        self.assertFalse(ui._light_from_reply(b"\033]11;rgb:1c1c/1c1c/1c1c\007\033[?1;2c"))
        self.assertFalse(ui._light_from_reply(b"\033]11;rgb:00/00/00\033\\"))
        self.assertIsNone(ui._light_from_reply(b"\033[?62;22c"))           # answered only the sentinel

    def test_colorfgbg(self):
        for value, light in (("0;15", True), ("15;0", False), ("12;7", True), ("", None), ("default;default", None)):
            with self.subTest(value), patch.dict("os.environ", {"COLORFGBG": value}):
                self.assertEqual(ui._background_from_env(), light)

    def test_terminal_bars_follow_the_background(self):
        with patch.object(ui, "_light", [True]):
            light = ui.theme_codes("terminal")["BAR"]
        with patch.object(ui, "_light", [False]):
            dark = ui.theme_codes("terminal")["BAR"]
        self.assertNotEqual(light, dark)


class WholeAppTest(unittest.TestCase):
    """On the app's screen a fixed theme is the terminal's own default text
    and background, put back off it; a terminal that won't take it is left be."""

    def setUp(self):
        import io
        self.out = io.StringIO()
        self.out.isatty = lambda: True
        self.patches = [patch.object(ui.sys, "stdout", self.out), patch.object(ui.sys, "__stdout__", self.out),
                        patch.dict(ui._full, {'on': False, 'supported': None}), patch.object(ui, "_in_app", [True]),
                        patch.object(ui, "_light", [False])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        ui._in_app[0] = False
        ui.set_theme(ui.DEFAULT_THEME)
        for p in self.patches:
            p.stop()

    def test_a_terminal_that_takes_it(self):
        with patch.object(ui, "_ask_terminal", lambda req: b"\033]11;rgb:0000/0000/0000\033\\\033[?62c"):
            ui.set_theme("teletext")
        self.assertIn("\033]11;rgb:00/00/00", self.out.getvalue())
        self.assertTrue(ui.full_themes_supported())
        ui.set_theme("terminal")                                    # Terminal: the terminal's own again
        self.assertTrue(self.out.getvalue().endswith("\033]110\033\\\033]111\033\\"))

    def test_leaving_the_app_screen_puts_them_back(self):
        with patch.object(ui, "_ask_terminal", lambda req: b"\033]11;rgb:9b9b/bcbc/0f0f\033\\"):
            ui.set_theme("pocket")
        ui._in_app[0] = False
        ui._paint_terminal()
        self.assertTrue(self.out.getvalue().endswith("\033]111\033\\"))
        self.assertFalse(ui._full['on'])

    def test_a_terminal_that_wont_is_left_be(self):
        with patch.object(ui, "_ask_terminal", lambda req: b"\033[?62c"):
            ui.set_theme("matrix")
        self.assertIs(ui.full_themes_supported(), False)
        self.assertFalse(ui._full['on'])
        written = self.out.getvalue()
        ui.set_theme("teletext")                                       # not tried again
        self.assertEqual(self.out.getvalue(), written)

    def test_outside_the_app_screen_nothing_is_set(self):
        ui._in_app[0] = False
        ui.set_theme("teletext")
        self.assertEqual(self.out.getvalue(), "")


class ItalicTest(unittest.TestCase):
    def test_italic_where_terminfo_has_it_else_underline(self):
        # A process of its own for each: curses reads terminfo once a process.
        for term, code in (("xterm-256color", "\033[3m"), ("screen-256color", ui.Colors.UNDERLINE)):
            got = subprocess.run([sys.executable, "-c", "from backbone import ui; print(ui._italic_code(), end='')"],
                                 env={**os.environ, "TERM": term}, capture_output=True, text=True).stdout
            self.assertEqual(got, code, term)


if __name__ == "__main__":
    unittest.main()
