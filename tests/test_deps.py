"""backbone.deps: what's found or missing, install hints for the platform
it's on, and a clear exit (not a traceback) when something required is
missing."""
import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from backbone import deps
from backbone.deps import Dep

HINTS = {"macos": "brew install thing", "debian": "sudo apt install thing", "linux": "get thing",
         "other": "https://thing.example"}


class DepsTest(unittest.TestCase):
    def test_found_missing_and_a_failing_probe(self):
        def boom():
            raise OSError("no")
        rows = deps.check([Dep("a", lambda: "/bin/a", "x"), Dep("b", lambda: None, "y"),
                           Dep("c", boom, "z")])
        self.assertEqual([found for _d, found in rows], ["/bin/a", None, None])

    def test_hint_for_each_platform(self):
        d = Dep("thing", lambda: None, "x", hints=HINTS)
        for key, want in (("macos", "brew install thing"), ("debian", "sudo apt install thing"),
                          ("fedora", "get thing"), ("windows", "https://thing.example")):
            with patch.object(deps, "platform_key", lambda k=key: k):
                self.assertEqual(deps.hint(d), want, key)

    def test_require_exits_with_the_hint_only_for_required(self):
        optional = Dep("opt", lambda: None, "extras", hints=HINTS)
        deps.require([optional], "tool")                       # nothing required missing
        needed = Dep("VLC", lambda: None, "playing audio", required=True, hints=HINTS)
        err = io.StringIO()
        with patch.object(deps, "platform_key", lambda: "macos"), redirect_stderr(err), \
             self.assertRaises(SystemExit) as stop:
            deps.require([needed, optional], "tool")
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("VLC (for playing audio): brew install thing", err.getvalue())
        self.assertNotIn("opt", err.getvalue())
        self.assertIn("`tool doctor`", err.getvalue())


if __name__ == "__main__":
    unittest.main()
