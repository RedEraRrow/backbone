"""The list editor opens, edits and saves, boxed and in a window too short for a box."""
import importlib
import io
import sys
import unittest
from unittest.mock import patch

from backbone import prompt, ui

le = importlib.import_module("backbone.prompt.list_edit")


def _run(cols, rows, keys):
    feed = iter(keys)
    W = type("W", (le._Widget,), {"render": lambda self, lines: None})
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
             patch.object(ui, 'get_terminal_width', lambda *a: cols), \
             patch.object(ui, 'get_terminal_height', lambda *a: rows), \
             patch.object(le, '_read_key', lambda fd: next(feed)), \
             patch.object(le, '_wait_for_keypress', lambda t: True), \
             patch.object(le, '_set_raw'), patch.object(le, '_restore_term_attrs'), \
             patch.object(le, '_get_term_attrs'), patch.object(le, '_Widget', W), \
             patch.object(le.sys.stdin, 'fileno', lambda: 0):
            return prompt.list_edit("Musicians:", [["vocals", "Ann"]], ("ROLE", "NAME"))
    finally:
        sys.stdout = real


class ListEditRenderTest(unittest.TestCase):
    def test_edits_and_saves_at_any_size(self):
        for cols, rows in ((90, 20), (60, 8), (30, 6)):
            got = _run(cols, rows, ['e', 'X', 'ENTER', 'ENTER'])
            self.assertEqual([list(r) for r in got], [["vocalsX", "Ann"]], (cols, rows))


if __name__ == "__main__":
    unittest.main()
