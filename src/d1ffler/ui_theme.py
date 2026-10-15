"""Application theme names and Qt stylesheets."""

from __future__ import annotations

import re
from PyQt6.QtWidgets import QApplication

THEMES = {
    "dark": "Dark",
    "light": "Light",
    "midnight": "Midnight",
    "dracula": "Dracula",
    "nord": "Nord",
    "solarized": "Solarized",
    "tokyo_night": "Tokyo Night",
    "catppuccin_mocha": "Catppuccin Mocha",
    "gruvbox": "Gruvbox",
    "one_dark": "One Dark",
    "monokai": "Monokai",
    "rose_pine": "Rosé Pine",
    "everforest": "Everforest",
    "ayu_dark": "Ayu Dark",
    "material_ocean": "Material Ocean",
    "github_dark": "GitHub Dark",
    "slate": "Slate",
    "twilight": "Twilight",
    "warm_gray": "Warm Gray",
    "mist": "Mist",
}



def _scaled_stylesheet(style: str, zoom_percent: int) -> str:
    if zoom_percent == 100:
        return style

    def scale(match) -> str:
        value = float(match.group(1)) * zoom_percent / 100
        formatted = str(int(value)) if value.is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}{match.group(2)}"

    return re.sub(r"(\d+(?:\.\d+)?)(px|pt)", scale, style)



