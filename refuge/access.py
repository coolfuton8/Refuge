"""GUI-gated admission control for connecting clients.

When an unknown client first talks to the web server, the handling worker
thread parks here and asks the GUI (via the event bus) whether to admit it.
The operator sees a popup naming the client and answers Allow / Deny; the
worker then either serves the request or returns 404.

Decisions are remembered for the life of the process:
  * Allow  -> the IP is admitted for the rest of the session.
  * Deny   -> denied now; the client may ask again after a short cooldown
              (so a denied page-load's retries don't spam popups).
  * Deny + "Always block this IP/client" -> the IP is 404'd for the rest of
              the session with no further popups.

Everything a worker thread touches here is thread-safe; the GUI only calls
resolve()/cancel_all() from the main thread.
"""

import threading
import time

LOOPBACK = {"127.0.0.1", "::1"}
APPROVAL_TIMEOUT = 180   # seconds a worker waits for the operator before denying
DENY_COOLDOWN = 15       # seconds a one-time-denied client is silently 404'd


class _PendingRequest:
    def __init__(self, ip, hostname):
        self.ip = ip
        self.hostname = hostname
        self.event = threading.Event()
        self.allowed = False
        self.decided = False


def label(ip, hostname):
    return f"{hostname} ({ip})" if hostname and hostname != ip else ip


class AccessControl:
    def __init__(self, bus):
        self._bus = bus
        self._lock = threading.Lock()
        self._allowed = set(LOOPBACK)   # operator's own machine is always allowed
        self._blocked = set()
        self._cooldown = {}             # ip -> expiry timestamp (one-time denials)
        self._pending = {}              # ip -> _PendingRequest (dedupes popups)

    # -- called from HTTP worker threads ------------------------------------

    def check(self, ip, hostname_provider):
        """Return True if `ip` may be served. Blocks (up to APPROVAL_TIMEOUT)
        while waiting for the operator on a first, undecided connection."""
        decided = self._fast_decision(ip)
        if decided is not None:
            return decided

        # Unknown client: resolve its name (may do reverse DNS, so do it off
        # the lock) and register a single pending popup per IP.
        try:
            hostname = hostname_provider() or ""
        except Exception:
            hostname = ""

        with self._lock:
            decided = self._fast_decision_locked(ip)
            if decided is not None:
                return decided
            record = self._pending.get(ip)
            fresh = record is None
            if fresh:
                record = _PendingRequest(ip, hostname)
                self._pending[ip] = record

        if fresh:
            self._bus.info(f"New client {label(ip, hostname)} is requesting "
                           "access — waiting for approval on the Refuge screen.")
            self._bus.emit("access_request", ip=ip, hostname=hostname)

        if not record.event.wait(APPROVAL_TIMEOUT):
            with self._lock:
                if self._pending.get(ip) is record and not record.decided:
                    del self._pending[ip]
                    self._cooldown[ip] = time.time() + DENY_COOLDOWN
            self._bus.warn(f"Access request from {label(ip, hostname)} timed "
                           "out with no answer — denied.")
            return False
        return record.allowed

    def _fast_decision(self, ip):
        with self._lock:
            return self._fast_decision_locked(ip)

    def _fast_decision_locked(self, ip):
        if ip in self._blocked:
            return False
        if ip in self._allowed:
            return True
        expiry = self._cooldown.get(ip)
        if expiry is not None:
            if time.time() < expiry:
                return False
            del self._cooldown[ip]
        return None

    # -- called from the GUI thread -----------------------------------------

    def resolve(self, ip, allow, always_block=False):
        with self._lock:
            record = self._pending.pop(ip, None)
            if allow:
                self._allowed.add(ip)
                self._blocked.discard(ip)
                self._cooldown.pop(ip, None)
            elif always_block:
                self._blocked.add(ip)
                self._allowed.discard(ip)
            else:
                self._cooldown[ip] = time.time() + DENY_COOLDOWN
            hostname = record.hostname if record else ""
            if record is not None:
                record.allowed = bool(allow)
                record.decided = True
                record.event.set()
        who = label(ip, hostname)
        if allow:
            self._bus.success(f"Allowed client {who} to connect (this session).")
        elif always_block:
            self._bus.warn(f"Blocking client {who} — all further requests from "
                           "it will be refused with 404 for this session.")
        else:
            self._bus.warn(f"Denied client {who} (it may ask again shortly).")

    def cancel_all(self):
        """Release every waiting worker with a denial (used on server stop)."""
        with self._lock:
            records = list(self._pending.values())
            self._pending.clear()
        for record in records:
            record.allowed = False
            record.decided = True
            record.event.set()

    def snapshot(self):
        with self._lock:
            return {"allowed": sorted(self._allowed - LOOPBACK),
                    "blocked": sorted(self._blocked)}
