"""Boxed panels: the box itself, and select() drawing its list inside one."""
import io
import time
import re
import sys
import unittest
from unittest.mock import patch

from backbone import prompt, ui
from backbone.prompt import core, lists


def _select(cols, rows, keys, **kw):
    """Drive select() at a window size; returns (result, last frame as plain text)."""
    shots = []

    class W:
        row = 1

        def __init__(self, fd): pass
        def render(self, lines): shots.append([ui.strip_ansi(l) for l in lines])
        def clear(self): pass
        def anchor_reset(self): pass
    feed = iter(keys)
    real, sys.stdout = sys.stdout, io.StringIO()
    try:
        with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
             patch.object(ui, 'get_terminal_width', lambda *a: cols), \
             patch.object(ui, 'get_terminal_height', lambda *a: rows), \
             patch.object(lists, '_read_key', lambda fd: next(feed)), \
             patch.object(lists, '_wait_for_keypress', lambda t: True), \
             patch.object(lists, '_set_raw'), patch.object(lists, '_restore_term_attrs'), \
             patch.object(lists, '_get_term_attrs'), patch.object(lists, '_Widget', W), \
             patch.object(lists.sys.stdin, 'fileno', lambda: 0):
            result = prompt.select("", **kw)
    finally:
        sys.stdout = real
    return result, shots[-1]


class BoxLinesTest(unittest.TestCase):
    def test_every_line_is_the_box_width(self):
        lines = core.box_lines(["short", "x" * 200], 40, 6, "Title", "[?] help")
        self.assertEqual(len(lines), 6)
        self.assertEqual({ui.visual_len(ui.strip_ansi(l)) for l in lines}, {40 + ui.MARGIN_H})
        plain = [ui.strip_ansi(l) for l in lines]
        self.assertTrue(plain[0].lstrip().startswith("╭─ Title ") and plain[0].endswith("─ [?] help ─╮"))
        self.assertTrue(plain[-1].lstrip().startswith("╰") and plain[-1].endswith("╯"))

    def test_a_title_too_long_goes_whole_never_cut(self):
        top = ui.strip_ansi(core.box_lines([], 30, 2, "A title far too long for this narrow box")[0])
        self.assertNotIn("…", top)
        self.assertNotIn("A title", top)
        self.assertEqual(ui.visual_len(top), 30 + ui.MARGIN_H)
        top = ui.strip_ansi(core.box_lines([], 18, 2, "Queue · 12 of 300")[0])
        self.assertIn(" Queue ─", top)                                       # its name kept, the rest whole or gone
        self.assertNotIn("12", top)


