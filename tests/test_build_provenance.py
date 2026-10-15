"""Unversioned source snapshots can be built without repository metadata."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_app


class BuildProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = {"D1ffler.py": b"source\r\n", "src/d1ffler/module.py": b"package source",
                       ".gitignore": b"build/\ndist/\n"}
        for name, content in self.inputs.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for name in ("build/work.spec", "dist/app.exe", "src/d1ffler/__pycache__/module.pyc",
                     ".git/config", "tests/__pycache__/test.pyc"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"generated")

    def test_snapshot_hashes_sources_without_git_and_excludes_generated_files(self):
        for error in (FileNotFoundError("git"), subprocess.CalledProcessError(128, "git")):
            with self.subTest(error=error), patch.object(build_app.subprocess, "run", side_effect=error):
                record = build_app.source_record(self.root)
            self.assertIsNone(record["revision"])
            self.assertTrue(record["dirty"])
            self.assertEqual(record["files_sha256"],
                             {name: build_app.digest(self.root / name) for name in self.inputs})

    def test_parent_repository_is_not_mistaken_for_snapshot_provenance(self):
        with patch.object(build_app.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=str(self.root.parent))):
            record = build_app.source_record(self.root)
        self.assertIsNone(record["revision"])
        self.assertEqual(set(record["files_sha256"]), set(self.inputs))


if __name__ == "__main__":
    unittest.main()
