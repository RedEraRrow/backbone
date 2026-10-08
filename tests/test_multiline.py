"""The text box of several lines: typing, new lines, joins, moving by rows."""
import io
import sys
import unittest
from unittest.mock import patch

from backbone import prompt, ui
import importlib

tx = importlib.import_module("backbone.prompt.text")      # the package exports a text() too


def _run(keys, default="", cols=40, rows=20, confirm=True):
    """multiline() fed `keys`; returns what it gave back and the last frame."""
    feed, frames = iter(keys), []
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
             patch.object(ui, 'get_terminal_width', lambda *a: cols), \
             patch.object(ui, 'get_terminal_height', lambda *a: rows), \
             patch.object(tx, '_read_key', lambda fd: next(feed)), \
             patch.object(tx, '_wait_for_keypress', lambda t: True), \
             patch.object(tx, '_byte_ready', lambda fd, t: False), \
             patch.object(tx, '_set_raw'), patch.object(tx, '_restore_term_attrs'), \
             patch.object(tx, '_get_term_attrs'), \
             patch.object(tx, 'screen_paint', lambda f: frames.append(dict(f))), \
             patch('backbone.prompt.lists.confirm', lambda *a, **k: confirm), \
             patch.object(tx.sys.stdin, 'fileno', lambda: 0):
            result = prompt.multiline("Comment", default)
    finally:
        sys.stdout = real
    return result, frames[-1] if frames else {}


class MultilineTest(unittest.TestCase):
    def test_typing_new_lines_and_saving(self):
        result, _ = _run(['h', 'i', 'ENTER', 'y', 'o', '\x13'])
        self.assertEqual(result, "hi\nyo")

    def test_backspace_and_delete_join_lines(self):
        self.assertEqual(_run(['HOME', 'BACKSPACE', '\x13'], "ab\ncd")[0], "abcd")
        self.assertEqual(_run(['UP', 'END', 'DELETE', '\x13'], "ab\ncd")[0], "abcd")

    def test_arrows_cross_lines_and_keep_the_column(self):
        # From the end of "cd" up to "abcdef" lands at its column 2, then a typed x.
        self.assertEqual(_run(['UP', 'x', '\x13'], "abcdef\ncd")[0], "abxcdef\ncd")
        self.assertEqual(_run(['HOME', 'LEFT', 'x', '\x13'], "ab\ncd")[0], "abx\ncd")

    def test_esc_asks_only_when_changed(self):
        self.assertIsNone(_run(['ESC'], "same")[0])
        self.assertIsNone(_run(['x', 'ESC'], "same", confirm=True)[0])
        self.assertEqual(_run(['x', 'ESC', '\x13'], "same", confirm=False)[0], "samex")

    def test_long_lines_wrap_inside_the_box(self):
        _r, frame = _run(['\x13'], "word " * 30, cols=40)
        widths = {ui.visual_len(line) for line in frame.values() if line}
        self.assertLessEqual(max(widths), 40)
        self.assertGreater(sum("word" in ui.strip_ansi(l) for l in frame.values()), 3)

    def test_lines_are_numbered_down_the_left(self):
        _r, frame = _run(['\x13'], "\n".join(f"line {i}" for i in range(1, 12)), rows=30)
        shown = [ui.strip_ansi(l) for l in frame.values()]
        self.assertTrue(any("│  1 line 1" in l for l in shown), shown)
        self.assertTrue(any("│ 11 line 11" in l for l in shown))

    def test_find_jumps_to_the_next_match_and_round(self):
        text = "alpha beta\ngamma beta\nbeta"
        # Find "beta" from the end: round to the first, Enter on to the second; type there.
        self.assertEqual(_run(['\x06', 'b', 'e', 't', 'a', 'ENTER', 'ESC', 'X', '\x13'], text)[0],
                         "alpha beta\ngamma Xbeta\nbeta")
        self.assertEqual(_run(['\x06', 'b', 'e', 't', 'a', 'UP', 'ESC', 'X', '\x13'], text)[0],
                         "alpha beta\ngamma beta\nXbeta")

    def test_find_ignores_case_unless_a_capital_is_typed(self):
        text = "Beta beta"
        self.assertEqual(_run(['HOME', '\x06', 'b', 'ESC', 'X', '\x13'], text)[0], "XBeta beta")
        self.assertEqual(_run(['HOME', '\x06', 'B', 'ENTER', 'ESC', 'X', '\x13'], text)[0], "XBeta beta")

    def test_replace_one_then_all(self):
        text = "a cat, a cat, a cat"
        one = ['HOME', '\x12', 'c', 'a', 't', 'TAB', 'd', 'o', 'g', 'ENTER', 'ESC', '\x13']
        self.assertEqual(_run(one, text)[0], "a dog, a cat, a cat")
        every = ['\x12', 'c', 'a', 't', 'TAB', 'd', 'o', 'g', '\x01', 'ESC', '\x13']
        self.assertEqual(_run(every, text + "\ncat")[0], "a dog, a dog, a dog\ndog")

    def test_switching_find_to_replace_keeps_what_was_typed(self):
        keys = ['\x06', 'c', 'a', 't', '\x12', 'TAB', 'd', 'o', 'g', '\x01', 'ESC', '\x13']
        self.assertEqual(_run(keys, "cat cat")[0], "dog dog")

    def test_a_pattern_with_groups_behind_its_key(self):
        keys = ['\x12', '\x14', '(', '\\', 'd', '+', ')', 'm', 'TAB', '\\', '1', ' ', 'm', 'i', 'n', '\x01', 'ESC', '\x13']
        self.assertEqual(_run(keys, "5m and 10m")[0], "5 min and 10 min")
        # Plain text by default: the same keys without the switch find nothing.
        plain = [k for k in keys if k != '\x14']
        self.assertEqual(_run(plain, "5m and 10m")[0], "5m and 10m")

    def test_a_bad_pattern_finds_nothing_and_says_so(self):
        _r, frame = _run(['\x06', '\x14', '(', '\x13'], "text")
        self.assertTrue(any("bad pattern" in ui.strip_ansi(l) for l in frame.values()))

    def test_wrapping_counts_wide_characters_twice(self):
        self.assertEqual(tx._wrap_rows(["日本語です"], 4), [(0, 0, 2), (0, 2, 4), (0, 4, 5)])
        rows = tx._wrap_rows(["abcdef", "g"], 4)
        self.assertEqual(rows, [(0, 0, 4), (0, 4, 6), (1, 0, 1)])
        self.assertEqual(tx._row_of(rows, 0, 4), 1)          # a break: the row it starts
        self.assertEqual(tx._row_of(rows, 0, 6), 1)          # the line's end
        self.assertEqual(tx._pos_at(rows, 1, 9, ["abcdef", "g"]), (0, 6))
        words = ["one two three"]
        self.assertEqual([words[0][a:b] for _n, a, b in tx._wrap_rows(words, 9)], ["one two ", "three"])


