"""End-to-end checks of the windowless comparison command."""

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import d1ffler.diff_tool as diff_tool
from d1ffler.headless_compare import run_compare


PROJECT = Path(__file__).resolve().parents[1]


class HeadlessCompareTests(unittest.TestCase):
    script = "D1ffler.py"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def compare(self, *arguments):
        environment = dict(os.environ, QT_QPA_PLATFORM="not_a_qt_platform")
        return subprocess.run([sys.executable, str(PROJECT / self.script), "compare",
                               *(str(value) for value in arguments)], cwd=PROJECT,
                              env=environment, capture_output=True, text=True,
                              encoding="utf-8", timeout=15)

    def test_file_exit_codes_and_unified_diff_without_qt_window(self):
        left, right = self.root / "left.txt", self.root / "right.txt"
        left.write_text("one\n", encoding="utf-8")
        right.write_text("one\n", encoding="utf-8")
        same = self.compare(left, right)
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertEqual(json.loads(same.stdout)["status"], "identical")
        right.write_text("two\n", encoding="utf-8")
        changed = self.compare(left, right, "--format", "diff")
        self.assertEqual(changed.returncode, 1, changed.stderr)
        self.assertIn("-one", changed.stdout)
        self.assertIn("+two", changed.stdout)
        missing = self.compare(left, self.root / "missing.txt")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")

    def test_folder_rules_csv_and_atomic_error(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        (left / "same.txt").write_text("a b\n", encoding="utf-8")
        (right / "same.txt").write_text("a  b\n", encoding="utf-8")
        (right / "only.txt").write_text("new\n", encoding="utf-8")
        result = self.compare(left, right, "--ignore-whitespace", "--progress")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["complete"])
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["rules"]["ignore_whitespace"])
        self.assertEqual({row["relative_path"]: row["statuses"][1] for row in report["paths"]},
                         {"only.txt": "added", "same.txt": "ignored"})
        self.assertIn("Compared", result.stderr)
        csv_result = self.compare(left, right, "--format", "csv")
        self.assertEqual(csv_result.returncode, 1, csv_result.stderr)
        self.assertIn("file,only.txt", csv_result.stdout)
        destination = self.root / "report.json"
        destination.write_text("previous report", encoding="utf-8")
        failed = self.compare(left, right, "--text-rules", self.root / "missing.json",
                              "--output", destination)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(destination.read_text(encoding="utf-8"), "previous report")

    def test_output_inside_source_and_invalid_options_fail(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        self.assertEqual(self.compare(left, right, "--output", left / "result.json").returncode, 2)
        self.assertFalse((left / "result.json").exists())
        self.assertEqual(self.compare(left, right, "--format", "diff").returncode, 2)

    def test_text_rule_scope_is_applied_per_file(self):
        left, right = self.root / "left.txt", self.root / "right.txt"
        left.write_text("version 1\n", encoding="utf-8")
        right.write_text("version 2\n", encoding="utf-8")
        rules = self.root / "rules.json"
        rules.write_text(json.dumps([{"name": "Version", "scope": "left.txt",
                                     "pattern": "[0-9]", "mode": "match", "enabled": True}]),
                         encoding="utf-8")
        result = self.compare(left, right, "--text-rules", rules)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "modified")

    def test_help_and_usage_streams(self):
        help_result = self.compare("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(f"{self.script} compare", help_result.stdout)
        self.assertEqual(help_result.stderr, "")
        usage = self.compare()
        self.assertEqual(usage.returncode, 2)
        self.assertEqual(usage.stdout, "")
        self.assertIn("usage:", usage.stderr)

    def test_closed_report_pipe_returns_two(self):
        left, right = self.root / "left.txt", self.root / "right.txt"
        left.write_text("same\n", encoding="utf-8")
        right.write_bytes(left.read_bytes())
        with subprocess.Popen([sys.executable, str(PROJECT / self.script), "compare", str(left), str(right)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            process.stdout.close()
            process.stdout = None
            _output, errors = process.communicate(timeout=15)
            diagnostics = errors.decode("utf-8")
            self.assertEqual(process.returncode, 2, diagnostics)
        self.assertIn("Comparison failed:", diagnostics)

    def test_unicode_reports_and_successful_atomic_replacement(self):
        left, right = self.root / "left café.txt", self.root / "right 日本語.txt"
        left.write_text("café 日本語\n", encoding="utf-8")
        right.write_text("changed 日本語\n", encoding="utf-8")
        diff = self.compare(left, right, "--format", "diff")
        self.assertEqual(diff.returncode, 1, diff.stderr)
        self.assertIn("-café 日本語", diff.stdout)
        self.assertIn("+changed 日本語", diff.stdout)
        destination = self.root / "résultat 日本語.json"
        destination.write_text("old report", encoding="utf-8")
        result = self.compare(left, right, "--output", destination)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("right 日本語.txt", json.loads(destination.read_text(encoding="utf-8"))["sources"][1]["path"])


class ConsoleEntryPointTests(HeadlessCompareTests):
    script = "D1ffler_cli.py"

    def test_root_help_and_unknown_command_do_not_launch_qt(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="not_a_qt_platform")
        for arguments, code in ((["--help"], 0), ([], 2), (["unknown"], 2)):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(PROJECT / self.script), *arguments],
                                        env=environment, capture_output=True, text=True,
                                        encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn("D1ffler_cli.py", result.stdout if code == 0 else result.stderr)


class UnavailableStreamTests(unittest.TestCase):
    def test_unexpected_folder_failure_is_logged_and_never_publishes_early_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left, right = root / "left", root / "right"
            left.mkdir()
            right.mkdir()
            destination = root / "report.json"
            destination.write_text("old report", encoding="utf-8")
            def fail_after_success(worker):
                worker.messages.put(("finished", False, ""))
                raise RuntimeError("PRIVATE_CONTENTS")
            diagnostics, output = io.StringIO(), io.StringIO()
            with patch("d1ffler.comparison_workers.FolderComparisonWorker.run", fail_after_success), \
                    patch("d1ffler.app_settings.user_state_directory", return_value=root / "state"), \
                    patch("sys.stderr", diagnostics), patch("sys.stdout", output):
                code = run_compare([str(left), str(right), "--output", str(destination)], diff_tool)
            self.assertEqual(code, 2)
            self.assertIn("diagnostic", diagnostics.getvalue())
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(destination.read_text(encoding="utf-8"), "old report")
            log = (root / "state" / "diagnostics" / "diagnostics.jsonl").read_text(encoding="utf-8")
            self.assertIn("Headless folder comparison", log)
            self.assertNotIn("PRIVATE_CONTENTS", log)

    def test_windowed_streams_return_failure_instead_of_crashing_or_discarding_report(self):
        for arguments in (["left.txt", "right.txt"], ["--help"], [],
                          ["left.txt", "right.txt", "--output", "report.json"]):
            with self.subTest(arguments=arguments), patch("sys.stdout", None), patch("sys.stderr", None):
                self.assertEqual(run_compare(arguments, None), 2)

    def test_missing_stdout_diagnoses_failure_before_comparison(self):
        for arguments in (["left.txt", "right.txt"], ["--help"]):
            diagnostics = io.StringIO()
            with self.subTest(arguments=arguments), patch("sys.stdout", None), patch("sys.stderr", diagnostics):
                self.assertEqual(run_compare(arguments, None), 2)
            self.assertIn("D1ffler-cli.exe", diagnostics.getvalue())

    def test_explicit_output_works_without_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left, right, output = root / "left.txt", root / "right.txt", root / "report.json"
            left.write_text("same\n", encoding="utf-8")
            right.write_bytes(left.read_bytes())
            diagnostics = io.StringIO()
            with patch("sys.stdout", None), patch("sys.stderr", diagnostics):
                self.assertEqual(run_compare([str(left), str(right), "--output", str(output)], diff_tool), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "identical")
            self.assertEqual(diagnostics.getvalue(), "")

    def test_buffered_output_failure_returns_two(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "same.txt"
            source.write_text("same\n", encoding="utf-8")
            stream, diagnostics = io.StringIO(), io.StringIO()
            with patch.object(stream, "flush", side_effect=OSError("report destination failed")):
                with patch("sys.stdout", stream), patch("sys.stderr", diagnostics):
                    self.assertEqual(run_compare([str(source), str(source)], diff_tool), 2)
            self.assertIn("report destination failed", diagnostics.getvalue())

    def test_closed_stream_returns_failure(self):
        stream = io.StringIO()
        stream.close()
        with patch("sys.stdout", stream), patch("sys.stderr", stream):
            self.assertEqual(run_compare(["--help"], None), 2)


if __name__ == "__main__":
    unittest.main()
