"""The polling key read the player uses: real terminal bytes in, the same key
names every other screen gets out."""
import os
import pty
import sys
import termios
import tty
import unittest
from unittest.mock import patch

from backbone import terminal_input as ti


class NonBlockingKeyTest(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave, termios.TCSANOW)   # raw_mode's drain would wait on the pty forever
        self.stdin = os.fdopen(self.slave, 'rb', buffering=0)
        self._p = patch.object(sys, 'stdin', self.stdin)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.stdin.close()
        os.close(self.master)

    def keys(self, raw: bytes) -> list:
        os.write(self.master, raw)
        out = []
        while (k := ti.get_key_non_blocking()) is not None:
            out.append(k)
        return out

    def test_nothing_waiting(self):
        self.assertEqual(self.keys(b''), [])

    def test_named_keys(self):
        self.assertEqual(self.keys(b'\x1b[C \x1b'), ['RIGHT', 'SPACE', 'ESC'])   # a lone Esc
        self.assertEqual(self.keys(b'p['), ['p', '['])

    def test_mouse_and_transport(self):
        self.assertEqual(self.keys(b'\x1b[<0;12;5M\x1b[<0;12;5m\x10'),
                         ['MOUSE_CLICK:0:5:12', 'MOUSE_RELEASE:0:5:12', '\x10'])


class WheelMomentumTest(unittest.TestCase):
    """A fast scroll coasts on and slows to a stop; a slow one doesn't; a
    click stops it dead and does nothing else."""

    def setUp(self):
        from backbone.prompt import core
        self.core, self.now = core, [1000.0]
        core._coast_stop()
        core._wheel.update(dir=0, last=0.0, rate=0.0, swallow=False)
        self._p = patch.object(core.time, 'monotonic', lambda: self.now[0])
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def notches(self, n, gap, key='SCROLL_DOWN'):
        for _ in range(n):
            self.now[0] += gap
            self.assertEqual(self.core._wheel_seen(key), key)

    def coast(self, seconds=5.0, tick=0.01):
        """When each coasting step came, over `seconds`."""
        at = []
        for _ in range(int(seconds / tick)):
            self.now[0] += tick
            if self.core._coast_step():
                at.append(self.now[0])
        return at

    def test_a_slow_scroll_does_not_coast(self):
        self.notches(5, 0.2)
        self.assertEqual(self.coast(), [])

    def test_a_fast_scroll_coasts_slowing_down_and_a_faster_one_further(self):
        self.notches(10, 0.02)              # 50 notches a second
        steps = self.coast()
        self.assertGreater(len(steps), 5)
        gaps = [b - a for a, b in zip(steps, steps[1:])]
        self.assertLess(gaps[0], gaps[-1])  # slowing down
        self.notches(10, 0.01, 'SCROLL_UP') # 100 a second, the other way
        self.assertGreater(len(self.coast()), len(steps))

    def test_a_click_stops_it_and_is_swallowed(self):
        self.notches(10, 0.02)
        self.assertTrue(self.coast(0.1))
        self.assertIsNone(self.core._wheel_seen('MOUSE_CLICK:0:5:12'))
        self.assertIsNone(self.core._wheel_seen('MOUSE_RELEASE:0:5:12'))
        self.assertEqual(self.coast(), [])
        self.now[0] += 1
        self.assertEqual(self.core._wheel_seen('MOUSE_CLICK:0:5:12'), 'MOUSE_CLICK:0:5:12')

    def test_no_coast_where_the_wheel_sets_a_level(self):
        self.core.wheel_steady(True)
        try:
            self.notches(10, 0.02)
            self.assertEqual(self.coast(), [])
        finally:
            self.core.wheel_steady(False)

    def test_a_key_stops_it(self):
        self.notches(10, 0.02)
        self.assertEqual(self.core._wheel_seen('DOWN'), 'DOWN')
        self.assertEqual(self.coast(), [])


if __name__ == "__main__":
    unittest.main()
