"""Execution safeguards for the rescue folder.

Files arriving here come from machines that may be infected, so the save
location is treated as a quarantine zone:

  Windows:
    - A deny-execute ACL on the rescue folder, inherited by every file at any
      depth. Windows then refuses to launch executables stored there, while
      reading and copying remain unaffected and folders stay traversable.
    - Mark-of-the-Web (Zone.Identifier "Internet" stream) on every saved file,
      so SmartScreen still warns even if a file is copied elsewhere first.
  Linux:
    - Execute bits stripped from every saved file.
    - For a kernel-level guarantee, mount the rescue drive with `noexec`
      (documented in the README; a mount option cannot be applied per-folder
      from here).

None of this disinfects anything - scan rescued files before restoring them.
"""

import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

EVERYONE_SID = "*S-1-1-0"  # locale-independent "Everyone"

MOTW_CONTENT = "[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=about:internet\r\n"

_motw_warned = False


def harden_destination(dest_dir, bus):
    """Apply folder-level no-execute protection. Best effort; logs outcome."""
    if not IS_WINDOWS:
        bus.info("Rescued files are saved without execute permission. For a "
                 "kernel-enforced guarantee, mount the rescue drive with the "
                 "'noexec' option.")
        return
    try:
        # Remove any previous Refuge deny entry first so repeated runs do not
        # stack duplicate ACEs, then deny Execute to Everyone, inherited by
        # files only ((OI)(IO) leaves the folders themselves traversable).
        subprocess.run(
            ["icacls", str(dest_dir), "/remove:d", EVERYONE_SID],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW)
        proc = subprocess.run(
            ["icacls", str(dest_dir), "/deny", f"{EVERYONE_SID}:(OI)(IO)(X)"],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        bus.warn(f"Could not apply no-execute ACL to the rescue folder: {exc}")
        return
    if proc.returncode == 0:
        bus.success("Rescue folder hardened: Windows will refuse to execute "
                    "files stored in it (deny-execute ACL).")
    else:
        detail = (proc.stderr or proc.stdout or "").strip()
        bus.warn("Could not apply no-execute ACL (is the rescue folder on an "
                 f"NTFS drive?): {detail}")


def unharden_destination(dest_dir):
    """Remove the deny-execute ACL (used when the operator turns the option off)."""
    if not IS_WINDOWS:
        return
    subprocess.run(
        ["icacls", str(dest_dir), "/remove:d", EVERYONE_SID],
        capture_output=True, text=True, timeout=30,
        creationflags=CREATE_NO_WINDOW)


def protect_file(path, bus):
    """Per-file safeguards applied right after a file finishes saving."""
    global _motw_warned
    if IS_WINDOWS:
        # Mark-of-the-Web: tag as Internet-zone content so SmartScreen warns
        # even if the file is later copied off the quarantined folder.
        try:
            with open(f"{path}:Zone.Identifier", "w", encoding="utf-8") as fh:
                fh.write(MOTW_CONTENT)
        except OSError as exc:
            if not _motw_warned:
                _motw_warned = True
                bus.warn("Could not write Mark-of-the-Web on rescued files "
                         f"(non-NTFS rescue drive?): {exc}")
    else:
        try:
            os.chmod(path, os.stat(path).st_mode & ~0o111)
        except OSError as exc:
            if not _motw_warned:
                _motw_warned = True
                bus.warn(f"Could not strip execute permission: {exc}")
