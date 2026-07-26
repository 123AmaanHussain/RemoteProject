"""
ui/main_window.py — Controller Application Main Window
=======================================================
The primary GUI window for the Remote Desktop Controller.

Layout:
  ┌─────────────────────────────────────────────────┐
  │  [Header] Remote Desktop Controller   [Status] │
  ├─────────────────────────────────────────────────┤
  │  [Code Entry Panel]                            │  ← visible before connection
  │    ┌───────────────┐    ┌──────────┐           │
  │    │  6-digit code │    │ Connect  │           │
  │    └───────────────┘    └──────────┘           │
  │    Status: Waiting for target user approval…   │
  ├─────────────────────────────────────────────────┤
  │  [Remote Viewport]                             │  ← visible after connection
  └─────────────────────────────────────────────────┘

State transitions triggered by network signals update the visible panel and
status bar text.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, pyqtSlot, QTimer,
)
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QAction, QKeySequence,
    QPalette, QIcon,
)
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSizePolicy, QStackedWidget, QStatusBar,
    QVBoxLayout, QWidget, QFrame,
)

from ui.remote_viewport import RemoteViewport
from ui.toast import show_toast
import config

log = logging.getLogger(__name__)


# ─── Stylesheet ───────────────────────────────────────────────────────────────

DARK_STYLE = """
/* ── Base ───────────────────────────────────────────── */
QMainWindow, QWidget {
    background-color: #0d1117;
    color: #c9d1d9;
    font-family: 'Segoe UI', 'Inter', Arial, sans-serif;
    font-size: 13px;
}

/* ── Header bar ─────────────────────────────────────── */
#HeaderBar {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #141b27, stop:1 #0d1117);
    border-bottom: 1px solid #21262d;
    min-height: 52px;
}
#AppTitle {
    color: #58a6ff;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 0.3px;
}
#StatusDot {
    min-width: 10px; max-width: 10px;
    min-height: 10px; max-height: 10px;
    border-radius: 5px;
    background-color: #3d4451;
}
#StatusDot[status="active"]       { background-color: #3ddc84; }
#StatusDot[status="waiting"]      { background-color: #f0a05a; }
#StatusDot[status="disconnected"] { background-color: #ff6b6b; }

/* ── Code Entry Panel ────────────────────────────────── */
#ConnectPanel {
    background-color: #161b22;
    border-radius: 12px;
    border: 1px solid #21262d;
}
#PanelTitle {
    color: #e6edf3;
    font-size: 20px;
    font-weight: 600;
}
#PanelSubtitle {
    color: #8b949e;
    font-size: 13px;
}
#CodeInput {
    background-color: #0d1117;
    border: 2px solid #30363d;
    border-radius: 8px;
    color: #e6edf3;
    font-size: 28px;
    font-weight: 700;
    letter-spacing: 6px;
    padding: 10px 20px;
    min-width: 200px;
    max-width: 260px;
    qproperty-alignment: AlignCenter;
}
#CodeInput:focus {
    border-color: #388bfd;
}
#ConnectBtn {
    background-color: #238636;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    padding: 12px 28px;
    min-width: 120px;
}
#ConnectBtn:hover   { background-color: #2ea043; }
#ConnectBtn:pressed { background-color: #1a7f37; }
#ConnectBtn:disabled {
    background-color: #21262d;
    color: #484f58;
}
#StatusLabel {
    color: #8b949e;
    font-size: 12px;
    font-style: italic;
}

/* ── Disconnect button ───────────────────────────────── */
#DisconnectBtn {
    background-color: #da3633;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    padding: 6px 14px;
}
#DisconnectBtn:hover { background-color: #f85149; }

