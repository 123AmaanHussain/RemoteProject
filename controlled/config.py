"""
config.py — Controlled PC Agent Configuration
=============================================
Central configuration constants. Edit this file to tune performance, network
targets, and UI behaviour without touching application logic.
"""

from __future__ import annotations

import os

# ─── Network ──────────────────────────────────────────────────────────────────

#: WebSocket URL of the signaling server.
SIGNALING_URL: str = os.getenv("SIGNALING_URL", "wss://remote-desktop-signaling-56cl.onrender.com/signal")

# ─── Screen Capture ───────────────────────────────────────────────────────────

#: Target frames per second for screen capture. Values above 30 may cause
#: high CPU usage depending on resolution and encoding quality.
TARGET_FPS: int = 25

#: JPEG encoding quality (1–100). Lower = faster + smaller but more artifact.
JPEG_QUALITY: int = 75

#: Preferred codec — "jpeg" or "webp". WebP offers better quality-per-byte
#: at the cost of slightly higher encode time.
VIDEO_CODEC: str = "jpeg"

#: Monitor index to capture (0 = primary display as reported by mss).
CAPTURE_MONITOR_INDEX: int = 1  # mss uses 1-based index; 0 = all combined

# ─── Input Injection ──────────────────────────────────────────────────────────

#: Inter-event pacing delay in seconds (1–3 ms). Prevents OS event queue
#: flooding under high-frequency remote input.
INPUT_PACING_DELAY_S: float = 0.002  # 2 ms

#: Pixel distance the local mouse must move before remote injection is paused.
LOCAL_MOUSE_INTERCEPT_PX: int = 10

#: How long (seconds) to pause remote injection after a local mouse move.
LOCAL_MOUSE_PAUSE_S: float = 2.0

# ─── Clipboard ────────────────────────────────────────────────────────────────

#: How frequently (seconds) the clipboard monitor polls for changes.
CLIPBOARD_POLL_INTERVAL_S: float = 0.5

# ─── Reconnection ────────────────────────────────────────────────────────────

#: Initial backoff in seconds.
RECONNECT_BACKOFF_INITIAL_S: float = 1.0

#: Maximum backoff cap in seconds.
RECONNECT_BACKOFF_MAX_S: float = 30.0

#: After this many cumulative seconds disconnected, alert the user via tray.
RECONNECT_ALERT_AFTER_S: float = 30.0

# ─── Application Identity ─────────────────────────────────────────────────────

APP_NAME: str      = "Remote Support Agent"
APP_VERSION: str   = "1.0.0"
TRAY_TOOLTIP: str  = "Remote Support Agent — Idle"
PEER_ID_LENGTH: int = 32  # characters of the hex UUID used as peer identity
