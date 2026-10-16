"""Mouse reporting is the app screen's switch: on while it's up, off when it
goes, and a widget finishing on it leaves it on for the screen underneath."""
import io
import sys
import unittest
from unittest.mock import patch

from backbone import ui
from backbone.prompt import chrome


class MouseTest(unittest.TestCase):
    def test_the_app_screen_owns_it(self):
        out = io.StringIO()
        with patch.object(sys, 'stdout', out), patch.object(sys, '__stdout__', out), \
             patch.object(ui, '_paint_terminal', lambda: None):
            ui.enter_alt_screen()
            self.assertIn(ui.MOUSE_ON, out.getvalue())
            out.truncate(0); out.seek(0)
            chrome.disable_mouse()                  # a widget on its way out
            self.assertNotIn(ui.MOUSE_OFF, out.getvalue())
            ui.exit_alt_screen()
            self.assertIn(ui.MOUSE_OFF, out.getvalue())

    def test_a_widget_on_its_own_turns_it_off(self):
        out = io.StringIO()
        with patch.object(sys, 'stdout', out):
            chrome.disable_mouse()
        self.assertIn(ui.MOUSE_OFF, out.getvalue())


if __name__ == '__main__':
    unittest.main()
