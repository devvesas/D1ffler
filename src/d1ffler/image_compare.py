"""Bounded, read-only image decoding and linked comparison views."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage, QImageReader, QPainter, QPixmap
from PyQt6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QLabel, QVBoxLayout, QWidget


MAX_IMAGE_FILE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 8_000_000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jpe", ".gif", ".bmp", ".dib",
                  ".webp", ".tif", ".tiff", ".ico", ".svg", ".svgz",
                  ".heic", ".heif", ".avif"}


@dataclass(frozen=True)
class ImageResult:
    image: QImage | None
    format: str = ""
    error: str = ""

    @property
    def details(self) -> str:
        if self.image is None:
            return self.error
        return f"{self.image.width():,} × {self.image.height():,} px · {self.format.upper()}"


@lru_cache(maxsize=1)
def supported_image_suffixes() -> frozenset[str]:
    return frozenset(IMAGE_SUFFIXES | {"." + bytes(value).decode("ascii", "ignore").lower()
                                     for value in QImageReader.supportedImageFormats()})


def is_image_source(source) -> bool:
    return source is not None and Path(source.name).suffix.lower() in supported_image_suffixes()


def decode_image(source) -> ImageResult:
    """Read and decode a filesystem file or ArchiveMember within explicit limits."""
    try:
        before = source.stat()
        size = before.st_size
        if size > MAX_IMAGE_FILE_BYTES:
            return ImageResult(None, error=f"Image exceeds the {MAX_IMAGE_FILE_BYTES // 1048576} MiB read limit.")
        with source.open("rb") as stream:
            data = stream.read(MAX_IMAGE_FILE_BYTES + 1)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return ImageResult(None, error="Image changed while reading; refresh the comparison.")
        if len(data) > MAX_IMAGE_FILE_BYTES:
            return ImageResult(None, error="Image exceeds the read limit.")
        buffer = QBuffer()
        buffer.setData(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        if not reader.canRead():
            return ImageResult(None, error=reader.errorString() or "Unsupported or corrupt image.")
        dimensions = reader.size()
        if not dimensions.isValid() or dimensions.width() * dimensions.height() > MAX_IMAGE_PIXELS:
            return ImageResult(None, error=f"Image dimensions exceed the {MAX_IMAGE_PIXELS:,} pixel limit or are invalid.")
        image_format = bytes(reader.format()).decode("ascii", "replace") or "image"
        image = reader.read()
        if image.isNull():
            return ImageResult(None, error=reader.errorString() or "Could not decode image.")
        if image.width() * image.height() > MAX_IMAGE_PIXELS:
            return ImageResult(None, error="Transformed image exceeds the pixel limit.")
        return ImageResult(image, image_format)
    except (OSError, ValueError) as exc:
        return ImageResult(None, error=str(exc))


def difference_image(left: QImage, right: QImage) -> QImage | None:
    if left.size() != right.size():
        return None
    # Flatten alpha against white so transparency changes remain visible.
    def opaque(image: QImage) -> QImage:
        result = QImage(image.size(), QImage.Format.Format_RGB32)
        result.fill(Qt.GlobalColor.white)
        painter = QPainter(result)
        painter.drawImage(0, 0, image)
        painter.end()
        return result

    result = opaque(left)
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
    painter.drawImage(0, 0, opaque(right))
    painter.end()
    return result


class ImageDecodeWorker:
    def __init__(self, sources, cancel_event, messages):
        self.sources = sources
        self.cancel_event = cancel_event
        self.messages = messages

    def run(self) -> None:
        results = []
        for source in self.sources:
            if self.cancel_event.is_set():
                break
            results.append(decode_image(source) if source is not None else ImageResult(None, error="No image in this panel."))
        self.messages.put(("finished", results if len(results) == len(self.sources) else None))


class ImageView(QGraphicsView):
    def __init__(self, zoom_request, scroll_changed, parent=None):
        super().__init__(parent)
        self.zoom_request = zoom_request
        self.drop_paths_callback = None
        self.activate_callback = None
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.scene_object = QGraphicsScene(self)
        self.setScene(self.scene_object)
        self.base_item = QGraphicsPixmapItem()
        self.overlay_item = QGraphicsPixmapItem()
        self.scene_object.addItem(self.base_item)
        self.scene_object.addItem(self.overlay_item)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.horizontalScrollBar().valueChanged.connect(scroll_changed)
        self.verticalScrollBar().valueChanged.connect(scroll_changed)

    def set_images(self, base: QImage | None, overlay: QImage | None = None, opacity: float = 0.5) -> None:
        self.base_item.setPixmap(QPixmap.fromImage(base) if base is not None else QPixmap())
        self.overlay_item.setPixmap(QPixmap.fromImage(overlay) if overlay is not None else QPixmap())
        self.overlay_item.setOpacity(opacity)
        self.scene_object.setSceneRect(self.scene_object.itemsBoundingRect())

    def set_zoom(self, value: int) -> None:
        self.resetTransform()
        self.scale(value / 100, value / 100)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_request(10 if event.angleDelta().y() > 0 else -10)
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        if self.activate_callback is not None:
            self.activate_callback()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths and self.drop_paths_callback is not None:
            self.drop_paths_callback(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class ImagePane(QWidget):
    def __init__(self, zoom_request, scroll_changed, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.details = QLabel()
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        self.view = ImageView(zoom_request, scroll_changed)
        layout.addWidget(self.view, 1)
