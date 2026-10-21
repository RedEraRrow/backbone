"""The one line editor every text field uses."""
import unittest

from backbone.prompt.core import edit_line


class EditLineTest(unittest.TestCase):
    def test_keys(self):
        buf = list("ac")
        pos = edit_line(buf, 1, 'b')                 # typing inserts at the caret
        self.assertEqual(("".join(buf), pos), ("abc", 2))
        pos = edit_line(buf, pos, 'SPACE')
        self.assertEqual(("".join(buf), pos), ("ab c", 3))
        pos = edit_line(buf, pos, 'BACKSPACE')
        self.assertEqual(("".join(buf), pos), ("abc", 2))
        self.assertEqual(edit_line(buf, 0, 'HOME'), 0)   # 0 is a position, not "unhandled"
        self.assertEqual(edit_line(buf, 0, 'BACKSPACE'), 0)
        self.assertEqual(edit_line(buf, 0, 'DELETE'), 0)
        self.assertEqual("".join(buf), "bc")
        self.assertEqual(edit_line(buf, 2, 'DELETE'), 2)  # at the end: nothing to delete
        self.assertEqual((edit_line(buf, 0, 'LEFT'), edit_line(buf, 2, 'RIGHT'), edit_line(buf, 0, 'END')), (0, 2, 2))
        self.assertIsNone(edit_line(buf, 0, 'ENTER'))
        self.assertIsNone(edit_line(buf, 0, 'MOUSE_CLICK:0:1:1'))


if __name__ == "__main__":
    unittest.main()


class CaretClickTest(unittest.TestCase):
    """A click in a field's text puts the caret there; a long edited cell
    scrolls to keep its caret in view."""
    def test_a_click_moves_the_caret_to_the_character_under_it(self):
        from backbone.prompt.core import caret_spans
        buf = list("hello world")
        caret_spans(buf, [(5, 10, 0, "hello "), (6, 10, 6, "world")])     # wrapped onto two rows
        self.assertEqual(edit_line(buf, 0, 'MOUSE_CLICK:0:5:12'), 2)      # the "l"
        self.assertEqual(edit_line(buf, 0, 'MOUSE_CLICK:0:6:11'), 7)      # "o" of world
        self.assertEqual(edit_line(buf, 0, 'MOUSE_CLICK:0:6:40'), 11)     # past the text: its end
        self.assertIsNone(edit_line(buf, 0, 'MOUSE_CLICK:0:7:11'))        # off the field
        self.assertIsNone(edit_line(list("hello world"), 0, 'MOUSE_CLICK:0:5:12'))   # another buffer

    def test_a_field_is_found_from_its_drawn_caret(self):
        from backbone.prompt.core import block_cursor, caret_span_at
        buf = list("hello")
        caret_span_at(buf, 2, "  ab " + block_cursor("hello", 2), 3)
        self.assertEqual(edit_line(buf, 2, 'MOUSE_CLICK:0:3:7'), 1)

    def test_a_click_in_a_form_picks_the_field_and_the_place(self):
        from backbone.prompt.core import clicked_field, field_row_spans
        h, m = list("12"), list("34")
        field_row_spans(2, 3, [(h, "12"), (None, ":"), (m, "34")])
        self.assertEqual(clicked_field('MOUSE_CLICK:0:2:7', {'h': h, 'm': m}), ('m', 1))
        self.assertEqual(clicked_field('MOUSE_CLICK:0:2:3', {'h': h, 'm': m}), ('h', 0))

    def test_a_long_cell_scrolls_to_its_caret(self):
        from backbone.prompt.core import _around_caret, edit_segments
        end = _around_caret(edit_segments("a" * 30 + "END", 33), 10)
        self.assertEqual("".join(t for t, _s in end), "…aaaaaEND ")
        self.assertEqual(end[-1], (" ", 'cursor'))                      # the caret, in view
        start = _around_caret(edit_segments("START" + "a" * 30, 0), 10)
        self.assertEqual((start[0], start[-1]), (("S", 'cursor'), ("…", 'dim')))

    def test_a_field_starts_where_its_frame_puts_it(self):
        import sys
        from unittest.mock import patch
        from backbone import ui
        from backbone.prompt.core import framed_col
        text = sys.modules['backbone.prompt.text']
        chrome = sys.modules['backbone.prompt.chrome']
        core = sys.modules['backbone.prompt.core']
        for fits in (True, False):
            with patch.object(text, 'box_fits', lambda: fits), patch.object(chrome, 'box_fits', lambda: fits), \
                    patch.object(core, 'box_fits', lambda: fits), patch.object(ui, 'get_terminal_size', lambda *a: (80, 30)):
                out = text.field_frame("Title:", ["  ABCDEF"], ["  note"], [], {})
                self.assertEqual(ui.strip_ansi(out[1]).index("ABCDEF") + 1, framed_col(), fits)
