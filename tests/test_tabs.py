"""Tabs: each runs in its own thread, one at a time, and comes back as it was left."""
import unittest
from unittest.mock import patch

from backbone import nav, ui
from backbone.prompt import core


def _run(*bodies, home=0):
    """run_tabs over tabs named A, B, C... whose run() is each body; returns the
    log. The first body quits (q) when it gets to its end."""
    log = []

    def first():
        bodies[0](log)
        raise nav.QuitToTerminal()
    tabs = [("A", first)] + [(chr(66 + i), (lambda b=b: b(log))) for i, b in enumerate(bodies[1:])]
    try:
        nav.run_tabs(tabs, home)
    except nav.QuitToTerminal:
        pass
    return log


class SwitchingTest(unittest.TestCase):
    def test_a_tab_comes_back_where_it_was(self):
        def a(log):
            log.append("A1"); nav.switch_to(1); log.append("A2")

        def b(log):
            log.append(("B1", nav.NAV_STACK[:])); nav.switch_to(0); log.append("B2")
        self.assertEqual(_run(a, b), ["A1", ("B1", ["B"]), "A2"])

    def test_backing_out_goes_to_the_tab_before(self):
        def a(log):
            log.append("A"); nav.switch_to(2); log.append("A again")

        def b(log):
            log.append("B")                                          # backs out at once

        def c(log):
            log.append("C"); nav.switch_to(1); log.append("C again")
        self.assertEqual(_run(a, b, c), ["A", "C", "B", "C again", "A again"])

    def test_a_tab_that_ended_starts_afresh(self):
        def a(log):
            nav.switch_to(1); nav.switch_to(1); log.append("A")

        def b(log):
            log.append("B")
        self.assertEqual(_run(a, b), ["B", "B", "A"])

    def test_backing_out_of_the_only_tab_starts_it_again(self):
        runs = []

        def home():
            runs.append(1)
            if len(runs) == 3:
                raise nav.QuitToTerminal()
        with self.assertRaises(nav.QuitToTerminal):
            nav.run_tabs([("Browse", home)])
        self.assertEqual(len(runs), 3)

    def test_quitting_from_a_tab_reaches_the_app(self):
        def b():
            raise nav.QuitToTerminal()
        with self.assertRaises(nav.QuitToTerminal):
            nav.run_tabs([("A", lambda: nav.switch_to(1)), ("B", b)])
        self.assertEqual(nav.TABS, [])
        self.assertIsNone(nav.current_tab())

    def test_a_tab_sent_for_fresh_starts_again_from_the_top(self):
        def a(log):
            nav.switch_to(1); nav.go_to_tab("B", fresh=True); log.append("A")

        def b(log):
            log.append("B"); nav.switch_to(0); log.append("B resumed")
        self.assertEqual(_run(a, b), ["B", "B", "A"])

    def test_the_keys(self):
        def a(log):
            log += [nav.tab_for(k) for k in ("TAB", "BACKTAB", "F3", "F1", "F9", "3", "x")]
            log += [nav.tab_for("TAB", use_keys=False), nav.tab_for("F2", use_keys=False)]
        self.assertEqual(_run(a, lambda log: None, lambda log: None),
                         [1, 2, 2, None, None, None, None, None, 1])
        self.assertIsNone(nav.tab_for("TAB"))                       # outside tabs

    def test_a_modal_screen_keeps_its_tab(self):
        def a(log):
            with nav.modal():
                log += [nav.tab_for("TAB"), nav.tab_for("F2")]
            log.append(nav.tab_for("TAB"))
        self.assertEqual(_run(a, lambda log: None), [None, None, 1])


