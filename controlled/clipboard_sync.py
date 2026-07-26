"""
clipboard_sync.py — Clipboard Monitoring & Synchronisation Module
==================================================================
Monitors the OS clipboard for changes and notifies the network layer.
Applies an anti-loop cache so that incoming clipboard updates from the
controller are not re-transmitted back.

Design:
- Polling-based monitor in a daemon thread (cross-platform, no OS hooks needed).
- Cache-based deduplication: only sends a clipboard event when the value
  differs from the last known value (either locally typed or remotely received).
- Callback-based: calls an async-aware send function when a change is detected.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

import pyperclip

from config import CLIPBOARD_POLL_INTERVAL_S

log = logging.getLogger(__name__)


class ClipboardMonitor(threading.Thread):
    """
    Background thread that polls the OS clipboard and fires a callback on
    genuine value changes.

    Args:
        on_change_async: An async coroutine function called with the new text
                         when a change is detected. Signature: (text: str) -> None
        loop: The running asyncio event loop used to schedule the callback.
        stop_event: When set, the polling loop exits cleanly.
    """

    def __init__(
        self,
        on_change_async: Callable[[str], "asyncio.coroutine"],
        loop: asyncio.AbstractEventLoop,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="ClipboardMonitor", daemon=True)
        self._on_change = on_change_async
        self._loop      = loop
        self._stop_event = stop_event

        # The last value that was either sent or received — prevents loops
        self._last_known: Optional[str] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def record_incoming(self, text: str) -> None:
        """
        Called by the network layer when a clipboard update arrives FROM the
        controller. Updates the local clipboard AND the cache so that the
        monitor does not re-transmit this value back.
        """
        if text == self._last_known:
            return

        self._last_known = text

        try:
            pyperclip.copy(text)
            log.debug("Clipboard set from remote: %.60r…", text)
        except pyperclip.PyperclipException as exc:
            log.warning("Could not write incoming clipboard text: %s", exc)

    # ── Thread Loop ───────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info(
            "Clipboard monitor started (poll interval: %.2fs)",
            CLIPBOARD_POLL_INTERVAL_S,
        )

        # Seed the cache with the current clipboard so we don't send a
        # spurious update on startup
        try:
            self._last_known = pyperclip.paste()
        except pyperclip.PyperclipException:
            self._last_known = ""

        while not self._stop_event.is_set():
            time.sleep(CLIPBOARD_POLL_INTERVAL_S)

            try:
                current = pyperclip.paste()
            except pyperclip.PyperclipException as exc:
                log.warning("Clipboard read error: %s", exc)
                continue

            if current != self._last_known:
                log.debug("Local clipboard change detected: %.60r…", current)
                self._last_known = current
                self._fire_callback(current)

        log.info("Clipboard monitor stopped")

    def _fire_callback(self, text: str) -> None:
        """Schedules the async callback on the asyncio event loop."""
        try:
            asyncio.run_coroutine_threadsafe(self._on_change(text), self._loop)
        except RuntimeError:
            # Event loop is closed (shutdown in progress)
            pass
