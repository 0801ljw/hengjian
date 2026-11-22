"""Small in-process registration limiter for public signup.

This is not a replacement for an edge rate limiter; it is a local guard so a
single process cannot be trivially spammed. A restart resets the window.
"""
from __future__ import annotations

import time

WINDOW_SECONDS = 300
MAX_ATTEMPTS = 5
_attempts: dict[str, list[float]] = {}


def allow(peer, now=None):
    peer = str(peer or "unknown")
    now = time.time() if now is None else float(now)
    recent = [stamp for stamp in _attempts.get(peer, []) if now - stamp < WINDOW_SECONDS]
    if len(recent) >= MAX_ATTEMPTS:
        _attempts[peer] = recent
        return False
    recent.append(now)
    _attempts[peer] = recent
    return True


def reset():
    _attempts.clear()
