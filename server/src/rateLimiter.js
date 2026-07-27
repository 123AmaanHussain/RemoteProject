/**
 * rateLimiter.js
 *
 * Per-WebSocket message rate limiter using a sliding-window token bucket.
 * Prevents a single misbehaving client from flooding the server with
 * signaling messages or control data.
 */

'use strict';

const MAX_TOKENS = parseInt(process.env.RATE_LIMIT_MAX ?? '1000', 10);
const WINDOW_MS  = parseInt(process.env.RATE_LIMIT_WINDOW_MS ?? '1000', 10);

/** @type {Map<import('ws').WebSocket, { tokens: number, windowStart: number }>} */
const buckets = new Map();

/**
 * Checks whether the given socket is within its allowed message rate.
 * Uses a sliding-window token bucket: refills MAX_TOKENS every WINDOW_MS.
 *
 * @param {import('ws').WebSocket} socket
 * @returns {boolean} True if the message is allowed, false if rate-limited
 */
function allow(socket) {
  const now = Date.now();
  let bucket = buckets.get(socket);

  if (!bucket) {
    bucket = { tokens: MAX_TOKENS - 1, windowStart: now };
    buckets.set(socket, bucket);
    return true;
  }

  // Refill tokens if the window has expired
  if (now - bucket.windowStart >= WINDOW_MS) {
    bucket.tokens = MAX_TOKENS - 1;
    bucket.windowStart = now;
    return true;
  }

  if (bucket.tokens > 0) {
    bucket.tokens -= 1;
    return true;
  }

  return false; // Rate limited
}

/**
 * Removes the bucket entry when a socket closes to free memory.
 * @param {import('ws').WebSocket} socket
 */
function cleanup(socket) {
  buckets.delete(socket);
}

module.exports = { allow, cleanup };
