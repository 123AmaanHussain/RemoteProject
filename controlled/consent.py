"""
consent.py — Native OS Consent Dialog
======================================
Displays a blocking, native OS dialog asking the local user for explicit
permission before any screen or input data is transmitted.

Design decisions:
- Uses tkinter (Python stdlib) so there is no extra install dependency.
- Runs in its own thread so that the asyncio event loop and pystray main loop
  are never blocked.
- Returns the result via a threading.Event + result flag pattern so callers
  can await it asynchronously using asyncio.get_event_loop().run_in_executor().
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable


def _show_dialog_on_thread(
    controller_id: str,
    result_event: threading.Event,
    result_holder: list[bool],
) -> None:
    """
    Create a hidden Tk root (required for messagebox), show the dialog, store
    the result, then destroy the root. Runs in a dedicated daemon thread.
    """
    root = tk.Tk()
    root.withdraw()          # Hide the blank Tk window
    root.attributes('-topmost', True)   # Ensure the dialog appears in front
    root.update()

    # ── Display the authoritative consent message ────────────────────────
    approved: bool = messagebox.askyesno(
        title="⚠ Remote Support Request",
        message=(
            f"Remote Support Request\n\n"
            f"A connection has been initiated from terminal ID:\n"
            f"  [{controller_id}]\n\n"
            f"Do you grant full remote access permissions to view your screen "
            f"and manage system inputs for this session?\n\n"
            f"Click YES to Approve or NO to Deny."
        ),
        icon=messagebox.QUESTION,
        default=messagebox.NO,  # Safe default is Deny
    )

    result_holder.append(approved)
    result_event.set()

    try:
        root.destroy()
    except tk.TclError:
        pass  # Already destroyed; ignore


def request_consent_sync(controller_id: str) -> bool:
    """
    Synchronous blocking call: shows the native OS dialog and returns True if
    the user clicked Approve, False otherwise.

    This function is designed to be called from a thread pool via
    asyncio.get_event_loop().run_in_executor() to remain non-blocking in async
    contexts.

    Args:
        controller_id: A human-readable identifier for the connecting terminal.

    Returns:
        True  — User clicked Approve; streaming may begin.
        False — User clicked Deny; connection must be severed.
    """
    result_event:  threading.Event = threading.Event()
    result_holder: list[bool]      = []

    dialog_thread = threading.Thread(
        target=_show_dialog_on_thread,
        args=(controller_id, result_event, result_holder),
        daemon=True,
        name="ConsentDialog",
    )
    dialog_thread.start()
    result_event.wait(timeout=120)  # 2-minute hard timeout; auto-deny if ignored

    return bool(result_holder and result_holder[0])


async def request_consent_async(
    controller_id: str,
    loop=None,
) -> bool:
    """
    Asynchronous wrapper around request_consent_sync.
    Runs the blocking dialog in a thread pool so the asyncio loop stays alive.

    Args:
        controller_id: Identifier of the connecting controller terminal.
        loop: Optional event loop; defaults to the running loop.

    Returns:
        True if approved, False if denied or timed-out.
    """
    import asyncio
    _loop = loop or asyncio.get_event_loop()
    result: bool = await _loop.run_in_executor(
        None,
        request_consent_sync,
        controller_id,
    )
    return result
