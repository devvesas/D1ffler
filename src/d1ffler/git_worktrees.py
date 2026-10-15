"""Small Git worktree adapter for folder comparison sources."""

from __future__ import annotations

import subprocess
import tempfile
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    pass


class PreviewChangedError(GitError):
    pass


class CommitContentMismatchError(GitError):
    def __init__(self, commit_hash: str, message: str):
        super().__init__(message)
        self.commit_hash = commit_hash


@dataclass(frozen=True)
class Revision:
    kind: str  # branch, tag, commit
    name: str
    commit: str


@dataclass(frozen=True)
class Commit:
    hash: str
    subject: str


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None
    commit: str


@dataclass(frozen=True)
class GitChange:
    path: str
    index_status: str
    worktree_status: str

    @property
    def staged(self) -> bool:
        return self.index_status not in {" ", "?"}


@dataclass(frozen=True)
class CommitPreview:
    paths: tuple[str, ...]
    patch: str
    tree: str
    head: str
    index_fingerprint: str
    statuses: tuple[tuple[str, str, str], ...]


class GitIgnoreMatcher:
    """Match .gitignore rules without creating metadata in the compared folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder.resolve()
        try:
            self.worktree_root = repository_root(self.folder)
        except GitError:
            self.worktree_root = self.folder
        self.prefix = self.folder.relative_to(self.worktree_root)
        self._temporary = tempfile.TemporaryDirectory()
        self.git_dir = Path(self._temporary.name) / ".git"
        try:
            _git(self.folder, "init", "-q", self._temporary.name)
        except BaseException:
            self._temporary.cleanup()
            raise

    def __enter__(self) -> "GitIgnoreMatcher":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self._temporary.cleanup()

    def ignored(self, relative_paths: list[str]) -> set[str]:
        if not relative_paths:
            return set()
        mapping = {
            (self.prefix / Path(relative)).as_posix(): relative
            for relative in relative_paths
        }
        try:
            result = subprocess.run(
                ["git", "-C", str(self.worktree_root), "-c", "core.excludesFile=",
                 f"--git-dir={self.git_dir}", f"--work-tree={self.worktree_root}",
                 "check-ignore", "--no-index", "-z", "--stdin"],
                input="\0".join(mapping) + "\0", capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False,
            )
        except OSError as exc:
            raise GitError(f"Could not check .gitignore rules: {exc}") from exc
        if result.returncode not in (0, 1):
            raise GitError(result.stderr.strip() or "Could not check .gitignore rules.")
        return {mapping[path] for path in result.stdout.split("\0") if path in mapping}


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("Git is not installed or is not available on PATH.") from exc
    except OSError as exc:
        raise GitError(f"Could not run Git: {exc}") from exc
    if result.returncode:
        raise GitError(result.stderr.strip() or result.stdout.strip() or "Git command failed.")
    return result.stdout.strip()


def repository_root(path: Path) -> Path:
    return Path(_git(path, "rev-parse", "--show-toplevel")).resolve()


def revisions(repo: Path) -> list[Revision]:
    result = []
    for kind, prefix in (("branch", "refs/heads"), ("tag", "refs/tags")):
        output = _git(repo, "for-each-ref", "--format=%(refname:short)", prefix)
        for name in output.splitlines():
            result.append(Revision(kind, name, resolve_commit(repo, f"{prefix}/{name}")))
    return result


def resolve_commit(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def recent_commits(repo: Path, limit: int = 200) -> list[Commit]:
    """List recent commits reachable from local refs or the current HEAD."""
    refs = ["--all"]
    try:
        resolve_commit(repo, "HEAD")
        refs.append("HEAD")
    except GitError:
        pass  # An empty repository has no HEAD commit yet.
    output = _git(repo, "log", *refs, "--topo-order", f"--max-count={limit}", "--format=%H%x09%s")
    result = []
    for line in output.splitlines():
        hash_value, separator, subject = line.partition("\t")
        if separator:
            result.append(Commit(hash_value, subject))
    return result


def worktrees(repo: Path) -> list[Worktree]:
    output = _git(repo, "worktree", "list", "--porcelain")
    result = []
    path = None
    branch = None
    commit = ""
    for line in (output + "\n\n").splitlines():
        if not line:
            if path is not None:
                result.append(Worktree(path.resolve(), branch, commit))
            path, branch, commit = None, None, ""
        elif line.startswith("worktree "):
            path = Path(line[9:])
        elif line.startswith("HEAD "):
            commit = line[5:]
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
    return result


def create_worktree(repo: Path, path: Path, revision: Revision) -> None:
    if path.exists():
        raise GitError(f"Worktree path already exists: {path}")
    if revision.kind == "branch":
        _git(repo, "worktree", "add", str(path), revision.name)
    else:
        _git(repo, "worktree", "add", "--detach", str(path), revision.commit)


def create_branch(path: Path, name: str) -> None:
    _git(path, "switch", "-c", name)


def is_dirty(path: Path, include_ignored: bool = False) -> bool:
    args = ["status", "--porcelain", "--untracked-files=all"]
    if include_ignored:
        args.append("--ignored=matching")
    return bool(_git(path, *args))


def changed_paths(repo: Path) -> list[GitChange]:
    """List changed files without Git's path quoting or rename records."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.quotepath=false", "status",
             "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all"],
            capture_output=True, check=False,
        )
    except OSError as exc:
        raise GitError(f"Could not run Git: {exc}") from exc
    if result.returncode:
        raise GitError(result.stderr.decode("utf-8", "replace").strip() or "Could not read Git status.")
    changes = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise GitError("Could not parse Git status.")
        path = record[3:].decode("utf-8", "surrogateescape")
        changes.append(GitChange(path, chr(record[0]), chr(record[1])))
    return changes


