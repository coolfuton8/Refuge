"""Tkinter front end: configuration editor plus a live activity dashboard."""

import datetime
import ipaddress
import os
import subprocess
import sys
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import Config
from .events import EventBus
from .network import Hotspot, NetworkMonitor, network_state
from .server import UploadServer

POLL_MS = 150
# Seconds the connection-approval popup waits for the operator before it
# automatically denies and closes (kept below AccessControl.APPROVAL_TIMEOUT so
# the GUI resolves the waiting connection first).
PROMPT_TIMEOUT_S = 60

BG = "#14181d"
PANEL = "#1c222a"
FG = "#e6e9ec"
MUTED = "#8a939c"
ACCENT = "#4fc3f7"
GOOD = "#66d179"
WARN = "#e8b849"
BAD = "#ef6a6a"


def fmt_bytes(n):
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024


class RefugeApp:
    def __init__(self):
        self.config = Config.load()
        self.bus = EventBus()
        self.server = UploadServer(self.bus, self.config)
        self.hotspot = Hotspot(self.bus, self.config)
        self.monitor = NetworkMonitor(self.bus, self.config, self.hotspot)

        self.files_rescued = 0
        self.bytes_rescued = 0
        self.active_transfers = {}  # transfer id -> treeview item id
        self.net_state = None  # last state reported by the network monitor
        self._prompt_queue = []  # pending client-approval popups (ip, hostname)
        self._active_prompt = None
        self._blocklist_dialog = None

        self.root = tk.Tk()
        self.root.title(f"Refuge {__version__} - Emergency File Rescue")
        self._set_app_icon()
        self.root.geometry("1100x680")
        self.root.minsize(980, 560)
        self.root.configure(bg=BG)
        self._build_style()
        self._build_header()
        self._build_notebook()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.report_callback_exception = self._on_tk_error

    # ------------------------------------------------------------------ style

    def _set_app_icon(self):
        """Window/taskbar icon: a folder with a download arrow (files pulled to
        safety), drawn in code so no image file is needed. Matches the web
        favicon. Non-fatal if the toolkit rejects it."""
        try:
            size = 32
            bg, folder, arrow = "#14181d", "#4fc3f7", "#eaf6fb"
            px = [[bg] * size for _ in range(size)]

            def rect(x0, y0, x1, y1, color):
                for y in range(max(0, y0), min(size, y1)):
                    for x in range(max(0, x0), min(size, x1)):
                        px[y][x] = color

            rect(5, 9, 15, 13, folder)     # folder tab
            rect(5, 12, 27, 26, folder)    # folder body
            rect(15, 14, 18, 20, arrow)    # arrow shaft
            for i, y in enumerate(range(20, 24)):  # arrowhead, pointing down
                half = 5 - i
                rect(16 - half, y, 16 + half + 1, y + 1, arrow)

            img = tk.PhotoImage(width=size, height=size)
            for y in range(size):
                img.put("{" + " ".join(px[y]) + "}", to=(0, y))
            self._app_icon = img  # keep a reference so it is not garbage-collected
            self.root.iconphoto(True, img)
        except Exception:
            pass  # fall back to the default toolkit icon

    def _build_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                        bordercolor="#2a323c", lightcolor=PANEL, darkcolor=PANEL)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                        padding=(16, 8))
        style.map("TNotebook.Tab",
                  background=[("selected", "#2a323c")],
                  foreground=[("selected", FG)])
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Stat.TLabel", background=PANEL, foreground=ACCENT,
                        font=("Segoe UI", 18, "bold"))
        style.configure("TButton", background="#2a323c", foreground=FG,
                        borderwidth=0, focuscolor=PANEL, padding=(12, 6))
        style.map("TButton", background=[("active", "#37414d")])
        style.configure("Accent.TButton", background="#1b6d94", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#2585b3")])
        style.configure("TCheckbutton", background=PANEL, foreground=FG)
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("TEntry", insertcolor=FG)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=FG, rowheight=24, borderwidth=0)
        style.configure("Treeview.Heading", background="#2a323c", foreground=MUTED,
                        borderwidth=0)
        style.map("Treeview", background=[("selected", "#31536a")])
        style.configure("Vertical.TScrollbar", background="#2a323c",
                        troughcolor=PANEL, bordercolor=PANEL, arrowcolor=MUTED)
        style.map("Vertical.TScrollbar", background=[("active", "#37414d")])

    # ----------------------------------------------------------------- header

    def _build_header(self):
        bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(14, 10))
        bar.pack(fill="x")

        self.server_dot = tk.Label(bar, text="●", bg=PANEL, fg=BAD,
                                   font=("Segoe UI", 14))
        self.server_dot.pack(side="left")
        self.server_label = ttk.Label(bar, text="Server stopped", style="Panel.TLabel")
        self.server_label.pack(side="left", padx=(2, 18))

        self.net_dot = tk.Label(bar, text="●", bg=PANEL, fg=MUTED,
                                font=("Segoe UI", 14))
        self.net_dot.pack(side="left")
        self.net_label = ttk.Label(bar, text="Checking network...", style="Panel.TLabel")
        self.net_label.pack(side="left", padx=(2, 18))

        self.url_var = tk.StringVar(value="")
        url_entry = tk.Entry(bar, textvariable=self.url_var, readonlybackground=BG,
                             fg=ACCENT, bg=BG, relief="flat", state="readonly",
                             font=("Consolas", 10), width=1)
        url_entry.pack(side="left", fill="x", expand=True, padx=(0, 12))

        self.hotspot_btn = ttk.Button(bar, text="Start Hotspot",
                                      command=self._toggle_hotspot)
        self.hotspot_btn.pack(side="right", padx=(6, 0))
        self.server_btn = ttk.Button(bar, text="Start Server", style="Accent.TButton",
                                     command=self._toggle_server)
        self.server_btn.pack(side="right")

    # --------------------------------------------------------------- notebook

    def _build_notebook(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        self._build_dashboard(notebook)
        self._build_settings(notebook)

    def _build_dashboard(self, notebook):
        page = ttk.Frame(notebook, padding=10)
        notebook.add(page, text="  Dashboard  ")

        stats = ttk.Frame(page)
        stats.pack(fill="x", pady=(0, 10))
        self.stat_files = self._stat_tile(stats, "Files rescued")
        self.stat_bytes = self._stat_tile(stats, "Data rescued")
        self.stat_active = self._stat_tile(stats, "Active transfers")

        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="Open rescue folder",
                   command=self._open_folder).pack(side="left")
        ttk.Button(actions, text="Open upload page",
                   command=self._open_page).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open client scans",
                   command=self._open_scans).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Blocked clients…",
                   command=self._open_blocklist).pack(side="left", padx=(8, 0))

        self._build_authcode_panel(page)

        columns = ("time", "client", "file", "size", "status")
        tree_frame = ttk.Frame(page)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                 height=8)
        for cid, text, width, anchor in (
                ("time", "Time", 80, "w"), ("client", "From", 120, "w"),
                ("file", "File", 340, "w"), ("size", "Received", 110, "e"),
                ("status", "Status", 110, "w")):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor=anchor, stretch=(cid == "file"))
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical",
                                    command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        ttk.Label(page, text="Activity log").pack(anchor="w", pady=(10, 2))
        log_frame = ttk.Frame(page)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=9, bg=PANEL, fg=FG, relief="flat",
                           font=("Consolas", 9), state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                   command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("info", MUTED), ("warn", WARN),
                           ("error", BAD), ("success", GOOD)):
            self.log.tag_configure(tag, foreground=color)

    def _build_authcode_panel(self, page):
        panel = ttk.Frame(page, style="Panel.TFrame", padding=(16, 12))
        panel.pack(fill="x", pady=(0, 10))

        left = ttk.Frame(panel, style="Panel.TFrame")
        left.pack(side="left")
        ttk.Label(left, text="DELETE / OVERWRITE AUTHORIZATION CODE",
                  style="Muted.TLabel").pack(anchor="w")
        self.code_label = tk.Label(left, text="------", bg=PANEL, fg=ACCENT,
                                   font=("Consolas", 26, "bold"))
        self.code_label.pack(anchor="w")
        self.code_hint = ttk.Label(
            left, style="Muted.TLabel",
            text="Type this on the web page to delete or overwrite a saved "
                 "file. Changes after each use.")
        self.code_hint.pack(anchor="w")

        right = ttk.Frame(panel, style="Panel.TFrame")
        right.pack(side="right")
        self.code_button = ttk.Button(right, text="New code",
                                      command=self._new_authcode)
        self.code_button.pack()

    def _new_authcode(self):
        self.server.authcodes.reset()
        self.bus.info("Authorization code manually refreshed from the GUI.")

    def _stat_tile(self, parent, caption):
        tile = ttk.Frame(parent, style="Panel.TFrame", padding=(16, 10))
        tile.pack(side="left", fill="x", expand=True, padx=(0, 8))
        value = ttk.Label(tile, text="0", style="Stat.TLabel")
        value.pack(anchor="w")
        ttk.Label(tile, text=caption, style="Muted.TLabel").pack(anchor="w")
        return value

    def _build_settings(self, notebook):
        page = ttk.Frame(notebook, padding=16)
        notebook.add(page, text="  Settings  ")
        form = ttk.Frame(page, style="Panel.TFrame", padding=18)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        self.vars = {
            "dest_dir": tk.StringVar(value=self.config.dest_dir),
            "port": tk.StringVar(value=str(self.config.port)),
            "hotspot_ssid": tk.StringVar(value=self.config.hotspot_ssid),
            "hotspot_password": tk.StringVar(value=self.config.hotspot_password),
            "auto_hotspot": tk.BooleanVar(value=self.config.auto_hotspot),
            "autostart_server": tk.BooleanVar(value=self.config.autostart_server),
            "block_execution": tk.BooleanVar(value=self.config.block_execution),
            "compress_to_zip": tk.BooleanVar(value=self.config.compress_to_zip),
            "allow_web_delete": tk.BooleanVar(value=self.config.allow_web_delete),
            "require_client_approval": tk.BooleanVar(
                value=self.config.require_client_approval),
            "single_client_only": tk.BooleanVar(
                value=self.config.single_client_only),
            "scan_clients": tk.BooleanVar(value=self.config.scan_clients),
        }

        def row(index, label):
            ttk.Label(form, text=label, style="Muted.TLabel").grid(
                row=index, column=0, sticky="w", pady=6, padx=(0, 14))

        row(0, "Rescue folder (where uploads are saved)")
        dest = ttk.Frame(form, style="Panel.TFrame")
        dest.grid(row=0, column=1, sticky="ew", pady=6)
        dest.columnconfigure(0, weight=1)
        ttk.Entry(dest, textvariable=self.vars["dest_dir"]).grid(row=0, column=0,
                                                                 sticky="ew")
        ttk.Button(dest, text="Browse...", command=self._browse).grid(
            row=0, column=1, padx=(8, 0))

        row(1, "Server port")
        ttk.Entry(form, textvariable=self.vars["port"], width=10).grid(
            row=1, column=1, sticky="w", pady=6)

        row(2, "Hotspot network name (SSID)")
        ttk.Entry(form, textvariable=self.vars["hotspot_ssid"], width=34).grid(
            row=2, column=1, sticky="w", pady=6)

        row(3, "Hotspot password (8-63 chars)")
        ttk.Entry(form, textvariable=self.vars["hotspot_password"], width=34).grid(
            row=3, column=1, sticky="w", pady=6)

        ttk.Checkbutton(
            form, text="Automatically start hotspot when no network is found",
            variable=self.vars["auto_hotspot"]).grid(
            row=4, column=1, sticky="w", pady=(10, 2))
        ttk.Checkbutton(
            form, text="Start the upload server when Refuge launches",
            variable=self.vars["autostart_server"]).grid(
            row=5, column=1, sticky="w", pady=2)
        ttk.Checkbutton(
            form, text="Block execution of rescued files (quarantine: "
                       "deny-execute ACL + Mark-of-the-Web)",
            variable=self.vars["block_execution"]).grid(
            row=6, column=1, sticky="w", pady=2)
        ttk.Checkbutton(
            form, text="Compress each rescued file into a .zip "
                       "(verified byte-exact, then original is removed)",
            variable=self.vars["compress_to_zip"]).grid(
            row=7, column=1, sticky="w", pady=2)
        ttk.Checkbutton(
            form, text="Allow code-authorized delete/overwrite from the web page "
                       "(uncheck to make saved files strictly read-only)",
            variable=self.vars["allow_web_delete"]).grid(
            row=8, column=1, sticky="w", pady=2)
        ttk.Checkbutton(
            form, text="Ask before admitting each new client (popup approval; "
                       "denied clients get a 404)",
            variable=self.vars["require_client_approval"]).grid(
            row=9, column=1, sticky="w", pady=2)
        ttk.Checkbutton(
            form, text="Serve only one remote client at a time (others get 404 "
                       "until it disconnects)",
            variable=self.vars["single_client_only"]).grid(
            row=10, column=1, sticky="w", pady=2)
        ttk.Checkbutton(
            form, text="Fingerprint connecting clients with nmap, if installed "
                       "(saves a report per client for review)",
            variable=self.vars["scan_clients"]).grid(
            row=11, column=1, sticky="w", pady=2)

        ttk.Button(form, text="Save settings", style="Accent.TButton",
                   command=self._save_settings).grid(
            row=12, column=1, sticky="w", pady=(16, 0))

        if sys.platform == "win32":
            hint = ("Tip: if client machines cannot reach the upload page, allow the "
                    "port through Windows Firewall (run as admin):\n"
                    "netsh advfirewall firewall add rule name=\"Refuge\" dir=in "
                    "action=allow protocol=TCP localport=<port>")
        else:
            hint = ("Tip: if client machines cannot reach the upload page, allow the "
                    "port through the firewall, e.g.:\n"
                    "sudo ufw allow <port>/tcp    (or firewalld: "
                    "sudo firewall-cmd --add-port=<port>/tcp)")
        ttk.Label(page, text=hint, style="TLabel", foreground=MUTED,
                  wraplength=760, justify="left").pack(anchor="w", pady=(14, 0))

    # ---------------------------------------------------------------- actions

    def _browse(self):
        chosen = filedialog.askdirectory(initialdir=self.vars["dest_dir"].get() or None)
        if chosen:
            self.vars["dest_dir"].set(chosen)

    def _save_settings(self):
        candidate = Config(
            dest_dir=self.vars["dest_dir"].get().strip(),
            port=int(self.vars["port"].get() or 0) if
                 self.vars["port"].get().strip().isdigit() else 0,
            bind_address=self.config.bind_address,
            hotspot_ssid=self.vars["hotspot_ssid"].get().strip(),
            hotspot_password=self.vars["hotspot_password"].get(),
            auto_hotspot=self.vars["auto_hotspot"].get(),
            check_interval_seconds=self.config.check_interval_seconds,
            autostart_server=self.vars["autostart_server"].get(),
            block_execution=self.vars["block_execution"].get(),
            compress_to_zip=self.vars["compress_to_zip"].get(),
            allow_web_delete=self.vars["allow_web_delete"].get(),
            require_client_approval=self.vars["require_client_approval"].get(),
            single_client_only=self.vars["single_client_only"].get(),
            scan_clients=self.vars["scan_clients"].get(),
        )
        problems = candidate.validate()
        if problems:
            messagebox.showerror("Refuge", "\n".join(problems))
            return
        if self.config.block_execution and not candidate.block_execution:
            from .quarantine import unharden_destination
            unharden_destination(self.config.dest_dir)
            self.bus.warn("Execution blocking disabled - deny-execute ACL "
                          "removed from the rescue folder.")
        restart_server = self.server.running and (
            candidate.port != self.config.port or
            candidate.dest_dir != self.config.dest_dir or
            candidate.block_execution != self.config.block_execution or
            candidate.compress_to_zip != self.config.compress_to_zip or
            candidate.allow_web_delete != self.config.allow_web_delete or
            candidate.require_client_approval != self.config.require_client_approval or
            candidate.single_client_only != self.config.single_client_only)
        scan_changed = candidate.scan_clients != self.config.scan_clients
        for field, value in vars(candidate).items():
            setattr(self.config, field, value)
        self.config.save()
        self.bus.success("Settings saved.")
        self._render_authcode(self.server.authcodes.current())
        self.server.scanner.enabled = self.config.scan_clients  # takes effect live
        if scan_changed:
            self.server.scanner.announce()  # confirm nmap status on toggle
        if restart_server:
            self.bus.info("Restarting server to apply new settings...")
            self.server.stop()
            self.server.start()

    def _toggle_server(self):
        if self.server.running:
            self.server.stop()
        else:
            self.server.start()

    def _toggle_hotspot(self):
        if not self.hotspot.active and self.net_state != "OFFLINE":
            return  # hotspot is only offered while disconnected
        self.hotspot_btn.state(["disabled"])
        target = self.hotspot.stop if self.hotspot.active else self.hotspot.start
        import threading
        threading.Thread(target=target, daemon=True).start()

    def _open_folder(self):
        self._open_path(self.config.dest_dir)

    def _open_scans(self):
        self._open_path(str(self.server.scanner.scan_dir))

    def _open_blocklist(self):
        """Manage the connection-level blocklist (block/unblock IPs identified
        from the scan reports). Blocked IPs have their TCP connection dropped."""
        if getattr(self, "_blocklist_dialog", None) is not None:
            try:
                self._blocklist_dialog.lift()
                return
            except tk.TclError:
                self._blocklist_dialog = None

        dlg = tk.Toplevel(self.root)
        self._blocklist_dialog = dlg
        dlg.title("Refuge - Blocked clients")
        dlg.configure(bg=PANEL)
        dlg.transient(self.root)
        body = ttk.Frame(dlg, style="Panel.TFrame", padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Block a client IP (its connections will be dropped "
                             "for this session):", style="Muted.TLabel").pack(anchor="w")
        entry_row = ttk.Frame(body, style="Panel.TFrame")
        entry_row.pack(fill="x", pady=(4, 10))
        ip_var = tk.StringVar()
        entry = ttk.Entry(entry_row, textvariable=ip_var, width=26)
        entry.pack(side="left")
        entry.focus_set()

        listbox = tk.Listbox(body, height=8, bg=BG, fg=FG, relief="flat",
                             selectbackground="#31536a", activestyle="none",
                             highlightthickness=0)
        msg = ttk.Label(body, style="Muted.TLabel", text="")

        def refresh():
            listbox.delete(0, "end")
            for ip in self.server.access.blocked_ips():
                listbox.insert("end", ip)

        def do_block():
            raw = ip_var.get().strip()
            try:
                ipaddress.ip_address(raw)
            except ValueError:
                msg.configure(text=f"'{raw}' is not a valid IP address.")
                return
            self.server.access.block(raw)
            ip_var.set("")
            msg.configure(text=f"Blocked {raw}.")
            refresh()

        def do_unblock():
            sel = listbox.curselection()
            if not sel:
                msg.configure(text="Select a blocked IP to unblock.")
                return
            ip = listbox.get(sel[0])
            self.server.access.unblock(ip)
            msg.configure(text=f"Unblocked {ip}.")
            refresh()

        def on_close():
            self._blocklist_dialog = None
            dlg.destroy()

        ttk.Button(entry_row, text="Block", style="Accent.TButton",
                   command=do_block).pack(side="left", padx=(8, 0))
        entry.bind("<Return>", lambda _e: do_block())
        ttk.Label(body, text="Currently blocked:",
                  style="Muted.TLabel").pack(anchor="w")
        listbox.pack(fill="both", expand=True, pady=(2, 8))
        buttons = ttk.Frame(body, style="Panel.TFrame")
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Unblock selected",
                   command=do_unblock).pack(side="left")
        ttk.Button(buttons, text="Refresh", command=refresh).pack(side="left",
                                                                  padx=(8, 0))
        ttk.Button(buttons, text="Close", command=on_close).pack(side="right")
        msg.pack(anchor="w", pady=(8, 0))

        dlg.protocol("WM_DELETE_WINDOW", on_close)
        refresh()

    def _open_path(self, path):
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _open_page(self):
        webbrowser.open(f"http://127.0.0.1:{self.config.port}/")

    def _on_close(self):
        self.monitor.stop()
        self.server.stop()
        if self.hotspot.active and messagebox.askyesno(
                "Refuge", "The emergency hotspot is still running.\nTurn it off?"):
            self.hotspot.stop()
        self.root.destroy()

    # ----------------------------------------------------------- event handling

    def _poll_events(self):
        try:
            for event in self.bus.drain():
                handler = getattr(self, f"_on_{event.kind}", None)
                if handler:
                    try:
                        handler(event)
                    except Exception as exc:
                        self._append_log(
                            "error", f"Dashboard error while displaying "
                            f"'{event.kind}': {type(exc).__name__}: {exc}")
        finally:
            # The poll loop must survive anything, or the dashboard freezes.
            self.root.after(POLL_MS, self._poll_events)

    def _on_log(self, event):
        self._append_log(event.data["level"], event.data["message"])

    MAX_LOG_LINES = 5000

    def _append_log(self, level, message):
        try:
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            # Only chase the tail if the user is already there, so scrolling
            # back to read an error is not fought by incoming messages.
            follow = self.log.yview()[1] >= 0.999
            self.log.configure(state="normal")
            self.log.insert("end", f"[{stamp}] {message}\n", level)
            lines = int(self.log.index("end-1c").split(".")[0])
            if lines > self.MAX_LOG_LINES:
                self.log.delete("1.0", f"{lines - self.MAX_LOG_LINES + 1}.0")
            if follow:
                self.log.see("end")
            self.log.configure(state="disabled")
        except tk.TclError:
            pass  # window is being torn down

    def _on_tk_error(self, exc_type, exc_value, exc_tb):
        """Uncaught Tkinter callback exceptions land in the activity log
        instead of an invisible stderr (pythonw has no console)."""
        self._append_log("error",
                         f"UI error: {exc_type.__name__}: {exc_value}")

    def _on_authcode(self, event):
        self._render_authcode(event.data["code"], event.data.get("locked", False))

    def _render_authcode(self, code, locked=False):
        """Show the current code, or a locked/disabled state."""
        if not self.config.allow_web_delete:
            self.code_label.configure(text="OFF", fg=MUTED)
            self.code_hint.configure(
                text="Web delete/overwrite is disabled - saved files are "
                     "read-only from the web page.")
            self.code_button.state(["disabled"])
            return
        self.code_button.state(["!disabled"])
        if locked:
            self.code_label.configure(text=code, fg=BAD)
            self.code_hint.configure(
                text="LOCKED: too many invalid attempts. Wait ~60s or press "
                     "New code. Someone may be attacking from the client.")
        else:
            self.code_label.configure(text=code, fg=ACCENT)
            self.code_hint.configure(
                text="Type this on the web page to delete or overwrite a saved "
                     "file. Changes after each use.")

    def _on_access_request(self, event):
        self._prompt_queue.append((event.data["ip"], event.data.get("hostname", "")))
        self._show_next_prompt()

    def _show_next_prompt(self):
        if self._active_prompt is not None or not self._prompt_queue:
            return
        ip, hostname = self._prompt_queue.pop(0)
        who = f"{hostname} ({ip})" if hostname and hostname != ip else ip

        dlg = tk.Toplevel(self.root)
        self._active_prompt = dlg
        dlg.title("Refuge - New connection")
        dlg.configure(bg=PANEL)
        dlg.resizable(False, False)
        dlg.transient(self.root)

        body = ttk.Frame(dlg, style="Panel.TFrame", padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="A new client wants to connect:",
                  style="Panel.TLabel").pack(anchor="w")
        tk.Label(body, text=who, bg=PANEL, fg=ACCENT,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(4, 2))
        ttk.Label(body, text="Allow this client to upload to and download from "
                             "the rescue drive?", style="Muted.TLabel",
                  wraplength=360, justify="left").pack(anchor="w", pady=(0, 10))

        block_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="Always block this IP/client (until Refuge "
                                   "is closed)", variable=block_var).pack(anchor="w")

        countdown = ttk.Label(body, style="Muted.TLabel")
        countdown.pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(body, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(14, 0))

        state = {"answered": False, "after": None}

        def answer(allow, auto=False):
            if state["answered"]:
                return  # already resolved (guards a click racing the timer)
            state["answered"] = True
            if state["after"] is not None:
                try:
                    self.root.after_cancel(state["after"])
                except tk.TclError:
                    pass
            if auto:
                self.bus.warn(f"No response for connection from {who}; "
                              "automatically denied.")
            self.server.access.resolve(ip, allow, always_block=block_var.get())
            self._active_prompt = None
            dlg.destroy()
            self._show_next_prompt()

        def tick(remaining):
            if state["answered"]:
                return
            if remaining <= 0:
                answer(False, auto=True)
                return
            countdown.configure(
                text=f"Denies automatically in {remaining}s if you don't respond.")
            state["after"] = self.root.after(1000, lambda: tick(remaining - 1))

        ttk.Button(buttons, text="Deny",
                   command=lambda: answer(False)).pack(side="right")
        ttk.Button(buttons, text="Allow", style="Accent.TButton",
                   command=lambda: answer(True)).pack(side="right", padx=(0, 8))
        dlg.protocol("WM_DELETE_WINDOW", lambda: answer(False))
        tick(PROMPT_TIMEOUT_S)

        # Bring it to the operator's attention.
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dlg.lift()
        dlg.attributes("-topmost", True)
        try:
            dlg.bell()
        except tk.TclError:
            pass

    def _on_server_state(self, event):
        running = event.data["running"]
        self.server_dot.configure(fg=GOOD if running else BAD)
        self.server_label.configure(
            text=f"Server on port {event.data['port']}" if running else "Server stopped")
        self.server_btn.configure(text="Stop Server" if running else "Start Server")
        self._refresh_urls()

    def _on_network_state(self, event):
        state = event.data["state"]
        colors = {"LAN": GOOD, "HOTSPOT": WARN, "LAN+HOTSPOT": GOOD, "OFFLINE": BAD}
        labels = {"LAN": "Connected to network", "HOTSPOT": "Hotspot mode",
                  "LAN+HOTSPOT": "Network + hotspot", "OFFLINE": "No network"}
        self.net_state = state
        self.net_dot.configure(fg=colors.get(state, MUTED))
        self.net_label.configure(text=labels.get(state, state))
        if (state == "OFFLINE" and not self.hotspot.active
                and not self.config.auto_hotspot):
            self.bus.warn("No network detected - the emergency hotspot is now "
                          "available via the Start Hotspot button.")
        self._update_hotspot_button()
        self._refresh_urls(event.data.get("addresses"))

    def _on_hotspot_state(self, event):
        self._update_hotspot_button()

    def _update_hotspot_button(self):
        """The hotspot is only offered while disconnected; stopping a running
        hotspot is always allowed."""
        if self.hotspot.active:
            self.hotspot_btn.configure(text="Stop Hotspot")
            self.hotspot_btn.state(["!disabled"])
        elif self.net_state == "OFFLINE":
            self.hotspot_btn.configure(text="Start Hotspot")
            self.hotspot_btn.state(["!disabled"])
        else:
            self.hotspot_btn.configure(text="Hotspot (connected)")
            self.hotspot_btn.state(["disabled"])

    def _refresh_urls(self, addresses=None):
        from .network import network_state as _ns
        if addresses is None:
            _, addresses = _ns(self.hotspot.active)
        if self.server.running and addresses:
            urls = "  ".join(f"http://{a}:{self.config.port}" for a in addresses)
            self.url_var.set(f"Clients browse to:  {urls}")
        elif self.server.running:
            self.url_var.set("Server running, waiting for a network address...")
        else:
            self.url_var.set("")

    def _on_transfer_start(self, event):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        item = self.tree.insert("", 0, values=(
            stamp, event.data["client"], event.data["name"], "0 B", "receiving..."))
        self.active_transfers[event.data["id"]] = item
        self.stat_active.configure(text=str(len(self.active_transfers)))

    def _on_transfer_progress(self, event):
        item = self.active_transfers.get(event.data["id"])
        if item:
            self.tree.set(item, "size", fmt_bytes(event.data["written"]))

    def _on_transfer_done(self, event):
        item = self.active_transfers.pop(event.data["id"], None)
        if item:
            self.tree.set(item, "size", fmt_bytes(event.data["written"]))
            self.tree.set(item, "status", "saved ✓")
        self.files_rescued += 1
        self.bytes_rescued += event.data["written"]
        self.stat_files.configure(text=str(self.files_rescued))
        self.stat_bytes.configure(text=fmt_bytes(self.bytes_rescued))
        self.stat_active.configure(text=str(len(self.active_transfers)))

    def _on_transfer_error(self, event):
        item = self.active_transfers.pop(event.data["id"], None)
        if item:
            self.tree.set(item, "status", "FAILED")
        self.stat_active.configure(text=str(len(self.active_transfers)))

    # -------------------------------------------------------------------- run

    def run(self):
        self.bus.info(f"Refuge {__version__} ready. "
                      f"Rescue folder: {self.config.dest_dir}")
        self._render_authcode(self.server.authcodes.current())
        state, addresses = network_state(False)
        self.bus.emit("network_state", state=state, addresses=addresses)
        if self.config.autostart_server:
            self.server.start()
        self.monitor.start()
        self._poll_events()
        self.root.mainloop()


def main():
    RefugeApp().run()
