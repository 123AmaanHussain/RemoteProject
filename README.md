# 🖥 Remote Desktop Application

> A production-grade, three-component remote desktop system with full consent workflow, encrypted signaling, and real-time screen streaming.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        System Architecture                                   │
│                                                                              │
│   ┌─────────────────┐         ┌──────────────────┐       ┌───────────────┐  │
│   │  Controller PC  │◄───────►│ Signaling Server │◄─────►│ Controlled PC │  │
│   │   (PyQt6 GUI)   │WebSocket│  (Fastify + ws)  │WebSocket│ (pystray Agent)│ │
│   └────────┬────────┘         └────────┬─────────┘       └──────┬────────┘  │
│            │                           │                         │           │
│            │  Control Channel (JSON)   │                         │           │
│            │  Video Channel (Binary)   │                         │           │
│            │  WebRTC SDP/ICE relay     │                         │           │
│            │                           │                         │           │
│   ┌────────▼────────┐                  │              ┌──────────▼────────┐  │
│   │  RemoteViewport │                  │              │  ScreenCapture    │  │
│   │  - JPEG render  │                  │              │  - mss + OpenCV   │  │
│   │  - Mouse/KB cap │                  │              │  InputInjector    │  │
│   │  - F11 fullscr  │                  │              │  - pynput inject  │  │
│   └─────────────────┘                  │              │  ClipboardMonitor │  │
│                                         │              │  - pyperclip poll │  │
│                                         │              └───────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
RemoteProject/
├── server/                         # Node.js Signaling Server
│   ├── package.json
│   ├── .env.example
│   └── src/
│       ├── index.js                # Fastify entry point, heartbeat, shutdown
│       ├── sessionManager.js       # In-memory session state machine + grace period
│       ├── messageRouter.js        # Full WebSocket message routing & relay
│       └── rateLimiter.js          # Per-socket token bucket rate limiter
│
├── controlled/                     # Controlled PC System Tray Agent (Python)
│   ├── requirements.txt
│   ├── config.py                   # All tunable constants
│   ├── main.py                     # pystray tray agent entry point
│   ├── network.py                  # WebSocket client + backoff + session lifecycle
│   ├── capture.py                  # mss + cv2 screen capture thread
│   ├── input_handler.py            # pynput mouse/keyboard injection + local intercept
│   ├── clipboard_sync.py           # pyperclip clipboard monitor (anti-loop)
│   └── consent.py                  # Native OS consent dialog (tkinter)
│
└── controller/                     # Controller PC PyQt6 GUI
    ├── requirements.txt
    ├── config.py
    ├── main.py                     # QApplication entry point + signal wiring
    ├── network.py                  # NetworkWorker (QThread + asyncio)
    └── ui/
        ├── __init__.py
        ├── main_window.py          # Main window: connect panel + viewport page
        ├── remote_viewport.py      # Live stream display + input capture widget
        └── toast.py                # Animated toast notification widget
```

---

## Prerequisites

| Component        | Requirement              |
|------------------|--------------------------|
| Node.js          | v18.0.0 or later         |
| Python           | 3.11 or later            |
| Operating System | Windows 10/11 (primary)  |
| Network          | LAN or internet with open port 3000 |

---

## Installation & Setup

### 1. Signaling Server

```bash
cd server
npm install
```

Copy the environment file and adjust as needed:
```bash
copy .env.example .env
```

Start the server:
```bash
npm start
# or for development with auto-reload:
npm run dev
```

The server will listen on `ws://0.0.0.0:3000/signal`.
Health check: `http://localhost:3000/health`

---

### 2. Controlled PC Agent

```bash
cd controlled
pip install -r requirements.txt
```

> **Note:** On Windows, `pyperclip` requires no extra setup. On Linux, install `xclip` or `xdotool`.
> `pynput` on Linux requires X11 or Wayland with appropriate permissions.

Edit `config.py` to point to your server:
```python
SIGNALING_URL = "ws://YOUR_SERVER_IP:3000/signal"
```

Run the agent:
```bash
python main.py
```

The agent will:
1. Connect to the server and receive a **6-digit session code**
2. Display the code in the system tray tooltip (right-click → hover over Code)
3. Show a **native OS consent dialog** when a controller connects
4. Begin streaming only after the user clicks **YES**

---

### 3. Controller PC Client

```bash
cd controller
pip install -r requirements.txt
```

Edit `config.py` to point to your server:
```python
SIGNALING_URL = "ws://YOUR_SERVER_IP:3000/signal"
```

Run the controller:
```bash
python main.py
```

Enter the 6-digit code from the tray agent and click **Connect**.

---

## Session Workflow

