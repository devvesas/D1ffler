import subprocess
import tempfile
import unittest
from pathlib import Path

from d1ffler.diff_tool import compare_folders, folder_files


class GitIgnoreFolderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def put(root: Path, relative: str, content: str = "data") -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_standalone_folder_uses_nested_rules_and_negations(self):
        folder = self.root / "standalone"
        folder.mkdir()
        self.put(folder, ".gitignore", "build/\n*.log\n!important.log\n")
        self.put(folder, "build/nested/output.bin")
        self.put(folder, "debug.log")
        self.put(folder, "important.log")
        self.put(folder, "nested/.gitignore", "*.tmp\n!keep.tmp\n")
        self.put(folder, "nested/drop.tmp")
        self.put(folder, "nested/keep.tmp")
        self.put(folder, "plain.txt")

        all_files, _ = folder_files(folder, [])
        filtered, _ = folder_files(folder, [], ignore_gitignore=True)
        self.assertIn("build/nested/output.bin", all_files)
        self.assertEqual(set(filtered), {
            ".gitignore", "important.log", "nested/.gitignore",
            "nested/keep.tmp", "plain.txt",
        })

    def test_tracked_file_matching_gitignore_is_excluded(self):
        folder = self.root / "repo"
        folder.mkdir()
        for args in (("init", "-q"), ("config", "user.name", "D1ffler Test"),
                     ("config", "user.email", "D1ffler@example.invalid")):
            subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)
        self.put(folder, "tracked.log")
        subprocess.run(["git", "-C", str(folder), "add", "tracked.log"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(folder), "commit", "-qm", "tracked"], check=True, capture_output=True)
        self.put(folder, ".gitignore", "*.log\n")
        filtered, _ = folder_files(folder, [".git"], ignore_gitignore=True)
        self.assertNotIn("tracked.log", filtered)

    def test_subfolder_source_honors_parent_gitignore(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        self.put(repo, ".gitignore", "sub/cache/\n")
        self.put(repo, "sub/cache/result.bin")
        self.put(repo, "sub/visible.txt")
        filtered, _ = folder_files(repo / "sub", [], ignore_gitignore=True)
        self.assertEqual(set(filtered), {"visible.txt"})

    def test_ignored_files_do_not_appear_in_comparison(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        self.put(left, ".gitignore", "cache/\n")
        self.put(right, ".gitignore", "cache/\n")
        self.put(left, "cache/a.bin")
        self.put(right, "cache/b.bin")
        self.put(left, "shared.txt", "left")
        self.put(right, "shared.txt", "right")
        results = compare_folders(left, right, ignore_gitignore=True)
        self.assertEqual([(item.relative_path, item.status) for item in results], [
            (".gitignore", "identical"), ("shared.txt", "modified"),
        ])


if __name__ == "__main__":
    unittest.main()
