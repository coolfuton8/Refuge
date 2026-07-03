"""Network state detection and Wi-Fi hotspot management (Windows + Linux).

Hotspot strategy (best effort, in order):
  Windows:
    1. Mobile Hotspot via WinRT (Windows 10/11, no admin needed, but Windows
       requires at least one connection profile to attach to).
    2. Legacy hosted network via `netsh wlan` (works with no network at all,
       but requires an elevated process and a driver that still supports it).
  Linux:
    1. NetworkManager via `nmcli device wifi hotspot` (needs a Wi-Fi adapter
       managed by NetworkManager; present on most desktop distros).
"""

import socket
import subprocess
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# Default gateway IP handed to hotspot clients:
# Windows ICS / Mobile Hotspot uses 192.168.137.1, NetworkManager uses 10.42.0.1.
HOTSPOT_HOST_IP = "192.168.137.1" if IS_WINDOWS else "10.42.0.1"

NMCLI_CONNECTION_NAME = "refuge-hotspot"

# --- PowerShell for the WinRT Mobile Hotspot API ---------------------------

_PS_ASYNC_HELPERS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
Function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $null = $netTask.Wait(-1)
    $netTask.Result
}
Function AwaitAction($WinRtAction) {
    $netTask = $asTaskAction.Invoke($null, @($WinRtAction))
    $null = $netTask.Wait(-1)
}
Function Get-TetheringManager {
    $null = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
    $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
    if ($null -eq $profile) {
        $profiles = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()
        if ($profiles.Count -gt 0) { $profile = $profiles[0] }
    }
    if ($null -eq $profile) { throw "No connection profile available to attach the hotspot to." }
    [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($profile)
}
"""

_PS_START_TETHERING = _PS_ASYNC_HELPERS + r"""
try {
    $manager = Get-TetheringManager
    try {
        $config = New-Object Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration
        $config.Ssid = '__SSID__'
        $config.Passphrase = '__KEY__'
        AwaitAction ($manager.ConfigureAccessPointAsync($config))
    } catch {
        Write-Output "WARN could not apply SSID/passphrase: $($_.Exception.Message)"
    }
    if ($manager.TetheringOperationalState -eq 1) { Write-Output 'OK already-on'; exit 0 }
    $result = Await ($manager.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    if ($result.Status.ToString() -eq 'Success' -or $result.Status -eq 0) {
        Write-Output 'OK started'
    } else {
        Write-Output "ERR $($result.Status) $($result.AdditionalErrorMessage)"
        exit 1
    }
} catch {
    Write-Output "ERR $($_.Exception.Message)"
    exit 1
}
"""

_PS_STOP_TETHERING = _PS_ASYNC_HELPERS + r"""
try {
    $manager = Get-TetheringManager
    if ($manager.TetheringOperationalState -ne 1) { Write-Output 'OK already-off'; exit 0 }
    $result = Await ($manager.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    Write-Output 'OK stopped'
} catch {
    Write-Output "ERR $($_.Exception.Message)"
    exit 1
}
"""


def _run_powershell(script, timeout=60):
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _run_netsh(args, timeout=30):
    proc = subprocess.run(
        ["netsh"] + args, capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _run_nmcli(args, timeout=30):
    try:
        proc = subprocess.run(
            ["nmcli"] + args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "nmcli not found (NetworkManager is not installed)"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _ps_quote(value):
    return value.replace("'", "''")


# --- Address / connectivity detection --------------------------------------

def get_ipv4_addresses():
    """All usable (non-loopback, non-APIPA) IPv4 addresses on this machine."""
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass
    # Outbound-route trick; adds the primary interface even if hostname lookup is stale.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    return sorted(
        addr for addr in addresses
        if not addr.startswith("127.") and not addr.startswith("169.254.")
    )


def network_state(hotspot_active):
    """Return (state, addresses). State is one of LAN / HOTSPOT / OFFLINE."""
    addresses = get_ipv4_addresses()
    if hotspot_active and HOTSPOT_HOST_IP in addresses:
        others = [a for a in addresses if a != HOTSPOT_HOST_IP]
        return ("LAN+HOTSPOT" if others else "HOTSPOT"), addresses
    if addresses:
        return "LAN", addresses
    return "OFFLINE", addresses


# --- Hotspot control --------------------------------------------------------

class Hotspot:
    def __init__(self, bus, config):
        self.bus = bus
        self.config = config
        self.active = False
        self.method = None  # "mobile-hotspot" or "hosted-network"
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.active:
                return True
            self.bus.info("Starting Wi-Fi hotspot "
                          f"(SSID: {self.config.hotspot_ssid})...")
            if IS_WINDOWS:
                if self._start_mobile_hotspot():
                    self.active, self.method = True, "mobile-hotspot"
                elif self._start_hosted_network():
                    self.active, self.method = True, "hosted-network"
                else:
                    self.bus.error(
                        "Could not start a hotspot with either method. "
                        "Check that Wi-Fi is enabled; the legacy method also needs "
                        "Refuge to run as Administrator.")
                    self.bus.emit("hotspot_state", active=False, method=None)
                    return False
            else:
                if self._start_nmcli_hotspot():
                    self.active, self.method = True, "nmcli"
                else:
                    self.bus.error(
                        "Could not start a hotspot via NetworkManager. Check that "
                        "a Wi-Fi adapter is present and managed by NetworkManager "
                        "(nmcli device).")
                    self.bus.emit("hotspot_state", active=False, method=None)
                    return False
            self.bus.success(
                f"Hotspot up via {self.method}. SSID '{self.config.hotspot_ssid}', "
                f"clients should browse to http://{HOTSPOT_HOST_IP}:{self.config.port}")
            self.bus.emit("hotspot_state", active=True, method=self.method)
            return True

    def stop(self):
        with self._lock:
            if not self.active:
                return
            if self.method == "mobile-hotspot":
                _run_powershell(_PS_STOP_TETHERING)
            elif self.method == "hosted-network":
                _run_netsh(["wlan", "stop", "hostednetwork"])
            elif self.method == "nmcli":
                _run_nmcli(["connection", "down", NMCLI_CONNECTION_NAME])
            self.active, self.method = False, None
            self.bus.info("Hotspot stopped.")
            self.bus.emit("hotspot_state", active=False, method=None)

    def _start_mobile_hotspot(self):
        script = (_PS_START_TETHERING
                  .replace("__SSID__", _ps_quote(self.config.hotspot_ssid))
                  .replace("__KEY__", _ps_quote(self.config.hotspot_password)))
        try:
            code, out, err = _run_powershell(script)
        except subprocess.TimeoutExpired:
            self.bus.warn("Mobile Hotspot attempt timed out.")
            return False
        for line in out.splitlines():
            if line.startswith("WARN"):
                self.bus.warn(f"Mobile Hotspot: {line[5:]}")
        if code == 0 and "OK" in out:
            return True
        detail = next((l[4:] for l in out.splitlines() if l.startswith("ERR")), err or out)
        self.bus.warn(f"Mobile Hotspot unavailable ({detail.strip() or 'unknown error'}); "
                      "trying legacy hosted network...")
        return False

    def _start_hosted_network(self):
        try:
            _run_netsh(["wlan", "set", "hostednetwork", "mode=allow",
                        f"ssid={self.config.hotspot_ssid}",
                        f"key={self.config.hotspot_password}"])
            code, out, err = _run_netsh(["wlan", "start", "hostednetwork"])
        except subprocess.TimeoutExpired:
            self.bus.warn("Hosted network attempt timed out.")
            return False
        if code == 0:
            return True
        self.bus.warn(f"Hosted network failed: {(out or err).strip()}")
        return False

    def _start_nmcli_hotspot(self):
        try:
            code, out, err = _run_nmcli(
                ["device", "wifi", "hotspot",
                 "con-name", NMCLI_CONNECTION_NAME,
                 "ssid", self.config.hotspot_ssid,
                 "password", self.config.hotspot_password])
        except subprocess.TimeoutExpired:
            self.bus.warn("nmcli hotspot attempt timed out.")
            return False
        if code == 0:
            return True
        self.bus.warn(f"nmcli hotspot failed: {(err or out).strip()}")
        return False


# --- Background monitor -----------------------------------------------------

class NetworkMonitor(threading.Thread):
    """Watches connectivity; brings up the hotspot when the machine is offline."""

    def __init__(self, bus, config, hotspot):
        super().__init__(daemon=True, name="refuge-netmon")
        self.bus = bus
        self.config = config
        self.hotspot = hotspot
        self._stop_event = threading.Event()
        self._last_state = None

    def run(self):
        while not self._stop_event.is_set():
            state, addresses = network_state(self.hotspot.active)
            if state != self._last_state:
                self.bus.emit("network_state", state=state, addresses=addresses)
                if self._last_state is not None:
                    self.bus.info(f"Network state changed: {self._last_state} -> {state}")
                self._last_state = state
            if state == "OFFLINE" and self.config.auto_hotspot and not self.hotspot.active:
                self.bus.warn("No usable network connection detected - "
                              "activating emergency hotspot.")
                self.hotspot.start()
            self._stop_event.wait(max(3, int(self.config.check_interval_seconds)))

    def stop(self):
        self._stop_event.set()
