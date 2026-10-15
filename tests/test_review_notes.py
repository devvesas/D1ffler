"""Review note identity, staleness, and report privacy checks."""

import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from d1ffler.review_notes import content_fingerprint, hunk_anchor, note_is_stale, report_text, source_key


class ReviewNotesTests(unittest.TestCase):
    def test_content_change_marks_note_stale_and_branch_head_does_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left, right = root / "left", root / "right"
            left.mkdir()
            right.mkdir()
            (left / "a.txt").write_text("before\n", encoding="utf-8")
            (right / "a.txt").write_text("after\n", encoding="utf-8")
            sources = [
                {"kind": "folder", "path": str(left), "git": None},
                {"kind": "folder", "path": str(right), "git": {
                    "repo": str(right), "kind": "branch", "name": "feature",
                    "commit": "old", "readonly": False}},
            ]
            key = source_key(sources)
            fingerprint = content_fingerprint(sources, "a.txt")
            note = {"source_key": key, "fingerprint": fingerprint}
            sources[1]["git"]["commit"] = "new"
            self.assertEqual(source_key(sources), key)
            self.assertFalse(note_is_stale(note, source_key(sources), content_fingerprint(sources, "a.txt")))
            (right / "a.txt").write_text("changed\n", encoding="utf-8")
            self.assertTrue(note_is_stale(note, key, content_fingerprint(sources, "a.txt")))
            self.assertNotEqual(source_key(list(reversed(sources))), key)

    def test_missing_file_and_unsafe_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [{"kind": "folder", "path": str(root)}]
            self.assertEqual(content_fingerprint(sources, "missing.txt"),
                             content_fingerprint(sources, "another.txt"))
            for path in ("../outside", "..\\outside", "/absolute", "C:/Windows/win.ini"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    content_fingerprint(sources, path)

    def test_zip_member_change_marks_note_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, folder = root / "left.zip", root / "right"
            folder.mkdir()
            (folder / "a.txt").write_bytes(b"same")
            sources = [{"kind": "folder", "path": str(archive)},
                       {"kind": "folder", "path": str(folder)}]
            with ZipFile(archive, "w") as stream:
                stream.writestr("a.txt", b"same")
            key = source_key(sources)
            fingerprint = content_fingerprint(sources, "a.txt")
            note = {"source_key": key, "fingerprint": fingerprint}
            with ZipFile(archive, "w") as stream:
                stream.writestr("a.txt", b"changed")
            self.assertTrue(note_is_stale(note, key, content_fingerprint(sources, "a.txt")))

    def test_hunk_anchor_tracks_text(self):
        rows = [[0, 0], [1, 1]]
        self.assertNotEqual(hunk_anchor(rows, [["a", "b"], ["a", "c"]], (1, 1)),
                            hunk_anchor(rows, [["a", "b"], ["a", "d"]], (1, 1)))

    def test_report_excludes_file_content_by_default_and_escapes_markup(self):
        context = {"sources": [{"name": "A", "path": "left/<repo>"}],
                   "rules": {"Excluded paths": "build/"}, "counts": {"Files changed": 1}}
        note = {"id": "one", "path": "a.md", "state": "Open", "text": "Review <script> & reason"}
        other = {"id": "two", "text": "OMITTED SECRET"}
        for html_format in (False, True):
            with self.subTest(html_format=html_format):
                report = report_text(context, [note], html_format=html_format)
                self.assertNotIn("OMITTED SECRET", report)
                self.assertNotIn("FILE CONTENT SECRET", report)
                self.assertNotIn("<script>", report)
                self.assertIn("Excluded paths", report)
                self.assertIn("Selected notes: Open", report)
                with_excerpt = report_text(context, [note], html_format=html_format,
                                           excerpts={"one": "FILE CONTENT SECRET"})
                self.assertIn("FILE CONTENT SECRET", with_excerpt)
        self.assertNotIn(other["text"], report)


if __name__ == "__main__":
    unittest.main()
