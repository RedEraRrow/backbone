"""Clicking a transport hint (^p, ^n/^b) acts like pressing it (consume_chrome
runs it rather than handing the control character to widgets that don't handle
it), the page arrows are clickable keys, and the help corner names the key that
works there: `?`, or Ctrl-/ where `?` is typed."""
import unittest
from unittest.mock import patch

from backbone import keys, prompt
from backbone.prompt import core as pc
from backbone.prompt import chrome


class TransportHintClickTest(unittest.TestCase):
    def test_clicked_transport_hints_run_the_transport(self):
        calls = []
        cells: dict = {}
        pc.add_hint_click_cells(cells, "[^p] play/pause  [^n/^b] next/prev", 5,
                                         [("^p", "play/pause"), ("^n/^b", "next/prev")])
        with patch.object(chrome, '_transport_handler', calls.append), \
             patch.object(chrome, 'footer_click_action', lambda r, c: None):
            click = lambda col: prompt.consume_chrome(f"MOUSE_CLICK:0:5:{col}", cells)
            cols = {v: c for (r, c), v in cells.items()}
            for key in ('\x10', '\x0e', '\x02'):
                self.assertIs(click(cols[key]), prompt.CHROME_HANDLED)
        self.assertEqual(calls, ['playpause', 'next', 'prev'])

    def test_page_arrows_are_clickable(self):
        self.assertEqual([t[2] for t in pc._hint_key_tokens("⇞⇟")], ['PGUP', 'PGDN'])


class HelpCornerKeyTest(unittest.TestCase):
    def _chrome(self, help_key):
        cells: dict = {}
        with patch.object(pc, 'hints_visible', lambda: False), \
             patch.object(pc.ui, 'get_terminal_width', lambda: 60):
            out = prompt.append_chrome(prompt.rounded_header("Title", " · Artist", "3:21"),
                                       [], cells, pin=False, help_key=help_key)
        return [pc.ui.strip_ansi(l) for l in out], cells

    def test_the_label_names_the_key_and_the_header_keeps_its_width(self):
        free, _ = self._chrome(help_key=True)
        typed, _ = self._chrome(help_key=False)
        self.assertIn("[?] help │", free[1])
        self.assertIn("[^/] help │", typed[1])
        self.assertEqual(pc.ui.visual_len(free[1]), pc.ui.visual_len(typed[1]))
        self.assertEqual(pc.ui.visual_len(free[1]), pc.ui.visual_len(free[0]))

    def test_ctrl_slash_toggles_everywhere_and_question_mark_only_where_free(self):
        for help_key in (True, False):
            _, cells = self._chrome(help_key)
            with patch.object(chrome, 'toggle_hints') as toggled:
                self.assertIs(prompt.consume_chrome(keys.of('global.help_typed')[0], cells), prompt.CHROME_REDRAW)
                self.assertEqual(prompt.consume_chrome('?', cells),
                                 prompt.CHROME_REDRAW if help_key else None)
            self.assertEqual(toggled.call_count, 2 if help_key else 1)


if __name__ == "__main__":
    unittest.main()


class StaleHeaderToggleTest(unittest.TestCase):
    """A header built once and drawn again after the hints were switched (a
    menu left open while ? is pressed) shows one toggle, in its new state,
    in the same place: not the old one plus a second on the border."""
    def test_one_toggle_after_switching(self):
        with patch.object(pc.ui, 'get_terminal_width', lambda: 60):
            with patch.object(pc, 'hints_visible', lambda: True):
                header = prompt.rounded_header("Title", " · Artist", "3:21")
            for shown, key_free in ((False, True), (False, False), (True, False)):
                with patch.object(pc, 'hints_visible', lambda: shown):
                    cells: dict = {}
                    out = prompt.append_chrome(list(header), [], cells, pin=False, help_key=key_free)
                plain = [pc.ui.strip_ansi(l) for l in out]
                text = " ".join(plain)
                self.assertEqual(text.count("help"), 1, plain)
                self.assertIn("hide help" if shown else "] help", plain[1])
                self.assertEqual(pc.ui.visual_len(plain[1]), pc.ui.visual_len(plain[0]))
                self.assertNotIn("help", plain[0])                    # nothing on the border
