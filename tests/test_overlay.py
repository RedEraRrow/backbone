"""Boxes over the middle of the screen (the : command line, the panel
picker): over the screen as it is, at any size, and they say what they're for."""
import importlib
import io
import sys
import unittest
from unittest.mock import patch

from backbone import prompt, ui

tx = importlib.import_module("backbone.prompt.text")


def _run(cols, rows, keys, fresh=True, raw=False, **kw):
    """overlay_text at cols × rows fed `keys`: (what it returned, what it
    painted, plain unless `raw`). `fresh`: over a blank screen, else over
    what's been painted."""
    feed = iter(keys)
    with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
         patch.object(tx, '_read_key', lambda fd: next(feed)):
        if fresh:
            from backbone.prompt import core
            core.screen_invalidate()
        got, out = _run_sized(keys, **kw)
    return got, out if raw else ui.strip_ansi(out)


def _run_sized(keys, **kw):
    """overlay_text at whatever size is patched in, keys as patched in."""
    out = io.StringIO()
    with patch.object(tx, '_wait_for_keypress', lambda t: True), \
         patch.object(tx, '_set_raw'), patch.object(tx, '_restore_term_attrs'), \
         patch.object(tx, '_get_term_attrs'), \
         patch.object(tx.sys.stdin, 'fileno', lambda: 0), patch.object(sys, 'stdout', out):
        got = prompt.overlay_text("Command, as after backtrack", **kw)
    return got, out.getvalue()


class OverlayTest(unittest.TestCase):
    def test_a_resize_lays_the_screen_under_it_out_again(self):
        from backbone.prompt import core
        drawn = []
        size = [(100, 30)]
        drop = core.screen_backdrop(lambda: drawn.append(size[0]))
        keys = iter(['x', 'ENTER'])

        def key(fd):
            size[0] = (90, 30)                                                # the window resized meanwhile
            return next(keys)
        try:
            with patch.object(ui, 'get_terminal_size', lambda *a: size[0]), patch.object(tx, '_read_key', key):
                got = _run_sized(['x', 'ENTER'])
        finally:
            drop()
        self.assertEqual(got[0], "x")
        self.assertEqual(drawn, [(90, 30)])                                   # the screen's own redraw, once
        self.assertNotIn("\033[2J", got[1])                                  # and never a clear

    def test_with_nothing_to_redraw_the_screen_is_asked_to_after(self):
        from backbone.prompt import core
        size = [(100, 30)]
        drop = core.screen_backdrop(None)                                     # a screen that redraws itself
        keys = iter(['x', 'ENTER'])

        def key(fd):
            size[0] = (90, 30)
            return next(keys)
        try:
            with patch.object(ui, 'get_terminal_size', lambda *a: size[0]), patch.object(tx, '_read_key', key), \
                 patch.object(ui, 'request_relayout') as asked:
                _run_sized(['x', 'ENTER'])
        finally:
            drop()
        asked.assert_called_once()

    def test_the_screen_under_it_stays(self):
        from backbone.prompt import core
        drawn = []
        drop = core.screen_backdrop(lambda: drawn.append(True))
        try:
            with patch.object(ui, 'get_terminal_size', lambda *a: (100, 30)):
                core.screen_invalidate()
                for r in range(12, 20):                                       # a box's border right under it
                    core.screen_row_paint(r, "─" * 100)
            got, shown = _run(100, 30, ['l', 's', 'ENTER'], fresh=False, raw=True)
        finally:
            drop()
        self.assertEqual(got, "ls")
        self.assertNotIn("\033[2J", shown)                                   # never cleared
        self.assertEqual(drawn, [])                                           # nor drawn again: its cells put back
        self.assertFalse(core._layers)
        self.assertIn("─" * 20, ui.strip_ansi(shown))                         # the border, back where the box was

    def test_the_panel_picker_over_a_screen_leaves_it_be(self):
        from backbone.prompt import core
        drop = core.screen_backdrop(lambda: self.fail("drawn again"))
        rows = [prompt.Choice(title=t, value=t) for t in ("Lyrics", "People", "Queue")]
        feed, out = iter(['DOWN', 'SPACE', 'ESC']), io.StringIO()
        try:
            with patch.object(ui, 'get_terminal_size', lambda *a: (100, 30)), \
                 patch.object(tx, '_read_key', lambda fd: next(feed)), \
                 patch.object(tx, '_wait_for_keypress', lambda t: True), \
                 patch.object(tx, '_set_raw'), patch.object(tx, '_restore_term_attrs'), \
                 patch.object(tx, '_get_term_attrs'), \
                 patch.object(tx.sys.stdin, 'fileno', lambda: 0), patch.object(sys, 'stdout', out):
                core.screen_invalidate()
                for r in range(1, 31):
                    core.screen_row_paint(r, "│" + "x" * 98 + "│")
                ticked, _picked = prompt.overlay_checklist("Panels", rows, {"Lyrics"},
                                                           [prompt.Choice(title="Arrange panels…", value='arrange')])
        finally:
            drop()
        self.assertEqual(ticked, {"Lyrics", "People"})
        self.assertNotIn("\033[2J", out.getvalue())
        self.assertFalse(core._layers)
        self.assertEqual(core._backdrops, [])                                 # nothing left registered
        tail = out.getvalue().rsplit("Panels", 1)[1]                          # after the box's last frame
        self.assertIn("x" * 20, ui.strip_ansi(tail))                          # the screen's own cells, back

    def test_a_screen_that_cant_show_at_this_size_gives_the_box_the_window(self):
        from backbone.prompt import core
        drawn = []
        drop = core.screen_backdrop(lambda: drawn.append(True))               # keeps its boxes or nothing
        try:
            with patch.object(ui, 'request_relayout') as asked:
                _got, _shown = _run(30, 4, ['ESC'])                           # too small for its boxes
        finally:
            drop()
        self.assertEqual(drawn, [])                                           # not drawn without its boxes
        asked.assert_called_once()                                            # it lays itself out once it can
        small = []
        drop = core.screen_backdrop(lambda: small.append(True), small=True)   # lays out any size itself
        try:
            _got, shown = _run(30, 4, ['ESC'], raw=True)
        finally:
            drop()
        self.assertNotIn("\033[2J", shown)                                   # the miniplayer stays under it
        self.assertEqual(small, [])

    def test_any_size_gets_a_field(self):
        for cols, rows in ((100, 30), (30, 8), (12, 5), (10, 2), (6, 1)):
            got, _shown = _run(cols, rows, ['l', 's', 'ENTER'])
            self.assertEqual(got, "ls", (cols, rows))

    def test_a_box_only_where_it_fits(self):
        self.enterContext(patch.object(sys, 'stdout', io.StringIO()))      # a small window wipes the screen under it
        with patch.object(ui, 'get_terminal_size', lambda *a: (100, 30)):
            self.assertEqual(tx._overlay_place(3, 90)['h'], 3)
        with patch.object(ui, 'get_terminal_size', lambda *a: (40, 2)):
            place = tx._overlay_place(3, 90)
            self.assertEqual((place['h'], place['w']), (1, 40))                 # one row, edge to edge
        with patch.object(ui, 'get_terminal_size', lambda *a: (30, 8)):
            self.assertLessEqual(tx._overlay_place(3, 90)['w'], 30)             # never past the window

    def test_the_keys_give_way_before_the_title(self):
        _got, wide = _run(100, 30, ['ESC'])
        self.assertIn("↵ run", wide)
        _got, narrow = _run(48, 5, ["ESC"])
        self.assertIn("Command, as after backtrack", narrow)                 # whole
        self.assertNotIn("↵", narrow)

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
