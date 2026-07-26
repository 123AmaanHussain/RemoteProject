"""
main.py — Controlled PC System Tray Agent
==========================================
Entry point for the Remote Support Agent.

Responsibilities:
- Generate a professional system tray icon using pystray + Pillow.
- Spawn the asyncio network client in a dedicated background thread.
- Expose a right-click context menu with:
    • Connection Status (dynamic label)
    • Copy Code (copies the session code to clipboard)
    • Revoke Permissions (ends any active session)
    • Exit (clean shutdown)
- Bridge tray UI updates (which must be on the main thread) with the
  asyncio network thread via thread-safe callbacks.

Threading model:
  Main thread   → pystray.Icon.run() (blocks — required by pystray on Windows)
  Thread-2      → asyncio event loop (all network + async I/O)
  Thread-3      → ScreenCaptureThread   (daemon)
  Thread-4      → InputInjector         (daemon)
  Thread-5      → ClipboardMonitor      (daemon)
  Thread-6      → ConsentDialog         (daemon, transient)
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Optional

import pyperclip
import pystray
from PIL import Image, ImageDraw, ImageFont

import config
from network import ControlledNetworkClient

# ─── Logging Configuration ────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("RemoteSupportAgent")


# ─── Tray Icon Generation ─────────────────────────────────────────────────────

def _generate_tray_icon(active: bool = False) -> Image.Image:
    """
    Programmatically generates a 64×64 tray icon using Pillow.
    Active sessions show a red dot; idle sessions show a grey dot.
    This avoids bundling an external .ico file.

    Returns:
        A PIL Image suitable for use as a pystray icon.
    """
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background circle — dark blue-grey
    bg_color = (30, 40, 60, 240)
    draw.ellipse([(2, 2), (size - 2, size - 2)], fill=bg_color)

    # Monitor icon outline
    mon_color = (180, 200, 255, 255)
    draw.rectangle([(12, 16), (52, 40)], outline=mon_color, width=2)
    draw.rectangle([(28, 40), (36, 48)], fill=mon_color)
    draw.rectangle([(20, 48), (44, 50)], fill=mon_color)

    # Status indicator dot (top-right)
    dot_color = (220, 50, 50, 255) if active else (120, 130, 145, 255)
    draw.ellipse([(44, 6), (58, 20)], fill=dot_color)

    return img


# ─── Alert Helper ─────────────────────────────────────────────────────────────

def _show_alert(message: str) -> None:
    """
    Displays a user-visible error/info dialog using tkinter.
    Must be called from any thread; uses its own Tk root.
    """
    def _run():
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        messagebox.showwarning("Remote Support Agent", message)
        try:
            root.destroy()
        except tk.TclError:
            pass

    t = threading.Thread(target=_run, daemon=True, name="AlertDialog")
    t.start()


# ─── Application State ────────────────────────────────────────────────────────

class AgentState:
    """Shared mutable state between the tray UI and the network thread."""

    def __init__(self) -> None:
        self.status_text:   str           = "Idle — not connected"
        self.session_code:  Optional[str] = None
        self.tray_icon:     Optional[pystray.Icon] = None
        self._lock = threading.Lock()

    def update_status(self, text: str) -> None:
        with self._lock:
            self.status_text = text
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def update_code(self, code: str) -> None:
        with self._lock:
            self.session_code = code
        log.info("Session code assigned: %s", code)
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass


STATE = AgentState()

# ─── Network Thread ───────────────────────────────────────────────────────────

_STOP_EVENT   = threading.Event()
_NET_CLIENT: Optional[ControlledNetworkClient] = None
_ASYNC_LOOP:  Optional[asyncio.AbstractEventLoop] = None


def _run_network_loop() -> None:
    """Entry function for the dedicated network thread."""
    global _ASYNC_LOOP, _NET_CLIENT

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _ASYNC_LOOP = loop

    _NET_CLIENT = ControlledNetworkClient(
        tray_status_cb=STATE.update_status,
        alert_cb=_show_alert,
        code_cb=STATE.update_code,
        stop_event=_STOP_EVENT,
    )

    try:
        loop.run_until_complete(_NET_CLIENT.run())
    except Exception as exc:
        log.exception("Fatal network thread error: %s", exc)
        _show_alert(
            f"Remote Support Agent encountered a fatal network error:\n\n{exc}\n\n"
            "The agent will exit."
        )
    finally:
        loop.close()


# ─── Tray Menu Callbacks ──────────────────────────────────────────────────────

def _on_copy_code(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Copies the active session code to the clipboard."""
    code = STATE.session_code
    if code:
        try:
            pyperclip.copy(code)
        except Exception as exc:
            _show_alert(f"Could not copy code to clipboard: {exc}")
    else:
        _show_alert("No active session code to copy.")


def _on_revoke(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Revokes the current remote session."""
    global _NET_CLIENT, _ASYNC_LOOP

    if _NET_CLIENT is None or _ASYNC_LOOP is None:
        _show_alert("No active session to revoke.")
        return

    asyncio.run_coroutine_threadsafe(
        _NET_CLIENT.revoke_session(), _ASYNC_LOOP
    )
    STATE.update_status("Permissions revoked — idle")
    STATE.update_code("")


def _on_exit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Gracefully shuts down the entire agent."""
    log.info("Exit requested via tray menu")
    _STOP_EVENT.set()
    icon.stop()


# ─── Tray Menu Builder ────────────────────────────────────────────────────────

def _build_menu() -> pystray.Menu:
    """
    Returns a dynamically evaluated tray menu.
    pystray calls the lambda each time the menu is opened, so dynamic text
    always reflects the current state.
    """
    return pystray.Menu(
        pystray.MenuItem(
            lambda _: f"Status: {STATE.status_text}",
            action=None,  # Display-only
            enabled=False,
        ),
        pystray.MenuItem(
            lambda _: (
                f"Code: {STATE.session_code}"
                if STATE.session_code else "Code: (none)"
            ),
            action=None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Copy Session Code", _on_copy_code),
        pystray.MenuItem("Revoke Permissions", _on_revoke),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", _on_exit),
    )


# ─── Main Entry ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Starting %s v%s", config.APP_NAME, config.APP_VERSION)
    log.info("Peer ID: (stored in .peer_id)")

    # Start network loop in background thread
    net_thread = threading.Thread(
        target=_run_network_loop,
        name="NetworkThread",
        daemon=True,
    )
    net_thread.start()

    # Build tray icon (pystray.Icon.run() must be on the main thread on Windows)
    icon_image = _generate_tray_icon(active=False)
    tray_icon  = pystray.Icon(
        name=config.APP_NAME,
        icon=icon_image,
        title=config.TRAY_TOOLTIP,
        menu=_build_menu(),
    )
    STATE.tray_icon = tray_icon

    log.info("System tray agent started. Right-click the tray icon to manage.")

    # Blocking call — runs the tray message loop on the main thread
    tray_icon.run()

    # ── Shutdown path (reached after icon.stop()) ─────────────────────────
    log.info("Tray icon stopped — initiating shutdown")
    _STOP_EVENT.set()
    net_thread.join(timeout=5.0)
    log.info("Remote Support Agent exited cleanly")


if __name__ == "__main__":
    main()
