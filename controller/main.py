"""
main.py — Controller PC Client Entry Point
==========================================
Initialises the PyQt6 application, creates the main window and the network
worker thread, wires all cross-thread signals together, and starts the event loop.
"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtCore import Qt, QCoreApplication
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow
from network import NetworkThread
import config

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ControllerClient")


def main() -> int:
    # Enable high-DPI scaling before QApplication is created (Qt6 enables this by default)
    if hasattr(Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
        QCoreApplication.setAttribute(getattr(Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"), True)
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QCoreApplication.setAttribute(getattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"), True)

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setApplicationVersion(config.APP_VERSION)

    # Set default font
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    # ── Create main window ────────────────────────────────────────────────
    window = MainWindow()
    window.show()

    # ── Create network thread ─────────────────────────────────────────────
    net_thread = NetworkThread(parent=app)

    # ── Wire signals: GUI → Network ───────────────────────────────────────
    # The user clicked Connect with a code
    window.connect_requested.connect(net_thread.worker.connect_to_session)

    # The user clicked Disconnect
    window.disconnect_requested.connect(net_thread.worker.disconnect_session)

    # Mouse/keyboard events from viewport → network send
    window.input_ready.connect(net_thread.worker.send_input_event)

    # Local clipboard changes → send to controlled PC
    window.clipboard_send.connect(net_thread.worker.send_clipboard)

    # ── Wire signals: Network → GUI ───────────────────────────────────────
    worker = net_thread.worker

    worker.status_changed.connect(window.set_status)
    worker.session_active.connect(window.on_session_active)
    worker.session_denied.connect(window.on_session_denied)
    worker.disconnected.connect(window.on_disconnected)
    worker.error_occurred.connect(window.on_error)
    worker.frame_received.connect(window.on_frame_received)
    worker.clipboard_received.connect(window.on_clipboard_received)

    # ── Start network thread ──────────────────────────────────────────────
    net_thread.start()
    log.info("Controller client started. Network thread running.")

    # ── Run Qt event loop ─────────────────────────────────────────────────
    exit_code = app.exec()

    # ── Clean shutdown ────────────────────────────────────────────────────
    log.info("Application closing — stopping network thread")
    net_thread.stop()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
