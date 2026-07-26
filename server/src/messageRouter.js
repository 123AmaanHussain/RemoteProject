/**
 * messageRouter.js
 *
 * Handles parsing, validation, and routing of all WebSocket messages between
 * the Controlled PC agent and the Controller PC client through the signaling server.
 *
 * ─── Message Protocol ────────────────────────────────────────────────────────
 * All messages are UTF-8 JSON strings with a top-level `type` field.
 * Binary frames are forwarded raw (video stream data) without JSON parsing.
 *
 * Controller → Server:
 *   { type: 'join',         code: '123456', peerId: '<uuid>' }
 *   { type: 'reconnect',    code: '123456', peerId: '<uuid>', role: 'controller' }
 *   { type: 'input',        code: '123456', payload: <InputEvent> }
 *   { type: 'clipboard',    code: '123456', text: '<string>' }
 *   { type: 'sdp_offer',    code: '123456', sdp: '<SDP string>' }
 *   { type: 'ice_candidate',code: '123456', candidate: <RTCIceCandidate> }
 *
 * Controlled PC → Server:
 *   { type: 'register',     code: '<generated>', peerId: '<uuid>' }
 *   { type: 'reconnect',    code: '123456', peerId: '<uuid>', role: 'controlled' }
 *   { type: 'consent_result', code: '123456', approved: boolean }
 *   { type: 'clipboard',    code: '123456', text: '<string>' }
 *   { type: 'sdp_answer',   code: '123456', sdp: '<SDP string>' }
 *   { type: 'ice_candidate',code: '123456', candidate: <RTCIceCandidate> }
 *
 * Server → Client (events):
 *   { type: 'registered',   code: '<6-digit>' }
 *   { type: 'join_success',  code: '...', controlledPeerId: '...' }
 *   { type: 'consent_request', code: '...', controllerId: '...' }
 *   { type: 'session_active' }
 *   { type: 'session_denied' }
 *   { type: 'peer_disconnected', role: '...' }
 *   { type: 'peer_reconnected',  role: '...' }
 *   { type: 'session_expired',   code: '...' }
 *   { type: 'error',         message: '...' }
 */

'use strict';

const sessionManager = require('./sessionManager');
const rateLimiter    = require('./rateLimiter');

/**
 * Sends a JSON message to a socket safely (checks readyState first).
 * @param {import('ws').WebSocket} socket
 * @param {object} payload
 */
function safeSend(socket, payload) {
  if (socket && socket.readyState === 1 /* WebSocket.OPEN */) {
    socket.send(JSON.stringify(payload));
  }
}

/**
 * Validates that a 6-digit code string is syntactically correct.
 * @param {unknown} code
 * @returns {boolean}
 */
function isValidCode(code) {
  return typeof code === 'string' && /^\d{6}$/.test(code);
}

/**
 * Validates that a peerId is a non-empty string (UUID-ish).
 * @param {unknown} id
 * @returns {boolean}
 */
function isValidPeerId(id) {
  return typeof id === 'string' && id.length > 0 && id.length <= 128;
}

/**
 * Core message dispatch function. Called for every text-frame WebSocket message.
 *
 * @param {import('fastify').FastifyInstance} fastify - For logging
 * @param {import('ws').WebSocket}            socket  - The sending socket
 * @param {Buffer|string}                     rawData - Raw WebSocket data
 */
