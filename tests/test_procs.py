"""Background processes: started, found however they were launched, stopped;
plus the timestamped log line and a send-nothing ntfy."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from backbone import files, notify, procs


class ProcsTest(unittest.TestCase):
    def test_start_find_stop(self):
        d = Path(tempfile.mkdtemp())
        script = d / "fakedaemon.py"
        script.write_text("import time\ntime.sleep(30)\n")
        procs.spawn_script(script, d / "fakedaemon.out")
        for _ in range(50):
            if procs.find_processes("fakedaemon"):
                break
            time.sleep(0.1)
        found = procs.find_processes("fakedaemon")
        self.assertEqual(len(found), 1, found)
        self.assertEqual(procs.find_processes("someotherdaemon"), [])
        self.assertEqual(procs.stop_processes("fakedaemon"), 1)
        for _ in range(50):
            if not procs.find_processes("fakedaemon"):
                break
            time.sleep(0.1)
        self.assertEqual(procs.find_processes("fakedaemon"), [])


class FilesTest(unittest.TestCase):
    def test_log_line_prints_and_appends(self):
        path = Path(tempfile.mkdtemp()) / "run.log"
        files.log_line(path, "OK  disc 1")
        files.log_line(path, "OK  disc 2")
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("  OK  disc 2"))

    def test_count_entries(self):
        d = tempfile.mkdtemp()
        self.assertEqual(files.count_entries(os.path.join(d, "missing")), 0)
        open(os.path.join(d, "a"), "w").close()
        self.assertEqual(files.count_entries(d), 1)


class NtfyTest(unittest.TestCase):
    def test_no_topic_sends_nothing(self):
        notify.ntfy("https://ntfy.invalid", "", "title", "message")   # returns at once, no thread


if __name__ == "__main__":
    unittest.main()
