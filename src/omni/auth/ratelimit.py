"""In-memory per-IP rate limit for the auth front door.

Brute-force resistance on ``/auth/login`` and ``/auth/setup``: a small number
of attempts per minute per client IP. In-memory and per-process -- sufficient
for a single-operator deployment where one api process serves the box. A
multi-replica deployment would need a shared store (each replica keeps its own
counter, so the effective limit scales with the replica count); that is a
documented follow-up, not a silent assumption.

Counts every attempt, successful or not: a brute-forcer does not authenticate,
and a legit single-operator login is never close to five a minute. The window
slides, so a burst ages out and the door reopens once real time passes.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from time import monotonic

WINDOW_SECONDS = 60.0
MAX_ATTEMPTS = 5

_attempts: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def check_rate_limit(
    ip: str, *, window: float = WINDOW_SECONDS, limit: int = MAX_ATTEMPTS
) -> bool:
    """Record an attempt from ``ip``; return whether it is within the limit.

    Returns False when ``ip`` already has ``limit`` attempts in the last
    ``window`` seconds. The check itself records the attempt, so the door stays
    closed through a sustained burst rather than aging out mid-attack.
    """
    now = monotonic()
    cutoff = now - window
    with _lock:
        q = _attempts[ip]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def reset_for_test() -> None:
    """Test-only: clear the in-memory counters between cases (vitest-style
    isolation is not automatic for module-level state)."""
    with _lock:
        _attempts.clear()