function handleMessage(fastify, socket, rawData) {
  // ── Rate check ──────────────────────────────────────────────────────────
  if (!rateLimiter.allow(socket)) {
    safeSend(socket, { type: 'error', message: 'Rate limit exceeded. Slow down.' });
    return;
  }

  // ── Parse JSON ──────────────────────────────────────────────────────────
  let msg;
  try {
    msg = JSON.parse(rawData.toString('utf-8'));
  } catch {
    safeSend(socket, { type: 'error', message: 'Invalid JSON payload.' });
    return;
  }

  if (!msg || typeof msg.type !== 'string') {
    safeSend(socket, { type: 'error', message: 'Missing required field: type.' });
    return;
  }

  fastify.log.debug({ msgType: msg.type }, 'Routing message');

  switch (msg.type) {

    // ── Controlled PC registers, receives its pairing code ────────────────
    case 'register': {
      if (!isValidPeerId(msg.peerId)) {
        safeSend(socket, { type: 'error', message: 'Invalid peerId in register.' });
        return;
      }
      let session;
      try {
        session = sessionManager.createSession(socket, msg.peerId);
      } catch (err) {
        safeSend(socket, { type: 'error', message: err.message });
        return;
      }
      fastify.log.info({ code: session.code, peerId: msg.peerId }, 'Session created');
      safeSend(socket, { type: 'registered', code: session.code });
      break;
    }

    // ── Controller PC joins with the 6-digit code ─────────────────────────
    case 'join': {
      if (!isValidCode(msg.code)) {
        safeSend(socket, { type: 'error', message: 'Invalid or missing session code.' });
        return;
      }
      if (!isValidPeerId(msg.peerId)) {
        safeSend(socket, { type: 'error', message: 'Invalid peerId in join.' });
        return;
      }

      const session = sessionManager.attachController(msg.code, socket, msg.peerId);
      if (!session) {
        safeSend(socket, { type: 'error', message: 'Session code not found or already occupied.' });
        return;
      }

      fastify.log.info({ code: msg.code, controllerId: msg.peerId }, 'Controller joined — awaiting consent');

      // Notify the controller it is now waiting for consent
      safeSend(socket, {
        type: 'join_success',
        code: msg.code,
        controlledPeerId: session.controlledId,
      });

      // Ask the controlled PC to display its consent dialog
      safeSend(session.controlledSocket, {
        type: 'consent_request',
        code: msg.code,
        controllerId: msg.peerId,
      });
      break;
    }

    // ── Controlled PC sends the consent decision ──────────────────────────
    case 'consent_result': {
      if (!isValidCode(msg.code)) {
        safeSend(socket, { type: 'error', message: 'Invalid code in consent_result.' });
        return;
      }

      const session = sessionManager.getSession(msg.code);
      if (!session) {
        safeSend(socket, { type: 'error', message: 'Session not found.' });
        return;
      }
      // Ensure this message originates from the controlled PC
      if (session.controlledSocket !== socket) {
        safeSend(socket, { type: 'error', message: 'Unauthorized consent_result sender.' });
        return;
      }

      if (msg.approved === true) {
        sessionManager.authorizeSession(msg.code);
        fastify.log.info({ code: msg.code }, 'Session authorized by controlled user');
        safeSend(session.controlledSocket, { type: 'session_active', code: msg.code });
        safeSend(session.controllerSocket, { type: 'session_active', code: msg.code });
      } else {
        fastify.log.info({ code: msg.code }, 'Session denied by controlled user');
        safeSend(session.controllerSocket, { type: 'session_denied', code: msg.code });
        sessionManager.revokeSession(msg.code, 1000, 'Consent denied');
      }
      break;
    }

    // ── Peer reconnect attempt on an existing GRACE-state session ─────────
    case 'reconnect': {
      if (!isValidCode(msg.code) || !isValidPeerId(msg.peerId)) {
        safeSend(socket, { type: 'error', message: 'Invalid reconnect payload.' });
        return;
      }
      const role = msg.role === 'controlled' ? 'controlled' : 'controller';
      const session = sessionManager.reconnectPeer(msg.code, role, socket, msg.peerId);
      if (!session) {
        // Grace period may have expired — tell client to start fresh
        safeSend(socket, { type: 'session_expired', code: msg.code });
        return;
      }

      fastify.log.info({ code: msg.code, role }, 'Peer reconnected within grace period');
      safeSend(socket, { type: 'reconnect_success', code: msg.code, status: session.status });

      // Notify the other peer
      const peerSocket = role === 'controlled'
        ? session.controllerSocket
        : session.controlledSocket;

      if (peerSocket) {
        safeSend(peerSocket, { type: 'peer_reconnected', role });
      }
      break;
    }

    // ── Input events (mouse / keyboard) — relay from controller to controlled ──
    case 'input': {
      if (!isValidCode(msg.code)) {
        safeSend(socket, { type: 'error', message: 'Invalid code in input event.' });
        return;
      }
      const session = sessionManager.getSession(msg.code);
      if (!session?.authorized) return; // Silently discard pre-auth input
      if (session.controllerSocket !== socket) return; // Must come from controller

      sessionManager.touchSession(msg.code);
      safeSend(session.controlledSocket, {
        type: 'input',
        payload: msg.payload,
      });
      break;
    }

    // ── Clipboard sync — relay in both directions ─────────────────────────
    case 'clipboard': {
      if (!isValidCode(msg.code) || typeof msg.text !== 'string') return;
      const session = sessionManager.getSession(msg.code);
      if (!session?.authorized) return;

      sessionManager.touchSession(msg.code);

      // Determine the destination (the other peer)
      const destSocket = session.controlledSocket === socket
        ? session.controllerSocket
        : session.controlledSocket;

      if (destSocket) {
        safeSend(destSocket, { type: 'clipboard', text: msg.text });
      }
      break;
    }

    // ── WebRTC SDP Offer (Controller → Controlled) ────────────────────────
    case 'sdp_offer': {
      if (!isValidCode(msg.code) || typeof msg.sdp !== 'string') return;
      const session = sessionManager.getSession(msg.code);
      if (!session?.authorized) return;
      if (session.controllerSocket !== socket) return;

      fastify.log.debug({ code: msg.code }, 'Relaying SDP offer');
      safeSend(session.controlledSocket, { type: 'sdp_offer', sdp: msg.sdp });
      break;
    }

    // ── WebRTC SDP Answer (Controlled → Controller) ───────────────────────
    case 'sdp_answer': {
      if (!isValidCode(msg.code) || typeof msg.sdp !== 'string') return;
      const session = sessionManager.getSession(msg.code);
      if (!session?.authorized) return;
      if (session.controlledSocket !== socket) return;

      fastify.log.debug({ code: msg.code }, 'Relaying SDP answer');
      safeSend(session.controllerSocket, { type: 'sdp_answer', sdp: msg.sdp });
      break;
    }

    // ── WebRTC ICE Candidates (bidirectional relay) ───────────────────────
    case 'ice_candidate': {
      if (!isValidCode(msg.code) || !msg.candidate) return;
      const session = sessionManager.getSession(msg.code);
      if (!session?.authorized) return;

      const destSocket = session.controlledSocket === socket
        ? session.controllerSocket
        : session.controlledSocket;

      if (destSocket) {
        safeSend(destSocket, { type: 'ice_candidate', candidate: msg.candidate });
      }
      break;
    }

    // ── Explicit revoke from controlled user ──────────────────────────────
    case 'revoke': {
      if (!isValidCode(msg.code)) return;
      const session = sessionManager.getSession(msg.code);
      if (!session) return;
      if (session.controlledSocket !== socket) return; // Only controlled can revoke

      fastify.log.info({ code: msg.code }, 'Session revoked by controlled user');
      if (session.controllerSocket) {
        safeSend(session.controllerSocket, {
          type: 'session_revoked',
          message: 'The remote user has ended the session.',
        });
      }
      sessionManager.revokeSession(msg.code, 1000, 'Revoked by controlled user');
      break;
    }

    default:
      safeSend(socket, { type: 'error', message: `Unknown message type: ${msg.type}` });
  }
}

