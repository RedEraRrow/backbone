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
        (d / "fakepkg").mkdir()
        (d / "fakepkg" / "fakedaemon.py").write_text("import time\ntime.sleep(30)\n")
        os.environ["PYTHONPATH"] = str(d)
        procs.spawn_module("fakepkg.fakedaemon", d / "fakedaemon.out")
        find = lambda name: procs.find_processes(name, launcher="fakepkg")
        for _ in range(50):
            if find("fakedaemon"):
                break
            time.sleep(0.1)
        found = find("fakedaemon")
        self.assertEqual(len(found), 1, found)
        self.assertEqual(find("someotherdaemon"), [])
        self.assertEqual(procs.find_processes("fakedaemon"), [], "only matched as the launcher's module")
        self.assertEqual(procs.stop_processes("fakedaemon", launcher="fakepkg"), 1)
        for _ in range(50):
            if not find("fakedaemon"):
                break
            time.sleep(0.1)
        self.assertEqual(find("fakedaemon"), [])


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
