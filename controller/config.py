"""
config.py — Controller PC Client Configuration
===============================================
Central configuration constants. Adjust these to match your server deployment.
"""

from __future__ import annotations

import os

# ─── Network ──────────────────────────────────────────────────────────────────

#: WebSocket URL of the signaling server.
SIGNALING_URL: str = os.getenv("SIGNALING_URL", "ws://localhost:3000/signal")

# ─── Reconnection ────────────────────────────────────────────────────────────

RECONNECT_BACKOFF_INITIAL_S: float = 1.0
RECONNECT_BACKOFF_MAX_S:     float = 30.0
RECONNECT_ALERT_AFTER_S:     float = 30.0

# ─── Video Rendering ─────────────────────────────────────────────────────────

#: Maximum frames per second to render in the viewport.
#: Higher values consume more CPU on the controller side.
MAX_RENDER_FPS: int = 30

# ─── Application Identity ─────────────────────────────────────────────────────

APP_NAME:    str = "Remote Desktop Controller"
APP_VERSION: str = "1.0.0"
WINDOW_TITLE: str = f"{APP_NAME} v{APP_VERSION}"
