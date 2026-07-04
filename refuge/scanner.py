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
import re
import shutil
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

LOOPBACK = {"127.0.0.1", "::1"}
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
SCAN_TIMEOUT = 360        # hard cap (seconds) on a single nmap run
NMAP_HOST_TIMEOUT = "240s"
HTTP_PROBE_TIMEOUT = 4    # seconds per web-port probe

# TCP ports nmap scans — top services plus management ports common to routers,
# cameras/DVRs, switches, printers, NAS and IoT gear (helps pin the device type).
DEVICE_PORTS = ("21,22,23,25,53,80,81,88,110,139,143,161,443,445,515,554,631,"
                "1723,1883,1900,2000,2323,3389,5000,5060,5357,7547,8000,8008,"
                "8080,8081,8443,8888,9000,9100,37777,49152,49153,49154")

# Web ports probed over HTTP(S) to read Server/realm/title (strong device hints).
WEB_PORTS = [80, 81, 8000, 8008, 8080, 8888, 443, 8443]

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


# --- device-type classification (best effort) ------------------------------

# Keyword signatures searched across nmap output + HTTP headers/titles.
_DEVICE_SIGNATURES = [
    ("IP camera / DVR", [
        "rtsp", "onvif", "hikvision", "dahua", "uc-httpd", "goahead-webs",
        "webcam", "ipcam", "ip camera", "dvrdvs", "nvr", "axis", "mobotix",
        "vivotek", "reolink", "amcrest", "camera", "netwave",
    ]),
    ("Router / gateway / Wi-Fi AP", [
        "routeros", "mikrotik", "openwrt", "dd-wrt", "luci", "hostapd",
        "rompager", "tr-069", "cwmp", "dnsmasq", "netgear", "tp-link", "tplink",
        "d-link", "dlink", "asuswrt", "tenda", "zyxel", "draytek", "ubiquiti",
        "unifi", "edgeos", "gateway", "router", "wireless", "access point",
        "wi-fi", "wifi", "openwrt", "gargoyle", "pfsense", "opnsense",
    ]),
    ("Network switch", [
        "cisco ios", "catalyst", "juniper", "arista", "procurve", "aruba",
        "managed switch", "switch", "dell networking", "brocade",
        "extreme networks", "fortiswitch",
    ]),
    ("Printer / MFP", [
        "jetdirect", "printer", "laserjet", "canon", "epson", "brother",
        "lexmark", "ipp", "cups", "xerox", "kyocera", "ricoh",
    ]),
    ("NAS / storage", [
        "synology", "qnap", "diskstation", "truenas", "freenas", "netapp",
        "my cloud", "readynas", "nas",
    ]),
    ("Windows PC / server", [
        "microsoft windows", "microsoft-ds", "netbios", "ms-wbt-server",
        "windows server", "microsoft iis",
    ]),
    ("Linux / Unix host", [
        "openssh", "ubuntu", "debian", "centos", "red hat", "linux kernel",
        "raspbian",
    ]),
    ("VoIP / phone", ["asterisk", "polycom", "yealink", "grandstream", "sip"]),
    ("Smart-home / IoT", ["espressif", "tasmota", "shelly", "sonoff", "tuya",
                          "homekit", "mqtt"]),
]

# Open TCP port -> (device type, human reason). Adds weight when the port is open.
_PORT_HINTS = {
    554: ("IP camera / DVR", "RTSP (554/tcp) open"),
    37777: ("IP camera / DVR", "Dahua proprietary (37777/tcp) open"),
    7547: ("Router / gateway / Wi-Fi AP", "TR-069/CWMP (7547/tcp) open"),
    1900: ("Router / gateway / Wi-Fi AP", "UPnP/SSDP (1900/tcp) open"),
    53: ("Router / gateway / Wi-Fi AP", "DNS (53/tcp) open"),
    9100: ("Printer / MFP", "raw printing (9100/tcp) open"),
    515: ("Printer / MFP", "LPD (515/tcp) open"),
    631: ("Printer / MFP", "IPP (631/tcp) open"),
    3389: ("Windows PC / server", "RDP (3389/tcp) open"),
    445: ("Windows PC / server", "SMB (445/tcp) open"),
    139: ("Windows PC / server", "NetBIOS (139/tcp) open"),
}

