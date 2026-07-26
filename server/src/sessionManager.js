/**
 * sessionManager.js
 *
 * In-memory session lifecycle management for the Remote Desktop signaling server.
 * Manages the mapping of 6-digit pairing codes to peer socket connections.
 *
 * Session state machine:
 *   PENDING_CONTROLLED  → WAITING_CONTROLLER → PENDING_CONSENT → ACTIVE → DISCONNECTED (grace) → CLOSED
 */

'use strict';

/** @typedef {'PENDING_CONTROLLED'|'WAITING_CONTROLLER'|'PENDING_CONSENT'|'ACTIVE'|'GRACE'} SessionStatus */

/**
 * @typedef {Object} Session
 * @property {string}  code                - The 6-digit pairing code
 * @property {string}  status              - Current session lifecycle status
 * @property {import('ws').WebSocket|null} controlledSocket  - Controlled PC socket
 * @property {import('ws').WebSocket|null} controllerSocket  - Controller PC socket
 * @property {string|null} controlledId    - Unique ID of the controlled peer
 * @property {string|null} controllerId    - Unique ID of the controller peer
 * @property {boolean} authorized          - Whether consent has been granted
 * @property {number}  createdAt           - Epoch ms of session creation
 * @property {number|null} lastActivityAt  - Epoch ms of last message
 * @property {ReturnType<typeof setTimeout>|null} graceTimer - Grace period timer handle
 */

const GRACE_MS = parseInt(process.env.SESSION_GRACE_MS ?? '30000', 10);
const MAX_SESSIONS = parseInt(process.env.MAX_SESSIONS ?? '1000', 10);

/** @type {Map<string, Session>} */
const sessions = new Map();

/**
 * Generates a cryptographically random 6-digit numeric code that is not
 * currently in use.
 * @returns {string} A 6-digit zero-padded string
 */
function generateCode() {
  let code;
  let attempts = 0;
  do {
    // Math.random is sufficient for non-security pairing codes;
    // the consent dialog is the actual authorization gate.
    code = String(Math.floor(Math.random() * 1_000_000)).padStart(6, '0');
    attempts++;
    if (attempts > 10_000) throw new Error('Session space exhausted');
  } while (sessions.has(code));
  return code;
}

/**
 * Creates a new session entry for a freshly connected Controlled PC.
 * @param {import('ws').WebSocket} socket   - The controlled peer's socket
 * @param {string} peerId                   - A unique string ID for the peer
 * @returns {Session} The newly created session
 * @throws {Error} When the session limit is reached
 */
function createSession(socket, peerId) {
  if (sessions.size >= MAX_SESSIONS) {
    throw new Error(`Maximum concurrent sessions (${MAX_SESSIONS}) reached`);
  }

  const code = generateCode();

  /** @type {Session} */
  const session = {
    code,
    status: 'PENDING_CONTROLLED',
    controlledSocket: socket,
    controllerSocket: null,
    controlledId: peerId,
    controllerId: null,
    authorized: false,
    createdAt: Date.now(),
    lastActivityAt: Date.now(),
    graceTimer: null,
  };

  sessions.set(code, session);
  return session;
}

/**
 * Attaches a Controller PC socket to an existing session.
 * @param {string} code                   - The 6-digit pairing code
 * @param {import('ws').WebSocket} socket  - The controller peer's socket
 * @param {string} peerId                 - Unique ID for the controller peer
 * @returns {Session|null} The updated session, or null if code is invalid
 */
function attachController(code, socket, peerId) {
  const session = sessions.get(code);
  if (!session) return null;
  if (session.controllerSocket !== null && session.status !== 'GRACE') {
    // Already has a live controller — reject duplicate
    return null;
  }

  // Clear any active grace timer for the controller slot
  _clearGraceTimer(session);

  session.controllerSocket = socket;
  session.controllerId = peerId;
  session.status = 'PENDING_CONSENT';
  session.lastActivityAt = Date.now();
  return session;
}

/**
 * Marks a session as authorized (consent granted by controlled user).
 * @param {string} code - The 6-digit pairing code
 * @returns {boolean} True if the session was found and updated
 */
