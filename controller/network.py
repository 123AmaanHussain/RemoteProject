"""
network.py — Controller PC WebSocket Client
============================================
Runs in a QThread to keep the UI fully responsive.
Manages the WebSocket connection to the signaling server, emitting Qt signals
to the main window for all state changes and incoming data.

Threading model:
  QApplication thread → GUI
  NetworkWorker thread → asyncio event loop (this file)

Signal flow:
  GUI → NetworkWorker: connect_requested, disconnect_requested, input_event, clipboard_send
  NetworkWorker → GUI: status_changed, session_active, session_denied,
                       frame_received, clipboard_received, error_occurred
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import os
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

import config

log = logging.getLogger(__name__)

# ─── Stable Peer Identity ─────────────────────────────────────────────────────

_PEER_ID_FILE = os.path.join(os.path.dirname(__file__), ".ctrl_peer_id")


def _get_or_create_peer_id() -> str:
    if os.path.exists(_PEER_ID_FILE):
        with open(_PEER_ID_FILE) as f:
            pid = f.read().strip()
            if len(pid) == 36:
                return pid
    pid = str(uuid.uuid4())
    with open(_PEER_ID_FILE, "w") as f:
        f.write(pid)
    return pid


PEER_ID: str = _get_or_create_peer_id()


class NetworkWorker(QObject):
    """
    QObject that runs an asyncio event loop on a dedicated QThread.
    Receives commands from the GUI via pyqtSlots and emits results back
    as pyqtSignals.

    Signals:
        status_changed(str):     Human-readable status for the UI.
        session_active():        Session is authorised and streaming.
        session_denied(str):     Session was denied or expired.
        disconnected(str):       Connection dropped (reason string).
        frame_received(bytes):   Raw JPEG/WebP frame bytes.
        clipboard_received(str): Clipboard text from controlled PC.
        error_occurred(str):     User-visible error description.
    """

    status_changed:    pyqtSignal = pyqtSignal(str)
    session_active:    pyqtSignal = pyqtSignal()
    session_denied:    pyqtSignal = pyqtSignal(str)
    disconnected:      pyqtSignal = pyqtSignal(str)
    frame_received:    pyqtSignal = pyqtSignal(bytes)
    clipboard_received: pyqtSignal = pyqtSignal(str)
    error_occurred:    pyqtSignal = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws:   Any  = None  # websockets.WebSocketClientProtocol
        self._code: Optional[str] = None
        self._authorized: bool    = False
        self._running:    bool    = False

        # Pending action queue (used to bridge Qt slots → asyncio coroutines)
        self._pending_connect_code: Optional[str] = None
        self._should_disconnect:    bool           = False

    # ─── QThread Entry ───────────────────────────────────────────────────────

    def start_network(self) -> None:
        """
        Called once when the QThread starts. Runs the asyncio event loop.
        The loop continues until stop() is called from outside.
        """
        self._loop    = asyncio.new_event_loop()
        self._running = True
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as exc:
            log.exception("Fatal network worker error: %s", exc)
            self.error_occurred.emit(
                f"Fatal network error: {exc}\n\nThe controller client may need to be restarted."
            )
        finally:
            self._loop.close()

    def stop(self) -> None:
        """Signals the asyncio loop to shut down cleanly."""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ─── Qt Slots (called from GUI thread) ───────────────────────────────────

    @pyqtSlot(str)
    def connect_to_session(self, code: str) -> None:
        """Initiates a join request for the given 6-digit code."""
        print(f"[DEBUG] connect_to_session called with code: {code}")
        log.info("connect_to_session called with code: %s", code)
        log.info("Event loop ready: %s", self._loop is not None)
        self._pending_connect_code = code
        # The main loop will detect this and establish connection + send join request
        log.info("Pending connect code set, main loop will handle connection")

    @pyqtSlot()
    def disconnect_session(self) -> None:
        """Closes the current session cleanly."""
        self._authorized = False
        self._code       = None
        self.status_changed.emit("Disconnecting…")

        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(
                self._ws.close(1000, "User disconnected"), self._loop
            )

    @pyqtSlot(dict)
    def send_input_event(self, event: dict) -> None:
        """Forwards a mouse/keyboard input event to the controlled PC."""
        if not self._authorized or self._code is None:
            return
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send({
                    "type":    "input",
                    "code":    self._code,
                    "payload": event,
                }),
                self._loop,
            )

    @pyqtSlot(str)
    def send_clipboard(self, text: str) -> None:
        """Sends local clipboard text to the controlled PC."""
        if not self._authorized or self._code is None:
            return
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send({
                    "type": "clipboard",
                    "code": self._code,
                    "text": text,
                }),
                self._loop,
            )

    # ─── Asyncio Main Loop ───────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """
        Persistent connection loop with exponential backoff.
        Waits for a connect request before initiating a connection.
        """
        backoff      = config.RECONNECT_BACKOFF_INITIAL_S
        total_down   = 0.0
        was_connected = False

        while self._running:
            # ── Wait for a connect request ────────────────────────────────
            if self._pending_connect_code is None:
                await asyncio.sleep(0.1)
                continue

            code = self._pending_connect_code
            self._pending_connect_code = None
            self.status_changed.emit("Connecting to server…")

            try:
                async with websockets.connect(
                    config.SIGNALING_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=20 * 1024 * 1024,  # 20 MB — allow large frames
                ) as ws:
                    self._ws     = ws
                    backoff      = config.RECONNECT_BACKOFF_INITIAL_S
                    total_down   = 0.0
                    was_connected = True
                    self.status_changed.emit("Connected — sending join request…")

                    # Send join request
                    await self._join_session(code)

                    # Receive messages
                    await self._receive_loop()

            except (ConnectionClosedOK, ConnectionClosedError) as exc:
                log.warning("WebSocket closed: %s", exc)
            except OSError as exc:
                log.error("Network error: %s", exc)
                self.error_occurred.emit(
                    f"Network Interface Error: {exc}\n\n"
                    "Check that the server is running and the host address is correct."
                )
            except Exception as exc:
                log.exception("Unexpected error: %s", exc)
                self.error_occurred.emit(f"Unexpected error: {exc}")
            finally:
                self._ws         = None
                self._authorized = False

            if not self._running:
                break

            # ── Exponential backoff before retry ──────────────────────────
            if was_connected and self._code:
                total_down += backoff

                if total_down >= config.RECONNECT_ALERT_AFTER_S:
                    self.disconnected.emit(
                        f"Disconnected for {int(total_down)}s — session may have expired."
                    )
                    self._code = None  # Give up; require fresh code entry
                    break

                self.status_changed.emit(f"Reconnecting in {backoff:.0f}s…")
                log.info("Reconnecting in %.1fs…", backoff)

                slept = 0.0
                while slept < backoff and self._running:
                    await asyncio.sleep(0.5)
                    slept += 0.5

                backoff = min(backoff * 2, config.RECONNECT_BACKOFF_MAX_S)

                # Attempt reconnect with saved code
                self._pending_connect_code = code
            else:
                break

    # ─── Session Join ────────────────────────────────────────────────────────

    async def _join_session(self, code: str) -> None:
        if self._ws is None:
            log.error("Cannot join: WebSocket is None")
            return
        self._code = code
        payload = {
            "type":   "join",
            "code":   code,
            "peerId": PEER_ID,
        }
        log.info("Sending join request: %s", payload)
        await self._send(payload)
        log.info("Join request sent for code: %s", code)

    # ─── Receive Loop ────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """Reads all incoming messages and dispatches them."""
        async for raw in self._ws:
            if isinstance(raw, bytes):
                # Binary frame = video frame
                if self._authorized:
                    log.info("Received binary frame: %d bytes", len(raw))
                    self.frame_received.emit(raw)
                else:
                    log.warning("Received frame but not authorized")
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Non-JSON message received — ignored")
                continue

            await self._dispatch(msg)

    # ─── Message Dispatch ────────────────────────────────────────────────────

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        log.info("Received message: %s - Full: %s", msg_type, msg)

        if msg_type == "join_success":
            self.status_changed.emit("Waiting for target user approval…")
            # Inform the main window (separate slot for styled display)
            # We emit status_changed which the window catches

        elif msg_type == "error":
            err_msg = msg.get("message", "An error occurred on the server.")
            log.warning("Server returned error: %s", err_msg)
            self.error_occurred.emit(err_msg)
            self.session_denied.emit(err_msg)

        elif msg_type == "session_active":
            self._authorized = True
            self.session_active.emit()
            log.info("Session active — streaming authorised")

        elif msg_type == "session_denied":
            self._authorized = False
            self._code       = None
            self.session_denied.emit("The remote user denied the connection request.")

        elif msg_type == "session_revoked":
            self._authorized = False
            self._code       = None
            reason = msg.get("message", "Session was revoked by the remote user.")
            self.session_denied.emit(reason)

        elif msg_type == "peer_disconnected":
            role = msg.get("role", "unknown")
            self.status_changed.emit(f"Remote peer ({role}) disconnected — grace period active")
            self._authorized = False

        elif msg_type == "peer_reconnected":
            self._authorized = True
            self.status_changed.emit("🔴 Session Active — reconnected")

        elif msg_type == "session_expired":
            self._authorized = False
            self._code       = None
            reason = msg.get("message", "Session expired — grace period ended.")
            self.disconnected.emit(reason)

        elif msg_type == "clipboard":
            text = msg.get("text", "")
            self.clipboard_received.emit(text)

        # ── WebRTC Signaling Relay Hooks ──────────────────────────────────
        elif msg_type == "sdp_answer":
            log.info("[WebRTC Hook] SDP Answer received — aiortc handler here")
            # TODO: Pass msg['sdp'] to aiortc RTCPeerConnection.setRemoteDescription()

        elif msg_type == "ice_candidate":
            log.info("[WebRTC Hook] ICE Candidate received — aiortc handler here")
            # TODO: Pass msg['candidate'] to aiortc RTCPeerConnection.addIceCandidate()

        elif msg_type == "error":
            server_msg = msg.get("message", "Unknown server error")
            log.error("Server error: %s", server_msg)
            self.error_occurred.emit(f"Server Error: {server_msg}")

        else:
            log.debug("Unhandled message type: %s", msg_type)

    # ─── Send Helper ─────────────────────────────────────────────────────────

    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:
            log.warning("Send failed: %s", exc)


class NetworkThread(QThread):
    """
    QThread wrapper that owns a NetworkWorker and exposes its signals.
    The worker is moved to this thread so all asyncio I/O runs off the GUI thread.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.worker = NetworkWorker()
        self.worker.moveToThread(self)

    def run(self) -> None:
        self.worker.start_network()

    def stop(self) -> None:
        self.worker.stop()
        self.quit()
        self.wait(3000)
