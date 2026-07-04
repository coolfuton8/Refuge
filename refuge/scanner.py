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
        self._scanned = set()
        self._lock = threading.Lock()
        self._nmap_warned = False

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
            self._bus.success(
                f"Client fingerprinting is ON — nmap detected at {nmap}. New "
                f"clients will be scanned; reports go to {self.scan_dir}.")
        else:
            self._bus.warn(
                "Client fingerprinting is ON but nmap was NOT found on this "
                "machine — install it (nmap.org) or turn the option off. No "
                "scans will run until nmap is available.")

    def observe(self, ip):
        """Called for every request. Scans each remote IP at most once."""
        if not self.enabled or ip in LOOPBACK:
            return
        with self._lock:
            if ip in self._scanned:
                return
            self._scanned.add(ip)
        threading.Thread(target=self._scan, args=(ip,), daemon=True,
                         name=f"refuge-nmap-{ip}").start()

    def _scan(self, ip):
        nmap = self._find_nmap()
        if not nmap:
            with self._lock:
                self._scanned.discard(ip)  # allow a retry if nmap is installed later
                warn = not self._nmap_warned
                self._nmap_warned = True
            if warn:
                self._bus.warn("Client fingerprinting is on but nmap was not "
                               "found on this machine — install nmap (and re-run "
                               "as Administrator for OS detection) or turn the "
                               "option off.")
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
            self._bus.warn(f"nmap scan of {ip} timed out; any partial report is "
                           f"at {out}")
            return
        except OSError as exc:
            self._bus.error(f"Could not run nmap for {ip}: {exc}")
            return
        if ok:
            self._bus.success(f"nmap fingerprint of {ip} saved to {out}")
        else:
            self._bus.warn(f"nmap scan of {ip} finished with warnings "
                           f"({detail or 'see report'}); report at {out}")

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
