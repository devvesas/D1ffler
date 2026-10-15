"""Release failure boundaries and artifact provenance; native tools are faked."""
import json
import platform
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from scripts import build_app as release
from scripts.update_licenses import copy_package_notices


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "requirements").mkdir()
        for name in ("build.lock.txt", "test.lock.txt"):
            (self.root / "requirements" / name).write_text("example==1.2.3\n", encoding="utf-8")
        (self.root / "dist").mkdir()
        self.old = self.root / "dist" / "D1ffler.exe"
        self.old.write_bytes(b"previous executable")
        for name in ("FIRST_RUN.txt", "CHANGELOG.md", "README.md"):
            (self.root / name).write_text(name, encoding="utf-8")
        for name in release.release_licensing.DOCUMENTS:
            (self.root / name).write_text("Example legal fixture: " + name, encoding="utf-8")
        (self.root / "D1ffler.py").write_text("print('source fixture')\n", encoding="utf-8")
        (self.root / "licensing").mkdir()
        (self.root / "licenses").mkdir()
        notice = self.root / "licenses" / "example.txt"
        notice.write_text("Example upstream license", encoding="utf-8")
        source_cache = self.root / "build" / "license-sources"
        source_cache.mkdir(parents=True)
        dependency_source = source_cache / "example.tar.gz"
        dependency_source.write_bytes(b"Example matching upstream source")
        inventory = {"application_license": "GPL-3.0-or-later", "python_version": platform.python_version(),
                     "packages": {"example": {"version": "1.2.3", "license_files": ["licenses/example.txt"]}},
                     "notice_files": {"licenses/example.txt": release.digest(notice)},
                     "sources": [{"component": "example", "version": "1.2.3", "filename": "example.tar.gz",
                                  "url": "https://example.invalid/source", "sha256": release.digest(dependency_source)}]}
        (self.root / "licensing" / "inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
        self.args = SimpleNamespace(release=False, skip_install=False, skip_smoke_tests=False)
        self.calls = []
        self.failure_stage = None
        self.omit = None
        self.addCleanup(patch.stopall)
        patch.object(release, "source_record", return_value={"revision": "abc123", "dirty": False,
                     "files_sha256": {"D1ffler.py": release.digest(self.root / "D1ffler.py")}}).start()

    def runner(self, command, log, **kwargs):
        self.calls.append((command, log))
        if log.name == self.failure_stage:
            raise RuntimeError("native command failed")
        log.write_text("ok\n", encoding="utf-8")
        if log.name == "dependencies.json":
            log.write_text(json.dumps({"example": "1.2.3"}), encoding="utf-8")
        elif log.name in ("D1ffler.exe.log", "D1ffler-cli.exe.log") and log.name != self.omit:
            dist = Path(command[command.index("--distpath") + 1])
            (dist / log.name[:-4]).write_bytes(b"current build")
            stem = Path(log.name[:-4]).stem
            work = Path(command[command.index("--workpath") + 1]) / stem
            work.mkdir(parents=True)
            binaries = [(f"PyQt6/Qt6/bin/{name}", "fixture", "BINARY") for name in release.release_licensing.QT_LIBRARIES]
            (work / "PKG-00.toc").write_text(repr(("fixture.pkg", {}, binaries)), encoding="utf-8")
        elif log.name == "packaged-tests.log":
            report = Path(command[command.index("--report") + 1])
            report.write_text(json.dumps({"status": "passed", "skipped_checks": ["external clean machine"]}), encoding="utf-8")
        elif log.name == "source-tests.log":
            path = Path(next(str(arg).split("=", 1)[1] for arg in command if str(arg).startswith("--junitxml=")))
            path.write_text('<testsuites><testsuite><testcase name="ok"/><testcase name="optional"><skipped message="not supported"/></testcase></testsuite></testsuites>', encoding="utf-8")

    def build(self):
        return release.build(self.args, self.root, self.runner)

    def test_failed_native_install_or_either_freezer_cannot_accept_old_executable(self):
        for stage in ("environment.log", "install.log", "dependency-check.log", "D1ffler.exe.log", "D1ffler-cli.exe.log", "packaged-tests.log"):
            with self.subTest(stage=stage):
                self.failure_stage = stage
                with self.assertRaisesRegex(RuntimeError, "native command failed"):
                    self.build()
                candidate = max((self.root / "dist" / "candidates").iterdir(), key=lambda path: path.stat().st_mtime_ns)
                self.assertEqual(json.loads((candidate / "release.json").read_text())["status"], "failed")
                self.assertEqual(list(candidate.glob("*.zip")), [])
                self.assertEqual(self.old.read_bytes(), b"previous executable")

    def test_native_success_without_current_output_is_a_failure(self):
        self.omit = "D1ffler.exe.log"
        with self.assertRaisesRegex(RuntimeError, "Current build did not produce"):
            self.build()
        self.assertEqual(self.old.read_bytes(), b"previous executable")

    def test_distinct_builds_manifest_checksums_zip_and_licensing_gate(self):
        first, second = self.build(), self.build()
        self.assertNotEqual(first, second)
        record = json.loads((second / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(record["version"], release.APP_VERSION)
        self.assertEqual(record["dependencies"], {"example": "1.2.3"})
        self.assertFalse(record["ready_for_publication"])
        self.assertEqual(record["licensing"]["status"], "verified_materials")
        source_archive = second / record["licensing"]["source_archive"]["filename"]
        self.assertEqual(release.digest(source_archive), record["licensing"]["source_archive"]["sha256"])
        with zipfile.ZipFile(source_archive) as source:
            self.assertEqual(source.read("D1ffler-source/D1ffler.py"), (self.root / "D1ffler.py").read_bytes())
            self.assertEqual(source.read("third-party-source/example.tar.gz"), b"Example matching upstream source")
        archive = next(second.glob("*-windows-x64.zip"))
        self.assertEqual((second / "SHA256SUMS.txt").read_text().split()[0], release.digest(archive))
        with zipfile.ZipFile(archive) as bundle:
            prefix = f"D1ffler-{release.APP_VERSION}-windows-x64/"
            self.assertEqual(bundle.read(prefix + "D1ffler.exe"), b"current build")
            self.assertIn(prefix + "USER_BUILD.txt", bundle.namelist())
            self.assertIn("signing", bundle.read(prefix + "USER_BUILD.txt").decode())
            for filename in release.release_licensing.DOCUMENTS:
                self.assertIn(prefix + filename, bundle.namelist())
            self.assertIn(prefix + "licenses/example.txt", bundle.namelist())
            hashes = bundle.read(prefix + "SHA256SUMS.txt").decode().splitlines()
            import hashlib
            for line in hashes:
                expected, filename = line.split("  ")
                self.assertEqual(expected, hashlib.sha256(bundle.read(prefix + filename)).hexdigest())

    def test_release_runs_source_suite_and_records_skips(self):
        self.args.release = True
        candidate = self.build()
        record = json.loads((candidate / "release.json").read_text())
        self.assertEqual(record["source_tests"]["status"], "passed")
        self.assertEqual(record["source_tests"]["skipped"][0]["reason"], "not supported")
        self.assertTrue(record["isolated_environment"])

    def test_source_access_bytes_are_preserved_in_companion(self):
        path = self.root / "SOURCE_ACCESS.md"
        path.write_bytes(b"Windows source instructions\r\nSecond line\r\n")
        candidate = self.build()
        record = json.loads((candidate / "release.json").read_text())
        with zipfile.ZipFile(candidate / record["licensing"]["source_archive"]["filename"]) as bundle:
            self.assertEqual(bundle.read("SOURCE_ACCESS.md"), path.read_bytes())

    def test_packaged_probes_use_a_unique_build_temp_directory(self):
        environments = []
        def runner(command, log, **kwargs):
            self.runner(command, log, **kwargs)
            if log.name == "packaged-tests.log":
                environments.append(kwargs["environment"])
        release.build(self.args, self.root, runner)
        release.build(self.args, self.root, runner)
        paths = [Path(e["TEMP"]) for e in environments]
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(p.is_relative_to(self.root / "build") and p.is_dir() for p in paths))
        self.assertTrue(all(e["TEMP"] == e["TMP"] == e["TMPDIR"] for e in environments))

    def test_interrupted_gui_probe_reports_before_terminating_and_preserves_failures(self):
        from d1ffler.packaged_verification import finish_probe
        path = self.root / "probe.json"
        for status, code in (("passed", 0), ("failed", 2)):
            report = {"status": status, "phase": "seed-recovery"}
            def stop(actual):
                self.assertEqual(actual, code)
                self.assertEqual(json.loads(path.read_text()), report)
                raise SystemExit(actual)
            with patch("d1ffler.packaged_verification.os._exit", side_effect=stop):
                with self.assertRaises(SystemExit):
                    finish_probe(path, report, interrupted=True)
            self.assertEqual(finish_probe(path, report), code)

    def test_release_cannot_skip_environment_or_packaged_verification(self):
        self.args.release = True
        for switch in ("skip_install", "skip_smoke_tests"):
            setattr(self.args, switch, True)
            with self.assertRaisesRegex(ValueError, "requires a fresh"):
                self.build()
            setattr(self.args, switch, False)

    def test_incorrect_lock_versions_fail_before_freezing(self):
        (self.root / "requirements" / "build.lock.txt").write_text("example==9.9.9\n")
        # Licensing validation now rejects drift before installing/freezing.
        (self.root / "requirements" / "test.lock.txt").write_text("example==9.9.9\n")
        with self.assertRaisesRegex(ValueError, "Dependency lock changed"):
            self.build()
        self.assertFalse(any(log.name == "D1ffler.exe.log" for _, log in self.calls))

    def test_command_wrapper_rejects_native_exit_code_and_preserves_log(self):
        log = self.root / "failure.log"
        with patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=42)):
            with self.assertRaisesRegex(RuntimeError, "failed \\(42\\)"):
                release.run(["not-a-real-command"], log, cwd=self.root)
        self.assertTrue(log.is_file())

    def test_version_resource_uses_single_version_and_prerelease_flag(self):
        with patch.object(release, "APP_VERSION", "2.3.4-rc.1"):
            text = release.version_resource("D1ffler.exe")
        self.assertIn("'ProductVersion', '2.3.4-rc.1'", text)
        self.assertIn("filevers=(2, 3, 4, 0)", text)
        self.assertIn("flags=2", text)
        with patch.object(release, "APP_VERSION", "2.3.4"):
            self.assertIn("flags=0", release.version_resource("D1ffler.exe"))
        with patch.object(release, "APP_VERSION", "../escape"):
            with self.assertRaises(ValueError):
                release.version_resource("D1ffler.exe")

    def test_missing_license_blocks_release_before_native_tools(self):
        (self.root / "LICENSE").unlink()
        with self.assertRaisesRegex(ValueError, "licensing document is missing"):
            self.build()
        self.assertEqual(self.calls, [])

    def test_notice_drift_requires_a_review(self):
        notice = self.root / "licenses" / "example.txt"
        notice.write_text("changed notice")
        with self.assertRaisesRegex(ValueError, "License texts changed"):
            self.build()

    def test_project_artwork_changes_do_not_require_an_inventory_refresh(self):
        (self.root / "icons").mkdir()
        icon = self.root / "icons" / "new.svg"
        icon.write_text("new artwork")
        self.build()
        icon.write_text("updated artwork")
        self.build()
        icon.unlink()
        self.build()

    def test_corrupt_corresponding_source_blocks_the_release(self):
        (self.root / "build" / "license-sources" / "example.tar.gz").write_bytes(b"wrong source")
        with self.assertRaisesRegex(ValueError, "source checksum mismatch"):
            self.build()
        self.assertEqual(self.calls, [])

    def test_source_change_during_freeze_cannot_produce_a_binary_zip(self):
        original = self.runner
        def runner(command, log, **kwargs):
            original(command, log, **kwargs)
            if log.name == "D1ffler-cli.exe.log":
                (self.root / "D1ffler.py").write_text("changed source")
        with self.assertRaisesRegex(ValueError, "Source changed during the build"):
            release.build(self.args, self.root, runner)
        self.assertFalse(list((self.root / "dist" / "candidates").rglob("*.zip")))

    def test_unreviewed_qt_module_blocks_release(self):
        original = self.runner
        def runner(command, log, **kwargs):
            original(command, log, **kwargs)
            if log.name == "D1ffler.exe.log":
                path = Path(command[command.index("--workpath") + 1]) / "D1ffler" / "PKG-00.toc"
                import ast
                data = ast.literal_eval(path.read_text())
                data[2].append(("PyQt6/Qt6/bin/Qt6New.dll", "fixture", "BINARY"))
                path.write_text(repr(data))
        with self.assertRaisesRegex(ValueError, "Frozen Qt modules changed"):
            release.build(self.args, self.root, runner)

    def test_vendored_notice_basenames_do_not_overwrite_package_license(self):
        installed = self.root / "installed"
        originals = {"example-1.2.3.dist-info/licenses/LICENSE": b"Main package terms",
                     "example/_vendor/other-2.0.dist-info/LICENSE": b"Different vendored terms"}
        for name, content in originals.items():
            path = installed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        distribution = SimpleNamespace(files=list(originals), locate_file=lambda name: installed / name)
        notices = copy_package_notices(self.root, "example", distribution)
        self.assertEqual(len(set(notices)), 2)
        self.assertEqual((self.root / "licenses/packages/example/LICENSE").read_bytes(), b"Main package terms")
        self.assertEqual((self.root / "licenses/packages/example/example/_vendor/other-2.0.dist-info/LICENSE").read_bytes(), b"Different vendored terms")