class BoxedSelectTest(unittest.TestCase):
    CHOICES = [prompt.Choice(title=f"Row {i}", value=i) for i in range(1, 6)]

    def test_the_title_is_in_the_border_and_the_box_fills_down_to_the_hints(self):
        _res, frame = _select(60, 20, ['ESC'], choices=self.CHOICES, header=prompt.PanelTitle("Albums", "Sub"))
        self.assertTrue(frame[0].lstrip().startswith("╭─ Albums"))
        self.assertIn("─ Sub ─", frame[0])                                  # the subtitle in the border
        self.assertTrue(frame[-1].lstrip().startswith("╰"))
        # No hints or now-playing box: it reaches the row above the status bar's margin.
        self.assertEqual(len(frame), 20 - 1 - 2 * ui.MARGIN_V)

    def test_a_click_lands_on_the_row_under_it(self):
        _res, frame = _select(60, 20, ['ESC'], choices=self.CHOICES, header=prompt.PanelTitle("Albums"))
        row = next(n for n, line in enumerate(frame) if "Row 3" in line)
        col = frame[row].index("Row 3") + 2
        # render() lays line j at screen row 1 + MARGIN_V + j; two clicks on a row choose it.
        click = f"MOUSE_CLICK:0:{1 + ui.MARGIN_V + row}:{col}"
        res, _ = _select(60, 20, [click, click], choices=self.CHOICES, header=prompt.PanelTitle("Albums"))
        self.assertEqual(res, 3)

    def test_a_cut_off_highlighted_row_scrolls(self):
        long = prompt.Choice(title="A row whose title is much too long for this narrow list", value=1)
        rest = prompt.Choice(title="Another row whose title is much too long for it", value=2)
        frames = []
        for t in (0.0, 4.0):
            with patch.object(lists.time, 'monotonic', lambda: t):
                frames.append(_select(44, 20, ['ESC'], choices=[long, rest])[1])
        k = next(n for n, l in enumerate(frames[0]) if "A row whose" in l)  # the highlighted row
        first = [f[k] for f in frames]
        self.assertNotEqual(first[0], first[1])                       # it moved
        self.assertTrue(all("…" in next(l for l in f if "Another" in l) for f in frames))  # the others don't

    def test_narrow_windows_stay_boxed(self):
        _res, frame = _select(14, 20, ['ESC'], choices=self.CHOICES, header=prompt.PanelTitle("Albums"))
        self.assertTrue(any("╭" in line for line in frame))
        self.assertTrue(any("│" in line and "Row" in line for line in frame))   # its rows inside it

    def test_too_small_for_a_box(self):
        _res, frame = _select(40, 9, ['ESC'], choices=self.CHOICES, header=prompt.PanelTitle("Albums"))
        self.assertFalse(any("╭" in line for line in frame))                  # short: the box gives way first
        self.assertIn("Albums", frame[0])
        self.assertFalse(any("─" in line for line in frame))                  # a title line, no rule under it


class PastTheBottomTest(unittest.TestCase):
    def test_a_row_below_the_window_is_never_written(self):
        core.screen_invalidate()
        with patch.object(ui, 'get_terminal_size', lambda *a: (40, 10)), \
             patch.object(ui, 'get_terminal_height', lambda *a: 10):
            self.assertEqual(core.screen_row_paint(11, ""), "")         # it would land on row 10
            self.assertEqual(core.screen_span_paint(12, 1, "x"), "")
            self.assertIn("kept", core.screen_row_paint(10, "kept"))
        core.screen_invalidate()


class ShortWindowLiveSelectTest(unittest.TestCase):
    def test_typing_in_a_short_window_doesnt_crash(self):
        for cols, rows in ((30, 8), (44, 10), (100, 7), (20, 6)):
            feed = iter(['a', 'd', 'ESC'])
            W = type("W", (lists._Widget,), {"render": lambda self, lines: None})
            real = sys.stdout
            sys.stdout = io.StringIO()
            try:
                with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
                     patch.object(ui, 'get_terminal_width', lambda *a: cols), \
                     patch.object(ui, 'get_terminal_height', lambda *a: rows), \
                     patch.object(lists, '_read_key', lambda fd: next(feed)), \
                     patch.object(lists, '_wait_for_keypress', lambda t: True), \
                     patch.object(lists, '_set_raw'), patch.object(lists, '_restore_term_attrs'), \
                     patch.object(lists, '_get_term_attrs'), patch.object(lists, '_Widget', W), \
                     patch.object(lists.sys.stdin, 'fileno', lambda: 0):
                    prompt.live_select("", lambda q: [prompt.Choice(title=f"Hit {i} {q}", value=i)
                                                      for i in range(9)],
                                       header=prompt.PanelTitle("Search", "sub"))
            finally:
                sys.stdout = real


class ScrollColumnTest(unittest.TestCase):
    def test_the_marked_column_scrolls_and_the_number_holds_still(self):
        cols = [prompt.Column(style='static-dim', align='right', gap=0),
                prompt.Column(style='primary', scroll=True, max_frac=0.4)]
        long_title = "A very long title that will not fit in the room it gets " * 2
        choices = [prompt.Choice(title="x", value=1, cells=["12", long_title])]
        frames = []
        for t in (0.0, 9.0):
            with patch.object(lists.time, 'monotonic', lambda t=t: t):
                frames.append(_select(60, 20, ['ESC'], choices=choices, columns=cols)[1])
        row = lambda f: next(ui.strip_ansi(l) for l in f if "12" in ui.strip_ansi(l))      # noqa: E731
        a, b = row(frames[0]), row(frames[1])
        self.assertNotEqual(a, b)                                          # the title moved
        self.assertEqual(a.index("12"), b.index("12"))                     # the number didn't


