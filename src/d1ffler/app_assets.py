"""Bundled asset paths in source and packaged builds."""

from __future__ import annotations

import sys
from pathlib import Path

def icon_asset_path(filename: str) -> Path:
    """Resolve bundled icon assets in source and PyInstaller builds."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "icons" / filename



def symbol_asset_path(filename: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "symbols" / filename



def logo_asset_path() -> Path:
    """Resolve the application logo in source and PyInstaller builds."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "logo" / "D1ffler.png"


def license_document_path(filename: str) -> Path:
    """Read offline legal documents embedded in the frozen app or source root."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / filename

