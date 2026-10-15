"""Source launchers, resource roots, and freezing use the src package."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from d1ffler import app_assets, app_settings
from scripts.release_licensing import freeze_spec


ROOT = Path(__file__).resolve().parents[1]


class SourceLayoutTests(unittest.TestCase):
    def test_source_assets_and_legacy_settings_use_repository_root(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(app_settings.settings_candidates()[-1], ROOT / "appsettings.json")
        paths = [app_assets.icon_asset_path("python.svg"),
                 app_assets.symbol_asset_path("lf.svg"), app_assets.logo_asset_path(),
                 app_assets.license_document_path("LICENSE")]
        self.assertTrue(all(path.is_file() and path.is_relative_to(ROOT) for path in paths))
        self.assertEqual(paths[0], ROOT / "icons" / "python.svg")

    def test_frozen_assets_use_extraction_root(self):
        with patch.object(sys, "_MEIPASS", str(ROOT / "build" / "extracted"), create=True):
            base = Path(sys._MEIPASS)
            self.assertEqual(app_assets.icon_asset_path("python.svg"), base / "icons" / "python.svg")
            self.assertEqual(app_assets.symbol_asset_path("lf.svg"), base / "symbols" / "lf.svg")
            self.assertEqual(app_assets.logo_asset_path(), base / "logo" / "D1ffler.png")
            self.assertEqual(app_assets.license_document_path("LICENSE"), base / "LICENSE")

    def test_launchers_work_without_pythonpath_from_another_directory(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="not_a_qt_platform")
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            left, right = directory / "left.txt", directory / "right.txt"
            left.write_text("same\n", encoding="utf-8")
            right.write_text("same\n", encoding="utf-8")
            for entry in ("D1ffler.py", "D1ffler_cli.py"):
                with self.subTest(entry=entry):
                    result = subprocess.run([sys.executable, str(ROOT / entry), "compare", str(left), str(right)],
                                            cwd=directory, env=environment, capture_output=True,
                                            text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_freeze_spec_includes_source_package_search_path(self):
        spec = freeze_spec(ROOT, "D1ffler.py", "D1ffler.exe", ROOT / "build" / "version.txt", True)
        self.assertIn(f"pathex=[{str(ROOT / 'src')!r}, {str(ROOT)!r}]", spec)
