"""Comparison masks leave source bytes intact and preserve meaningful changes."""

import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from d1ffler.diff_tool import DiffApp, FolderComparisonWorker, _files_match_cancellable
from d1ffler.text_rules import RuleError, normalize_line, validate_rule


class TextRuleTests(unittest.TestCase):
    def setUp(self):
        self.rule = validate_rule({"name": "Timestamp", "scope": "*.log",
                                   "pattern": r"time=\d+", "mode": "match", "enabled": True})

    def test_match_scope_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            left, right = Path(directory) / "a.log", Path(directory) / "b.log"
            left.write_text("time=123 level=INFO\n", encoding="utf-8")
            right.write_text("time=456 level=INFO\n", encoding="utf-8")
            self.assertEqual(_files_match_cancellable(left, right, False, False,
                                                      lambda: False, [self.rule]), "ignored")
            self.assertEqual(right.read_text(encoding="utf-8"), "time=456 level=INFO\n")
            right.write_text("time=456 level=ERROR\n", encoding="utf-8")
            self.assertEqual(_files_match_cancellable(left, right, False, False,
                                                      lambda: False, [self.rule]), "modified")
            self.assertEqual(_files_match_cancellable(left, right, False, False,
                                                      lambda: False), "modified")

    def test_line_mode_and_bounded_pattern(self):
        rule = validate_rule({"name": "Generated", "pattern": "^generated", "mode": "line"})
        self.assertEqual(normalize_line("generated 123", [rule])[0],
                         normalize_line("generated 456", [rule])[0])
        with self.assertRaises(RuleError):
            validate_rule({"name": "Empty", "pattern": ".*", "mode": "match"})

    def test_malformed_saved_rule_reports_rule_error(self):
        for rule in ({"name": "Broken", "pattern": "(", "mode": "match"},
                     {"name": "Missing", "mode": "match"}):
            with self.subTest(rule=rule), self.assertRaises(RuleError):
                normalize_line("sample", [rule])

    def test_text_alignment_marks_ignored_without_hiding_meaningful_edit(self):
        before = [["time=123 level=INFO"] , ["time=456 level=INFO"]]
        keys = [[normalize_line(line, [self.rule])[0] for line in lines] for lines in before]
        _rows, statuses, _pairs = DiffApp._build_aligned_rows(before, keys)
        self.assertEqual(statuses, [["ignored"], ["ignored"]])
        before[1][0] = "time=456 level=ERROR"
        keys[1][0] = normalize_line(before[1][0], [self.rule])[0]
        _rows, statuses, _pairs = DiffApp._build_aligned_rows(before, keys)
        self.assertEqual(statuses, [["modified"], ["modified"]])

    def test_folder_worker_reports_ignored_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            left, right = Path(directory) / "left", Path(directory) / "right"
            left.mkdir()
            right.mkdir()
            for root, stamp in ((left, "123"), (right, "456")):
                (root / "a.log").write_text(f"time={stamp}\n", encoding="utf-8")
                (root / "b.txt").write_text(f"time={stamp}\n", encoding="utf-8")
            messages = queue.Queue()
            worker = FolderComparisonWorker([left, right], [], False, False, False, {},
                                            threading.Event(), messages, text_rules=[self.rule])
            worker.run()
            rows = {relative: statuses for message, *payload in list(messages.queue)
                    if message == "batch" for relative, statuses in payload[0] if statuses is not None}
            self.assertEqual(rows["a.log"][1], "ignored")
            self.assertEqual(rows["b.txt"][1], "modified")


if __name__ == "__main__":
    unittest.main()
