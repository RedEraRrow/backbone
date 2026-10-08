"""Boxes over the middle of the screen (the : command line, the panel
picker): they take the screen over at any size, and say what they're for."""
import importlib
import io
import sys
import unittest
from unittest.mock import patch

from backbone import prompt, ui

tx = importlib.import_module("backbone.prompt.text")


def _run(cols, rows, keys, **kw):
    """overlay_text at cols × rows fed `keys`: (what it returned, what it painted, plain)."""
    feed, out = iter(keys), io.StringIO()
    with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
         patch.object(tx, '_read_key', lambda fd: next(feed)), \
         patch.object(tx, '_wait_for_keypress', lambda t: True), \
         patch.object(tx, '_set_raw'), patch.object(tx, '_restore_term_attrs'), \
         patch.object(tx, '_get_term_attrs'), \
         patch.object(tx.sys.stdin, 'fileno', lambda: 0), patch.object(sys, 'stdout', out):
        from backbone.prompt import core
        core.screen_invalidate()
        got = prompt.overlay_text("Command, as after backtrack", **kw)
    return got, ui.strip_ansi(out.getvalue())


class OverlayTest(unittest.TestCase):
    def test_any_size_gets_a_field(self):
        for cols, rows in ((100, 30), (30, 8), (12, 5), (10, 2), (6, 1)):
            got, _shown = _run(cols, rows, ['l', 's', 'ENTER'])
            self.assertEqual(got, "ls", (cols, rows))

    def test_a_box_only_where_it_fits(self):
        with patch.object(ui, 'get_terminal_size', lambda *a: (100, 30)):
            self.assertEqual(tx._overlay_place(3, 90)['h'], 3)
        with patch.object(ui, 'get_terminal_size', lambda *a: (40, 2)):
            place = tx._overlay_place(3, 90)
            self.assertEqual((place['h'], place['w']), (1, 40))                 # one row, edge to edge
        with patch.object(ui, 'get_terminal_size', lambda *a: (30, 8)):
            self.assertLessEqual(tx._overlay_place(3, 90)['w'], 30)             # never past the window

    def test_the_empty_field_shows_its_placeholder_at_any_size(self):
        _got, wide = _run(100, 30, ['ESC'], placeholder="command")
        self.assertIn("Command, as after backtrack", wide)
        self.assertIn("command", wide.replace("Command", ""))
        _got, untitled = _run(14, 5, ['ESC'])
        self.assertIn("Command", untitled)                                       # no placeholder: the cut title says what it's for
        _got, small = _run(14, 5, ['ESC'], placeholder="command")
        self.assertIn("command", small)
        _got, bare = _run(10, 2, ['ESC'], placeholder="command")
        self.assertIn(": command", bare)


if __name__ == "__main__":
    unittest.main()
