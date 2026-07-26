"""
capture.py — Screen Capture Module
====================================
Captures the primary display at a configurable frame rate using `mss` for
fast raw byte acquisition and `cv2` (OpenCV) for JPEG/WebP compression.

Threading model:
- Runs in a dedicated daemon thread spawned by the main agent.
- Puts compressed frame bytes into an asyncio.Queue that the network module
  reads from.
- Stops cleanly when the stop_event is set.

Performance notes:
- mss grabs the framebuffer directly via OS APIs — minimal CPU overhead.
- cv2.imencode() with IMWRITE_JPEG_QUALITY ~75 achieves ~5–15 KB/frame at
  1080p, sustaining 24–30 FPS over a 10 Mbps link.
- The frame queue has a maxsize of 2 so that a slow network link simply
  causes dropped frames rather than memory growth.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

import cv2
import mss
import numpy as np

from config import (
    CAPTURE_MONITOR_INDEX,
    JPEG_QUALITY,
    TARGET_FPS,
    VIDEO_CODEC,
)

log = logging.getLogger(__name__)


class ScreenCaptureThread(threading.Thread):
    """
    Dedicated background thread that continuously captures the screen and
    places compressed frames into a thread-safe asyncio.Queue.

    Args:
        frame_queue: An asyncio.Queue shared with the network coroutine.
                     The queue is filled from this thread using
                     loop.call_soon_threadsafe().
        loop:        The running asyncio event loop (needed for
                     thread-safe queue operations).
        stop_event:  When set, the capture loop exits cleanly.
    """

    def __init__(
        self,
        frame_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="ScreenCapture", daemon=True)
        self._frame_queue  = frame_queue
        self._loop         = loop
        self._stop_event   = stop_event
        self._frame_interval = 1.0 / TARGET_FPS

        # Pre-compute encoding params once for efficiency
        if VIDEO_CODEC == "webp":
            self._encode_ext    = ".webp"
            self._encode_params = [cv2.IMWRITE_WEBP_QUALITY, JPEG_QUALITY]
        else:
            self._encode_ext    = ".jpg"
            self._encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]

        # Expose current monitor dimensions so the network layer can include
        # them in the frame metadata header.
        self.monitor_width:  int = 0
        self.monitor_height: int = 0

    def run(self) -> None:
        """Main capture loop — runs until stop_event is set."""
        log.info(
            "Screen capture started — monitor=%d, fps=%d, codec=%s, quality=%d",
            CAPTURE_MONITOR_INDEX, TARGET_FPS, VIDEO_CODEC, JPEG_QUALITY,
        )

        # Use mss.MSS if available (mss 10+), otherwise fallback to mss.mss
        mss_factory = getattr(mss, "MSS", None) or getattr(mss, "mss")

        with mss_factory() as sct:
            # Validate monitor index
            available = len(sct.monitors)
            if CAPTURE_MONITOR_INDEX >= available:
                log.error(
                    "Monitor index %d out of range (available: %d). "
                    "Falling back to monitor 1.",
                    CAPTURE_MONITOR_INDEX, available,
                )
                monitor_def = sct.monitors[1]
            else:
                monitor_def = sct.monitors[CAPTURE_MONITOR_INDEX]

            self.monitor_width  = monitor_def["width"]
            self.monitor_height = monitor_def["height"]
            log.info(
                "Capturing monitor: %dx%d",
                self.monitor_width, self.monitor_height,
            )

            next_capture_time = time.monotonic()

            while not self._stop_event.is_set():
                now = time.monotonic()

                # Frame-rate pacing — sleep until the next capture slot
                if now < next_capture_time:
                    sleep_for = next_capture_time - now
                    time.sleep(sleep_for)

                next_capture_time = time.monotonic() + self._frame_interval

                try:
                    frame_bytes = self._capture_frame(sct, monitor_def)
                    if frame_bytes is not None:
                        self._enqueue_frame(frame_bytes)
                except Exception as exc:
                    # Log but never crash — the agent must survive transient errors
                    log.warning("Frame capture error (will retry): %s", exc)

        log.info("Screen capture thread stopped")

    def _capture_frame(
        self, sct: Any, monitor_def: dict
    ) -> Optional[bytes]:
        """
        Grabs one frame from mss and encodes it to JPEG/WebP bytes.

        Returns:
            Compressed bytes on success, None if encoding fails.
        """
        # mss returns BGRA raw bytes
        raw = sct.grab(monitor_def)

        # Convert to numpy array (no copy — uses the raw buffer)
        img_bgra: np.ndarray = np.frombuffer(raw.raw, dtype=np.uint8).reshape(
            (raw.height, raw.width, 4)
        )

        # Drop alpha channel → BGR (required by cv2.imencode)
        img_bgr: np.ndarray = img_bgra[:, :, :3]

        # Encode to JPEG or WebP bytes
        success, encoded_buf = cv2.imencode(
            self._encode_ext, img_bgr, self._encode_params
        )

        if not success:
            log.warning("cv2.imencode returned False — skipping frame")
            return None

        return encoded_buf.tobytes()

    def _enqueue_frame(self, frame_bytes: bytes) -> None:
        """
        Puts a frame into the asyncio queue in a thread-safe manner.
        If the queue is full (slow consumer), the oldest frame is discarded
        to keep the stream live without growing memory unboundedly.
        """
        try:
            # put_nowait equivalent for asyncio.Queue from a different thread
            self._loop.call_soon_threadsafe(
                self._try_put_frame, frame_bytes
            )
        except RuntimeError:
            # Event loop has been closed — capture thread will exit on next
            # stop_event check
            pass

    def _try_put_frame(self, frame_bytes: bytes) -> None:
        """Called on the event loop thread via call_soon_threadsafe."""
        try:
            self._frame_queue.put_nowait(frame_bytes)
        except asyncio.QueueFull:
            # Queue full → discard oldest frame, insert newest
            try:
                self._frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._frame_queue.put_nowait(frame_bytes)
            except asyncio.QueueFull:
                pass  # Extremely unlikely; give up for this frame
