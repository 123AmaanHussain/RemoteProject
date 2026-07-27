"""
ui/remote_viewport.py — Remote Desktop Viewport Widget
========================================================
Displays the live screen feed from the Controlled PC.
Captures and translates all mouse and keyboard events for transmission.

Features:
- Renders incoming JPEG/WebP binary frames as QImages at up to MAX_RENDER_FPS.
- Translates local mouse position (in viewport pixels) to normalised [0–1]
  coordinates, which are then mapped to absolute target monitor pixels on
  the Controlled PC side.
- Captures all keyboard events (both press and release) using Qt event
  override methods.
- F11 toggles between windowed and borderless full-screen modes.
- Scales the remote frame to fill the viewport while preserving aspect ratio.
- Overlays a latency/FPS counter in debug mode.
"""

from __future__ import annotations

import time
import logging
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, QRect, pyqtSignal
from PyQt6.QtGui import (
    QImage, QPixmap, QPainter, QColor, QFont, QCursor,
    QMouseEvent, QKeyEvent, QWheelEvent,
)
from PyQt6.QtWidgets import QWidget, QApplication, QSizePolicy

from config import MAX_RENDER_FPS

log = logging.getLogger(__name__)

# ─── Key Serialisation ────────────────────────────────────────────────────────

# Maps Qt.Key enum values to the string names the server/controlled PC expects.
_QT_KEY_MAP: dict[int, str] = {
    Qt.Key.Key_Return.value:    "enter",
    Qt.Key.Key_Enter.value:     "enter",
    Qt.Key.Key_Tab.value:       "tab",
    Qt.Key.Key_Space.value:     "space",
    Qt.Key.Key_Backspace.value: "backspace",
    Qt.Key.Key_Delete.value:    "delete",
    Qt.Key.Key_Escape.value:    "escape",
    Qt.Key.Key_Control.value:   "ctrl",
    Qt.Key.Key_Alt.value:       "alt",
    Qt.Key.Key_Shift.value:     "shift",
    Qt.Key.Key_Meta.value:      "super",
    Qt.Key.Key_Up.value:        "up",
    Qt.Key.Key_Down.value:      "down",
    Qt.Key.Key_Left.value:      "left",
    Qt.Key.Key_Right.value:     "right",
    Qt.Key.Key_Home.value:      "home",
    Qt.Key.Key_End.value:       "end",
    Qt.Key.Key_PageUp.value:    "page_up",
    Qt.Key.Key_PageDown.value:  "page_down",
    Qt.Key.Key_Insert.value:    "insert",
    Qt.Key.Key_F1.value:  "f1",  Qt.Key.Key_F2.value:  "f2",
    Qt.Key.Key_F3.value:  "f3",  Qt.Key.Key_F4.value:  "f4",
    Qt.Key.Key_F5.value:  "f5",  Qt.Key.Key_F6.value:  "f6",
    Qt.Key.Key_F7.value:  "f7",  Qt.Key.Key_F8.value:  "f8",
    Qt.Key.Key_F9.value:  "f9",  Qt.Key.Key_F10.value: "f10",
    Qt.Key.Key_F11.value: "f11", Qt.Key.Key_F12.value: "f12",
}

_MOUSE_BUTTON_MAP: dict[Qt.MouseButton, str] = {
    Qt.MouseButton.LeftButton:   "left",
    Qt.MouseButton.RightButton:  "right",
    Qt.MouseButton.MiddleButton: "middle",
}


