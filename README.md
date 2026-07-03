# Refuge — Emergency File Rescue

A portable safety net for MSP field work. Run Refuge on a laptop or portable
machine, and any failing client machine on the same network (or connected to
Refuge's emergency Wi-Fi hotspot) can evacuate files to it through a simple
web page — no software installed on the dying machine, just a browser.

**Zero dependencies.** Pure Python standard library (3.9+, tested on 3.13),
so it runs from a USB stick on any machine with Python — no internet, no
`pip install`, exactly when you need that most.

## Quick start

1. **Windows:** double-click `run.bat` (from PowerShell: `.\run.bat`).
   **Linux/macOS:** `chmod +x run.sh` once, then `./run.sh`.
   Either way you can also just run `python run.py`.
2. The upload server starts automatically. The header shows the URL to give
   the client machine, e.g. `http://192.168.1.122:8080`.
3. On the failing machine, open that URL in any browser, optionally type a
   machine label (files get grouped into a folder with that name), and drag
   files in. Watch them land in real time on the Refuge dashboard.

## How networking works

- **On a working LAN:** Refuge just listens on the machine's existing address.
- **No network at all:** the monitor detects it and automatically stands up an
  emergency Wi-Fi hotspot (configurable SSID/password). Join the failing
  machine to that Wi-Fi and browse to `http://192.168.137.1:<port>`.
- You can also force the hotspot on/off with the **Start Hotspot** button.

Hotspot support is best-effort:

**Windows** (clients browse to `http://192.168.137.1:<port>`):
1. **Mobile Hotspot** (WinRT API) — Windows 10/11, no admin required,
   but Windows needs at least one connection profile to attach to.
2. **Legacy hosted network** (`netsh wlan`) — works with no network present,
   but requires running Refuge **as Administrator** and a Wi-Fi driver that
   still supports hosted networks.

**Linux** (clients browse to `http://10.42.0.1:<port>`):
1. **NetworkManager** (`nmcli device wifi hotspot`) — needs a Wi-Fi adapter
   managed by NetworkManager, which is standard on desktop distros.

If every method fails, the dashboard log tells you why.

## Configuration (Settings tab)

| Setting | Default | Notes |
|---|---|---|
| Rescue folder | `rescued_files` next to the app | Where uploads are saved |
| Server port | `8080` | |
| Hotspot SSID / password | `REFUGE-RESCUE` / `rescue-me-now` | Password must be 8–63 chars |
| Auto-hotspot | on | Start hotspot when offline |
| Autostart server | on | Start listening at launch |

Settings persist to `refuge_config.json` next to the app, so they travel with
the USB drive.

## Reliability details

- Uploads stream to disk in 64 KiB chunks — multi-GB files are fine and never
  held in memory.
- Files are written as `<name>.part` and renamed only when complete, so a
  dropped connection never leaves a file that *looks* rescued but isn't.
- Duplicate names are auto-suffixed (`report (1).xlsx`); nothing is overwritten.
- Filenames are sanitized server-side (no path traversal from the web page).

## Firewall

If a client can't reach the page, allow the port through the firewall on the
Refuge machine.

Windows (elevated prompt) — note that Windows usually offers the standard
"Allow access" prompt on first start; accept it for **both** private and
public networks (hotspot clients are often classified as public):

```
netsh advfirewall firewall add rule name="Refuge" dir=in action=allow protocol=TCP localport=8080
```

Linux:

```
sudo ufw allow 8080/tcp                      # Ubuntu/Debian with ufw
sudo firewall-cmd --add-port=8080/tcp        # Fedora/RHEL with firewalld
```

## Linux notes

- Requires `python3` with tkinter (`sudo apt install python3-tk` /
  `sudo dnf install python3-tkinter`); `run.sh` checks and tells you.
- Hotspot mode uses NetworkManager; verify with `nmcli device` that your
  Wi-Fi adapter shows as managed.

## Layout

```
run.bat               Windows launcher (double-click)
run.sh                Linux/macOS launcher
run.py                python entry point
refuge/
  ui.py               Tkinter configuration + live dashboard
  server.py           threaded HTTP server, streaming multipart parser
  network.py          connectivity monitor + hotspot control (WinRT / netsh)
  web.py              the embedded single-page upload site
  config.py           JSON config persistence
  events.py           thread-safe event bus feeding the dashboard
```
