"""Image previews for files, folder entries, and ZIP sources."""

import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.image_compare import MAX_IMAGE_FILE_BYTES, decode_image, difference_image


def make_image(path: Path, color: str, width=20, height=20) -> None:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    image.setPixelColor(4, 5, QColor(color))
    assert image.save(str(path), "PNG")


class ImageComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_direct_images_have_linked_views_overlay_difference_and_summary(self):
        left, right = self.root / "left.png", self.root / "right.png"
        make_image(left, "red")
        make_image(right, "blue")
        self.window._load_panel(0, "file", left)
        self.window._load_panel(1, "file", right)
        self.wait_until(lambda: self.window._image_results is not None and
                        self.window._binary_results is not None)
        self.assertEqual([panel["view_stack"].currentIndex() for panel in self.window.panels], [2, 2])
        self.assertIn("20 × 20", self.window.panels[0]["image_pane"].details.text())
        self.window._change_image_zoom(25)
        self.assertEqual(self.window.panels[0]["image_pane"].view.transform().m11(), 1.25)
        self.assertEqual(self.window.panels[1]["image_pane"].view.transform().m11(), 1.25)
        self.window.image_mode.setCurrentIndex(1)
        self.assertFalse(self.window.panels[0]["image_pane"].view.overlay_item.pixmap().isNull())
        self.window.image_mode.setCurrentIndex(2)
        diff = difference_image(decode_image(left).image, decode_image(right).image)
        self.assertIsNotNone(diff)
        self.assertNotEqual(diff.pixelColor(4, 5), QColor("black"))
        self.assertEqual(diff.pixelColor(0, 0), QColor("black"))
        self.window.image_summary_button.setChecked(True)
        self.assertEqual(self.window.panels[0]["view_stack"].currentIndex(), 0)
        self.assertIn("SHA-256", self.window.panels[0]["editor"].toPlainText())

    def test_different_dimensions_and_corrupt_image_fallback(self):
        left, right = self.root / "left.png", self.root / "right.png"
        make_image(left, "red")
        make_image(right, "blue", 24, 20)
        self.window._load_panel(0, "file", left)
        self.window._load_panel(1, "file", right)
        self.wait_until(lambda: self.window._image_results is not None)
        self.window.image_mode.setCurrentIndex(2)
        self.assertIn("dimensions differ", self.window.panels[0]["image_pane"].details.text())
        right.write_bytes(b"\x89PNG\r\n\x1a\ncorrupt")
        self.window._load_panel(1, "file", right)
        self.wait_until(lambda: self.window._image_results is not None and
                        self.window._image_results[1].image is None)
        self.assertTrue(self.window.image_controls.isHidden())
        self.assertEqual(self.window.panels[0]["view_stack"].currentIndex(), 0)
        self.assertIn("Image preview unavailable", self.window.panels[1]["editor"].toPlainText())

    def test_image_inside_zip_opens_read_only_visual_view(self):
        folder = self.root / "folder"
        folder.mkdir()
        image = folder / "picture.png"
        make_image(image, "red")
        archive = self.root / "images.zip"
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr("picture.png", image.read_bytes())
        self.window._load_panel(0, "folder", archive)
        self.window._load_panel(1, "folder", folder)
        self.wait_until(lambda: self.window._folder_worker is None and
                        "picture.png" in self.window._folder_tree_items[0])
        self.window._open_folder_item(self.window.panels[0]["widget"],
                                      self.window._folder_tree_items[0]["picture.png"])
        self.wait_until(lambda: self.window._image_results is not None)
        self.assertEqual(self.window.panels[0]["view_stack"].currentIndex(), 2)
        self.assertTrue(self.window.panels[0]["editor"].isReadOnly())
        self.assertIsNone(self.window.panels[0]["editing_path"])

    def test_oversized_source_returns_bounded_fallback_reason(self):
        path = self.root / "oversized.png"
        with path.open("wb") as stream:
            stream.truncate(MAX_IMAGE_FILE_BYTES + 1)
        result = decode_image(path)
        self.assertIsNone(result.image)
        self.assertIn("read limit", result.error)


if __name__ == "__main__":
    unittest.main()