class RemoteViewport(QWidget):
    """
    The remote desktop display and input capture widget.

    Signals:
        input_event(dict): Emitted for every mouse/keyboard event to be sent.
        frame_received():  Emitted when a new frame has been rendered.
    """

    input_event: pyqtSignal = pyqtSignal(dict)
    frame_received: pyqtSignal = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._pixmap:       Optional[QPixmap] = None
        self._frame_rect:   QRect = QRect()          # Rendered frame rect (aspect-preserved)
        self._is_fullscreen: bool = False

        # Frame rate limiter
        self._last_render_time: float = 0.0
        self._render_interval:  float = 1.0 / MAX_RENDER_FPS

        # FPS tracking
        self._frame_count:    int   = 0
        self._fps_window_start: float = time.monotonic()
        self._current_fps:    float = 0.0

        # Remote monitor dimensions (updated from frame metadata header)
        self._remote_width:  int = 1920
        self._remote_height: int = 1080

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(640, 360)
        self.setStyleSheet("background-color: #0d1117;")

        # Capture cursor changes
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    # ─── Public API ───────────────────────────────────────────────────────────

    def ingest_frame(self, frame_bytes: bytes) -> None:
        """
        Receives a compressed JPEG/WebP frame from the network layer and
        schedules a repaint. Rate-limited to MAX_RENDER_FPS.

        Args:
            frame_bytes: Raw JPEG or WebP encoded bytes from the Controlled PC.
        """
        now = time.monotonic()
        if now - self._last_render_time < self._render_interval:
            return  # Drop frame — rendering too fast

        self._last_render_time = now

        # Decode bytes to QImage
        image = QImage()
        loaded = image.loadFromData(frame_bytes)
        if not loaded or image.isNull():
            log.warning("Failed to decode incoming frame — skipping")
            return

        self._pixmap = QPixmap.fromImage(image)

        # FPS counter update
        self._frame_count += 1
        elapsed = now - self._fps_window_start
        if elapsed >= 1.0:
            self._current_fps     = self._frame_count / elapsed
            self._frame_count     = 0
            self._fps_window_start = now

        self.update()  # Triggers paintEvent on the GUI thread
        self.frame_received.emit()

    def update_remote_dimensions(self, width: int, height: int) -> None:
        """Called when the remote monitor resolution is known/changes."""
        self._remote_width  = max(1, width)
        self._remote_height = max(1, height)

    def toggle_fullscreen(self) -> None:
        """Toggles between windowed and borderless full-screen mode."""
        window = self.window()
        if self._is_fullscreen:
            window.setWindowFlags(
                window.windowFlags() & ~Qt.WindowType.FramelessWindowHint
            )
            window.showNormal()
            window.show()
            self._is_fullscreen = False
        else:
            window.setWindowFlags(
                window.windowFlags() | Qt.WindowType.FramelessWindowHint
            )
            window.showFullScreen()
            window.show()
            self._is_fullscreen = True

    # ─── Painting ────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w = self.width()
        h = self.height()

        if self._pixmap is None:
            # Draw placeholder
            painter.fillRect(0, 0, w, h, QColor("#0d1117"))
            painter.setPen(QColor("#2d3748"))
            painter.setFont(QFont("Segoe UI", 14))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Waiting for remote stream…",
            )
            return

        # Scale pixmap to fit viewport while preserving aspect ratio
        scaled = self._pixmap.scaled(
            w, h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x_off = (w - scaled.width())  // 2
        y_off = (h - scaled.height()) // 2

        self._frame_rect = QRect(x_off, y_off, scaled.width(), scaled.height())

        painter.fillRect(0, 0, w, h, QColor("#0d1117"))
        painter.drawPixmap(x_off, y_off, scaled)

        # FPS overlay (top-left corner)
        painter.setPen(QColor("#3ddc84"))
        painter.setFont(QFont("Consolas", 10))
        painter.drawText(
            x_off + 8, y_off + 18,
            f"{self._current_fps:.1f} FPS",
        )

    # ─── Coordinate Translation ───────────────────────────────────────────────

    def _viewport_to_normalised(self, vx: int, vy: int) -> tuple[float, float] | None:
        """
        Converts a viewport pixel position to normalised [0.0–1.0] coordinates.
        Returns None if the position is outside the rendered frame area.
        """
        fr = self._frame_rect
        if fr.isEmpty():
            return None

        rx = vx - fr.left()
        ry = vy - fr.top()

        if rx < 0 or ry < 0 or rx >= fr.width() or ry >= fr.height():
            return None  # Outside frame area

        nx = rx / fr.width()
        ny = ry / fr.height()
        return nx, ny

    # ─── Mouse Events ────────────────────────────────────────────────────────

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        coords = self._viewport_to_normalised(event.position().x(), event.position().y())
        if coords:
            self.input_event.emit({
                "kind": "mouse_move",
                "nx":   round(coords[0], 5),
                "ny":   round(coords[1], 5),
            })

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus()
        coords = self._viewport_to_normalised(event.position().x(), event.position().y())
        button = _MOUSE_BUTTON_MAP.get(event.button(), "left")
        if coords:
            self.input_event.emit({
                "kind":    "mouse_click",
                "nx":      round(coords[0], 5),
                "ny":      round(coords[1], 5),
                "button":  button,
                "pressed": True,
            })

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        coords = self._viewport_to_normalised(event.position().x(), event.position().y())
        button = _MOUSE_BUTTON_MAP.get(event.button(), "left")
        if coords:
            self.input_event.emit({
                "kind":    "mouse_click",
                "nx":      round(coords[0], 5),
                "ny":      round(coords[1], 5),
                "button":  button,
                "pressed": False,
            })

    def wheelEvent(self, event: QWheelEvent) -> None:
        coords = self._viewport_to_normalised(
            event.position().x(), event.position().y()
        )
        if coords:
            delta = event.angleDelta()
            self.input_event.emit({
                "kind": "mouse_scroll",
                "nx":   round(coords[0], 5),
                "ny":   round(coords[1], 5),
                "dx":   delta.x() // 120,
                "dy":   delta.y() // 120,
            })

    # ─── Keyboard Events ─────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return  # Suppress auto-repeat to avoid event flooding

        key_val = event.key()

        # F11 → toggle fullscreen (local action, not forwarded)
        if key_val == Qt.Key.Key_F11.value:
            self.toggle_fullscreen()
            return

        key_str = self._resolve_key(event)
        if key_str:
            self.input_event.emit({"kind": "key_press", "key": key_str})

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return

        key_val = event.key()
        if key_val == Qt.Key.Key_F11.value:
            return

        key_str = self._resolve_key(event)
        if key_str:
            self.input_event.emit({"kind": "key_release", "key": key_str})

    def _resolve_key(self, event: QKeyEvent) -> str | None:
        """
        Converts a QKeyEvent to the string representation used by the
        Controlled PC input handler.
        """
        key_val = event.key()

        # Check special keys first
        if key_val in _QT_KEY_MAP:
            return _QT_KEY_MAP[key_val]

        # For printable characters, use the text representation
        text = event.text()
        if text and len(text) == 1 and text.isprintable():
            return text

        log.debug("Unmapped key: %d — skipping", key_val)
        return None
