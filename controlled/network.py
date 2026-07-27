"""
network.py — Controlled PC WebSocket Client
============================================
Manages the persistent WebSocket connection to the signaling server.
Handles:
  - Registration and 6-digit code acquisition
  - Consent request dispatch + result reporting
  - Screen frame streaming (binary WebSocket frames)
  - Input event reception and forwarding to InputInjector
  - Clipboard sync (bidirectional)
  - Exponential backoff reconnection with 30s alert threshold
  - WebRTC signaling relay hooks (SDP/ICE)

State transitions driven by server messages follow the session state machine
documented in messageRouter.js.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from capture import ScreenCaptureThread
from clipboard_sync import ClipboardMonitor
from consent import request_consent_async
from input_handler import InputInjector
from config import (
    RECONNECT_ALERT_AFTER_S,
    RECONNECT_BACKOFF_INITIAL_S,
    RECONNECT_BACKOFF_MAX_S,
    SIGNALING_URL,
)

log = logging.getLogger(__name__)

# ─── Stable Peer Identity ─────────────────────────────────────────────────────
# Persisted so that reconnection attempts are matched to the original session.
_PEER_ID_FILE = os.path.join(os.path.dirname(__file__), ".peer_id")


def _get_or_create_peer_id() -> str:
    """Loads or generates a stable UUID-based peer identifier."""
    if os.path.exists(_PEER_ID_FILE):
        with open(_PEER_ID_FILE, "r") as f:
            pid = f.read().strip()
            if len(pid) == 36:
                return pid
    pid = str(uuid.uuid4())
    with open(_PEER_ID_FILE, "w") as f:
        f.write(pid)
    return pid


PEER_ID: str = _get_or_create_peer_id()


class ControlledNetworkClient:
    """
    Asyncio-based network client for the Controlled PC agent.

    Args:
        tray_status_cb:  Callable(str) → sets the tray tooltip/status text.
        alert_cb:        Callable(str) → shows a user-facing alert message.
        code_cb:         Callable(str) → called with the assigned session code.
        stop_event:      When set, shuts down the entire client.
    """

    def __init__(
        self,
        tray_status_cb: Callable[[str], None],
        alert_cb:       Callable[[str], None],
        code_cb:        Callable[[str], None],
        stop_event:     threading.Event,
    ) -> None:
        self._tray_status = tray_status_cb
        self._alert       = alert_cb
        self._code_cb     = code_cb
        self._stop_event  = stop_event

        self._session_code:    Optional[str] = None
        self._authorized:      bool          = False
        self._ws:              Any           = None  # websockets.WebSocketClientProtocol

        # Child subsystems — created once session is authorised
        self._capture_thread:  Optional[ScreenCaptureThread] = None
        self._input_injector:  Optional[InputInjector]       = None
        self._clipboard_mon:   Optional[ClipboardMonitor]    = None

        # Shared stop event for subsystems (reset each new session)
        self._sub_stop: threading.Event = threading.Event()

        # Frame queue for the capture→network pipeline
        self._frame_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._loop:        Optional[asyncio.AbstractEventLoop] = None

    # ─── Main Entry ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Main coroutine. Connects with exponential backoff and runs the
        message loop until stop_event is set.
        """
        self._loop = asyncio.get_running_loop()
        backoff    = RECONNECT_BACKOFF_INITIAL_S
        total_down = 0.0

        while not self._stop_event.is_set():
            try:
                log.info("Connecting to signaling server: %s", SIGNALING_URL)
                async with websockets.connect(
                    SIGNALING_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=10 * 1024 * 1024,  # 10 MB max frame
                ) as ws:
                    self._ws   = ws
                    backoff    = RECONNECT_BACKOFF_INITIAL_S  # Reset on success
                    total_down = 0.0
                    self._tray_status("Connected — waiting for controller")

                    # Register with server to receive a pairing code
                    await self._register()

                    # Run the message receive loop + frame streaming concurrently
                    await asyncio.gather(
                        self._receive_loop(),
                        self._stream_frames(),
                    )

            except (ConnectionClosedOK, ConnectionClosedError) as exc:
                log.warning("WebSocket closed: %s", exc)
            except OSError as exc:
                log.error("Network error: %s", exc)
            except Exception as exc:
                log.exception("Unexpected error in network client: %s", exc)
            finally:
                self._ws = None
                self._stop_subsystems()

            if self._stop_event.is_set():
                break

            # ── Exponential Backoff ───────────────────────────────────────
            total_down += backoff

            if total_down >= RECONNECT_ALERT_AFTER_S:
                msg = (
                    f"Remote Support Agent has been disconnected for "
                    f"{int(total_down)}s. It will continue retrying."
                )
                self._tray_status(f"Disconnected ({int(total_down)}s)")
                self._alert(msg)

            log.info("Reconnecting in %.1fs…", backoff)
            self._tray_status(f"Reconnecting in {backoff:.0f}s…")

            # Sleep in small increments so stop_event can interrupt
            slept = 0.0
            while slept < backoff and not self._stop_event.is_set():
                await asyncio.sleep(0.5)
                slept += 0.5

            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)

    # ─── Registration ───────────────────────────────────────────────────────

    async def _register(self) -> None:
        """Send 'register' to server; or 'reconnect' if a session code exists."""
        if self._session_code:
            log.info("Attempting reconnection for code %s", self._session_code)
            await self._send({
                "type":   "reconnect",
                "code":   self._session_code,
                "peerId": PEER_ID,
                "role":   "controlled",
            })
        else:
            await self._send({"type": "register", "peerId": PEER_ID})

    # ─── Receive Loop ────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """Continuously reads messages from the WebSocket and dispatches them."""
        async for raw in self._ws:
            if isinstance(raw, bytes):
                # Binary frames on the control socket are unexpected — ignore
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Received non-JSON message — ignored")
                continue

            await self._dispatch(msg)

    # ─── Message Dispatch ────────────────────────────────────────────────────

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Routes server messages to appropriate handlers."""
        msg_type = msg.get("type", "")
        log.info("Received message: %s - Full: %s", msg_type, msg)

        if msg_type == "registered":
            code = msg.get("code", "")
            self._session_code = code
            self._code_cb(code)
            self._tray_status(f"Session Code: {code} — Waiting for controller")
            log.info("Registered with code: %s", code)

        elif msg_type == "consent_request":
            controller_id = msg.get("controllerId", "Unknown")
            log.info("Consent request from: %s", controller_id)
            self._tray_status("⚠ Consent request — check dialog")

            # Run consent dialog in executor (blocking tkinter call)
            approved = await request_consent_async(controller_id)
            code = msg.get("code", self._session_code)

            await self._send({
                "type":     "consent_result",
                "code":     code,
                "approved": approved,
            })

            if approved:
                log.info("User approved consent for controller %s", controller_id)
            else:
                log.info("User denied consent — session invalidated")
                self._session_code = None  # Force fresh registration
                self._tray_status("Connection denied — idle")

        elif msg_type == "session_active":
            self._authorized = True
            self._tray_status("🔴 Session Active — streaming")
            log.info("Session is now active and authorised")
            self._start_subsystems()

        elif msg_type == "reconnect_success":
            log.info("Reconnect acknowledged by server")
            if self._authorized:
                self._start_subsystems()

        elif msg_type == "peer_disconnected":
            role = msg.get("role", "unknown")
            log.warning("Controller (%s) disconnected — grace period active", role)
            self._tray_status("Controller disconnected — grace period active")
            self._stop_subsystems()

        elif msg_type == "peer_reconnected":
            log.info("Controller reconnected within grace period")
            self._tray_status("🔴 Session Active — streaming")
            self._start_subsystems()

        elif msg_type == "session_expired":
            log.warning("Session expired (grace period ended): %s", msg.get("code"))
            self._session_code = None
            self._authorized   = False
            self._tray_status("Session expired — idle")
            self._alert("Remote session expired. Please reconnect.")

        elif msg_type == "session_denied":
            self._tray_status("Session denied — idle")

        elif msg_type == "input":
            # Validated and forwarded to InputInjector
            payload = msg.get("payload")
            if self._input_injector and isinstance(payload, dict):
                self._input_injector.enqueue(payload)

        elif msg_type == "clipboard":
            text = msg.get("text", "")
            if self._clipboard_mon:
                self._clipboard_mon.record_incoming(text)

        # ── WebRTC Signaling Relay Hooks ──────────────────────────────────
        elif msg_type == "sdp_offer":
            log.info("[WebRTC Hook] SDP Offer received — aiortc handler here")
            # TODO: Pass msg['sdp'] to aiortc PC.setRemoteDescription()

        elif msg_type == "ice_candidate":
            log.info("[WebRTC Hook] ICE Candidate received — aiortc handler here")
            # TODO: Pass msg['candidate'] to aiortc PC.addIceCandidate()

        elif msg_type == "error":
            error_msg = msg.get("message", "Unknown server error")
            log.error("Server error: %s", error_msg)
            self._alert(f"Server Error: {error_msg}")

        else:
            log.debug("Unhandled message type: %s", msg_type)

    # ─── Frame Streaming ─────────────────────────────────────────────────────

    async def _stream_frames(self) -> None:
        """
        Continuously dequeues compressed frame bytes and sends them as binary
        WebSocket frames. Only runs while authorised; otherwise sleeps.
        """
        while True:
            if not self._authorized or self._ws is None:
                await asyncio.sleep(0.05)
                continue

            try:
                frame_bytes: bytes = await asyncio.wait_for(
                    self._frame_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            try:
                log.info("Sending frame: %d bytes", len(frame_bytes))
                await self._ws.send(frame_bytes)
            except (ConnectionClosedError, ConnectionClosedOK):
                log.warning("Stream frame send failed — connection closed")
                break
            except Exception as exc:
                log.warning("Frame send error: %s", exc)

    # ─── Subsystem Lifecycle ─────────────────────────────────────────────────

    def _start_subsystems(self) -> None:
        """Starts capture, input injection, and clipboard monitoring threads."""
        if self._capture_thread and self._capture_thread.is_alive():
            return  # Already running

        self._sub_stop = threading.Event()

        # Screen capture
        self._capture_thread = ScreenCaptureThread(
            frame_queue=self._frame_queue,
            loop=self._loop,
            stop_event=self._sub_stop,
        )
        self._capture_thread.start()

        # Input injection — use monitor size from capture thread
        # Give capture a moment to read monitor dimensions
        time.sleep(0.1)
        w = self._capture_thread.monitor_width  or 1920
        h = self._capture_thread.monitor_height or 1080

        self._input_injector = InputInjector(
            monitor_width=w,
            monitor_height=h,
            stop_event=self._sub_stop,
        )
        self._input_injector.start()

        # Clipboard monitor
        self._clipboard_mon = ClipboardMonitor(
            on_change_async=self._on_clipboard_change,
            loop=self._loop,
            stop_event=self._sub_stop,
        )
        self._clipboard_mon.start()

        log.info("All subsystems started")

    def _stop_subsystems(self) -> None:
        """Signals all subsystem threads to stop."""
        self._sub_stop.set()
        # Threads are daemon threads; they will exit automatically.
        # Reset frame queue
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._capture_thread = None
        self._input_injector = None
        self._clipboard_mon  = None
        log.info("Subsystems stopped")

    # ─── Clipboard Callback ──────────────────────────────────────────────────

    async def _on_clipboard_change(self, text: str) -> None:
        """Called by ClipboardMonitor when local clipboard changes."""
        if not self._authorized or self._ws is None or not self._session_code:
            return
        await self._send({
            "type": "clipboard",
            "code": self._session_code,
            "text": text,
        })

    # ─── Revoke Session ──────────────────────────────────────────────────────

    async def revoke_session(self) -> None:
        """Called from the tray 'Revoke Permissions' menu item."""
        if self._ws and self._session_code:
            await self._send({
                "type": "revoke",
                "code": self._session_code,
            })
        self._authorized   = False
        self._session_code = None
        self._stop_subsystems()
        self._tray_status("Permissions revoked — idle")
        log.info("Session revoked by local user")

    # ─── Helpers ─────────────────────────────────────────────────────────────

    async def _send(self, payload: dict) -> None:
        """Serialises and sends a JSON message, with error handling."""
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:
            log.warning("Send failed: %s", exc)