class BorderSqueezeTest(unittest.TestCase):
    def test_the_title_and_the_toggle_outlast_the_subtitle_and_nothing_is_cut(self):
        right = core.border_right("A long subtitle here", True)
        for width in (80, 40, 30, 28):
            top = ui.strip_ansi(core.box_lines([], width, 3, "Atom Heart", right)[0])
            self.assertEqual(ui.visual_len(top), width + ui.MARGIN_H, width)     # never past its box
            self.assertTrue(top.rstrip().endswith("╮"), top)
            self.assertIn("[?] help", top, width)                                # the toggle, whole
            self.assertIn(" Atom Heart ", top, width)                            # the title, whole
            self.assertNotIn("…", top, width)                                    # nothing cut short
        narrow = ui.strip_ansi(core.box_lines([], 20, 3, "Atom Heart", right)[0])
        self.assertIn(" Atom Heart ", narrow)                                    # the toggle goes before the title
        self.assertNotIn("help", narrow)
        self.assertIn("A long subtitle here", ui.strip_ansi(core.box_lines([], 80, 3, "T", right)[0]))


class ColumnBrowserTest(unittest.TestCase):
    CHOICES = [prompt.Choice(title=f"Row {i}", value=i) for i in range(1, 6)]
    TRAIL = [prompt.Trail(["Artists", "Albums"], ["artists", "albums"], "albums", 0),
             prompt.Trail([f"Album {i}" for i in range(9)], list(range(9)), 4, 1)]

    def setUp(self):
        pass

    def _frame(self, cols, keys=('ESC',), trail=TRAIL, rows=30):
        seen = []

        def preview(value):
            seen.append(value)
            return prompt.Preview(lambda w, h: [f"P{value}"], "Holds", [f"C{value}"], want=12)
        with patch.object(core, '_columns_on', [True]):
            res, frame = _select(cols, rows, list(keys), choices=self.CHOICES, header=prompt.PanelTitle("Main"),
                                 trail=trail, preview=preview)
        return res, frame, seen

    def test_the_edge_between_the_boxes_moves_out_from_under_the_tab(self):
        def edge(frame):
            top = next(line for line in frame if "╭" in line and "Main" in line)
            return top.index("╮")                                              # the browser box's right corner
        here = edge(self._frame(120)[1])
        for notch in ((here - 3, here + 2), (here - 1, here + 6)):
            with patch.object(ui, 'tab_notch', lambda n=notch: n):
                moved = edge(self._frame(120)[1])
            self.assertTrue(moved <= notch[0] - 2 or moved >= notch[1], (notch, moved))   # clear of it
            self.assertLessEqual(abs(moved - here), 10)                                # only a nudge

    def test_the_levels_above_start_at_the_top_whether_or_not_the_list_has_scrolled(self):
        def rows_of(frame, *words):
            return [next(k for k, line in enumerate(frame) if w in line) for w in words]
        _res, frame, _seen = self._frame(160)
        a, b = rows_of(frame, "Artists", "Row 1")
        self.assertEqual(a, b)                                             # not scrolled: no "above" row to clear
        many = [prompt.Choice(title=f"Row {i}", value=i) for i in range(1, 60)]
        with patch.object(self, 'CHOICES', many):
            _res, frame, _seen = self._frame(160, keys=('END', 'ESC'), rows=20)
        above = next(k for k, line in enumerate(frame) if "above" in line)
        self.assertEqual(rows_of(frame, "Artists")[0], above)              # scrolled: still at the top, not moved down by "N above"

    def test_widths_follow_content(self):
        pv = prompt.Preview(lambda w, h: [], want=10)
        shown, pw, strip = core.column_widths(60, self.TRAIL, pv)
        self.assertEqual((pw, strip), (0, True))                           # too narrow beside: a strip
        shown, pw, strip = core.column_widths(196, self.TRAIL, pv)
        self.assertEqual(len(shown), 2)
        self.assertTrue(pw and not strip)
        self.assertEqual(shown[1][1], 10)                                  # "Album 8" and its room
        self.assertGreater(core.column_widths(160, self.TRAIL, pv, want=40, list_want=100)[1],
                           core.column_widths(160, self.TRAIL, pv, list_want=100)[1])

    def test_spare_room_is_shared_and_levels_narrow_before_going(self):
        pv = prompt.Preview(lambda w, h: [], want=10)
        _shown, pw, _strip = core.column_widths(200, self.TRAIL, pv, list_want=20)
        self.assertEqual(pw, 200 * core._COL_PREVIEW[1] // 100)            # a short list: the preview its share
        self.assertGreater(core.column_widths(200, self.TRAIL, pv, list_want=150)[1], core._COL_PREVIEW[0])
        shown, pw, strip = core.column_widths(50, self.TRAIL, pv, list_want=10)
        self.assertTrue(strip and shown)                                    # the preview under; a level kept
        self.assertEqual(shown[-1][1], core._COL_TRAIL[0]) if len(shown) > 1 else None

    def test_the_strip_takes_what_the_list_leaves(self):
        self.assertEqual(core.strip_rows(30, 5, 40), 30 - 9)                # a short list: the rest
        self.assertEqual(core.strip_rows(30, 100, 40), 10)                  # a long one: a third
        self.assertEqual(core.strip_rows(30, 5, 6), 6)                      # no more than it can use
        self.assertEqual(core.strip_rows(8, 100, 40), 0)                    # the list keeps its least

    def test_the_preview_holds_its_size_through_the_list(self):
        def preview(value):                                                 # rows that want different sizes
            return prompt.Preview(lambda w, h: [f"P{value}"], "Holds", ["x"] * value, want=8 * value)
        shapes = []
        for keys in (['ESC'], ['DOWN', 'DOWN', 'ESC'], ['DOWN', 'DOWN', 'DOWN', 'DOWN', 'ESC']):
            with patch.object(core, '_columns_on', [True]):
                _res, frame = _select(200, 30, keys, choices=self.CHOICES, header=prompt.PanelTitle("Main"),
                                      trail=self.TRAIL, preview=preview)
            top = next(line for line in frame if "╭" in line)
            holds = next(n for n, line in enumerate(frame) if "Holds" in line)
            shapes.append((top.rindex("╭"), holds))
        self.assertEqual(len(set(shapes)), 1)                              # the widest row's, for all

    def test_the_levels_share_a_box_the_preview_has_two(self):
        _res, frame, seen = self._frame(200, ['DOWN', 'ESC'])
        top = next(line for line in frame if "╭" in line)
        self.assertEqual(top.count("╭"), 2)                                 # the browser's box, the details'
        self.assertTrue(any("Holds" in line and "╭" in line for line in frame[1:]))   # the contents' box
        col = lambda text: next(line.index(text) for line in frame if text in line)
        self.assertLess(col("Albums"), col("Album 4"))
        self.assertLess(col("Album 4"), col("Row 3"))
        self.assertLess(top.index("╮"), col("P2"))
        self.assertEqual(seen[-1], 2)
        self.assertTrue(all(ui.visual_len(line) <= 200 for line in frame))

    def test_narrow_puts_the_details_in_a_strip_under_the_list(self):
        _res, frame, _seen = self._frame(50)
        strip = next(n for n, line in enumerate(frame) if "P1" in line)
        self.assertGreater(strip, next(n for n, line in enumerate(frame) if "Row 5" in line))
        self.assertFalse(any("C1" in line for line in frame))               # no room for what it holds

    def test_clicks(self):
        _res, frame, _seen = self._frame(200)
        row = next(n for n, line in enumerate(frame) if "Row 3" in line)
        click = lambda c: f"MOUSE_CLICK:0:{1 + ui.top_margin() + row}:{c}"
        on_row = click(frame[row].index("Row 3") + 2)
        self.assertEqual(self._frame(200, [on_row, on_row])[0], 3)
        album = next(n for n, line in enumerate(frame) if "Album 2" in line)
        res = self._frame(200, [f"MOUSE_CLICK:0:{1 + ui.top_margin() + album}:{frame[album].index('Album 2') + 2}"])[0]
        self.assertEqual(res, prompt.JumpTo(1, 2))                          # back to that level, at it
        prow = next(n for n, line in enumerate(frame) if "P1" in line)
        res = self._frame(200, [f"MOUSE_CLICK:0:{1 + ui.top_margin() + prow}:{frame[prow].index('P1') + 1}",
                                'ESC'])[0]
        self.assertIsNone(res)                                              # the preview does nothing

    def test_v_turns_it_off(self):
        with patch.object(lists, "set_columns_shown", lambda on: core._columns_on.__setitem__(0, on)):
            _res, frame, _seen = self._frame(200, ['v', 'ESC'])
        self.assertFalse(any("Album 4" in line or "P1" in line for line in frame))


class PictureTest(unittest.TestCase):
    """A picture over a widget's frame is drawn when the rows under it are, and
    only then: a repainted row wipes it, an untouched one keeps it."""

    def _render(self, w, lines, pictures):
        w.pictures = pictures
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            with patch.object(ui, 'get_terminal_size', lambda *a: (80, 24)), \
                 patch.object(ui, 'get_terminal_height', lambda *a: 24), \
                 patch.object(ui, 'get_terminal_width', lambda *a: 80):
                w.render(lines)
        finally:
            sys.stdout = real
        return buf.getvalue().count("PIC")

    def test_drawn_when_cells_under_it_are(self):
        core.screen_invalidate()
        w = core._Widget(0)
        lines = ["a.........", "b.........", "c.........", "d........."]
        pic = [(3, 5, 2, "k", "PIC", 4)]                   # rows 3-4 (lines 1-2), columns 5-8
        self.assertEqual(self._render(w, lines, pic), 1)
        self.assertEqual(self._render(w, lines, pic), 0)   # nothing repainted: still there
        lines[1] = "B........."                              # its row, but left of it
        self.assertEqual(self._render(w, lines, pic), 0)
        lines[1] = "B.....X..."                              # a cell under it
        self.assertEqual(self._render(w, lines, pic), 1)
        self.assertEqual(self._render(w, lines, [(3, 5, 2, "new", "PIC", 4)]), 1)   # a new picture


class CellPaintTest(unittest.TestCase):
    """A changed row writes only the cells that changed."""

    def tearDown(self):
        core.screen_release()

    def test_reserved_cells_are_left_alone(self):
        core.screen_invalidate()
        core.screen_reserve(9, 9, 5, 8)
        out = core.screen_row_paint(9, "abcdefghij")                 # unknown row: no wipe
        self.assertNotIn("\033[2K", out)
        self.assertNotIn("efgh", ui.strip_ansi(out))
        core.screen_span_paint(9, 5, "LYRC")                          # the owner draws them
        out = core.screen_row_paint(9, "abcdXXXXij")                  # the row changes around them
        self.assertEqual(out, "")                                     # only reserved cells differ: nothing
        self.assertEqual("".join(ch for _st, ch in core._cells[9][4:8]), "LYRC")

    def test_an_overlay_is_put_back_as_it_was(self):
        core.screen_invalidate()
        core.screen_row_paint(9, f"ab{ui.Colors.DIM}cdef{ui.Colors.RESET}gh")
        before = list(core._cells[9])
        saved = core.screen_save(9, 9, 3, 6)
        core.screen_span_paint(9, 3, "XXXX")                          # the overlay
        core._shown_pictures[:] = [(9, 4, 1, "k", "PIC", 2)]          # an image under it
        out = core.screen_restore(saved)
        self.assertEqual(core._cells[9], before)                      # every cell, style and all
        self.assertIn("PIC", out)                                     # and the image drawn again
        core._shown_pictures.clear()

    def test_a_span_writes_only_what_changed(self):
        core.screen_invalidate()
        core.screen_row_paint(9, "0123456789")
        core.screen_span_paint(9, 4, "abc")
        out = core.screen_span_paint(9, 4, "abX")
        self.assertIn("\033[9;6H", out)
        self.assertNotIn("ab", ui.strip_ansi(out))
        self.assertEqual("".join(ch for _st, ch in core._cells[9]), "012abX6789")


    def test_only_the_changed_cells(self):
        core.screen_invalidate()
        core.screen_row_paint(9, "hello world")
        out = core.screen_row_paint(9, "hello there")
        self.assertIn("\033[9;7H", out)                   # from the first changed column
        self.assertNotIn("hello", out)
        self.assertNotIn("\033[2K", out)                   # the row isn't wiped

    def test_styles_wide_characters_and_a_shorter_row(self):
        core.screen_invalidate()
        core.screen_row_paint(9, f"ab{ui.Colors.DIM}cd{ui.Colors.RESET}⏸xy")
        out = core.screen_row_paint(9, f"ab{ui.Colors.DIM}cX{ui.Colors.RESET}⏸")
        self.assertIn(ui.Colors.DIM + "X", out)             # the changed cell keeps its style
        self.assertIn("\033[K", out)                       # the end that went is erased
        self.assertEqual(core._cells[9][4], (ui.Colors.RESET and "", "⏸"))
        self.assertEqual(core._cells[9][5], ("", ""))      # the wide character's second cell


class FloatTest(unittest.TestCase):
    """A box floating over the screen for a moment (the volume as it changes):
    the screen paints on round it, and is put back under it after."""

    def setUp(self):
        core.screen_invalidate()
        self.size = patch.object(ui, 'get_terminal_size', lambda *a: (40, 10))
        self.size.start()

    def tearDown(self):
        core._float.clear()
        self.size.stop()
        core.screen_invalidate()

    def test_progress_in_the_app_floats_and_goes_when_cleared(self):
        core.screen_row_paint(5, "x" * 40)
        out = io.StringIO()
        with patch.object(ui, '_on_app_screen', lambda: True), patch.object(sys, 'stdout', out):
            ui.print_inline_progress("Measuring 1/3: one.mp3", 0.0)
            first = dict(core._float)
            ui.print_inline_progress("Measuring 2/3: a much longer name.mp3", 0.5)
            self.assertEqual((core._float['top'], core._float['w']), (first['top'], first['w']))   # holds still
            self.assertNotIn("\r", out.getvalue())                      # no line wherever the cursor was
            ui.clear_inline_progress()
        self.assertFalse(core._float)
        self.assertIn("x" * 10, ui.strip_ansi(out.getvalue()))           # what it covered, back
        out = io.StringIO()
        with patch.object(ui, '_on_app_screen', lambda: False), patch.object(sys, 'stdout', out):
            ui.print_inline_progress("Measuring", 0.5)                   # the command line: the inline bar
        self.assertTrue(out.getvalue().startswith("\r"))
        self.assertFalse(core._float)

    def test_a_pulse_takes_it_away_on_time_too(self):
        """With music playing, state-change pulses come faster than the idle
        tick: each one must do the tick's work, or the box never goes."""
        import os as _os
        out = io.StringIO()
        with patch.object(sys, 'stdout', out):
            core.screen_float(["[ VOL ]"], 60)
        core._float['until'] = 0                                            # its time is up
        r, w = _os.pipe()
        _os.write(w, b"x")
        try:
            with patch.object(core, '_wake_r', r), patch.object(core, '_footer_last_draw', [time.time()]), \
                 patch.object(core, '_render_footer_bar', lambda: None), \
                 patch.object(core._sel, 'select', lambda *a: ([r], [], [])), patch.object(sys, 'stdout', out):
                core._wait_for_keypress(0)
        finally:
            _os.close(r); _os.close(w)
        self.assertFalse(core._float)                                       # gone, though no idle tick was due

    def test_rows_painted_under_it_leave_it_be_and_come_back_after(self):
        row = "x" * 40
        core.screen_row_paint(5, row)
        out = io.StringIO()
        with patch.object(sys, 'stdout', out):
            core.screen_float(["[ VOL ]"], 60)
        top, left, w = core._float['top'], core._float['left'], core._float['w']
        self.assertEqual(top, 5)
        painted = core.screen_row_paint(5, "y" * 40)                     # the screen goes on under it
        cols = [int(m) for m in re.findall(r'\x1b\[5;(\d+)H', painted)]
        self.assertTrue(cols and all(c < left or c >= left + w for c in cols))   # nothing under the box
        self.assertEqual(ui.strip_ansi(painted).count("y"), 40 - w)      # every cell but the box's
        core._float['until'] = 0
        out = io.StringIO()
        with patch.object(sys, 'stdout', out):
            self.assertTrue(core.float_tick())                          # its time up: gone
        self.assertIn("y" * w, ui.strip_ansi(out.getvalue()))           # what's under it now, put back
        self.assertFalse(core.float_tick())

    def test_showing_again_replaces_it(self):
        with patch.object(sys, 'stdout', io.StringIO()):
            core.screen_float(["one"], 60)
            core.screen_float(["two two"], 60)
        self.assertEqual(core._float['lines'], ["two two"])



class PathBoxTest(unittest.TestCase):
    def test_a_path_breaks_after_its_slashes(self):
        lines = core.wrap_path("/Users/me/Music/Album/01 Song.mp3", 16)
        self.assertEqual("".join(lines), "/Users/me/Music/Album/01 Song.mp3")
        self.assertTrue(all(len(l) <= 16 for l in lines))
        self.assertEqual(lines[0], "/Users/me/Music/")                       # broken at a slash
        self.assertEqual(core.wrap_path("/" + "a" * 12, 5), ["/", "aaaaa", "aaaaa", "aa"])   # a long name, cut



class OverPictureTest(unittest.TestCase):
    """Something drawn over a picture writes every cell it covers: an image
    shows through any cell left unwritten, blank or not."""

    def setUp(self):
        core.screen_invalidate()
        self.size = patch.object(ui, 'get_terminal_size', lambda *a: (40, 10))
        self.size.start()
        core.screen_row_paint(1, "")                                        # the size settled: no resize wipe after

    def tearDown(self):
        core._shown_pictures.clear()
        core.screen_picture_area('test', None)
        self.size.stop()
        core.screen_invalidate()

    def _blank_cells_written(self, row):
        core.screen_row_paint(row, " " * 40)                               # blank where the image is
        out = core.screen_span_paint(row, 5, "│" + " " * 10 + "│")          # a box's blank inside over it
        return ui.strip_ansi(re.sub(r'\x1b\[\d+;\d+H', '|', out))

    def test_a_listed_picture(self):
        core._shown_pictures[:] = [(3, 8, 4, 'k', '', 20)]                  # rows 3-6, columns 8-27
        self.assertEqual(self._blank_cells_written(4).count(" "), 8)        # the blanks over it (8-15) too
        self.assertEqual(self._blank_cells_written(8).count(" "), 0)        # a row clear of it: only changes

    def test_a_picture_another_screen_drew(self):
        core.screen_picture_area('test', (2, 1, 5, 40))
        self.assertEqual(self._blank_cells_written(3).count(" "), 10)
        core.screen_picture_area('test', None)
        self.assertEqual(self._blank_cells_written(3).count(" "), 0)



class CrampedTest(unittest.TestCase):
    """A window too small for a screen's boxes gives way to the miniplayer:
    the screen's frames are held back, and the miniplayer runs till they fit."""

    def setUp(self):
        self.ran = []
        core.set_cramped_view(lambda: self.ran.append(core._cramped_showing[0]))

    def tearDown(self):
        core.set_cramped_view(None)

    def test_too_small_runs_the_miniplayer_and_holds_frames_back(self):
        with patch.object(core, 'box_fits', lambda: False), patch.object(ui, 'request_relayout') as relayout:
            self.assertTrue(core._cramped())
            self.assertFalse(core._wait_for_keypress(0))
            self.assertEqual(self.ran, [1])                                  # it ran, flagged as showing
            relayout.assert_called_once()                                    # and the screen lays out again after
        self.assertEqual(self.ran, [1])

    def test_only_the_screen_on_top_decides(self):
        player = core.screen_backdrop(None, small=True)                       # lays out small windows itself
        try:
            with patch.object(core, 'box_fits', lambda: False):
                self.assertFalse(core._cramped())
                opened = core.screen_backdrop(None)                           # a screen it opens: like any other
                self.assertTrue(core._cramped())
                layer = core.overlay_layer()                                  # a box over that: any size
                self.assertFalse(core._cramped())
                layer()
                opened()
                self.assertFalse(core._cramped())                             # back to the player's say
        finally:
            player()
        with patch.object(core, 'box_fits', lambda: True):
            self.assertFalse(core._cramped())                                # boxes fit: nothing to do

    def test_an_overlay_redraws_the_screen_under_it(self):
        drawn = []
        screen = core.screen_backdrop(lambda: drawn.append('screen'))
        layer = core.overlay_layer()
        try:
            core.redraw_under_overlay()
        finally:
            layer()
            screen()
        self.assertEqual(drawn, ['screen'])

    def test_with_no_miniplayer_a_notice_stands_in(self):
        core.set_cramped_view(None)                                          # a tool without one (backcrack)
        shown = []
        with patch.object(core, 'box_fits', lambda: False), \
             patch.object(core, '_too_small_notice', lambda: shown.append(True)), \
             patch.object(ui, 'request_relayout'):
            self.assertTrue(core._cramped())                                 # still no screen without its boxes
            core._wait_for_keypress(0)
        self.assertEqual(shown, [True])

    def test_the_notice_is_boxed_or_nothing(self):
        for cols, rows, boxed in ((40, 5, True), (12, 5, False), (40, 2, False)):
            out = io.StringIO()
            fits = iter([False, True])
            with patch.object(core, 'box_fits', lambda: next(fits)), \
                 patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
                 patch.object(core, '_byte_ready', lambda *a: False), \
                 patch.object(core.sys.stdin, 'fileno', lambda: 0), patch.object(sys, 'stdout', out):
                core._too_small_notice()
            text = ui.strip_ansi(out.getvalue())
            self.assertEqual("Make the window bigger" in text, boxed, (cols, rows))
            self.assertEqual("╭" in text, boxed, (cols, rows))                # the words only ever in their box



class OverlayCutTest(unittest.TestCase):
    """An overlay never leaves half a box: where it would cut a box's border
    it has the window to itself."""

    def setUp(self):
        core.screen_invalidate()
        self.size = patch.object(ui, 'get_terminal_size', lambda *a: (40, 12))
        self.size.start()
        core.screen_row_paint(1, "")

    def tearDown(self):
        self.size.stop()
        core.screen_invalidate()

    def test_a_border_inside_the_rectangle_is_a_cut(self):
        core.screen_row_paint(4, "  ╭──────────╮")
        core.screen_row_paint(5, "  │ inside   │")
        self.assertTrue(core.screen_crosses_box(3, 6, 1, 20))              # over the border
        self.assertFalse(core.screen_crosses_box(5, 5, 5, 10))             # within the box's inside
        self.assertFalse(core.screen_crosses_box(8, 9, 1, 20))             # clear of it


class HelpToggleRoomTest(unittest.TestCase):
    def test_a_border_with_no_room_for_the_toggle_keeps_its_corner(self):
        out = [core.box_lines([], 20, 3, "Browse", core.border_right(None, True))[0]]
        cells: dict = {}
        with patch.object(core, 'help_toggle_shown', lambda: True):
            core.place_help_toggle(out, 1, cells, help_key=True)
        top = ui.strip_ansi(out[0])
        self.assertTrue(top.rstrip().endswith("╮"), top)                     # not clipped for a toggle
        self.assertNotIn("help", top)
        self.assertTrue(cells.get('__help_key__'))                           # `?` still toggles


if __name__ == "__main__":
    unittest.main()