def _index_path(repo: Path) -> Path:
    path = Path(_git(repo, "rev-parse", "--git-path", "index"))
    return path if path.is_absolute() else repo / path


def _index_fingerprint(repo: Path) -> str:
    path = _index_path(repo)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"
    except OSError as exc:
        raise GitError(f"Could not read Git index: {exc}") from exc


def _run_git(repo: Path, *args: str, index: Path | None = None) -> str:
    environment = os.environ.copy()
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], env=environment,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", check=False)
    except OSError as exc:
        raise GitError(f"Could not run Git: {exc}") from exc
    if result.returncode:
        raise GitError(result.stderr.strip() or result.stdout.strip() or "Git command failed.")
    return result.stdout


def preview_selected(repo: Path, paths: list[str]) -> CommitPreview:
    """Build the selected working-tree patch against HEAD without touching the real index."""
    selected = tuple(dict.fromkeys(paths))
    if not selected:
        raise GitError("Select at least one file.")
    current = {item.path: item for item in changed_paths(repo)}
    if any(path not in current for path in selected):
        raise PreviewChangedError("Selected files changed. Refresh the commit list and review again.")
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    before_index = _index_fingerprint(repo)
    pathspecs = [f":(literal){path}" for path in selected]
    with tempfile.TemporaryDirectory(prefix="D1ffler-commit-") as temporary:
        index = Path(temporary) / "index"
        _run_git(repo, "read-tree", "HEAD", index=index)
        _run_git(repo, "add", "-A", "--", *pathspecs, index=index)
        tree = _run_git(repo, "write-tree", index=index).strip()
        patch = _run_git(repo, "diff", "--cached", "--binary", "--no-ext-diff",
                         "--find-renames", "HEAD", "--", *pathspecs, index=index)
    if (_git(repo, "rev-parse", "--verify", "HEAD") != head or
            _index_fingerprint(repo) != before_index):
        raise PreviewChangedError("Repository changed while preparing the preview. Review again.")
    return CommitPreview(selected, patch, tree, head, before_index,
                         tuple((path, current[path].index_status, current[path].worktree_status)
                               for path in selected))


def commit_selected(repo: Path, paths: list[str], message: str,
                    expected: dict[str, tuple[str, str]] | None = None,
                    preview: CommitPreview | None = None) -> str:
    """Commit selected working-tree paths without including unrelated staged files."""
    if not message.strip():
        raise GitError("Enter a commit message.")
    try:
        _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    except GitError as exc:
        raise GitError("Create a branch before committing from a detached revision.") from exc
    current = {item.path: item for item in changed_paths(repo)}
    selected = list(dict.fromkeys(paths))
    if not selected or any(path not in current for path in selected):
        error = PreviewChangedError if preview is not None else GitError
        raise error("Selected files changed since review. Refresh the commit list.")
    if expected and any(
        (current[path].index_status, current[path].worktree_status) != expected.get(path)
        for path in selected
    ):
        raise GitError("Selected files changed since review. Refresh the commit list.")
    if preview is not None:
        if tuple(selected) != preview.paths:
            raise PreviewChangedError("Selection changed. Review the selected patch again.")
        fresh = preview_selected(repo, selected)
        if (fresh.tree != preview.tree or fresh.head != preview.head or
                fresh.index_fingerprint != preview.index_fingerprint or
                fresh.statuses != preview.statuses):
            raise PreviewChangedError("Selected files or staging changed. Review the updated patch before committing.")
    pathspecs = [f":(literal){path}" for path in selected]
    index_path = _index_path(repo)
    try:
        original_index = index_path.read_bytes() if index_path.exists() else None
    except OSError as exc:
        raise GitError(f"Could not back up Git index: {exc}") from exc
    fingerprint = hashlib.sha256(original_index).hexdigest() if original_index is not None else "absent"
    if preview is not None and fingerprint != preview.index_fingerprint:
        raise PreviewChangedError("Staging changed. Review the selected patch again.")
    previous_head = resolve_commit(repo, "HEAD")
    try:
        _git(repo, "add", "-A", "--", *pathspecs)
        _git(repo, "commit", "--only", "-m", message.strip(), "--", *pathspecs)
    except GitError as exc:
        if resolve_commit(repo, "HEAD") != previous_head:
            raise GitError(f"Commit may have been created despite a Git error. Check HEAD before retrying: {exc}") from exc
        try:
            if original_index is None:
                index_path.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile(dir=index_path.parent, prefix="D1ffler-index-",
                                                 delete=False) as saved:
                    saved.write(original_index)
                    saved_path = Path(saved.name)
                os.replace(saved_path, index_path)
        except OSError as restore_exc:
            raise GitError(f"Commit failed: {exc}. Git index could not be restored: {restore_exc}. "
                           "Inspect staged changes with git diff --cached before retrying.") from restore_exc
        raise GitError(f"Commit failed; previous staging was restored: {exc}") from exc
    commit_hash = resolve_commit(repo, "HEAD")
    if preview is not None and _git(repo, "rev-parse", "HEAD^{tree}") != preview.tree:
        raise CommitContentMismatchError(
            commit_hash, f"Commit {commit_hash[:12]} was created, but a Git hook changed its contents. "
            "Inspect the commit with git show HEAD before continuing."
        )
    return commit_hash


def remove_worktree(repo: Path, path: Path) -> None:
    if is_dirty(path, include_ignored=True):
        raise GitError("This worktree has uncommitted, untracked, or ignored files. Keep it or handle those files in Git first.")
    _git(repo, "worktree", "remove", str(path))