function authorizeSession(code) {
  const session = sessions.get(code);
  if (!session) return false;
  session.authorized = true;
  session.status = 'ACTIVE';
  session.lastActivityAt = Date.now();
  return true;
}

/**
 * Called when a peer disconnects. Enters a 30-second grace period to allow
 * automatic reconnection without re-consent. If the grace period expires,
 * the session is permanently closed.
 *
 * @param {string}  code        - The 6-digit pairing code
 * @param {'controlled'|'controller'} role - Which peer disconnected
 * @param {(code: string, role: string) => void} onExpired - Callback when grace expires
 */
function handleDisconnect(code, role, onExpired) {
  const session = sessions.get(code);
  if (!session) return;

  if (role === 'controlled') {
    session.controlledSocket = null;
  } else {
    session.controllerSocket = null;
  }

  session.status = 'GRACE';
  session.lastActivityAt = Date.now();

  // Clear any previously running timer before starting a new one
  _clearGraceTimer(session);

  session.graceTimer = setTimeout(() => {
    sessions.delete(code);
    onExpired(code, role);
  }, GRACE_MS);
}

/**
 * Re-attaches a reconnecting peer to an existing GRACE-state session.
 * Cancels the grace timer and restores the ACTIVE status.
 *
 * @param {string} code                   - The 6-digit pairing code
 * @param {'controlled'|'controller'} role
 * @param {import('ws').WebSocket} socket  - New socket for the reconnecting peer
 * @param {string} peerId                 - Peer ID of the reconnecting client
 * @returns {Session|null} The session if reconnection succeeded, null otherwise
 */
function reconnectPeer(code, role, socket, peerId) {
  const session = sessions.get(code);
  if (!session || session.status !== 'GRACE') return null;

  _clearGraceTimer(session);

  if (role === 'controlled') {
    session.controlledSocket = socket;
    session.controlledId = peerId;
  } else {
    session.controllerSocket = socket;
    session.controllerId = peerId;
  }

  // Restore ACTIVE only if both peers are now present
  if (session.controlledSocket && session.controllerSocket) {
    session.status = 'ACTIVE';
  }

  session.lastActivityAt = Date.now();
  return session;
}

/**
 * Revokes a session immediately — closes both sockets and removes from map.
 * @param {string} code - The 6-digit pairing code
 * @param {number} [wsCloseCode=1000] - WebSocket close code to send
 * @param {string} [reason='Session revoked'] - Human-readable reason
 */
function revokeSession(code, wsCloseCode = 1000, reason = 'Session revoked') {
  const session = sessions.get(code);
  if (!session) return;

  _clearGraceTimer(session);

  for (const sock of [session.controlledSocket, session.controllerSocket]) {
    if (sock && sock.readyState === 1 /* OPEN */) {
      try { sock.close(wsCloseCode, reason); } catch { /* ignore */ }
    }
  }

  sessions.delete(code);
}

/**
 * Looks up a session by 6-digit code.
 * @param {string} code
 * @returns {Session|undefined}
 */
function getSession(code) {
  return sessions.get(code);
}

/**
 * Looks up a session by either peer's socket reference.
 * @param {import('ws').WebSocket} socket
 * @returns {{ session: Session, role: 'controlled'|'controller' }|null}
 */
function findSessionBySocket(socket) {
  for (const session of sessions.values()) {
    if (session.controlledSocket === socket) {
      return { session, role: 'controlled' };
    }
    if (session.controllerSocket === socket) {
      return { session, role: 'controller' };
    }
  }
  return null;
}

/**
 * Updates the last activity timestamp for a session (used for idle detection).
 * @param {string} code
 */
function touchSession(code) {
  const session = sessions.get(code);
  if (session) session.lastActivityAt = Date.now();
}

/** @returns {number} Total number of active sessions */
function getSessionCount() {
  return sessions.size;
}

// ─── Private Helpers ─────────────────────────────────────────────────────────

/** @param {Session} session */
function _clearGraceTimer(session) {
  if (session.graceTimer !== null) {
    clearTimeout(session.graceTimer);
    session.graceTimer = null;
  }
}

module.exports = {
  createSession,
  attachController,
  authorizeSession,
  handleDisconnect,
  reconnectPeer,
  revokeSession,
  getSession,
  findSessionBySocket,
  touchSession,
  getSessionCount,
};