def apply_theme(app: QApplication, theme: str = "dark", zoom_percent: int = 100) -> None:
    if theme == "light":
        app.setStyleSheet(_scaled_stylesheet("""
            QWidget { background: #f4f7fb; color: #1d2733; font-family: 'Segoe UI'; font-size: 10pt; }
            QMainWindow { background: #f4f7fb; }
            #leftToolbar { background: #ffffff; border: 0; border-right: 1px solid #d8e0ea; spacing: 6px; padding: 8px 6px; }
            #toolbarButton { background: #e7edf4; color: #334155; border: 1px solid #c3d0dd; margin: 3px 0; padding: 8px 10px; }
            #toolbarButton:hover, #toolbarButton:checked { background: #cfe5fa; color: #174f7b; }
            #title { font-size: 28pt; font-weight: 700; color: #17202b; }
            #subtitle { color: #647184; font-size: 11pt; }
            #statusPill { background: #e4f0fc; color: #21639a; border: 1px solid #b7d5ef; border-radius: 14px; padding: 7px 14px; }
            #zoomIndicator { background: #17202b; color: #ffffff; border-radius: 12px; padding: 8px 14px; font-size: 12pt; font-weight: 600; }
            #card { background: #ffffff; border: 1px solid #d8e0ea; border-radius: 10px; }
            #comparisonPanel, #resultPanel { background: #ffffff; border: 1px solid #d8e0ea; border-radius: 10px; }
            #panelName, #resultName { color: #1d2733; font-weight: 600; }
            #modifiedIndicator { color: #9a3412; background: #ffedd5; border: 1px solid #fed7aa; border-radius: 8px; padding: 2px 7px; font-size: 8pt; }
            #smallButton, #addPanelButton, #removePanelButton { background: #e7edf4; color: #334155; padding: 6px 10px; }
            #smallButton:hover, #addPanelButton:hover, #removePanelButton:hover { background: #d7e1ec; }
            #emptyState { color: #647184; padding: 20px; }
            #pathLabel { color: #526174; min-width: 82px; }
            QLineEdit, QPlainTextEdit, QTreeWidget { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 7px; padding: 8px; selection-background-color: #cfe5fa; selection-color: #17202b; }
            QLineEdit:focus, QPlainTextEdit:focus, QTreeWidget:focus { border: 1px solid #4d9bd6; }
            QPushButton { border: 0; border-radius: 7px; padding: 9px 16px; font-weight: 600; }
            #primaryButton { background: #277ac0; color: white; }
            #primaryButton:hover { background: #398dd4; }
            #secondaryButton, #settingsButton, #infoButton { background: #e7edf4; color: #334155; padding: 8px 14px; }
            #secondaryButton:hover, #settingsButton:hover, #infoButton:hover { background: #d7e1ec; }
            #aboutTitle { font-size: 16pt; font-weight: 700; color: #17202b; }
            QPushButton:checked { background: #cfe5fa; color: #174f7b; }
            #settingsNote { color: #647184; }
            QTabWidget::pane { border: 0; }
            QTabBar::tab { background: #edf1f6; color: #526174; border: 1px solid #d8e0ea; border-bottom: 3px solid transparent; border-top-left-radius: 7px; border-top-right-radius: 7px; padding: 10px 14px; margin-right: 4px; }
            QTabBar::tab:hover:!selected { background: #e4f0fc; color: #21639a; }
            QTabBar::tab:selected { background: #277ac0; color: #ffffff; border: 1px solid #277ac0; border-bottom: 3px solid #174f7b; font-weight: 700; }
            QHeaderView::section { background: #ffffff; color: #526174; border: 0; border-bottom: 1px solid #d8e0ea; padding: 9px; }
            QTreeWidget { padding: 4px; }
            QTreeWidget::item { padding: 7px 4px; }
            QTreeWidget::item:selected { background: #d9ebfa; }
            QScrollBar:vertical { background: #edf1f6; width: 10px; margin: 2px; }
            QScrollBar::handle:vertical { background: #b5c4d5; border-radius: 5px; min-height: 25px; }
        """, zoom_percent))
        return
    style = """
        QWidget { background: #10151d; color: #e8edf5; font-family: 'Segoe UI'; font-size: 10pt; }
        QMainWindow { background: #10151d; }
        #leftToolbar { background: #171e28; border: 0; border-right: 1px solid #273342; spacing: 6px; padding: 8px 6px; }
        #toolbarButton { background: #263342; color: #d4dce7; border: 1px solid #405269; margin: 3px 0; padding: 8px 10px; }
        #toolbarButton:hover, #toolbarButton:checked { background: #28547a; color: #ffffff; }
        #title { font-size: 28pt; font-weight: 700; color: #f5f7fb; }
        #subtitle { color: #8d98aa; font-size: 11pt; }
        #statusPill { background: #1a2c3e; color: #9ecbff; border: 1px solid #294a68; border-radius: 14px; padding: 7px 14px; }
        #zoomIndicator { background: #f5f7fb; color: #10151d; border-radius: 12px; padding: 8px 14px; font-size: 12pt; font-weight: 600; }
        #card { background: #171e28; border: 1px solid #273342; border-radius: 10px; }
        #comparisonPanel, #resultPanel { background: #171e28; border: 1px solid #273342; border-radius: 10px; }
        #panelName, #resultName { color: #e8edf5; font-weight: 600; }
        #modifiedIndicator { color: #f3c969; background: #3d3017; border: 1px solid #6b5222; border-radius: 8px; padding: 2px 7px; font-size: 8pt; }
        #smallButton, #addPanelButton, #removePanelButton { background: #263342; color: #d4dce7; padding: 6px 10px; }
        #smallButton:hover, #addPanelButton:hover, #removePanelButton:hover { background: #334458; }
        #emptyState { color: #8d98aa; padding: 20px; }
        #pathLabel { color: #aeb8c7; min-width: 82px; }
        QLineEdit, QPlainTextEdit, QTreeWidget { background: #0d1219; border: 1px solid #2a3747; border-radius: 7px; padding: 8px; selection-background-color: #28547a; }
        QLineEdit:focus, QPlainTextEdit:focus, QTreeWidget:focus { border: 1px solid #5ca8e6; }
        QPushButton { border: 0; border-radius: 7px; padding: 9px 16px; font-weight: 600; }
        #primaryButton { background: #3b82d0; color: white; }
        #primaryButton:hover { background: #5299e5; }
        #secondaryButton, #settingsButton, #infoButton { background: #263342; color: #d4dce7; padding: 8px 14px; }
        #secondaryButton:hover, #settingsButton:hover, #infoButton:hover { background: #334458; }
        #aboutTitle { font-size: 16pt; font-weight: 700; color: #f5f7fb; }
        #settingsNote { color: #8d98aa; }
        QPushButton:checked { background: #28547a; color: #ffffff; }
        QTabWidget::pane { border: 0; }
        QTabBar::tab { background: #1a2c3e; color: #aeb8c7; border: 1px solid #405269; border-bottom: 3px solid transparent; border-top-left-radius: 7px; border-top-right-radius: 7px; padding: 10px 14px; margin-right: 4px; }
        QTabBar::tab:hover:!selected { background: #263342; color: #f5f7fb; }
        QTabBar::tab:selected { background: #28547a; color: #ffffff; border: 1px solid #5ca8e6; border-bottom: 3px solid #5ca8e6; font-weight: 700; }
        QHeaderView::section { background: #171e28; color: #aeb8c7; border: 0; border-bottom: 1px solid #2a3747; padding: 9px; }
        QTreeWidget { padding: 4px; }
        QTreeWidget::item { padding: 7px 4px; }
        QTreeWidget::item:selected { background: #1e4568; }
        QScrollBar:vertical { background: #111821; width: 10px; margin: 2px; }
        QScrollBar::handle:vertical { background: #34465a; border-radius: 5px; min-height: 25px; }
    """
    palettes = {
        "midnight": {
            "#10151d": "#080b14", "#171e28": "#101525", "#0d1219": "#060912",
        },

        "dracula": {
            "#10151d": "#282a36", "#171e28": "#343746", "#0d1219": "#21222c",
            "#e8edf5": "#f8f8f2", "#f5f7fb": "#ffffff", "#8d98aa": "#b8b8c8",
            "#3b82d0": "#bd93f9", "#5299e5": "#caa8ff", "#5ca8e6": "#ff79c6",
            "#28547a": "#493866", "#263342": "#44475a", "#334458": "#565a72",
        },

        "nord": {
            "#10151d": "#2e3440", "#171e28": "#3b4252", "#0d1219": "#242933",
            "#e8edf5": "#eceff4", "#f5f7fb": "#ffffff", "#8d98aa": "#a7b0c0",
            "#3b82d0": "#88c0d0", "#5299e5": "#8fbcbb", "#5ca8e6": "#88c0d0",
            "#28547a": "#40566b", "#263342": "#434c5e", "#334458": "#4c566a",
        },

        "solarized": {
            "#10151d": "#002b36", "#171e28": "#073642", "#0d1219": "#00212b",
            "#e8edf5": "#eee8d5", "#f5f7fb": "#fdf6e3", "#8d98aa": "#93a1a1",
            "#3b82d0": "#2aa198", "#5299e5": "#35bcb2", "#5ca8e6": "#2aa198",
            "#28547a": "#145965", "#263342": "#264b53", "#334458": "#38656d",
        },

        "tokyo_night": {
            "#10151d": "#1a1b26", "#171e28": "#24283b", "#0d1219": "#16161e",
            "#e8edf5": "#c0caf5", "#f5f7fb": "#d5d6db", "#8d98aa": "#9aa5ce",
            "#3b82d0": "#7aa2f7", "#5299e5": "#7dcfff", "#5ca8e6": "#bb9af7",
            "#28547a": "#3d59a1", "#263342": "#292e42", "#334458": "#3b4261",
        },

        "catppuccin_mocha": {
            "#10151d": "#1e1e2e", "#171e28": "#313244", "#0d1219": "#181825",
            "#e8edf5": "#cdd6f4", "#f5f7fb": "#ffffff", "#8d98aa": "#a6adc8",
            "#3b82d0": "#89b4fa", "#5299e5": "#74c7ec", "#5ca8e6": "#cba6f7",
            "#28547a": "#45475a", "#263342": "#313244", "#334458": "#585b70",
        },

        "gruvbox": {
            "#10151d": "#282828", "#171e28": "#3c3836", "#0d1219": "#1d2021",
            "#e8edf5": "#ebdbb2", "#f5f7fb": "#fbf1c7", "#8d98aa": "#a89984",
            "#3b82d0": "#458588", "#5299e5": "#83a598", "#5ca8e6": "#d3869b",
            "#28547a": "#504945", "#263342": "#3c3836", "#334458": "#665c54",
        },

        "one_dark": {
            "#10151d": "#282c34", "#171e28": "#353b45", "#0d1219": "#21252b",
            "#e8edf5": "#abb2bf", "#f5f7fb": "#d7dae0", "#8d98aa": "#828997",
            "#3b82d0": "#61afef", "#5299e5": "#56b6c2", "#5ca8e6": "#c678dd",
            "#28547a": "#3b536b", "#263342": "#353b45", "#334458": "#4b5263",
        },

        "monokai": {
            "#10151d": "#272822", "#171e28": "#34352f", "#0d1219": "#1e1f1c",
            "#e8edf5": "#f8f8f2", "#f5f7fb": "#ffffff", "#8d98aa": "#a6a69c",
            "#3b82d0": "#66d9ef", "#5299e5": "#a6e22e", "#5ca8e6": "#f92672",
            "#28547a": "#3f5960", "#263342": "#3a3b35", "#334458": "#57584f",
        },

        "rose_pine": {
            "#10151d": "#191724", "#171e28": "#26233a", "#0d1219": "#13111c",
            "#e8edf5": "#e0def4", "#f5f7fb": "#f4f2ff", "#8d98aa": "#908caa",
            "#3b82d0": "#31748f", "#5299e5": "#9ccfd8", "#5ca8e6": "#c4a7e7",
            "#28547a": "#403d52", "#263342": "#26233a", "#334458": "#524f67",
        },

        "everforest": {
            "#10151d": "#2d353b", "#171e28": "#343f44", "#0d1219": "#232a2e",
            "#e8edf5": "#d3c6aa", "#f5f7fb": "#e7ddc5", "#8d98aa": "#9da9a0",
            "#3b82d0": "#7fbbb3", "#5299e5": "#83c092", "#5ca8e6": "#d699b6",
            "#28547a": "#425a5b", "#263342": "#3d484d", "#334458": "#4f585e",
        },

        "ayu_dark": {
            "#10151d": "#0f1419", "#171e28": "#172029", "#0d1219": "#0b0f14",
            "#e8edf5": "#e6e1cf", "#f5f7fb": "#fff8e7", "#8d98aa": "#8a9199",
            "#3b82d0": "#59c2ff", "#5299e5": "#73d0ff", "#5ca8e6": "#d2a6ff",
            "#28547a": "#294b62", "#263342": "#1f2c36", "#334458": "#334552",
        },

        "material_ocean": {
            "#10151d": "#0f111a", "#171e28": "#1a1c25", "#0d1219": "#090b10",
            "#e8edf5": "#a6accd", "#f5f7fb": "#ffffff", "#8d98aa": "#717cb4",
            "#3b82d0": "#82aaff", "#5299e5": "#89ddff", "#5ca8e6": "#c792ea",
            "#28547a": "#314665", "#263342": "#1f2230", "#334458": "#32364a",
        },

        "github_dark": {
            "#10151d": "#0d1117", "#171e28": "#161b22", "#0d1219": "#090c10",
            "#e8edf5": "#c9d1d9", "#f5f7fb": "#f0f6fc", "#8d98aa": "#8b949e",
            "#3b82d0": "#58a6ff", "#5299e5": "#79c0ff", "#5ca8e6": "#d2a8ff",
            "#28547a": "#24486d", "#263342": "#21262d", "#334458": "#30363d",
        },
        "slate": {
            "#10151d": "#374151", "#171e28": "#4b5563", "#0d1219": "#303846",
            "#e8edf5": "#f3f4f6", "#f5f7fb": "#ffffff", "#8d98aa": "#c5cbd4",
            "#3b82d0": "#60a5fa", "#5299e5": "#7dd3fc", "#5ca8e6": "#a78bfa",
            "#28547a": "#526b85", "#263342": "#4b5563", "#334458": "#64748b",
        },

        "twilight": {
            "#10151d": "#3b3f52", "#171e28": "#4a5066", "#0d1219": "#323647",
            "#e8edf5": "#f0f1f5", "#f5f7fb": "#ffffff", "#8d98aa": "#b9bdca",
            "#3b82d0": "#78a9ff", "#5299e5": "#8bd5ca", "#5ca8e6": "#c6a0f6",
            "#28547a": "#596984", "#263342": "#4a5066", "#334458": "#626981",
        },

        "warm_gray": {
            "#10151d": "#464340", "#171e28": "#57534e", "#0d1219": "#3b3936",
            "#e8edf5": "#f5f3ef", "#f5f7fb": "#fffdf9", "#8d98aa": "#c8c2b9",
            "#3b82d0": "#6d9bd2", "#5299e5": "#82b5c9", "#5ca8e6": "#b59ad6",
            "#28547a": "#65707c", "#263342": "#57534e", "#334458": "#706b65",
        },

        "mist": {
            "#10151d": "#525b66", "#171e28": "#65707d", "#0d1219": "#454d57",
            "#e8edf5": "#f5f7fa", "#f5f7fb": "#ffffff", "#8d98aa": "#d0d5dc",
            "#3b82d0": "#6ea8e5", "#5299e5": "#83bcd8", "#5ca8e6": "#a89bd8",
            "#28547a": "#677f98", "#263342": "#65707d", "#334458": "#7b8794",
        },
    }
    for old, new in palettes.get(theme, {}).items():
        style = style.replace(old, new)
    scaled = _scaled_stylesheet(style, zoom_percent)
    if app.styleSheet() != scaled:
        app.setStyleSheet(scaled)