class SuggestingTextTest(unittest.TestCase):
    """prompt.text with suggestions and a check."""

    def _run(self, keys, paint=lambda f: None, **kw):
        feed = iter(keys)
        real = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with patch.object(ui, 'get_terminal_size', lambda *a: (60, 20)), \
                 patch.object(tx, '_read_key', lambda fd: next(feed)), \
                 patch.object(tx, '_wait_for_keypress', lambda t: True), \
                 patch.object(tx, '_set_raw'), patch.object(tx, '_restore_term_attrs'), \
                 patch.object(tx, '_get_term_attrs'), patch.object(tx, 'screen_paint', paint), \
                 patch.object(tx.sys.stdin, 'fileno', lambda: 0):
                return prompt.text("Tag:", **kw)
        finally:
            sys.stdout = real

    WORDS = ["Mood", "Moon", "Music"]

    def suggest(self, t):
        return [(w, "") for w in self.WORDS if w.lower().startswith(t.lower())]

    def test_tab_takes_the_first_and_arrows_pick_another(self):
        self.assertEqual(self._run(['m', 'o', 'TAB', 'ENTER'], suggest=self.suggest), "Mood")
        self.assertEqual(self._run(['m', 'o', 'DOWN', 'DOWN', 'ENTER', 'ENTER'], suggest=self.suggest), "Moon")

    def test_the_field_box_fits_its_text_over_a_blank_box(self):
        frames = []
        with patch.object(ui, 'tab_rows', lambda: 0):
            self._run(['x', 'ENTER'], paint=frames.append, default="a short value")
        rows = [ui.strip_ansi(frames[-1][k]) for k in sorted(frames[-1])]
        tops = [k for k, r in enumerate(rows) if r.strip().startswith("╭")]
        self.assertEqual(len(tops), 2, rows)                                  # the field's box, then the blank one
        self.assertIn("a short valuex", rows[tops[0] + 1])
        self.assertTrue(rows[tops[0] + 2].strip().startswith("╰"))            # one line of text: three rows
        frames.clear()
        with patch.object(ui, 'tab_rows', lambda: 0):
            self._run(['ENTER'], paint=frames.append, default="word " * 20)  # wraps at 60 columns
        rows = [ui.strip_ansi(frames[-1][k]) for k in sorted(frames[-1])]
        top = next(k for k, r in enumerate(rows) if r.strip().startswith("╭"))
        self.assertFalse(rows[top + 2].strip().startswith("╰"))               # grown for the second line

    def test_keys_the_filter_refuses_do_nothing(self):
        allow = lambda t: t.isalpha()                                       # noqa: E731
        self.assertEqual(self._run(['a', '1', 'b', ' ', 'BACKSPACE', 'c', 'ENTER'], allow=allow), "ac")

    def test_tokens_complete_where_one_is_open(self):
        names = {"track": "number", "title": "name"}
        self.assertEqual(prompt.token_completions("%track% %ti", names), [("%track% %title%", "name")])
        self.assertEqual(prompt.token_completions("%track% ", names), [])
        self.assertEqual(prompt.token_completions("(?P<tr", names, "(?P<", ">"), [("(?P<track>", "number")])
        self.assertEqual(prompt.token_completions("(?P<track>x", names, "(?P<", ">"), [])

    def test_enter_waits_till_the_check_is_happy(self):
        check = lambda t: None if t in self.WORDS else "not a word"       # noqa: E731
        self.assertEqual(self._run(['x', 'ENTER', 'BACKSPACE', 'M', 'u', 's', 'i', 'c', 'ENTER'], check=check), "Music")

    def test_a_broken_suggester_never_breaks_the_field(self):
        self.assertEqual(self._run(['a', 'TAB', 'ENTER'], suggest=lambda t: 1 / 0), "a")


if __name__ == "__main__":
    unittest.main()
