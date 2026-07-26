"""
ui/toast.py — Toast Notification Widget
========================================
A lightweight, auto-dismissing toast notification that appears in the
bottom-right corner of the parent window (or screen). Used for transient
status messages that don't require user interaction.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtWidgets import QLabel, QWidget, QApplication
from PyQt6.QtGui import QColor


class ToastNotification(QLabel):
    """
    A transient, floating label that fades in, displays a message, then
    fades out and destroys itself after a timeout.

    Args:
        message:    The text to display.
        parent:     Optional parent widget; if None, the toast floats on screen.
        duration_ms: Time in milliseconds before the toast begins dismissing.
        level:      'info' | 'warning' | 'error' — determines background color.
    """

    LEVEL_COLORS = {
        "info":    ("rgba(30, 40, 60, 220)",  "#8bbcff"),
        "warning": ("rgba(80, 55, 10, 230)",  "#ffc866"),
        "error":   ("rgba(80, 20, 20, 230)",  "#ff8080"),
    }

    def __init__(
        self,
        message: str,
        parent: QWidget | None = None,
        duration_ms: int = 4000,
        level: str = "info",
    ) -> None:
        super().__init__(message, parent)

        bg, fg = self.LEVEL_COLORS.get(level, self.LEVEL_COLORS["info"])

        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setStyleSheet(f"""
            QLabel {{
                background-color: {bg};
                color: {fg};
                border-radius: 8px;
                padding: 12px 18px;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 13px;
                font-weight: 500;
                border: 1px solid rgba(255,255,255,0.12);
            }}
        """)

        # Size to content
        self.adjustSize()
        self.setMaximumWidth(420)
        self.adjustSize()

        # Position in bottom-right of screen
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = geom.right()  - self.width()  - 24
            y = geom.bottom() - self.height() - 60
            self.move(x, y)

        # Opacity animation
        self._opacity: float = 0.0
        self._anim_in  = QPropertyAnimation(self, b"_opacity_prop", self)
        self._anim_out = QPropertyAnimation(self, b"_opacity_prop", self)

        self._anim_in.setDuration(300)
        self._anim_in.setStartValue(0.0)
        self._anim_in.setEndValue(1.0)
        self._anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._anim_out.setDuration(400)
        self._anim_out.setStartValue(1.0)
        self._anim_out.setEndValue(0.0)
        self._anim_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim_out.finished.connect(self.deleteLater)

        self.show()
        self._anim_in.start()

        # Auto-dismiss timer
        QTimer.singleShot(duration_ms, self._start_fade_out)

    def _start_fade_out(self) -> None:
        self._anim_out.start()

    # ── Opacity property (required for QPropertyAnimation) ────────────────

    def _get_opacity(self) -> float:
        return self._opacity

    def _set_opacity(self, value: float) -> None:
        self._opacity = value
        self.setWindowOpacity(value)

    _opacity_prop = pyqtProperty(float, _get_opacity, _set_opacity)


def show_toast(
    message: str,
    parent: QWidget | None = None,
    duration_ms: int = 4000,
    level: str = "info",
) -> ToastNotification:
    """
    Convenience factory that creates and shows a ToastNotification.

    Args:
        message:     The text to display.
        parent:      Parent widget (optional).
        duration_ms: Display duration before fade-out.
        level:       'info' | 'warning' | 'error'

    Returns:
        The created ToastNotification instance.
    """
    return ToastNotification(message, parent=parent, duration_ms=duration_ms, level=level)
