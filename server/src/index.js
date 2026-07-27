/**
 * index.js — Remote Desktop Signaling Server
 *
 * Entry point for the Fastify + WebSocket signaling server.
 * Handles peer matchmaking, session lifecycle, and full-duplex
 * message relay between the Controlled PC agent and Controller client.
 *
 * Usage:
 *   node src/index.js
 *
 * Environment variables (see .env.example):
 *   PORT, HOST, LOG_LEVEL, SESSION_GRACE_MS, MAX_SESSIONS,
 *   RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_MS
 */

'use strict';

require('dotenv').config();

const Fastify        = require('fastify');
const fastifyWs      = require('@fastify/websocket');
const fastifyCors    = require('@fastify/cors');
const messageRouter  = require('./messageRouter');
const sessionManager = require('./sessionManager');

const PORT     = parseInt(process.env.PORT ?? '3000', 10);
const HOST     = process.env.HOST ?? '0.0.0.0';
const LOG_LEVEL = process.env.LOG_LEVEL ?? 'info';

// ─── Fastify Instance ────────────────────────────────────────────────────────

const fastify = Fastify({
  logger: {
    level: LOG_LEVEL,
    // Use pino-pretty for development readability
    transport: process.env.NODE_ENV !== 'production'
      ? { target: 'pino-pretty', options: { colorize: true } }
      : undefined,
  },
});

// ─── Plugin Registration ─────────────────────────────────────────────────────

fastify.register(fastifyCors, {
  origin: '*', // Tighten this to specific origins in production
  methods: ['GET'],
});

fastify.register(fastifyWs);

// ─── Health Check Route ──────────────────────────────────────────────────────

fastify.get('/health', async () => ({
  status: 'ok',
  sessions: sessionManager.getSessionCount(),
  uptime: process.uptime(),
  timestamp: new Date().toISOString(),
}));

// ─── WebSocket Signaling Route ───────────────────────────────────────────────

fastify.register(async (instance) => {
  instance.get('/signal', { websocket: true }, (socket, _req) => {
    fastify.log.info('New WebSocket connection established');

    // ── Incoming message (text or binary) ────────────────────────────────
    socket.on('message', (rawData, isBinary) => {
      if (isBinary) {
        // Binary frame = video stream data from controlled PC
        // Forward to controller peer if session is active
        messageRouter.handleBinaryFrame(fastify, socket, rawData);
        return;
      }
      messageRouter.handleMessage(fastify, socket, rawData);
    });

    // ── Connection closed (clean or error) ──────────────────────────────
    socket.on('close', (code, reason) => {
      fastify.log.info(
        { code, reason: reason.toString() },
        'WebSocket connection closed',
      );
      messageRouter.handleClose(fastify, socket);
    });

    // ── Socket-level error ──────────────────────────────────────────────
    socket.on('error', (err) => {
      fastify.log.error({ err }, 'WebSocket socket error');
      messageRouter.handleClose(fastify, socket);
    });

    // ── Keep-alive ping/pong ────────────────────────────────────────────
    socket.on('pong', () => {
      // Socket is alive; used by the heartbeat below
      socket._isAlive = true;
    });

    socket._isAlive = true;
  });
});

// ─── WebSocket Heartbeat ─────────────────────────────────────────────────────
// Detects zombie connections that have dropped without a proper close handshake.

const HEARTBEAT_INTERVAL_MS = 20_000; // Ping every 20 seconds

function startHeartbeat(wss) {
  const interval = setInterval(() => {
    wss.clients.forEach((socket) => {
      if (socket._isAlive === false) {
        fastify.log.warn('Terminating zombie WebSocket (no pong received)');
        messageRouter.handleClose(fastify, socket);
        return socket.terminate();
      }
      socket._isAlive = false;
      try { socket.ping(); } catch { /* ignore if socket is already closed */ }
    });
  }, HEARTBEAT_INTERVAL_MS);

  // Stop the heartbeat when the server shuts down
  wss.on('close', () => clearInterval(interval));
}

// ─── Graceful Shutdown ───────────────────────────────────────────────────────

async function shutdown(signal) {
  fastify.log.info({ signal }, 'Received shutdown signal — closing server gracefully');
  await fastify.close();
  process.exit(0);
}

process.on('SIGINT',  () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

// ─── Server Start ────────────────────────────────────────────────────────────

(async () => {
  try {
    const address = await fastify.listen({ port: PORT, host: HOST });

    // Access the underlying ws.Server after all plugins are loaded (Fastify v5)
    const wss = fastify.websocketServer;
    if (wss) startHeartbeat(wss);

    fastify.log.info(`🚀 Remote Desktop Signaling Server listening at ${address}`);
    fastify.log.info(`📡 WebSocket endpoint: ws://${HOST}:${PORT}/signal`);
    fastify.log.info(`🏥 Health check:       http://${HOST}:${PORT}/health`);
  } catch (err) {
    fastify.log.fatal({ err }, 'Failed to start server');
    process.exit(1);
  }
})();
