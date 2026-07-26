"""
input_handler.py — Remote Input Injection Module
=================================================
Receives serialised input event dicts from the network layer and injects
them into the OS via pynput.

Features:
- Thread-safe input event queue with configurable inter-event pacing.
- Local physical mouse intercept: pauses remote injection for 2 seconds
  when the local user moves the mouse more than 10 pixels to avoid
  cursor jitter during concurrent usage.
- Coordinate translation: maps normalised [0.0–1.0] fractions from the
  controller's viewport to absolute screen pixel positions, supporting
  any target monitor resolution.
- Full keyboard injection: regular keys + special keys (Enter, Tab, Ctrl, etc.)
- Mouse injection: move, click (left/right/middle), scroll.

Input event dict schema (from controller):
  Mouse move:   { "kind": "mouse_move",   "nx": float, "ny": float }
  Mouse click:  { "kind": "mouse_click",  "nx": float, "ny": float,
                  "button": "left"|"right"|"middle", "pressed": bool }
  Mouse scroll: { "kind": "mouse_scroll", "nx": float, "ny": float,
                  "dx": int, "dy": int }
  Key press:    { "kind": "key_press",    "key": str }
  Key release:  { "kind": "key_release",  "key": str }
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from pynput import keyboard as kb
from pynput import mouse as ms

from config import (
    INPUT_PACING_DELAY_S,
    LOCAL_MOUSE_INTERCEPT_PX,
    LOCAL_MOUSE_PAUSE_S,
)

log = logging.getLogger(__name__)

# ─── pynput Controller Singletons ────────────────────────────────────────────
# Single instances prevent multiple OS handles from being opened.
_mouse_ctrl: ms.Controller    = ms.Controller()
_kb_ctrl:    kb.Controller    = kb.Controller()

# ─── Special key mapping ─────────────────────────────────────────────────────
# Maps string representations sent by the controller to pynput Key enums.
_SPECIAL_KEYS: dict[str, kb.Key] = {
    "enter":      kb.Key.enter,
    "tab":        kb.Key.tab,
    "space":      kb.Key.space,
    "backspace":  kb.Key.backspace,
    "delete":     kb.Key.delete,
    "escape":     kb.Key.esc,
    "ctrl":       kb.Key.ctrl,
    "ctrl_l":     kb.Key.ctrl_l,
    "ctrl_r":     kb.Key.ctrl_r,
    "alt":        kb.Key.alt,
    "alt_l":      kb.Key.alt_l,
    "alt_r":      kb.Key.alt_r,
    "shift":      kb.Key.shift,
    "shift_l":    kb.Key.shift_l,
    "shift_r":    kb.Key.shift_r,
    "super":      kb.Key.cmd,
    "cmd":        kb.Key.cmd,
    "up":         kb.Key.up,
    "down":       kb.Key.down,
    "left":       kb.Key.left,
    "right":      kb.Key.right,
    "home":       kb.Key.home,
    "end":        kb.Key.end,
    "page_up":    kb.Key.page_up,
    "page_down":  kb.Key.page_down,
    "insert":     kb.Key.insert,
    "f1":  kb.Key.f1,  "f2":  kb.Key.f2,  "f3":  kb.Key.f3,  "f4":  kb.Key.f4,
    "f5":  kb.Key.f5,  "f6":  kb.Key.f6,  "f7":  kb.Key.f7,  "f8":  kb.Key.f8,
    "f9":  kb.Key.f9,  "f10": kb.Key.f10, "f11": kb.Key.f11, "f12": kb.Key.f12,
}

_MOUSE_BUTTONS: dict[str, ms.Button] = {
    "left":   ms.Button.left,
    "right":  ms.Button.right,
    "middle": ms.Button.middle,
}


class InputInjector(threading.Thread):
    """
    Background thread that dequeues input events and injects them into the OS.

    Args:
        monitor_width:  Pixel width of the target monitor.
        monitor_height: Pixel height of the target monitor.
        stop_event:     When set, the injection loop exits cleanly.
    """

    def __init__(
        self,
        monitor_width: int,
        monitor_height: int,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="InputInjector", daemon=True)

        self._monitor_width  = monitor_width
        self._monitor_height = monitor_height
        self._stop_event     = stop_event

        # Bounded queue — at most 256 pending events to limit memory
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)

        # Local mouse intercept state
        self._pause_injection: bool     = False
        self._pause_until:     float    = 0.0
        self._last_local_pos:  tuple[int, int] = (0, 0)

        # Start the local mouse listener for intercept detection
        self._local_listener = ms.Listener(on_move=self._on_local_move)
        self._local_listener.start()

    # ── Public API ───────────────────────────────────────────────────────────

    def enqueue(self, event: dict[str, Any]) -> None:
        """
        Add a remote input event to the injection queue.
        Silently drops events when the queue is full to prevent memory growth.

        Args:
            event: A validated input event dict from the network layer.
        """
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            log.debug("Input queue full — event dropped (rate too high)")

    def update_monitor_size(self, width: int, height: int) -> None:
        """Called when the target monitor resolution changes mid-session."""
        self._monitor_width  = width
        self._monitor_height = height
        log.info("Monitor size updated to %dx%d", width, height)

    # ── Thread Loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info(
            "Input injector started (monitor: %dx%d, pacing: %.1f ms)",
            self._monitor_width, self._monitor_height,
            INPUT_PACING_DELAY_S * 1000,
        )

        while not self._stop_event.is_set():
            try:
                event = self._event_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            # Check if local-user intercept has expired
            if self._pause_injection and time.monotonic() >= self._pause_until:
                self._pause_injection = False
                log.debug("Remote input injection resumed after local intercept")

            if not self._pause_injection:
                self._dispatch(event)

            # Pacing delay — gives the OS event queue breathing room
            time.sleep(INPUT_PACING_DELAY_S)

        self._local_listener.stop()
        log.info("Input injector stopped")

    # ── Dispatch ─────────────────────────────────────────────────────────────

    def _dispatch(self, event: dict[str, Any]) -> None:
        """Routes an event dict to the appropriate injection method."""
        kind = event.get("kind", "")

        try:
            if kind == "mouse_move":
                self._inject_mouse_move(event)
            elif kind == "mouse_click":
                self._inject_mouse_click(event)
            elif kind == "mouse_scroll":
                self._inject_mouse_scroll(event)
            elif kind == "key_press":
                self._inject_key(event, press=True)
            elif kind == "key_release":
                self._inject_key(event, press=False)
            else:
                log.warning("Unknown input event kind: %r", kind)
        except Exception as exc:
            log.warning("Input injection error for event %r: %s", kind, exc)

    # ── Mouse Injection ───────────────────────────────────────────────────────

    def _norm_to_abs(self, nx: Any, ny: Any) -> tuple[int, int] | None:
        """
        Converts normalised [0.0–1.0] coordinates from the controller viewport
        to absolute OS pixel positions.

        Returns None and logs a warning if coordinates are out of range.
        """
        try:
            fx = float(nx)
            fy = float(ny)
        except (TypeError, ValueError):
            log.warning("Invalid coordinate values: nx=%r ny=%r", nx, ny)
            return None

        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            log.warning(
                "Coordinate out of [0,1] range: nx=%.4f ny=%.4f — clamping",
                fx, fy,
            )
            fx = max(0.0, min(1.0, fx))
            fy = max(0.0, min(1.0, fy))

        abs_x = int(fx * self._monitor_width)
        abs_y = int(fy * self._monitor_height)

        # Final bounds check
        abs_x = max(0, min(self._monitor_width  - 1, abs_x))
        abs_y = max(0, min(self._monitor_height - 1, abs_y))

        return abs_x, abs_y

    def _inject_mouse_move(self, event: dict) -> None:
        pos = self._norm_to_abs(event.get("nx"), event.get("ny"))
        if pos:
            _mouse_ctrl.position = pos

    def _inject_mouse_click(self, event: dict) -> None:
        pos = self._norm_to_abs(event.get("nx"), event.get("ny"))
        if not pos:
            return

        button_str = event.get("button", "left")
        button = _MOUSE_BUTTONS.get(button_str, ms.Button.left)
        pressed = bool(event.get("pressed", True))

        _mouse_ctrl.position = pos
        if pressed:
            _mouse_ctrl.press(button)
        else:
            _mouse_ctrl.release(button)

    def _inject_mouse_scroll(self, event: dict) -> None:
        pos = self._norm_to_abs(event.get("nx"), event.get("ny"))
        if not pos:
            return

        dx = int(event.get("dx", 0))
        dy = int(event.get("dy", 0))

        _mouse_ctrl.position = pos
        _mouse_ctrl.scroll(dx, dy)

    # ── Keyboard Injection ───────────────────────────────────────────────────

    def _inject_key(self, event: dict, *, press: bool) -> None:
        raw_key = event.get("key", "")
        if not isinstance(raw_key, str) or not raw_key:
            log.warning("Empty or invalid key field in event: %r", event)
            return

        key_lower = raw_key.lower()

        # Resolve to pynput Key enum or a single character
        resolved_key: kb.Key | str
        if key_lower in _SPECIAL_KEYS:
            resolved_key = _SPECIAL_KEYS[key_lower]
        elif len(raw_key) == 1:
            resolved_key = raw_key
        else:
            log.warning("Unknown key: %r — skipping injection", raw_key)
            return

        if press:
            _kb_ctrl.press(resolved_key)
        else:
            _kb_ctrl.release(resolved_key)

    # ── Local Mouse Intercept ────────────────────────────────────────────────

    def _on_local_move(self, x: int, y: int) -> None:
        """
        Called by pynput whenever the local physical mouse moves.
        If movement exceeds the intercept threshold, remote injection is
        paused for LOCAL_MOUSE_PAUSE_S seconds.
        """
        lx, ly = self._last_local_pos
        delta = ((x - lx) ** 2 + (y - ly) ** 2) ** 0.5

        if delta >= LOCAL_MOUSE_INTERCEPT_PX and not self._pause_injection:
            self._pause_injection = True
            self._pause_until     = time.monotonic() + LOCAL_MOUSE_PAUSE_S
            log.debug(
                "Local mouse moved %.1fpx — pausing remote injection for %.1fs",
                delta, LOCAL_MOUSE_PAUSE_S,
            )

        self._last_local_pos = (x, y)