```
Controlled PC                 Server                   Controller PC
     │                           │                           │
     │──── register ────────────►│                           │
     │◄─── registered (code) ────│                           │
     │                           │                           │
     │                           │◄──── join (code) ─────────│
     │◄─── consent_request ──────│                           │
     │                           │──── join_success ─────────►│
     │                           │                           │
     │  [USER SEES DIALOG]        │    "Waiting for approval" │
     │──── consent_result ───────►│                           │
     │         (approved=true)    │──── session_active ───────►│
     │◄─── session_active ───────│                           │
     │                           │                           │
     │══════ Binary Video Frames ═════════════════════════════►│
     │◄═════ Input Events (JSON) ═══════════════════════════════│
     │◄═════ Clipboard Sync ════════════════════════════════════│
```

---

## Consent Dialog

When a controller connects, the Controlled PC shows:

```
┌─────────────────────────────────────────────────────┐
│ ⚠ Remote Support Request                           │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Remote Support Request                             │
│                                                     │
│  A connection has been initiated from terminal ID:  │
│    [550e8400-e29b-41d4-a716-446655440000]           │
│                                                     │
│  Do you grant full remote access permissions to     │
│  view your screen and manage system inputs for      │
│  this session?                                      │
│                                                     │
│  Click YES to Approve or NO to Deny.               │
│                                                     │
│              [  YES  ]      [  NO  ]               │
└─────────────────────────────────────────────────────┘
```

- **YES**: Session becomes active, streaming begins immediately.
- **NO**: Session code is invalidated, controller receives "session_denied".
- **Ignored for 120 seconds**: Auto-denies for safety.

---

## System Tray Menu (Controlled PC)

Right-click the tray icon to access:

```
Remote Support Agent
├── Status: Session Code: 482910 — Waiting for controller
├── Code: 482910
├── ─────────────────────
├── Copy Session Code
├── Revoke Permissions
├── ─────────────────────
└── Exit
```

- **Copy Session Code** — Copies the active 6-digit code to clipboard for easy sharing.
- **Revoke Permissions** — Immediately ends any active session and invalidates the code.
- **Exit** — Gracefully shuts down the agent and all background threads.

---

## Configuration Reference

### Server (`server/.env`)

| Variable              | Default  | Description                                      |
|-----------------------|----------|--------------------------------------------------|
| `PORT`                | `3000`   | TCP port to listen on                           |
| `HOST`                | `0.0.0.0`| Bind address                                    |
| `LOG_LEVEL`           | `info`   | Pino log level (`trace/debug/info/warn/error`)  |
| `SESSION_GRACE_MS`    | `30000`  | Grace period ms before session cleanup          |
| `MAX_SESSIONS`        | `1000`   | Max concurrent sessions                         |
| `RATE_LIMIT_MAX`      | `120`    | Max messages per socket per window              |
| `RATE_LIMIT_WINDOW_MS`| `1000`   | Rate limit window in ms                         |

### Controlled PC (`controlled/config.py`)

| Variable                   | Default         | Description                              |
|----------------------------|-----------------|------------------------------------------|
| `SIGNALING_URL`            | `ws://localhost:3000/signal` | Server WebSocket URL      |
| `TARGET_FPS`               | `25`            | Screen capture frames per second         |
| `JPEG_QUALITY`             | `75`            | JPEG quality (1–100)                    |
| `VIDEO_CODEC`              | `"jpeg"`        | `"jpeg"` or `"webp"`                   |
| `CAPTURE_MONITOR_INDEX`    | `1`             | mss monitor index (1 = primary)         |
| `INPUT_PACING_DELAY_S`     | `0.002`         | Inter-event pacing (2 ms)               |
| `LOCAL_MOUSE_INTERCEPT_PX` | `10`            | Pixel threshold for local intercept     |
| `LOCAL_MOUSE_PAUSE_S`      | `2.0`           | Pause duration after local mouse move   |
| `CLIPBOARD_POLL_INTERVAL_S`| `0.5`           | Clipboard polling frequency (seconds)   |
| `RECONNECT_BACKOFF_INITIAL_S`| `1.0`         | Initial reconnect delay                 |
| `RECONNECT_BACKOFF_MAX_S`  | `30.0`          | Maximum reconnect delay cap             |

### Controller PC (`controller/config.py`)

| Variable          | Default                       | Description                          |
|-------------------|-------------------------------|--------------------------------------|
| `SIGNALING_URL`   | `ws://localhost:3000/signal`  | Server WebSocket URL                 |
| `MAX_RENDER_FPS`  | `30`                          | Max frame render rate in viewport    |

---

## Keyboard Shortcuts (Controller Viewport)

| Shortcut | Action                                            |
|----------|---------------------------------------------------|
| `F11`    | Toggle borderless full-screen / windowed mode     |
| All other keys | Forwarded to the controlled PC           |

