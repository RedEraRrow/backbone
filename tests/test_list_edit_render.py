"""The list editor opens, edits and saves, boxed and in a window too short for a box."""
import importlib
import io
import sys
import unittest
from unittest.mock import patch

from backbone import prompt, ui

le = importlib.import_module("backbone.prompt.list_edit")


def _run(cols, rows, keys, items=None, headers=("ROLE", "NAME"), **kw):
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
            return prompt.list_edit("Musicians", [["vocals", "Ann"]] if items is None else items, headers, **kw)
    finally:
        sys.stdout = real


class ListEditRenderTest(unittest.TestCase):
    def test_edits_and_saves_at_any_size(self):
        for cols, rows in ((90, 20), (60, 8), (30, 6)):
            got = _run(cols, rows, ['e', 'X', 'ENTER', 'ENTER'])
            self.assertEqual([list(r) for r in got], [["vocalsX", "Ann"]], (cols, rows))


    def test_back_with_nothing_changed_asks_nothing(self):
        with patch.object(le, 'confirm', side_effect=AssertionError("asked")):
            self.assertIsNone(_run(90, 20, ['DOWN', 'ESC']))

    def test_back_with_changes_asks_whether_to_drop_them(self):
        with patch.object(le, 'confirm', return_value=True):
            self.assertIsNone(_run(90, 20, ['e', 'X', 'ENTER', 'ESC']))
        with patch.object(le, 'confirm', return_value=False):
            self.assertEqual([list(r) for r in _run(90, 20, ['e', 'X', 'ENTER', 'ESC'])], [["vocalsX", "Ann"]])

    def test_one_column_rows_stay_values(self):
        got = _run(90, 20, ['e', 'X', 'ENTER', 'ENTER'], items=["Musical", "Soundtrack"], headers=("VALUE",))
        self.assertEqual(got, ["MusicalX", "Soundtrack"])

    def test_tab_skips_a_locked_column_and_a_choice_steps(self):
        got = _run(90, 20, ['e', 'DOWN', 'ENTER', 'ENTER'], items=[("TIT2", "Title")],
                   fixed_rows=True, locked_cols={0}, col_hints=lambda c, r: ["Title", "Name"] if c == 1 else [])
        self.assertEqual([list(r) for r in got], [["TIT2", "Name"]])

    def test_a_timestamp_cell_takes_digits_only(self):
        got = _run(90, 20, ['e', 'TAB'] + list("2024x05") + ['ENTER', 'ENTER'], items=[("1", "")],
                   headers=("FROM", "START"), col_types={1: 'timestamp'})
        self.assertEqual([list(r) for r in got], [["1", "2024-05"]])

    def test_the_frame_is_a_ruled_grid_with_the_note(self):
        lines = le._build_list_edit_lines("Ranges", [("1", "12", "Disc {n}")], ("FROM", "TO", "VALUE"), 0, 0,
                                          None, [], {}, note="Positions 1 to 24.")[0]
        text = "\n".join(ui.strip_ansi(ln) for ln in lines)
        self.assertIn("Positions 1 to 24.", text)
        self.assertRegex(text, r"FROM │ TO │ VALUE")
        self.assertRegex(text, r"─+┼─+┼─+")
        self.assertRegex(text, r"1    │ 12 │ Disc \{n\}")

    def test_a_grid_fills_its_width_and_shows_a_window_of_rows(self):
        rows = [["Composer", "Stephen Sondheim"], ["Lyricist", "Stephen Sondheim"], ["Producer", "Tom"]]
        with patch.object(ui, 'get_terminal_size', lambda *a: (80, 24)):
            lines = [ui.strip_ansi(ln) for ln in prompt.table_lines(("ROLE", "NAME"), rows, 40, first=1, count=1)]
        self.assertEqual(len(lines), 3)                                   # headings, rule, the one row
        self.assertTrue(all(ui.visual_len(ln) == 40 for ln in lines))      # every line edge to edge
        self.assertIn("Lyricist", lines[2])
        self.assertEqual(lines[0].index("│"), lines[2].index("│"))         # laid out by every row, not just those shown


class FractionEditTest(unittest.TestCase):
    def _run(self, keys, **kw):
        va = importlib.import_module("backbone.prompt.values")
        feed = iter(keys)
        W = type("W", (va._Widget,), {"render": lambda self, lines: None})
        with patch.object(ui, 'get_terminal_size', lambda *a: (90, 20)), \
             patch.object(va, '_read_key', lambda fd: next(feed)), \
             patch.object(va, '_wait_for_keypress', lambda t: True), \
             patch.object(va, '_set_raw'), patch.object(va, '_restore_term_attrs'), \
             patch.object(va, '_get_term_attrs'), patch.object(va, '_Widget', W), \
             patch.object(va.sys.stdin, 'fileno', lambda: 0), patch.object(sys, 'stdout', io.StringIO()):
            return prompt.fraction_edit("Track number", **kw)

    def test_edits_either_half(self):
        self.assertEqual(self._run(['BACKSPACE', '4', 'TAB', '3', 'ENTER'], value="3/12"),
                         {'current': '4', 'total': '123'})

    def test_a_varying_half_left_blank_is_kept_per_file(self):
        self.assertEqual(self._run(['TAB', 'BACKSPACE', '9', 'ENTER'], value="/12", varies={'current'}),
                         {'current': None, 'total': '19'})


if __name__ == "__main__":
    unittest.main()