class BarTest(unittest.TestCase):
    NAMES = ["Home", "Now playing", "Browse", "Search", "History", "Settings"]

    def _bar(self, cols, on=2, keys=False):
        """The bar's two rows, plain."""
        with patch.object(nav, 'TABS', [(n, None) for n in self.NAMES]), \
             patch.object(nav, '_active', [on]), \
             patch.object(ui, '_tab_keys_shown', lambda: keys), \
             patch.object(ui, 'get_terminal_width', lambda *a: cols):
            lines = [ui.strip_ansi(l) for l in ui.tab_bar_lines()]
            self.notch = ui._notch[0]
        self.assertEqual(len(lines), 2)
        return lines

    def test_wide_names_them_all_and_outlines_the_showing_one(self):
        top, mid = self._bar(120)
        self.assertTrue(all(n in mid for n in self.NAMES))
        self.assertNotIn("F1", mid)                                      # keys only with the hints
        a, b = self.notch
        self.assertEqual((top[a], top[b], mid[a], mid[b]), ("╭", "╮", "│", "│"))
        self.assertEqual(mid[a + 2:b - 1], "Browse")
        self.assertEqual(ui.tab_at(mid.index("Search") + 1), 3)          # 1-based column
        self.assertIsNone(ui.tab_at(mid.index("Search") - 2))            # between two tabs

    def test_the_last_tab_is_outlined_on_both_sides(self):
        top, mid = self._bar(120, on=5)
        a, b = self.notch
        self.assertEqual((mid[a], mid[b]), ("│", "│"))

    def test_the_hints_show_the_keys(self):
        _top, mid = self._bar(120, keys=True)
        self.assertIn("F3 Browse", mid)
        self.assertIn("F6 Settings", mid)

    def test_narrow_names_only_the_one_showing(self):
        top, mid = self._bar(50)
        self.assertIn("Browse", mid)
        self.assertNotIn("Search", mid)
        self.assertIn("F4", mid)
        self.assertLessEqual(max(map(ui.visual_len, (top, mid))), 50)
        self.assertEqual(ui.tab_at(mid.index("F4") + 1), 3)

    def test_very_narrow_keeps_inside_the_window(self):
        self.assertLessEqual(max(map(ui.visual_len, self._bar(12, on=1))), 12)

    def _moved(self, title, after=22):
        row = ([("", " ")] * 2 + [("", "╭"), ("", "─")] + [("", c) for c in f" {title} "]
               + [("", "─")] * 10 + [("", c) for c in " help "] + [("", "─"), ("", "╮")])
        core._title_right(row, 2, len(row) - 1, after)
        text = "".join(c for _s, c in row)
        self.assertTrue(text.rstrip().endswith("help ─╮"))
        self.assertEqual(len(row), len(text))
        return text

    def test_a_moved_title_short_of_room_loses_whole_parts_never_a_cut_one(self):
        text = self._moved("Queue · 1 of 28", after=20)
        self.assertIn(" Queue ─", text)                      # the part that fits, whole
        self.assertNotIn("1 of", text)
        self.assertNotIn("…", text)
        text = self._moved("A long title for a field")      # one part, too long: no title at all
        self.assertNotIn("A long", text)
        self.assertNotIn("…", text)
        self.assertIn("Queue · 1 of 28", self._moved("Queue · 1 of 28", after=4))   # room: all of it

    def test_where_the_tab_is_follows_a_resize_before_any_paint(self):
        with patch.object(nav, 'TABS', [(n, None) for n in self.NAMES]), \
             patch.object(nav, '_active', [2]), \
             patch.object(ui, '_tab_keys_shown', lambda: False):
            with patch.object(ui, 'get_terminal_width', lambda *a: 120):
                wide = ui.tab_notch()
            with patch.object(ui, 'get_terminal_width', lambda *a: 40):
                narrow = ui.tab_notch()                     # asked straight after: no bar painted between
        self.assertNotEqual(wide, narrow)                   # names drop to keys: the tab moves left

    def test_the_row_under_it_opens_into_the_tab(self):
        core.screen_invalidate()
        box = "  ╭─ Artists " + "─" * 80 + " Sub ─╮"
        with patch.object(nav, 'TABS', [(n, None) for n in self.NAMES]), \
             patch.object(nav, '_active', [2]), \
             patch.object(ui, 'get_terminal_width', lambda *a: 100), \
             patch.object(ui, 'get_terminal_size', lambda *a: (100, 30)):
            self.assertIn("Now playing", ui.strip_ansi(core.screen_row_paint(2, "")))
            core.screen_row_paint(3, box)
            a, b = ui._notch[0]
            row = "".join(ch or "" for _st, ch in core._cells[3])
            self.assertEqual((row[a], row[b]), ("╯", "╰"))
            self.assertEqual(row[a + 1:b].strip(), "")                     # open into the tab
            self.assertTrue(row.rstrip().endswith("Artists ─ Sub ─╮"), row)  # its title, clear of the tab
            ui.set_tabs_hidden(True)
            try:
                core.screen_invalidate()
                self.assertNotIn("Now playing", ui.strip_ansi(core.screen_row_paint(2, "")))
            finally:
                ui.set_tabs_hidden(False)
        core.screen_invalidate()
        self.assertNotIn("Home", ui.strip_ansi(core.screen_row_paint(1, "")))


if __name__ == "__main__":
    unittest.main()
