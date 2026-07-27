"""
network.py — Controller PC WebSocket Client
============================================
Runs in a dedicated thread with an asyncio event loop.
Communicates with the GUI thread via direct method calls (Main → Network)
and pyqtSignals (Network → Main, which work because the GUI thread has a
running Qt event loop).

Main → Network (direct calls, thread-safe):
  - connect_to_session(code)
  - disconnect_session()
  - send_input_event(event)
  - send_clipboard(text)

Network → Main (Qt signals, delivered via GUI thread's event loop):
  - status_changed(str)
  - join_success(str)
  - session_active()
  - session_denied(str)
  - disconnected(str)
  - frame_received(bytes)
  - clipboard_received(str)
  - error_occurred(str)
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

from PyQt6.QtCore import QObject, QThread, pyqtSignal

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
    Receives commands from the GUI thread via direct method calls (not Qt signals,
    since the network thread does not run a Qt event loop).
    Emits results back to the GUI thread via pyqtSignals (which are delivered
    through the GUI thread's running Qt event loop).

    Signals (Network → GUI, all delivered on GUI thread):
        status_changed(str):         Human-readable status for the UI.
        join_success(str):           Controlled peer ID on successful join.
        session_active():            Session is authorised and streaming.
        session_denied(str):         Session was denied or expired.
        disconnected(str):           Connection dropped (reason string).
        frame_received(bytes):       Raw JPEG/WebP frame bytes.
        clipboard_received(str):     Clipboard text from controlled PC.
        error_occurred(str):         User-visible error description.
    """

    status_changed:      pyqtSignal = pyqtSignal(str)
    join_success:        pyqtSignal = pyqtSignal(str)
    session_active:      pyqtSignal = pyqtSignal()
    session_denied:      pyqtSignal = pyqtSignal(str)
    disconnected:        pyqtSignal = pyqtSignal(str)
    frame_received:      pyqtSignal = pyqtSignal(bytes)
    clipboard_received:  pyqtSignal = pyqtSignal(str)
    error_occurred:      pyqtSignal = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws:   Any = None
        self._code: Optional[str] = None
        self._authorized: bool = False
        self._running: bool = False

        # Thread-safe command queue for Main → Network communication.
        # The GUI thread pushes commands; the asyncio loop pops them.
        self._cmd_queue: asyncio.Queue[dict] = asyncio.Queue()

    # ─── QThread Entry ───────────────────────────────────────────────────────

    def start_network(self) -> None:
        """Called once when the QThread starts. Runs the asyncio event loop."""
        self._loop = asyncio.new_event_loop()
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

    # ─── Public API (called directly from GUI thread) ────────────────────────

    def connect_to_session(self, code: str) -> None:
        """Initiates a join request for the given 6-digit code.
        Called directly from the GUI thread (thread-safe)."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                self._cmd_queue.put_nowait,
                {"type": "connect", "code": code}
            )

    def disconnect_session(self) -> None:
        """Closes the current session cleanly.
        Called directly from the GUI thread (thread-safe)."""
        self._authorized = False
        self._code = None
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                self._cmd_queue.put_nowait,
                {"type": "disconnect"}
            )

    def send_input_event(self, event: dict) -> None:
        """Forwards a mouse/keyboard input event to the controlled PC.
        Called directly from the GUI thread (thread-safe)."""
        if not self._authorized or self._code is None:
            return
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                self._cmd_queue.put_nowait,
                {"type": "input", "payload": event}
            )

    def send_clipboard(self, text: str) -> None:
        """Sends local clipboard text to the controlled PC.
        Called directly from the GUI thread (thread-safe)."""
        if not self._authorized or self._code is None:
            return
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                self._cmd_queue.put_nowait,
                {"type": "clipboard", "text": text}
            )

    # ─── Asyncio Main Loop ───────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """Persistent connection loop with exponential backoff."""
        backoff = config.RECONNECT_BACKOFF_INITIAL_S
        total_down = 0.0
        was_connected = False
        current_code: Optional[str] = None

        while self._running:
            # ── Process command queue ────────────────────────────────────
            if current_code is None:
                cmd = await self._wait_for_connect_cmd()
                if cmd is None:
                    continue
                current_code = cmd["code"]
                self.status_changed.emit("Connecting to server…")

            try:
                async with websockets.connect(
                    config.SIGNALING_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=20 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    backoff = config.RECONNECT_BACKOFF_INITIAL_S
                    total_down = 0.0
                    was_connected = True
                    self._code = current_code
                    self.status_changed.emit("Connected — sending join request…")

                    await self._join_session(current_code)

                    reader_task = asyncio.create_task(self._receive_loop())
                    commander_task = asyncio.create_task(self._command_processor())

                    done, pending = await asyncio.wait(
                        [reader_task, commander_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    for task in pending:
                        task.cancel()

            except (ConnectionClosedOK, ConnectionClosedError) as exc:
                log.warning("WebSocket closed: %s", exc)
            except OSError as exc:
                log.error("Network error: %s", exc)
                self.error_occurred.emit(
                    f"Network Interface Error: {exc}\n\n"
                    "Check that the server is running and the host address is correct."
                )
                break
            except Exception as exc:
                log.exception("Unexpected error: %s", exc)
                self.error_occurred.emit(f"Unexpected error: {exc}")
                break
            finally:
                self._ws = None
                self._authorized = False

            if not self._running:
                break

            # ── Exponential backoff before reconnection ──────────────────
            if was_connected and current_code and self._code:
                total_down += backoff

                if total_down >= config.RECONNECT_ALERT_AFTER_S:
                    self.disconnected.emit(
                        f"Disconnected for {int(total_down)}s — session may have expired."
                    )
                    self._code = None
                    current_code = None
                    break

                self.status_changed.emit(f"Reconnecting in {backoff:.0f}s…")
                log.info("Reconnecting in %.1fs…", backoff)

                slept = 0.0
                while slept < backoff and self._running:
                    await asyncio.sleep(0.5)
                    slept += 0.5

                backoff = min(backoff * 2, config.RECONNECT_BACKOFF_MAX_S)
            else:
                current_code = None

    async def _wait_for_connect_cmd(self) -> Optional[dict]:
        """Wait for a 'connect' command from the GUI thread, or return None."""
        while self._running:
            try:
                cmd = await asyncio.wait_for(self._cmd_queue.get(), timeout=0.2)
                if cmd.get("type") == "connect":
                    self.status_changed.emit("Connecting to server…")
                    return cmd
            except asyncio.TimeoutError:
                continue
        return None

    async def _command_processor(self) -> None:
        """Process commands from the GUI thread during an active connection."""
        while self._running and self._ws:
            try:
                cmd = await asyncio.wait_for(self._cmd_queue.get(), timeout=0.2)
                cmd_type = cmd.get("type")

                if cmd_type == "disconnect":
                    await self._ws.close(1000, "User disconnected")
                    break
                elif cmd_type == "input":
                    if not self._authorized:
                        continue
                    await self._send({
                        "type": "input",
                        "code": self._code,
                        "payload": cmd["payload"],
                    })
                elif cmd_type == "clipboard":
                    if not self._authorized:
                        continue
                    await self._send({
                        "type": "clipboard",
                        "code": self._code,
                        "text": cmd["text"],
                    })
            except asyncio.TimeoutError:
                continue

    # ─── Session Join ────────────────────────────────────────────────────────

    async def _join_session(self, code: str) -> None:
        if self._ws is None:
            log.error("Cannot join: WebSocket is None")
            return
        payload = {
            "type": "join",
            "code": code,
            "peerId": PEER_ID,
        }
        log.info("Sending join request: %s", payload)
        await self._send(payload)

    # ─── Receive Loop ────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """Reads all incoming messages and dispatches them."""
        async for raw in self._ws:
            if isinstance(raw, bytes):
                if self._authorized:
                    self.frame_received.emit(raw)
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
        log.info("Controller received: %s", msg_type)

        if msg_type == "join_success":
            ctrl_id = msg.get("controlledPeerId", "unknown")
            self.join_success.emit(ctrl_id)

        elif msg_type == "error":
            err_msg = msg.get("message", "An error occurred on the server.")
            log.warning("Server returned error: %s", err_msg)
            self.error_occurred.emit(err_msg)

        elif msg_type == "session_active":
            self._authorized = True
            self.session_active.emit()
            log.info("Session active — streaming authorised")

        elif msg_type == "session_denied":
            self._authorized = False
            self._code = None
            self.session_denied.emit("The remote user denied the connection request.")

        elif msg_type == "session_revoked":
            self._authorized = False
            self._code = None
            reason = msg.get("message", "Session was revoked by the remote user.")
            self.session_denied.emit(reason)

        elif msg_type == "peer_disconnected":
            role = msg.get("role", "unknown")
            self.status_changed.emit(f"Remote peer ({role}) disconnected — grace period active")
            self._authorized = False

        elif msg_type == "peer_reconnected":
            self._authorized = True
            self.status_changed.emit("Session Active — reconnected")

        elif msg_type == "session_expired":
            self._authorized = False
            self._code = None
            reason = msg.get("message", "Session expired — grace period ended.")
            self.disconnected.emit(reason)

        elif msg_type == "clipboard":
            text = msg.get("text", "")
            self.clipboard_received.emit(text)

        elif msg_type == "sdp_answer":
            log.info("[WebRTC Hook] SDP Answer received — aiortc handler here")

        elif msg_type == "ice_candidate":
            log.info("[WebRTC Hook] ICE Candidate received — aiortc handler here")

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
    QThread wrapper that owns a NetworkWorker.
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
