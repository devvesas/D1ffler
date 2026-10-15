"""Linked paths remain visible and cannot be used by folder sync."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from d1ffler.diff_tool import folder_inventory
from d1ffler.folder_sync import build_items, safe_path, SyncError


class FolderLinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.left = self.base / "left"
        self.right = self.base / "right"
        self.left.mkdir()
        self.right.mkdir()

    def link(self, target: Path, path: Path, directory=False):
        try:
            os.symlink(target, path, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

    def test_inventory_shows_links_and_only_follows_internal_links_when_requested(self):
        inner = self.left / "inner"
        inner.mkdir()
        (inner / "data.txt").write_text("inside", encoding="utf-8")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        self.link(inner, self.left / "alias", directory=True)
        self.link(outside, self.left / "external", directory=True)
        self.link(self.left / "missing", self.left / "broken", directory=True)
        self.link(self.left / "alias", inner / "cycle", directory=True)
        links = {}
        files, _ = folder_inventory(self.left, [], links=links)
        self.assertIn("alias", files)
        self.assertIn("external", files)
        self.assertIn("broken", files)
        self.assertNotIn("alias/data.txt", files)
        self.assertEqual(links["alias"].state, "internal")
        self.assertEqual(links["external"].state, "external")
        self.assertEqual(links["broken"].state, "broken")
        followed, _ = folder_inventory(self.left, [], links={}, follow_internal_links=True)
        self.assertIn("alias/data.txt", followed)
        self.assertNotIn("external/secret.txt", followed)
        self.assertNotIn("alias/cycle/data.txt", followed)
        self.assertLess(len(followed), 20)

    def test_sync_plan_disables_link_and_linked_parent(self):
        inner = self.left / "inner"
        inner.mkdir()
        (inner / "data.txt").write_text("inside", encoding="utf-8")
        self.link(inner, self.left / "alias", directory=True)
        left_files, _ = folder_inventory(self.left, [], follow_internal_links=True)
        items = {item.relative: item for item in build_items(self.left, self.right, left_files, {})}
        self.assertEqual(items["alias"].status, "error")
        self.assertIn("linked paths", items["alias"].error)
        self.assertEqual(items["alias/data.txt"].status, "error")
        with self.assertRaises(SyncError):
            safe_path(self.left, "alias/new.txt")

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_junction_is_listed_and_blocked_from_sync(self):
        inner = self.left / "inner"
        inner.mkdir()
        (inner / "data.txt").write_text("inside", encoding="utf-8")
        junction = self.left / "junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(inner)],
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest(f"Junctions unavailable: {result.stderr or result.stdout}")
        links = {}
        files, _ = folder_inventory(self.left, [], links=links)
        self.assertIn("junction", files)
        self.assertEqual(links["junction"].kind, "junction")
        self.assertEqual(links["junction"].state, "internal")
        self.assertNotIn("junction/data.txt", files)
        items = {item.relative: item for item in build_items(self.left, self.right, files, {})}
        self.assertEqual(items["junction"].status, "error")
        self.assertIn("junction", items["junction"].error)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        for link, target in ((self.left / "external", outside), (inner / "cycle", junction)):
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        followed, _ = folder_inventory(self.left, [], links={}, follow_internal_links=True)
        self.assertIn("junction/data.txt", followed)
        self.assertNotIn("external/secret.txt", followed)
        self.assertNotIn("junction/cycle/data.txt", followed)
        self.assertLess(len(followed), 20)
        vanished = self.left / "vanished"
        vanished.mkdir()
        broken = self.left / "broken-junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(broken), str(vanished)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        vanished.rmdir()
        broken_links = {}
        broken_files, _ = folder_inventory(self.left, [], links=broken_links)
        self.assertEqual(broken_links["broken-junction"].state, "broken")
        broken_items = {item.relative: item for item in build_items(self.left, self.right, broken_files, {})}
        self.assertEqual(broken_items["broken-junction"].status, "error")


if __name__ == "__main__":
    unittest.main()
