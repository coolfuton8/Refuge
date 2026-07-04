"""Optional nmap fingerprinting of connecting clients.

When enabled, the first time a remote IP contacts the server Refuge runs an
nmap scan against it in the background and writes the report to a file for
later review. Intended for investigating unexpected/persistent connection
attempts. Scanning is active traffic — only enable it on networks you are
authorized to scan.

nmap is used if present; if it isn't installed the feature logs a note and
does nothing else.
"""

import datetime
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

LOOPBACK = {"127.0.0.1", "::1"}
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
SCAN_TIMEOUT = 240  # hard cap (seconds) on a single nmap run

# Common Windows install locations (nmap often isn't added to PATH there).
_WINDOWS_NMAP = [
    r"C:\Program Files (x86)\Nmap\nmap.exe",
    r"C:\Program Files\Nmap\nmap.exe",
]


def find_nmap():
    found = shutil.which("nmap")
    if found:
        return found
    for candidate in _WINDOWS_NMAP:
        if os.path.isfile(candidate):
            return candidate
    return None


class ClientScanner:
    def __init__(self, bus, scan_dir, runner=None, nmap_finder=find_nmap):
        self._bus = bus
        self.scan_dir = Path(scan_dir)
        self._runner = runner or self._default_runner
        self._find_nmap = nmap_finder
        self.enabled = False
        self._lock = threading.Lock()
        self._nmap_warned = False
        # A host counts as already fingerprinted if a report for it is present in
        # the scans folder — the folder itself is the record, so dedup survives
        # restarts with no separate index file. _in_flight guards against a
        # second scan of the same IP while its first is still running.
        self._in_flight = set()

    def _report_glob(self, ip):
        return f"scan_{ip.replace(':', '-')}_*.txt"

    def _already_scanned(self, ip):
        try:
            return any(self.scan_dir.glob(self._report_glob(ip)))
        except OSError:
            return False

    def announce(self):
        """Log the current fingerprinting status so the operator gets immediate
        confirmation whether scans will run — called at startup and whenever the
        setting is toggled, not lazily on the first connection."""
        if not self.enabled:
            self._bus.info("Client fingerprinting is OFF — connecting clients "
                           "will not be scanned.")
            return
        nmap = self._find_nmap()
        if nmap:
            self._nmap_warned = False  # a real detection resets the missing-warning
            try:
                known = sum(1 for _ in self.scan_dir.glob("scan_*_*.txt"))
            except OSError:
                known = 0
            extra = (f" {known} host(s) already have a report and will be skipped."
                     if known else "")
            self._bus.success(
                f"Client fingerprinting is ON — nmap detected at {nmap}. Each new "
                f"host is scanned once; reports go to {self.scan_dir}.{extra}")
        else:
            self._bus.warn(
                "Client fingerprinting is ON but nmap was NOT found on this "
                "machine — install it (nmap.org) or turn the option off. No "
                "scans will run until nmap is available.")

    def observe(self, ip):
        """Called for every connection. Fingerprints each remote IP at most
        once, ever: if a report for it is already in the scans folder it is
        skipped (dedup survives restarts)."""
        if not self.enabled or ip in LOOPBACK:
            return
        with self._lock:
            if ip in self._in_flight:
                return
            self._in_flight.add(ip)
        if self._already_scanned(ip):
            with self._lock:
                self._in_flight.discard(ip)
            return
        threading.Thread(target=self._scan, args=(ip,), daemon=True,
                         name=f"refuge-nmap-{ip}").start()

    def _scan(self, ip):
        try:
            nmap = self._find_nmap()
            if not nmap:
                with self._lock:
                    warn = not self._nmap_warned
                    self._nmap_warned = True
                if warn:
                    self._bus.warn("Client fingerprinting is on but nmap was not "
                                   "found on this machine — install nmap (and "
                                   "re-run as Administrator for OS detection) or "
                                   "turn the option off.")
                return

            try:
                self.scan_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._bus.error(f"Cannot write scan folder {self.scan_dir}: {exc}")
                return

            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            out = self.scan_dir / f"scan_{ip.replace(':', '-')}_{stamp}.txt"
            self._bus.info(f"nmap: fingerprinting {ip} — this can take a minute; "
                           f"report will be saved to {out}")
            try:
                ok, detail = self._runner(nmap, ip, out)
            except subprocess.TimeoutExpired:
                # nmap ran (its -oN report file exists), so this host won't be
                # picked again by _already_scanned.
                self._bus.warn(f"nmap scan of {ip} timed out; any partial report "
                               f"is at {out}. It won't be scanned again.")
                return
            except OSError as exc:
                self._bus.error(f"Could not run nmap for {ip}: {exc}")
                return

            if ok:
                self._bus.success(f"nmap fingerprint of {ip} saved to {out} "
                                  "(this host won't be scanned again).")
            else:
                self._bus.warn(f"nmap scan of {ip} finished with warnings "
                               f"({detail or 'see report'}); report at {out}. "
                               "It won't be scanned again.")
        finally:
            with self._lock:
                self._in_flight.discard(ip)

    def _default_runner(self, nmap, ip, out):
        # -Pn: skip host discovery — the ping needs raw sockets (admin) and a
        #      suspicious host may not answer it anyway; scan it regardless.
        # -sV: service/version detection (works unprivileged via connect).
        # -O:  OS detection (needs admin/Npcap; nmap warns and skips otherwise).
        # bounded by --host-timeout and a hard subprocess timeout.
        proc = subprocess.run(
            [nmap, "-Pn", "-O", "-sV", "-T4", "--host-timeout", "180s",
             "-oN", str(out), ip],
            capture_output=True, text=True, timeout=SCAN_TIMEOUT,
            creationflags=CREATE_NO_WINDOW)
        return proc.returncode == 0, (proc.stderr or "").strip()[:200]
