"""Tests for the inline blocking-loop progress line (backbone/ui.py):
a per-track ffmpeg scan (trim commit, loudness measurement) has no other
redraw happening, so this is the only thing that keeps it from looking hung."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from backbone import ui


class PrintInlineProgressTest(unittest.TestCase):
    def test_writes_carriage_return_and_clears_to_end_of_line(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ui.print_inline_progress("Measuring 1/3: track.mp3", 0.5)
        out = buf.getvalue()
        self.assertTrue(out.startswith("\r"))
        self.assertTrue(out.endswith("\033[K"))
        self.assertIn("track.mp3", ui.strip_ansi(out))

    def test_truncates_message_to_fit_terminal_width(self):
        buf = io.StringIO()
        long_name = "x" * 200 + ".mp3"
        with patch.object(ui, "get_terminal_width", return_value=40):
            with redirect_stdout(buf):
                ui.print_inline_progress(f"Measuring 1/1: {long_name}", 0.0)
        plain = ui.strip_ansi(buf.getvalue())
        self.assertLessEqual(ui.visual_len(buf.getvalue()), 40)
        self.assertIn("…", plain)

    def test_inset_by_margin_and_centred_when_short(self):
        buf = io.StringIO()
        with patch.object(ui, "get_terminal_width", return_value=80):
            with redirect_stdout(buf):
                ui.print_inline_progress("short", 0.0)
        plain = ui.strip_ansi(buf.getvalue()).lstrip("\r")
        leading_spaces = len(plain) - len(plain.lstrip(" "))
        self.assertGreaterEqual(leading_spaces, ui.MARGIN_H)
        # centred: pad should be roughly (width - content) // 2, not just the minimum
        self.assertGreater(leading_spaces, ui.MARGIN_H)

    def test_never_pads_below_margin_when_content_fills_width(self):
        buf = io.StringIO()
        long_name = "x" * 200 + ".mp3"
        with patch.object(ui, "get_terminal_width", return_value=40):
            with redirect_stdout(buf):
                ui.print_inline_progress(f"Measuring 1/1: {long_name}", 0.0)
        plain = ui.strip_ansi(buf.getvalue()).lstrip("\r")
        leading_spaces = len(plain) - len(plain.lstrip(" "))
        self.assertGreaterEqual(leading_spaces, ui.MARGIN_H)

    def test_clear_inline_progress_writes_clear_sequence(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ui.clear_inline_progress()
        self.assertEqual(buf.getvalue(), "\r\033[K")


class OneLineTest(unittest.TestCase):
    """A row is one line: a value with line breaks (Windows' \\r\\n among
    them) never sends the cursor back over the row's start."""

    def test_every_kind_of_line_break_becomes_the_mark(self):
        self.assertEqual(ui.one_line("way\r\nJust\rso\nend", "\\"), "way\\Just\\so\\end")
        self.assertEqual(ui.one_line("a\tb\x07c"), "a bc")
        self.assertEqual(ui.one_line("\x1b[1mbold\x1b[0m"), "\x1b[1mbold\x1b[0m")   # styling kept

    def test_a_table_cell_holds_no_carriage_return(self):
        from backbone.prompt import core
        for cell in ("way\r\nJust", [("way\r\n", 'primary'), "Just"], ("way\r\nJust", 'dim')):
            text = core._render_cell_segments(cell, 'normal', False, 40, 'left')
            self.assertNotIn("\r", text)
            self.assertNotIn("\n", text)


if __name__ == "__main__":
    unittest.main()


class ProgressBarSpanTest(unittest.TestCase):
    def test_span_is_accent_up_to_now_then_dim(self):
        bar = ui.get_progress_bar(0.5, 20, (0.4, 0.7))
        self.assertEqual(ui.strip_ansi(bar), "[" + "━" * 14 + "─" * 6 + "]")
        self.assertIn(f"{ui.Colors.ACCENT}━━{ui.Colors.RESET}", bar)       # cells 8-9: played
        self.assertIn(f"{ui.Colors.DIM}━━━━──", bar)                        # cells 10-13: to come, then the rest

    def test_cells_alone_take_a_rest_glyph(self):
        cells = ui.strip_ansi(ui.progress_cells(0.5, 10, (0.3, 0.8)))
        self.assertEqual(cells, "━" * 8 + "──")

    def test_whole_cells_only(self):
        self.assertEqual(ui.strip_ansi(ui.progress_cells(0.44, 10)), "━━━━──────")
        self.assertEqual(ui.strip_ansi(ui.progress_cells(0.46, 10)), "━━━━━─────")

    def test_a_boxed_bar_drops_its_caps(self):
        self.assertEqual(ui.strip_ansi(ui.get_progress_bar(0.5, 4, caps=False)), "━━──")

    def test_no_span_is_unchanged(self):
        self.assertNotIn(ui.Colors.ACCENT, ui.get_progress_bar(0.5, 20))


class StatusLineTest(unittest.TestCase):
    def test_a_message_sits_at_the_right_with_nothing_on_the_left(self):
        ui._toast_message = ""
        with patch.object(ui, 'get_terminal_width', return_value=80):
            self.assertEqual(ui.get_status_line(), "")
            ui.show_status("Volume: 80%")
            try:
                line = ui.strip_ansi(ui.get_status_line())
                self.assertTrue(line.rstrip().endswith("Volume: 80%") and line.startswith(" " * 40), line)
            finally:
                ui._toast_message = ""


class MarqueeTest(unittest.TestCase):
    def test_fits_holds_scrolls_and_starts_over(self):
        text = "abcdefghij"                       # 10 wide, shown in 6: 4 to scroll
        self.assertEqual(ui.marquee("short", 10, 99.0), "short")
        self.assertEqual(ui.marquee(text, 6, 0.0, speed=4), "abcdef")          # held at the start
        self.assertEqual(ui.marquee(text, 6, 2.5, speed=4), "cdefgh")          # 0.5 s in at 4 cols/s
        self.assertEqual(ui.marquee(text, 6, 3.5, speed=4), "efghij")          # at the end, held
        self.assertEqual(ui.marquee(text, 6, 5.0, speed=4), "abcdef")          # one cycle is 5 s

    def test_the_default_moves_a_column_a_beat(self):
        beat = 60 / ui.MARQUEE_BPM
        self.assertEqual(ui.marquee("abcdefghij", 6, 2.0 + 2 * beat + 0.01), "cdefgh")

    def test_the_speed_can_be_set(self):
        was = ui.MARQUEE_BPM
        self.addCleanup(ui.set_marquee_bpm, was)
        ui.set_marquee_bpm(120)                                                # half a second a column
        self.assertEqual(ui.MARQUEE_STEP_S, 0.5)
        self.assertEqual(ui.marquee("abcdefghij", 6, 2.0 + 1.01), "cdefgh")
