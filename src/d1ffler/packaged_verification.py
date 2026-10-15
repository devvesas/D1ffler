"""Opt-in frozen GUI integration probe, driven by scripts/smoke_packaged.py.

All data and user state must be isolated by the driver. Normal startup never runs
this probe. Reports go to a file because the GUI executable has no console.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
import zipfile

from PyQt6.QtCore import QEvent, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from d1ffler.app_assets import icon_asset_path, logo_asset_path, symbol_asset_path
from d1ffler.app_settings import load_app_settings, save_app_settings, user_state_directory
from d1ffler.app_version import APP_VERSION
from d1ffler.comparison_tabs import ComparisonWindow
from d1ffler.diff_tool import DiffApp
from d1ffler.git_worktrees import GitError, repository_root
from d1ffler.recovery_dialog import RecoveryDialog
from d1ffler.ui_dialogs import InfoDialog
from d1ffler.ui_theme import apply_theme

RECOVERY_TEXT = "Unfinished café 日本語 edits\nrestored after restart\n"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait(app, predicate):
    deadline = time.monotonic() + 30
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    require(predicate(), "Background GUI operation did not complete")


def save_in_probe(view, index):
    """Retain the actual save error instead of blocking on a test-only dialog."""
    messages = []
    original = QMessageBox.critical
    try:
        QMessageBox.critical = lambda parent, title, message: messages.append(f"{title}: {message}")
        saved = view._save_panel_file(index)
    finally:
        QMessageBox.critical = original
    require(saved, f"Frozen GUI could not save edited text: {messages}")


def finish_probe(path, report, *, interrupted=False):
    """Durably report an intentional interrupted session before stopping Qt."""
    with path.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    code = 0 if report["status"] == "passed" else 2
    if interrupted:
        # The seed deliberately leaves dirty widgets/recovery ownership alive.
        # Unwinding QApplication here can crash while those widgets are destroyed.
        # OS termination preserves the durable snapshot and releases process locks.
        os._exit(code)
    return code


def exercise(app, window, root):
    checks = []
    require(not QPixmap(str(logo_asset_path())).isNull(), "Bundled logo cannot be decoded")
    icons = list(icon_asset_path("text.svg").parent.glob("*.svg"))
    require(icons and all(QSvgRenderer(str(path)).isValid() for path in icons), "Bundled icons are missing or invalid")
    require(all(QSvgRenderer(str(symbol_asset_path(name))).isValid() for name in ("lf.svg", "cr.svg")), "Bundled line-ending symbols invalid")
    help_dialog = InfoDialog(window)
    require(help_dialog.tabs.count() >= 5, "Offline help tabs missing")
    require(any(APP_VERSION in label.text() for label in help_dialog.findChildren(QLabel)), "About version missing")
    for label in help_dialog.license_documents:
        help_dialog.license_selector.setCurrentText(label)
        text = help_dialog.license_text.toPlainText()
        require(len(text) > 100 and "document unavailable" not in text, "Frozen offline license/source document missing")
    help_dialog.show()
    app.processEvents()
    require(help_dialog.isVisible(), "Help window did not display")
    help_dialog.close()
    checks.extend(["bundled_logo_icons_symbols", "offline_help_about_version", "offline_licenses_and_source_access"])
    left, right = root / "left café.txt", root / "right 日本語.txt"
    left.write_text("before\n", encoding="utf-8")
    right.write_text("other\n", encoding="utf-8")
    text = window.open_paths([left, right], new_tab=False)
    text.panels[0]["editor"].setPlainText("saved café 日本語\n")
    save_in_probe(text, 0)
    require(left.read_text(encoding="utf-8") == "saved café 日本語\n", "Saved bytes incorrect")
    checks.append("unicode_text_edit_save")
    folders = [root / "folder café", root / "folder 日本語"]
    for folder in folders:
        folder.mkdir()
        (folder / "entry.txt").write_text("same\n", encoding="utf-8")
    folder_view = window.open_paths(folders)
    wait(app, lambda: folder_view._folder_worker is None and "entry.txt" in folder_view._folder_rows)
    require(not folder_view._folder_partial, "Frozen folder scan incomplete")
    archive = root / "archive 日本語.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("entry.txt", "same\n")
    zip_view = window.open_paths([archive, folders[0]])
    wait(app, lambda: zip_view._folder_worker is None and "entry.txt" in zip_view._folder_rows)
    require(not zip_view._folder_partial, "Frozen ZIP scan incomplete")
    checks.extend(["folder_sources", "zip_sources"])
    images = [root / "image café.png", root / "image 日本語.jpg"]
    image = QImage(24, 18, QImage.Format.Format_RGB32)
    image.fill(0xff336699)
    for path in images:
        require(image.save(str(path)), "Frozen image writer plugin missing")
    image_view = window.open_paths(images)
    wait(app, lambda: image_view._image_results is not None and image_view._image_worker is None)
    require(all(result.image is not None for result in image_view._image_results), "Frozen image reader plugin missing")
    require(not image_view.image_controls.isHidden(), "Frozen image view unavailable")
    checks.append("png_jpeg_image_plugins")
    require(shutil.which("git") is None, "Probe must run without optional Git on PATH")
    try:
        repository_root(root)
    except GitError:
        pass
    else:
        raise AssertionError("Missing Git did not produce a controlled error")
    checks.append("missing_optional_git")
    require(window.isVisible() and window.width() > 0 and window.height() > 0, "Scaled main window is not visible")
    require(window.active_comparison.save_button.height() >= window.active_comparison.save_button.fontMetrics().height(), "Scaled save button clips text vertically")
    checks.append("display_scaling_basic")
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--phase", choices=("exercise", "seed-recovery", "restore"), required=True)
    args = parser.parse_args(argv)
    report = {"status": "failed", "phase": args.phase, "version": APP_VERSION, "checks": []}
    window = None
    try:
        args.root.mkdir(parents=True, exist_ok=True)
        app = QApplication([sys.argv[0]])
        app.setApplicationName("D1ffler")
        app.setApplicationVersion(APP_VERSION)
        require(user_state_directory().resolve().is_relative_to(args.root.resolve()), "Probe user state is not isolated under its root")
        settings, settings_path = load_app_settings()
        require(settings_path.resolve().is_relative_to(args.root.resolve()), "Probe user state is not isolated under its root")
        apply_theme(app, settings["theme"])
        window = ComparisonWindow(settings, settings_path, DiffApp)
        window.show()
        app.processEvents()
        if args.phase == "exercise":
            report["checks"] = exercise(app, window, args.root)
        elif args.phase == "seed-recovery":
            settings["theme"] = "nord"
            save_app_settings(settings, settings_path)
            source = args.root / "unfinished café.txt"
            source.write_text("original source\n", encoding="utf-8")
            view = window.open_paths([source], new_tab=False)
            view.panels[0]["editor"].setPlainText(RECOVERY_TEXT)
            view.panels[1]["editor"].setPlainText("unfinished pasted 日本語\n")
            require(view._write_recovery_snapshot(), "Frozen recovery snapshot write failed")
            require(source.read_text(encoding="utf-8") == "original source\n", "Snapshot unexpectedly saved the source")
            report["checks"] = ["per_user_settings_write", "unfinished_file_and_pasted_snapshots"]
        else:
            require(settings["theme"] == "nord", "Upgrade lost existing user settings")
            # Use the actual startup recovery dialog and Restore action.
            timer = QTimer()
            timer.setInterval(25)
            def accept_recovery():
                for widget in app.topLevelWidgets():
                    if isinstance(widget, RecoveryDialog) and widget.isVisible():
                        widget._finish("restore")
            timer.timeout.connect(accept_recovery)
            timer.start()
            require(window.offer_recovery(), "Frozen startup could not restore the previous session")
            timer.stop()
            view = window.active_comparison
            require(view._panel_text(view.panels[0]) == RECOVERY_TEXT, "Recovery text was not retained")
            require(view._panel_text(view.panels[1]) == "unfinished pasted 日本語\n", "Pasted recovery text was not retained")
            source = args.root / "unfinished café.txt"
            require(source.read_text(encoding="utf-8") == "original source\n", "Restore unexpectedly saved the source")
            save_in_probe(view, 0)
            require(source.read_text(encoding="utf-8") == RECOVERY_TEXT, "Restored file save bytes incorrect")
            report["checks"] = ["settings_retained_after_move", "restart_recovery_dialog_restores_file_and_pasted_edits", "explicit_save_after_recovery"]
        # Seed exits with dirty buffers, simulating an interrupted process after
        # its durable snapshot. Normal close would prompt to save/discard files.
        if args.phase != "seed-recovery":
            require(window.close(), "Frozen main window refused a clean close")
            window.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            window = None
        report["status"] = "passed"
        report["device_pixel_ratio"] = app.primaryScreen().devicePixelRatio()
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        # Exit promptly on a failed probe; never interact with real user data.
    return finish_probe(args.report, report, interrupted=args.phase == "seed-recovery")