---

## Reconnection & Resilience

### Controlled PC Agent
- Uses **exponential backoff**: 1s → 2s → 4s → 8s → 16s → 30s (cap).
- If disconnection persists beyond 30 cumulative seconds, shows a tray alert.
- Maintains `session_code` across reconnects for seamless re-join without re-consent.

### Controller PC Client
- Same exponential backoff strategy.
- Shows toast notification on reconnect attempt.
- After 30s of failed reconnection, prompts the user to enter a new code.

### Server Grace Period
- When a peer drops, the session is held for **30 seconds**.
- If the peer reconnects in time, the session resumes **without re-consent**.
- If grace expires, both peers are notified via `session_expired`.

---

## WebRTC Upgrade Path

The codebase includes clearly marked hooks for upgrading the video channel from WebSocket binary frames to peer-to-peer WebRTC using `aiortc`:

```python
# In controlled/network.py:
elif msg_type == "sdp_offer":
    log.info("[WebRTC Hook] SDP Offer received — aiortc handler here")
    # TODO: Pass msg['sdp'] to aiortc PC.setRemoteDescription()

elif msg_type == "ice_candidate":
    log.info("[WebRTC Hook] ICE Candidate received — aiortc handler here")
    # TODO: Pass msg['candidate'] to aiortc PC.addIceCandidate()
```

To enable WebRTC:
1. `pip install aiortc` on both Controller and Controlled.
2. Replace the TODO comments with `RTCPeerConnection` handling.
3. The server already relays SDP offers/answers and ICE candidates correctly.

---

## Security Considerations

> ⚠ **This application is designed for trusted network environments.**

- **No encryption on the WebSocket transport** in this configuration. For production, place the server behind **Nginx with TLS** (`wss://`).
- Session codes are **not cryptographically secure** — they are convenience codes. The consent dialog is the primary authorization gate.
- **Rate limiting** prevents event flooding on the server (120 msgs/s per socket).
- Remote input injection is disabled until `consent_result: approved=true` is received.
- The controlled user can **revoke permissions** at any time from the tray menu.

### Recommended Production Hardening
1. Enable TLS: `wss://` with Nginx reverse proxy + Let's Encrypt.
2. Add JWT authentication to the WebSocket upgrade handshake.
3. Store session codes in Redis for multi-server deployments.
4. Restrict CORS origin in `server/src/index.js`.
5. Set `NODE_ENV=production` to disable pino-pretty (use structured JSON logs).

---

## Troubleshooting

### "Session code not found or already occupied"
- The 6-digit code shown in the tray has expired or the controlled agent restarted.
- Wait for the tray to show a new code and try again.

### Black screen in viewport
- The controlled agent may still be in the consent dialog waiting state.
- Check that the user on the controlled PC clicked **YES**.

### `pyperclip.PyperclipException` on Linux
```bash
sudo apt-get install xclip
# or
sudo apt-get install xdotool
```

### `pynput` fails to inject on Linux/Wayland
- Run with `XDG_SESSION_TYPE=x11` or use an X11 session.
- Ensure the process has `DISPLAY` set: `export DISPLAY=:0`.

### High CPU on Controlled PC
- Lower `TARGET_FPS` in `config.py` (e.g., 15 FPS).
- Lower `JPEG_QUALITY` to 60.
- Reduce `CAPTURE_MONITOR_INDEX` to capture a smaller monitor.

### "Rate limit exceeded"
- The controller is sending input events too fast.
- This is handled automatically. The rate limiter resets every second.

---

## Dependencies Summary

### Server
| Package | Version | Purpose |
|---------|---------|---------|
| `fastify` | ^4.26 | HTTP/WS framework |
| `@fastify/websocket` | ^10.0 | WebSocket plugin |
| `@fastify/cors` | ^9.0 | CORS headers |
| `@fastify/rate-limit` | ^9.1 | Request rate limiting |
| `dotenv` | ^16.4 | Environment config |
| `pino-pretty` | ^11.0 | Dev log formatting |

### Controlled PC
| Package | Purpose |
|---------|---------|
| `pystray` + `Pillow` | System tray icon |
| `mss` | Fast screen capture |
| `opencv-python-headless` | JPEG/WebP encoding |
| `numpy` | Array operations for cv2 |
| `pynput` | Mouse & keyboard injection |
| `pyperclip` | Clipboard read/write |
| `websockets` | Async WebSocket client |

### Controller PC
| Package | Purpose |
|---------|---------|
| `PyQt6` | GUI framework |
| `websockets` | Async WebSocket client |
| `pyperclip` | Clipboard write |
| `Pillow` | Image utility (optional) |

---

## License

MIT — for internal/authorized remote support use only.
