"""In-process IP-based brute-force lockout.

Tracks failed login attempts per client IP using a monotonic clock so
it is immune to system clock changes. State is lost on process restart —
acceptable for a single-process deployment.
"""

import time
from dataclasses import dataclass, field


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = field(default=0.0)


class LockoutTracker:
    def __init__(self, max_attempts: int, lockout_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._entries: dict[str, _Entry] = {}

    def is_locked(self, ip: str) -> bool:
        """Return True if this IP is currently locked out."""
        entry = self._entries.get(ip)
        if entry is None:
            return False
        if entry.locked_until > time.monotonic():
            return True
        # Lockout window has elapsed — reset so failures don't accumulate forever.
        if entry.failures >= self._max_attempts:
            entry.failures = 0
            entry.locked_until = 0.0
        return False

    def record_failure(self, ip: str) -> None:
        """Increment the failure counter and lock if threshold is reached."""
        entry = self._entries.setdefault(ip, _Entry())
        entry.failures += 1
        if entry.failures >= self._max_attempts:
            entry.locked_until = time.monotonic() + self._lockout_seconds

    def reset(self, ip: str) -> None:
        """Clear all failure state for this IP (call on successful login)."""
        self._entries.pop(ip, None)
