"""Out-of-band authorization codes for destructive web operations.

Deletes and overwrites requested through the web page must carry a short code
that is displayed only on the operator's GUI. A remote attacker driving the
upload page from a compromised client machine (virus/RAT) can see the web
page but *not* the rescue laptop's screen, so it cannot obtain the code.

The code rotates after every successful destructive operation (single use),
and a burst of invalid attempts is rate-limited and surfaced on the dashboard,
so brute force over HTTP is infeasible.
"""

import secrets
import threading
import time
from collections import deque

# Unambiguous uppercase charset: no I, L, O, 0, 1 (avoids misreads off a screen).
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6


def generate_code():
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


class AuthCodeManager:
    """Thread-safe holder of the current authorization code plus a rolling
    lockout that throttles repeated invalid attempts."""

    def __init__(self, bus, max_failures=5, window_seconds=60):
        self._bus = bus
        self._lock = threading.Lock()
        self._code = generate_code()
        self._failures = deque()
        self._max_failures = max_failures
        self._window = window_seconds

    def current(self):
        with self._lock:
            return self._code

    def _locked_unsafe(self, now):
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        return len(self._failures) >= self._max_failures

    def is_locked(self):
        with self._lock:
            return self._locked_unsafe(time.time())

    def verify(self, code, actor="a web client"):
        """Check a supplied code. Returns 'ok', 'locked', or 'invalid'.
        On 'ok' the code is rotated so it cannot be replayed."""
        now = time.time()
        with self._lock:
            if self._locked_unsafe(now):
                self._bus.warn(
                    f"Delete/overwrite from {actor} blocked: too many invalid "
                    f"authorization attempts (locked for up to {self._window}s).")
                self._bus.emit("authcode", code=self._code, locked=True)
                return "locked"
            if code and secrets.compare_digest(str(code).upper(), self._code):
                self._code = generate_code()
                self._failures.clear()
                self._bus.emit("authcode", code=self._code, locked=False)
                return "ok"
            self._failures.append(now)
            remaining = self._max_failures - len(self._failures)
            if remaining > 0:
                self._bus.warn(
                    f"Invalid delete/overwrite authorization code from {actor} "
                    f"({remaining} attempt(s) left before lockout).")
            else:
                self._bus.error(
                    f"Delete/overwrite from {actor} LOCKED after repeated invalid "
                    "codes - possible attack from the uploading machine.")
            self._bus.emit("authcode", code=self._code,
                           locked=(remaining <= 0))
            return "invalid"

    def reset(self):
        """Operator action from the GUI: clear any lockout and issue a fresh code."""
        with self._lock:
            self._code = generate_code()
            self._failures.clear()
            self._bus.emit("authcode", code=self._code, locked=False)
            return self._code
