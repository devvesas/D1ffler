from pathlib import Path
from PyQt6.QtGui import QIcon

from icon_map import ICON_MAP, FILENAME_ICON_MAP

ICON_DIR = Path(__file__).parent / "icons"

def icon_for_path(path: Path) -> QIcon:
    icon_name = FILENAME_ICON_MAP.get(path.name)

    if icon_name is None:
        icon_name = ICON_MAP.get(path.suffix.lower())

    if icon_name is None:
        return QIcon(str(ICON_DIR / "text.svg"))

    return QIcon(str(ICON_DIR / icon_name))