/**
 * Called when a WebSocket connection closes unexpectedly or normally.
 * Enters the 30-second grace period for that peer's slot.
 *
 * @param {import('fastify').FastifyInstance} fastify
 * @param {import('ws').WebSocket} socket
 */
function handleClose(fastify, socket) {
  rateLimiter.cleanup(socket);

  const found = sessionManager.findSessionBySocket(socket);
  if (!found) return;

  const { session, role } = found;
  fastify.log.warn({ code: session.code, role }, 'Peer disconnected — entering grace period');

  // Notify the remaining peer
  const peerSocket = role === 'controlled'
    ? session.controllerSocket
    : session.controlledSocket;

  if (peerSocket) {
    safeSend(peerSocket, { type: 'peer_disconnected', role });
  }

  sessionManager.handleDisconnect(session.code, role, (code, expiredRole) => {
    fastify.log.info({ code, expiredRole }, 'Grace period expired — session closed');
    // Notify the remaining peer that the session is gone for good
    const remaining = sessionManager.getSession(code); // Already deleted, will be undefined
    // Attempt one last notification to any lingering open socket on peerSocket ref
    if (peerSocket && peerSocket.readyState === 1) {
      safeSend(peerSocket, {
        type: 'session_expired',
        code,
        message: `Remote peer (${expiredRole}) did not reconnect within the grace period.`,
      });
    }
  });
}

module.exports = { handleMessage, handleClose, safeSend };
