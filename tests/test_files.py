"""The one file-name rule every tool uses."""
import unittest

from backbone.files import safe_filename


class SafeFilenameTest(unittest.TestCase):
    def test_colons_and_slashes_become_dashes(self):
        self.assertEqual(safe_filename("Verve Jazz Masters 6: Ella Fitzgerald"), "Verve Jazz Masters 6 - Ella Fitzgerald")
        self.assertEqual(safe_filename("D:Votion"), "D-Votion")
        self.assertEqual(safe_filename("AC/DC"), "AC-DC")

    def test_what_windows_refuses_goes_and_the_name_is_never_empty(self):
        self.assertEqual(safe_filename('Who? <me> "now"|*'), "Who me now")
        self.assertEqual(safe_filename("Ends in dots..  "), "Ends in dots")
        self.assertEqual(safe_filename(".hidden"), "hidden")                     # never a hidden file
        self.assertEqual(safe_filename("???"), "untitled")
        self.assertEqual(safe_filename("abcdef", 3), "abc")


if __name__ == "__main__":
    unittest.main()