# MAC OUI vendor keyword -> device type (a strong signal when nmap has the MAC).
_VENDOR_TYPES = [
    ("IP camera / DVR", ["hikvision", "dahua", "axis", "mobotix", "hanwha",
                         "vivotek", "reolink", "amcrest", "foscam"]),
    ("Router / gateway / Wi-Fi AP", ["ubiquiti", "tp-link", "netgear", "d-link",
                                     "asustek", "tenda", "zyxel", "mikrotik",
                                     "draytek", "ruckus", "cambium", "eero",
                                     "linksys", "belkin", "google", "sagemcom",
                                     "arris", "technicolor", "aruba"]),
    ("Network switch", ["cisco", "juniper", "arista", "extreme", "brocade",
                        "hewlett packard enterprise"]),
    ("Printer / MFP", ["hewlett", "canon", "epson", "brother", "lexmark",
                       "xerox", "kyocera", "ricoh"]),
    ("NAS / storage", ["synology", "qnap", "western digital", "netapp"]),
    ("Smart-home / IoT", ["espressif", "sonoff", "shelly", "tuya", "sonos",
                          "nest", "amazon technologies", "raspberry"]),
]


def _mac_vendor(nmap_text):
    m = re.search(r"MAC Address:\s*[0-9A-Fa-f:]{17}\s*\(([^)]+)\)", nmap_text)
    return m.group(1).strip() if m else ""


def _open_ports(nmap_text):
    return {int(m.group(1))
            for m in re.finditer(r"^(\d+)/tcp\s+open", nmap_text, re.MULTILINE)}


def classify_device(nmap_text, http_results):
    """Best-effort guess of the device type from all collected signals.
    Returns (guess, reasons)."""
    blob = nmap_text.lower()
    for r in http_results:
        blob += " " + " ".join(str(r.get(k, "")) for k in
                               ("server", "realm", "title", "location")).lower()

    scores = defaultdict(int)
    reasons = []

    vendor = _mac_vendor(nmap_text)
    if vendor:
        reasons.append(f"MAC vendor: {vendor}")
        vl = vendor.lower()
        for dtype, names in _VENDOR_TYPES:
            if any(n in vl for n in names):
                scores[dtype] += 3
                break

    for dtype, keywords in _DEVICE_SIGNATURES:
        hit = next((kw for kw in keywords if kw in blob), None)
        if hit:
            scores[dtype] += 1
            reasons.append(f"keyword '{hit}' -> {dtype}")

    for port, (dtype, why) in _PORT_HINTS.items():
        if port in _open_ports(nmap_text):
            scores[dtype] += 2
            reasons.append(why)

    for r in http_results:
        if r.get("server"):
            reasons.append(f"HTTP Server on {r['port']}: {r['server']}")
        if r.get("realm"):
            reasons.append(f"HTTP auth realm on {r['port']}: {r['realm']}")
        if r.get("title"):
            reasons.append(f"HTTP page title on {r['port']}: {r['title']}")

    if not scores:
        return "Unknown — not enough signals to classify", reasons
    best = max(scores.values())
    winners = sorted(d for d, s in scores.items() if s == best)
    return " or ".join(winners), reasons


# --- lightweight HTTP probing ----------------------------------------------

