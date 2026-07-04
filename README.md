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
- The **Start Hotspot** button is only offered while disconnected — when the
  machine already has a network, the button reads "Hotspot (connected)" and is
  disabled. Stopping a running hotspot is always allowed.

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

**Not every Wi-Fi adapter can host a hotspot.** Access Point mode is a driver
and firmware feature, and some chipsets (older Intel cards in particular)
don't support it at all — NetworkManager will fail every attempt with an
error like "Hotspot network creation took too long" / "supplicant took too
long to authenticate". If Auto-hotspot is on and the machine has no other
network, Refuge will keep retrying that failing hotspot every few seconds
(and keep logging the same error) for as long as it stays offline. If you see
this, the fix isn't in Refuge's settings — try a different Wi-Fi adapter (a
USB Wi-Fi dongle with confirmed AP-mode support works well) on the Refuge
machine.

## Configuration (Settings tab)

| Setting | Default | Notes |
|---|---|---|
| Rescue folder | `rescued_files` next to the app | Where uploads are saved |
| Server port | `8080` | |
| Hotspot SSID / password | `REFUGE-RESCUE` / `rescue-me-now` | Password must be 8–63 chars |
| Auto-hotspot | on | Start hotspot when offline |
| Autostart server | on | Start listening at launch |
| Block execution of rescued files | on | Quarantine the rescue folder — see below |
| Compress each rescued file into a .zip | off | Verify-then-delete original — see below |
| Allow code-authorized delete/overwrite | on | Uncheck to make saved files strictly read-only — see below |

Settings persist to `refuge_config.json` next to the app, so they travel with
the USB drive. Changing the port, rescue folder, quarantine, or compression
option while the server is running restarts it automatically to apply.

## Live dashboard

The Dashboard tab shows everything happening in real time so you're never
guessing during a rescue:

- **Stat tiles** — files rescued, total data rescued, and active transfers.
- **Transfer list** — one row per file with the source machine, live byte
  count as it streams in, and final status. Scrollable.
- **Activity log** — timestamped, colour-coded events (info / success /
  warning / error), scrollable back through history. It auto-follows new
  messages only when you're already scrolled to the bottom, so reading an
  error isn't interrupted by incoming transfers; scroll back down to resume
  following. Kept to the last 5000 lines.
- **Authorization code** — the 6-character code required to delete or
  overwrite a saved file from the web page (see below).

**All errors surface here.** Because Refuge normally runs windowless (via
`pythonw`, so no console exists), errors that would otherwise vanish to
stderr — HTTP server-thread failures, client machines dropping mid-transfer,
disk/compression failures, and internal UI errors — are all routed into the
activity log instead. A "Open rescue folder" and "Open upload page" button sit
above the transfer list.

## Protecting saved files (delete / overwrite authorization)

The machine you're rescuing files *from* may be infected — possibly with a
remote-access trojan actively driving that computer. Refuge assumes the web
side is hostile and protects files that are already on the rescue drive:

- **Uploads never overwrite.** A file whose name already exists is saved as a
  numbered copy (`report (1).xlsx`); an upload can't silently replace or
  corrupt an existing rescued file.
- **Deleting or overwriting an existing file requires a one-time code shown
  only on the rescue machine's GUI.** The web page has a field to enter it.
  Because the code is displayed on the operator's screen — which an attacker
  on the *client* machine cannot see — only the person physically at the
  rescue laptop can authorize a destructive action. This is an out-of-band
  confirmation: driving the web interface is not enough.

How it behaves:

- The current 6-character code is shown on the Dashboard (unambiguous
  characters only — no `O`/`0` or `I`/`1` to misread). It **changes after
  every successful delete or overwrite**, so each destructive action needs a
  fresh look at the screen and a used code can't be replayed.
- Wrong codes are rejected, logged, and after a few rapid failures the
  destructive endpoints **lock for ~60 seconds** (the dashboard warns you —
  it may be an attack from the client machine). Combined with the code space
  this makes brute force over HTTP infeasible. The **New code** button clears
  a lockout and issues a fresh code.
- To overwrite instead of making a numbered copy, tick "Overwrite files that
  already exist" on the web page and enter the code; without a valid code the
  overwrite is ignored and the upload is saved as a numbered copy instead.
- **Uncheck "Allow code-authorized delete/overwrite" in Settings** to remove
  the capability entirely — saved files become strictly read-only from the
  web page and the code panel shows `OFF`.

Threat model: this defends against a compromised *client* (uploading) machine.
It does not defend the rescue laptop itself — if that machine is compromised,
the attacker can see the screen and all bets are off.

## Execution safeguard (quarantine)

Files rescued from a failing machine may be infected, so by default the
rescue folder is treated as a quarantine zone ("Block execution of rescued
files" in Settings):

- **Windows:** a deny-execute ACL is applied to the rescue folder and
  inherited by every file in it — Windows refuses to launch executables
  stored there (Access denied), while reading, copying, and browsing work
  normally. Each saved file is also tagged with Mark-of-the-Web
  (Internet zone), so SmartScreen still warns if a file is copied elsewhere
  and run. Requires the rescue folder to be on an NTFS drive; on FAT32/exFAT
  the dashboard logs a warning instead.
- **Linux:** execute permission is stripped from every saved file. For a
  kernel-enforced guarantee, mount the rescue drive with the `noexec` option,
  e.g. `sudo mount -o noexec,nosuid,nodev /dev/sdb1 /mnt/rescue`.

Turning the option off removes the folder ACL. To remove it manually:
`icacls "<rescue folder>" /remove:d *S-1-1-0`

This blocks in-place execution; it does **not** disinfect anything. Scan
rescued files with AV before restoring them to a rebuilt machine.

## Compress to zip (optional)

Enable "Compress each rescued file into a .zip" in Settings and every file is
compressed right after it finishes uploading. The original is only deleted
after the archive passes verification: a zip CRC integrity test **and** a full
byte-for-byte comparison of the archived content against the original. If
verification fails for any reason, the uncompressed original is kept and the
dashboard logs an error — rescue data is never lost to a bad archive.

Off by default. Zips inherit the quarantine protections (Mark-of-the-Web,
deny-execute folder ACL). Zip64 is enabled, so files over 4 GB are fine.

## Reliability details

- Uploads stream to disk in 64 KiB chunks — multi-GB files are fine and never
  held in memory.
- Files are written as `<name>.part` and renamed only when complete, so a
  dropped connection never leaves a file that *looks* rescued but isn't.
- Duplicate names are auto-suffixed (`report (1).xlsx`); nothing is
  overwritten without the GUI authorization code (see above).
- Filenames are sanitized server-side, and download/delete/overwrite targets
  are confined to the rescue folder (no path traversal from the web page).
- Each entry in "Files already rescued to this drive" is a link back to that
  file, streamed to the browser in 64 KiB chunks, so a rescued file can be
  pulled back down through the same page if needed.
- A Delete button next to each entry removes that file, but only with the
  GUI authorization code (see above); deletions are logged to the activity log.

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
  ui.py               Tkinter configuration + live dashboard (scrollable log)
  server.py           threaded HTTP server, streaming multipart parser,
                      verified compress-to-zip
  network.py          connectivity monitor + hotspot control
                      (Windows WinRT/netsh, Linux nmcli)
  quarantine.py       execution safeguards (deny-execute ACL, Mark-of-the-Web,
                      Linux no-exec)
  authcode.py         out-of-band delete/overwrite authorization codes
  web.py              the embedded single-page upload site
  config.py           JSON config persistence
  events.py           thread-safe event bus feeding the dashboard
```