/* ── Status bar ─────────────────────────────────────── */
QStatusBar {
    background-color: #161b22;
    color: #8b949e;
    border-top: 1px solid #21262d;
    font-size: 11px;
    padding: 2px 8px;
}
"""


class MainWindow(QMainWindow):
    """
    The primary application window.

    Signals (consumed by the network worker):
        connect_requested(str):     Emitted when the user clicks Connect with a code.
        disconnect_requested():     Emitted when the user clicks Disconnect.
        input_ready(dict):          Forwarded input events from the viewport.
        clipboard_send(str):        Clipboard text to send to controlled PC.
    """

    connect_requested:    pyqtSignal = pyqtSignal(str)
    disconnect_requested: pyqtSignal = pyqtSignal()
    input_ready:          pyqtSignal = pyqtSignal(dict)
    clipboard_send:       pyqtSignal = pyqtSignal(str)

    def __init__(self, network_worker=None) -> None:
        super().__init__()

        self._network_worker = network_worker  # Store reference for direct calls

        self.setWindowTitle(config.WINDOW_TITLE)
        self.resize(1200, 750)
        self.setMinimumSize(800, 500)
        self.setStyleSheet(DARK_STYLE)

        self._build_ui()
        self._connect_signals()

    # ─── UI Construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Header bar
        root_layout.addWidget(self._build_header())

        # Stacked widget: page 0 = connect panel, page 1 = remote viewport
        self._stack = QStackedWidget()
        root_layout.addWidget(self._stack, stretch=1)

        self._connect_page  = self._build_connect_panel()
        self._viewport_page = self._build_viewport_page()

        self._stack.addWidget(self._connect_page)   # index 0
        self._stack.addWidget(self._viewport_page)  # index 1
        self._stack.setCurrentIndex(0)

        # Status bar
        self.statusBar().showMessage(
            f"  {config.APP_NAME} — Not connected   |   Press F11 in viewport for fullscreen"
        )

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 0, 18, 0)

        # App title
        title = QLabel(config.APP_NAME)
        title.setObjectName("AppTitle")
        layout.addWidget(title)

        layout.addStretch()

        # Status dot + label
        self._status_dot = QLabel()
        self._status_dot.setObjectName("StatusDot")
        self._status_dot.setProperty("status", "disconnected")
        layout.addWidget(self._status_dot)

        self._status_label_header = QLabel("Not Connected")
        self._status_label_header.setStyleSheet("color: #8b949e; font-size: 12px;")
        layout.addSpacing(6)
        layout.addWidget(self._status_label_header)

        layout.addSpacing(16)

        # Disconnect button (hidden until connected)
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setObjectName("DisconnectBtn")
        self._disconnect_btn.setVisible(False)
        layout.addWidget(self._disconnect_btn)

        return bar

    def _build_connect_panel(self) -> QWidget:
        """Builds the code-entry landing page."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.setContentsMargins(40, 40, 40, 40)

        panel = QFrame()
        panel.setObjectName("ConnectPanel")
        panel.setFixedWidth(480)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(36, 36, 36, 36)
        panel_layout.setSpacing(18)

        # Icon placeholder (text-based)
        icon_label = QLabel("🖥 →  🖱")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("font-size: 40px; padding: 8px 0;")
        panel_layout.addWidget(icon_label)

        # Title
        title = QLabel("Connect to Remote PC")
        title.setObjectName("PanelTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(title)

        # Subtitle
        sub = QLabel("Enter the 6-digit session code displayed on the target machine.")
        sub.setObjectName("PanelSubtitle")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        panel_layout.addWidget(sub)

        panel_layout.addSpacing(8)

        # Code input + connect button row
        row = QHBoxLayout()
        row.setSpacing(12)
        row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._code_input = QLineEdit()
        self._code_input.setObjectName("CodeInput")
        self._code_input.setPlaceholderText("000000")
        self._code_input.setMaxLength(6)
        self._code_input.setInputMask("999999")  # Digits only
        row.addWidget(self._code_input)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setObjectName("ConnectBtn")
        self._connect_btn.setDefault(True)
        row.addWidget(self._connect_btn)

        panel_layout.addLayout(row)

        # Status label
        self._status_label = QLabel("Enter code and click Connect.")
        self._status_label.setObjectName("StatusLabel")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(self._status_label)

        outer.addWidget(panel)
        return page

    def _build_viewport_page(self) -> QWidget:
        """Wraps the RemoteViewport in a page widget."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        self.viewport = RemoteViewport()
        layout.addWidget(self.viewport)
        return page

    def _connect_signals(self) -> None:
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._disconnect_btn.clicked.connect(self.disconnect_requested)
        self._code_input.returnPressed.connect(self._on_connect_clicked)
        self.viewport.input_event.connect(self.input_ready)

    # ─── Public Slots (called by NetworkWorker via signals) ───────────────────

    @pyqtSlot(str)
    def set_status(self, message: str) -> None:
        """Updates header status label and status bar."""
        self._status_label_header.setText(message)
        self.statusBar().showMessage(f"  {message}")
        self._status_label.setText(message)

    @pyqtSlot()
    def on_waiting_for_approval(self) -> None:
        self._set_status_dot("waiting")
        self.set_status("Waiting for target user approval…")
        self._connect_btn.setEnabled(False)
        show_toast(
            "Connection request sent.\nWaiting for the target user to approve.",
            level="info",
        )

    @pyqtSlot()
    def on_session_active(self) -> None:
        self._set_status_dot("active")
        self.set_status("🔴 Session Active")
        self._stack.setCurrentIndex(1)
        self._disconnect_btn.setVisible(True)
        self.viewport.setFocus()
        show_toast("Remote session established. Stream active.", level="info")

    @pyqtSlot(str)
    def on_session_denied(self, reason: str) -> None:
        self._reset_to_connect()
        self._set_status_dot("disconnected")
        self.set_status(f"Connection denied: {reason}")
        show_toast(f"Connection denied.\n{reason}", duration_ms=6000, level="warning")

    @pyqtSlot(str)
    def on_disconnected(self, reason: str) -> None:
        self._reset_to_connect()
        self._set_status_dot("disconnected")
        self.set_status(f"Disconnected: {reason}")
        show_toast(f"Disconnected: {reason}", level="warning")

    @pyqtSlot(str)
    def on_error(self, message: str) -> None:
        self._set_status_dot("disconnected")
        self.set_status(f"Error: {message}")
        show_toast(f"Error: {message}", duration_ms=8000, level="error")

    @pyqtSlot(bytes)
    def on_frame_received(self, frame_bytes: bytes) -> None:
        """Passes raw frame bytes to the viewport for rendering."""
        self.viewport.ingest_frame(frame_bytes)

    @pyqtSlot(str)
    def on_clipboard_received(self, text: str) -> None:
        """Writes remote clipboard text to local clipboard."""
        import pyperclip
        try:
            pyperclip.copy(text)
        except Exception as exc:
            log.warning("Could not write clipboard: %s", exc)

    # ─── Private Helpers ─────────────────────────────────────────────────────

    def _on_connect_clicked(self) -> None:
        code = self._code_input.text().strip()
        log.info("Connect button clicked. Code: '%s'", code)
        if len(code) != 6 or not code.isdigit():
            log.warning("Invalid code: '%s' (len=%d, isdigit=%s)", code, len(code), code.isdigit())
            show_toast("Please enter a valid 6-digit numeric code.", level="warning")
            self._code_input.setFocus()
            return
        log.info("Emitting connect_requested signal with code: %s", code)
        self.connect_requested.emit(code)
        # TEMPORARY: Direct call to bypass signal/slot issue (Python 3.13 compatibility)
        if self._network_worker:
            log.info("Directly calling connect_to_session on worker")
            self._network_worker.connect_to_session(code)

    def _reset_to_connect(self) -> None:
        self._stack.setCurrentIndex(0)
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setVisible(False)
        self._code_input.clear()
        self._code_input.setFocus()

    def _set_status_dot(self, status: str) -> None:
        """Updates the header status indicator dot colour."""
        self._status_dot.setProperty("status", status)
        # Force Qt to re-evaluate the stylesheet for the dynamic property
        self._status_dot.style().unpolish(self._status_dot)
        self._status_dot.style().polish(self._status_dot)