def _probe_one(ip, scheme, port):
    url = f"{scheme}://{ip}:{port}/"
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    req = urllib.request.Request(url, headers={"User-Agent": "Refuge-Fingerprint"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_PROBE_TIMEOUT,
                                    context=ctx) as resp:
            headers, status = resp.headers, resp.status
            body = resp.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        headers, status, body = exc.headers, exc.code, ""
    except (urllib.error.URLError, ssl.SSLError, OSError, ValueError):
        return None
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    return {
        "port": port, "scheme": scheme, "status": status,
        "server": (headers.get("Server", "") or "").strip()[:120],
        "realm": (headers.get("WWW-Authenticate", "") or "").strip()[:120],
        "location": (headers.get("Location", "") or "").strip()[:160],
        "title": title,
    }


def probe_http(ip, ports):
    results = []
    for port in ports:
        scheme = "https" if port in (443, 8443) else "http"
        info = _probe_one(ip, scheme, port)
        if info is not None:
            results.append(info)
    return results


def compose_report(ip, guess, reasons, http_results, nmap_text):
    lines = [
        f"Refuge device fingerprint for {ip}",
        f"Generated: {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
        f"LIKELY DEVICE TYPE: {guess}",
        "",
        "Signals used:",
    ]
    lines += [f"  - {r}" for r in reasons] or ["  - (none)"]
    lines += ["", "=== HTTP probe (web management interface) ==="]
    if http_results:
        for r in http_results:
            lines.append(f"[{r.get('scheme', 'http')}://{ip}:{r.get('port', '?')}]"
                         f" HTTP {r.get('status', '?')}")
            for key, label in (("server", "Server"), ("realm", "Auth realm"),
                               ("title", "Title"), ("location", "Redirect")):
                if r.get(key):
                    lines.append(f"    {label}: {r[key]}")
    else:
        lines.append("  no web service answered on probed ports "
                     f"({', '.join(map(str, WEB_PORTS))})")
    lines += ["", "=== nmap ===", nmap_text.rstrip(), ""]
    return "\n".join(lines)


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
            self._bus.info(f"nmap: fingerprinting {ip} (deep scan — this can take "
                           f"a few minutes); report will be saved to {out}")
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
                self._bus.success(f"Fingerprint of {ip} saved to {out} — likely "
                                  f"{detail or 'unknown'} (won't be scanned again).")
            else:
                self._bus.warn(f"nmap scan of {ip} finished with warnings "
                               f"({detail or 'see report'}); report at {out}. "
                               "It won't be scanned again.")
        finally:
            with self._lock:
                self._in_flight.discard(ip)

    def _default_runner(self, nmap, ip, out):
        """Deep fingerprint: verbose nmap + an HTTP probe of the device's web
        ports + a heuristic device-type guess, all written to `out`.

        nmap flags: -Pn (skip host discovery — needs admin and a suspect host
        may not answer a ping), -A (OS + version + default NSE scripts +
        traceroute), --version-all (thorough version probes), a curated device
        --script set (http title/headers/auth, ssl-cert, rtsp, upnp, snmp), and
        DEVICE_PORTS (management ports of common device classes). OS detection
        and MAC/OUI need Administrator/Npcap; nmap degrades gracefully without.
        """
        nmap_tmp = out.with_name(out.stem + ".nmap")
        cmd = [nmap, "-Pn", "-A", "--version-all",
               "--script", "default,http-title,http-server-header,http-headers,"
                           "http-auth,ssl-cert,rtsp-methods,upnp-info,snmp-info,"
                           "banner",
               "-T4", "-p", DEVICE_PORTS, "--host-timeout", NMAP_HOST_TIMEOUT,
               "-oN", str(nmap_tmp), ip]
        timed_out, stderr, fallback = False, "", ""
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=SCAN_TIMEOUT, creationflags=CREATE_NO_WINDOW)
            rc, stderr, fallback = proc.returncode, (proc.stderr or "").strip()[:200], \
                proc.stdout or ""
        except subprocess.TimeoutExpired as exc:
            timed_out, rc = True, 1
            fallback = exc.stdout if isinstance(exc.stdout, str) else ""

        try:
            nmap_text = nmap_tmp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            nmap_text = fallback or "(no nmap output captured)"

        open_ports = _open_ports(nmap_text)
        web = [p for p in WEB_PORTS if p in open_ports] or WEB_PORTS
        http_results = probe_http(ip, web)

        guess, reasons = classify_device(nmap_text, http_results)
        out.write_text(compose_report(ip, guess, reasons, http_results, nmap_text),
                       encoding="utf-8")
        try:
            nmap_tmp.unlink()
        except OSError:
            pass

        if timed_out:
            return False, f"scan timed out (partial report); likely {guess}"
        return rc == 0, (guess if rc == 0 else stderr)
