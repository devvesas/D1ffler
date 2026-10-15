import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import d1ffler.git_worktrees as git_worktrees

from d1ffler.git_worktrees import (
    GitError, PreviewChangedError, changed_paths, commit_selected, create_branch, create_worktree,
    is_dirty, preview_selected, remove_worktree,
    recent_commits, repository_root, revisions, worktrees,
)


class GitWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "D1ffler Test")
        self.git("config", "user.email", "D1ffler@example.invalid")
        (self.repo / "sample.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "sample.txt")
        self.git("commit", "-qm", "base")
        self.git("branch", "feature")
        self.git("tag", "v1")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def test_branch_and_tag_worktrees(self):
        self.assertEqual(repository_root(self.repo), self.repo.resolve())
        available = revisions(self.repo)
        branch = next(item for item in available if item.kind == "branch" and item.name == "feature")
        tag = next(item for item in available if item.kind == "tag" and item.name == "v1")
        branch_path = self.root / "branch"
        tag_path = self.root / "tag"
        create_worktree(self.repo, branch_path, branch)
        create_worktree(self.repo, tag_path, tag)
        listed = {item.path: item for item in worktrees(self.repo)}
        self.assertEqual(listed[branch_path].branch, "feature")
        self.assertIsNone(listed[tag_path].branch)
        self.assertEqual((tag_path / "sample.txt").read_text(encoding="utf-8"), "base\n")
        create_branch(tag_path, "from-v1")
        self.assertEqual(next(item for item in worktrees(self.repo) if item.path == tag_path).branch, "from-v1")
        remove_worktree(self.repo, branch_path)
        self.assertFalse(branch_path.exists())

    def test_dirty_worktree_cannot_be_removed(self):
        tag = next(item for item in revisions(self.repo) if item.kind == "tag")
        path = self.root / "tag"
        create_worktree(self.repo, path, tag)
        (path / "untracked.txt").write_text("keep me", encoding="utf-8")
        self.assertTrue(is_dirty(path))
        with self.assertRaises(GitError):
            remove_worktree(self.repo, path)
        self.assertTrue(path.exists())

    def test_recent_commits_list_hashes_and_subjects(self):
        first = self.git("rev-parse", "HEAD")
        (self.repo / "sample.txt").write_text("second\n", encoding="utf-8")
        self.git("commit", "-qam", "second change")
        second = self.git("rev-parse", "HEAD")
        self.assertEqual(
            [(item.hash, item.subject) for item in recent_commits(self.repo, limit=2)],
            [(second, "second change"), (first, "base")],
        )

    def test_empty_repository_has_no_commits_to_list(self):
        empty = self.root / "empty"
        empty.mkdir()
        subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True, capture_output=True)
        self.assertEqual(recent_commits(empty), [])

    def test_ignored_files_are_retained(self):
        (self.repo / ".gitignore").write_text("build/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore build")
        self.git("branch", "with-ignore")
        branch = next(item for item in revisions(self.repo) if item.kind == "branch" and item.name == "with-ignore")
        path = self.root / "feature"
        create_worktree(self.repo, path, branch)
        (path / "build").mkdir()
        (path / "build" / "output.bin").write_bytes(b"keep")
        self.assertFalse(is_dirty(path))
        with self.assertRaises(GitError):
            remove_worktree(self.repo, path)
        self.assertTrue((path / "build" / "output.bin").exists())

    def test_selected_commit_preserves_unrelated_staged_changes(self):
        (self.repo / "sample.txt").write_text("selected\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("staged\n", encoding="utf-8")
        (self.repo / "new file.txt").write_text("new\n", encoding="utf-8")
        self.git("add", "other.txt")
        changes = {item.path: item for item in changed_paths(self.repo)}
        self.assertTrue(changes["other.txt"].staged)
        expected = {name: (changes[name].index_status, changes[name].worktree_status)
                    for name in ("sample.txt", "new file.txt")}
        commit_hash = commit_selected(self.repo, ["sample.txt", "new file.txt"],
                                      "Selected changes", expected)
        self.assertEqual(commit_hash, self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("show", "--format=", "--name-only", "HEAD").splitlines(),
                         ["new file.txt", "sample.txt"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "other.txt")

    def test_commit_refuses_stale_selection(self):
        (self.repo / "sample.txt").write_text("edited\n", encoding="utf-8")
        with self.assertRaises(GitError):
            commit_selected(self.repo, ["sample.txt"], "message", {"sample.txt": ("A", " ")})
        self.assertEqual(self.git("log", "-1", "--format=%s"), "base")

    def test_preview_uses_saved_files_and_preserves_unselected_staging(self):
        (self.repo / "sample.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "sample.txt")
        (self.repo / "sample.txt").write_text("saved\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("unselected\n", encoding="utf-8")
        self.git("add", "other.txt")
        before = self.git("diff", "--cached", "--binary")
        preview = preview_selected(self.repo, ["sample.txt"])
        self.assertIn("+saved", preview.patch)
        self.assertNotIn("+staged", preview.patch)
        self.assertNotIn("other.txt", preview.patch)
        self.assertEqual(self.git("diff", "--cached", "--binary"), before)
        commit_selected(self.repo, ["sample.txt"], "saved version", preview=preview)
        self.assertEqual(self.git("show", "HEAD:sample.txt"), "saved")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "other.txt")

    def test_preview_includes_deletions_and_binary_content(self):
        (self.repo / "sample.txt").unlink()
        (self.repo / "asset.bin").write_bytes(bytes(range(256)))
        preview = preview_selected(self.repo, ["sample.txt", "asset.bin"])
        self.assertIn("deleted file mode", preview.patch)
        self.assertIn("GIT binary patch", preview.patch)
        self.assertEqual(self.git("status", "--porcelain"), "D sample.txt\n?? asset.bin")

    def test_changed_content_or_index_refuses_reviewed_commit(self):
        (self.repo / "sample.txt").write_text("reviewed\n", encoding="utf-8")
        preview = preview_selected(self.repo, ["sample.txt"])
        (self.repo / "sample.txt").write_text("different\n", encoding="utf-8")
        with self.assertRaises(PreviewChangedError):
            commit_selected(self.repo, ["sample.txt"], "message", preview=preview)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "base")
        (self.repo / "sample.txt").write_text("reviewed\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "other.txt")
        with self.assertRaises(PreviewChangedError):
            commit_selected(self.repo, ["sample.txt"], "message", preview=preview)

    def test_failed_commit_restores_preexisting_index(self):
        (self.repo / "sample.txt").write_text("reviewed\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "other.txt")
        preview = preview_selected(self.repo, ["sample.txt"])
        before = self.git("diff", "--cached", "--binary")
        original_git = git_worktrees._git

        def fail_commit(repo, *args):
            if args[0] == "commit":
                self.assertIn("sample.txt", self.git("diff", "--cached", "--name-only"))
                raise GitError("Simulated commit failure after staging")
            return original_git(repo, *args)

        with patch("d1ffler.git_worktrees._git", side_effect=fail_commit), self.assertRaises(GitError) as error:
            commit_selected(self.repo, ["sample.txt"], "message", preview=preview)
        self.assertIn("staging was restored", str(error.exception))
        self.assertEqual(self.git("diff", "--cached", "--binary"), before)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "base")


if __name__ == "__main__":
    unittest.main()
