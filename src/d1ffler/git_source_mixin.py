"""Git revision, branch, commit, and worktree actions."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PyQt6.QtWidgets import QDialog, QInputDialog, QMessageBox

from d1ffler.git_dialog import CommitDialog
from d1ffler.shared_sources import SourceScope
from d1ffler.source_paths import absolute_source, absolute_worktrees
from d1ffler.git_worktrees import (
    GitError, Revision, create_branch, create_worktree, is_dirty,
    recent_commits, remove_worktree, repository_root, resolve_commit,
    revisions, worktrees,
)


class GitSourceMixin:
    def _baseline_repository(self) -> Path | None:
        if not self.panels or self.panels[0]["kind"] != "folder":
            return None
        path = self.panels[0]["path"]
        if (path / ".git").exists():
            return path
        try:
            return path if repository_root(path) == path.resolve() else None
        except GitError:
            return None

    def _set_git_source(self, panel: dict, source: dict | None) -> None:
        if source is not None:
            source = dict(source)
            if source.get("repo"):
                source["repo"] = str(absolute_source(source["repo"]))
        panel["git"] = source
        label = panel["source"]
        if source is None:
            label.clear()
            label.setVisible(False)
            return
        path = panel["path"]
        try:
            dirty = is_dirty(path)
        except GitError:
            dirty = None
        state = "local changes" if dirty else "clean" if dirty is False else "status unavailable"
        access = "read-only, detached" if source["readonly"] else "editable"
        title = source["name"] if source["kind"] != "commit" else source["commit"][:12]
        label.setText(f"{source['kind'].title()}: {title} · {source['commit'][:12]} · {access} · {state}\n{path}")
        label.setToolTip(str(path))
        label.setVisible(True)

    def _choose_revision(self, index: int) -> None:
        repo = self._baseline_repository()
        if repo is None:
            return
        try:
            repository_root(repo)
            options = [
                (f"{item.kind.title()}: {item.name} ({item.commit[:12]})", item)
                for item in revisions(repo)
            ]
            options.extend(
                (f"Commit: {item.hash[:12]}  {item.subject}", Revision("commit", item.hash, item.hash))
                for item in recent_commits(repo)
            )
            entries = [label for label, _revision in options] + ["Enter commit hash…"]
            choice, accepted = QInputDialog.getItem(self, "Choose Git revision", "Revision:", entries, 0, False)
            if not accepted:
                return
            if choice == "Enter commit hash…":
                value, accepted = QInputDialog.getText(self, "Choose Git commit", "Commit hash:")
                if not accepted or not value.strip():
                    return
                revision = Revision("commit", value.strip(), resolve_commit(repo, value.strip()))
            else:
                revision = options[entries.index(choice)][1]
            if not self._confirm_replace_panel(index):
                return
            existing = next(
                (item for item in worktrees(repo) if item.branch == revision.name), None
            ) if revision.kind == "branch" else None
            if existing is not None:
                choice = QMessageBox.question(
                    self, "Branch already checked out",
                    f"{revision.name} is already checked out at:\n{existing.path}\n\nUse that worktree?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if choice != QMessageBox.StandardButton.Yes:
                    return
                path = existing.path
                revision = Revision(revision.kind, revision.name, existing.commit)
                managed = str(path) in self.managed_worktrees
            else:
                suggested = repo.parent / f"{repo.name}-D1ffler-{revision.kind}-{uuid4().hex[:8]}"
                value, accepted = QInputDialog.getText(
                    self, "New Git worktree", "Worktree path (new directory):", text=str(suggested)
                )
                if not accepted or not value.strip():
                    return
                path = Path(value.strip()).expanduser().resolve()
                create_worktree(repo, path, revision)
                self.managed_worktrees[str(path)] = str(repo)
                self._persist_managed_worktrees()
                managed = True
            self._loading_workspace = True
            try:
                self._load_panel(index, "folder", path, confirm=False)
            finally:
                self._loading_workspace = False
            source = {"repo": str(repo), "kind": revision.kind, "name": revision.name,
                      "commit": revision.commit, "readonly": revision.kind != "branch", "managed": managed}
            self._set_git_source(self.panels[index], source)
            self._update_panel_locking()
            self._record_recent_workspace()
        except GitError as exc:
            QMessageBox.warning(self, "Git revision", str(exc))

    def _create_branch_from_panel(self, index: int) -> None:
        panel = self.panels[index]
        source = panel.get("git")
        if not source or not source["readonly"]:
            return
        name, accepted = QInputDialog.getText(self, "Create branch", "New branch name:")
        if not accepted or not name.strip():
            return
        try:
            create_branch(panel["path"], name.strip())
            source.update({"kind": "branch", "name": name.strip(), "readonly": False})
            self._set_git_source(panel, source)
            self._update_panel_locking()
            self._record_recent_workspace()
        except GitError as exc:
            QMessageBox.warning(self, "Create branch", str(exc))

    def _create_commit_from_panel(self, index: int) -> None:
        panel = self.panels[index]
        source = panel.get("git")
        if panel["kind"] != "folder" or not source or source["readonly"]:
            return
        root = panel["path"].resolve()
        if self.shared_sources.prepare([SourceScope(root, folder=True)], self, "Create Git commit") is None:
            return
        dialog = CommitDialog(root, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.commit_hash:
            return
        source["commit"] = dialog.commit_hash
        self._set_git_source(panel, source)
        self._record_recent_workspace()
        self._refresh_folder_comparison()
        self.status.setText(f"Created commit {dialog.commit_hash[:12]} in {source['name']}")

    def _persist_managed_worktrees(self) -> None:
        legacy = [item for item in self.settings.get("managed_worktrees", [])
                  if isinstance(item, dict) and not absolute_worktrees([item])]
        self.settings["managed_worktrees"] = legacy + [
            {"path": path, "repo": repo} for path, repo in self.managed_worktrees.items()
        ]
        self._persist_workspaces()

    def _manage_worktrees(self) -> None:
        if not self.managed_worktrees:
            return
        paths = list(self.managed_worktrees)
        value, accepted = QInputDialog.getItem(self, "D1ffler worktrees", "Worktree:", paths, 0, False)
        if not accepted:
            return
        path = Path(value)
        action, accepted = QInputDialog.getItem(
            self, "Manage worktree", "Action:", ["Keep worktree", "Remove clean worktree"], 0, False
        )
        if not accepted:
            return
        if action == "Keep worktree":
            self.managed_worktrees.pop(value, None)
            self._persist_managed_worktrees()
            self.status.setText(f"Kept worktree: {path}")
        elif action == "Remove clean worktree":
            comparisons = self.comparison_host.comparisons if self.comparison_host is not None else [self]
            if any(panel["path"] is not None and panel["path"].resolve().is_relative_to(path.resolve())
                   for comparison in comparisons for panel in comparison.panels):
                QMessageBox.information(self, "Remove worktree", "Clear its panels in every comparison tab before removing this worktree.")
                return
            try:
                remove_worktree(Path(self.managed_worktrees[value]), path)
                del self.managed_worktrees[value]
                self._persist_managed_worktrees()
                self.status.setText(f"Removed worktree: {path}")
            except GitError as exc:
                QMessageBox.warning(self, "Remove worktree", str(exc))
