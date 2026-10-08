"""The keymap (backbone.keys): defaults and the user's bindings, which action a
key is on a screen, conflicts, keys.json kept and survived when broken, and
hint labels whose clicks replay the bound key whatever it looks like."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backbone import keys
from backbone.prompt import core as pc


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.file = Path(self.dir.name) / "keys.json"
        self._p = patch.object(keys, "_path", lambda: self.file)
        self._p.start()
        keys._saved.update(map=None, mtime=None, checked=0.0)
        keys.define("t_player", "Test player", [
            ("next", ("]",), "next track"), ("prev", ("[",), "previous track"),
            ("back", ("b", "B", "ESC"), "back"), ("up", ("UP",), "up"), ("down", ("DOWN",), "down")])
        keys.define("t_lyrics", "Test lyrics", [("plus1", ("]",), "+1s")])

    def tearDown(self):
        self._p.stop()
        keys._saved.update(map=None, mtime=None, checked=0.0)
        for scope in ("t_player", "t_lyrics"):
            for a in keys.actions(scope):
                del keys._actions[a.id]
        self.dir.cleanup()

    def test_defaults_and_rebinding(self):
        self.assertEqual(keys.action("]", "t_player"), "t_player.next")
        keys.bind("t_player.next", ("n",))
        self.assertEqual(keys.action("n", "t_player"), "t_player.next")
        self.assertIsNone(keys.action("]", "t_player"))
        self.assertEqual(keys.action("]", "t_lyrics"), "t_lyrics.plus1")   # other scope untouched
        self.assertTrue(keys.changed("t_player.next"))
        keys.reset("t_player.next")
        self.assertEqual(keys.of("t_player.next"), ("]",))
        self.assertFalse(self.file.exists() and json.loads(self.file.read_text()))

    def test_global_keys_are_found_after_the_screens_own(self):
        self.assertEqual(keys.action(keys.of("global.playpause")[0], "t_player"), "global.playpause")

    def test_conflicts_cover_the_scope_and_global_but_not_other_screens(self):
        self.assertEqual(keys.conflicts("t_player.next", "["), ["t_player.prev"])
        self.assertEqual(keys.conflicts("t_player.next", keys.of("global.next")[0]), ["global.next"])
        self.assertEqual(keys.conflicts("t_lyrics.plus1", "["), [])

    def test_a_broken_file_means_the_defaults_and_unknown_entries_are_kept(self):
        self.file.write_text("{not json")
        self.assertEqual(keys.of("t_player.next"), ("]",))
        self.file.write_text(json.dumps({"gone.action": ["x"], "t_player.next": "n"}))
        keys._saved.update(map=None, checked=0.0)
        self.assertEqual(keys.of("t_player.next"), ("]",))          # not a list: ignored
        keys.bind("t_player.prev", ("p",))
        self.assertEqual(json.loads(self.file.read_text()), {"gone.action": ["x"], "t_player.prev": ["p"]})

    def test_an_action_can_be_left_unbound_and_its_hint_drops_out(self):
        keys.bind("t_player.next", ())
        self.assertIsNone(keys.action("]", "t_player"))
        with patch.object(pc, "hints_visible", lambda: True):
            bar = pc.ui.strip_ansi(pc._hint((keys.label("t_player.next"), "next"), ("q", "quit")))
        self.assertNotIn("next", bar)
        self.assertIn("quit", bar)

    def test_labels_and_the_keys_a_click_replays(self):
        self.assertEqual(keys.label("t_player.prev", "t_player.next"), "[/]")
        self.assertEqual(keys.label("t_player.back"), "b/esc")              # b/B is one key to the reader
        self.assertEqual(keys.label("t_player.up", "t_player.down"), "↑↓")
        keys.bind("t_player.next", ("/",))
        keys.bind("t_player.prev", ("\x1f",))                             # ^/: crashed the old parser
        hint = keys.label("t_player.prev", "t_player.next")
        self.assertEqual(hint, "^///")                                # ^/, the separator, /
        self.assertEqual([t[2] for t in pc._hint_key_tokens(hint)], ["\x1f", "/"])
        cells: dict = {}
        pc.add_hint_click_cells(cells, f"[{hint}] prev/next", 3, [(hint, "prev/next")])
        self.assertEqual(sorted(set(cells.values())), sorted(["\x1f", "/"]))

    def test_fixed_and_event_keys_cant_be_bound(self):
        self.assertFalse(keys.bindable("CTRL_C"))
        self.assertFalse(keys.bindable("MOUSE_CLICK:0:3:4"))
        self.assertTrue(keys.bindable("n"))


if __name__ == "__main__":
    unittest.main()
