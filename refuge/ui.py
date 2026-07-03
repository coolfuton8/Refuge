"""Tkinter front end: configuration editor plus a live activity dashboard."""

import datetime
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

        self.root = tk.Tk()
        self.root.title(f"Refuge {__version__} - Emergency File Rescue")
        self.root.geometry("980x680")
        self.root.minsize(820, 560)
        self.root.configure(bg=BG)
        self._build_style()
        self._build_header()
        self._build_notebook()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ style

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
                             font=("Consolas", 10), width=42)
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

        columns = ("time", "client", "file", "size", "status")
        self.tree = ttk.Treeview(page, columns=columns, show="headings", height=8)
        for cid, text, width, anchor in (
                ("time", "Time", 80, "w"), ("client", "From", 120, "w"),
                ("file", "File", 340, "w"), ("size", "Received", 110, "e"),
                ("status", "Status", 110, "w")):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor=anchor, stretch=(cid == "file"))
        self.tree.pack(fill="both", expand=True)

        ttk.Label(page, text="Activity log").pack(anchor="w", pady=(10, 2))
        self.log = tk.Text(page, height=9, bg=PANEL, fg=FG, relief="flat",
                           font=("Consolas", 9), state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)
        for tag, color in (("info", MUTED), ("warn", WARN),
                           ("error", BAD), ("success", GOOD)):
            self.log.tag_configure(tag, foreground=color)

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

        ttk.Button(form, text="Save settings", style="Accent.TButton",
                   command=self._save_settings).grid(
            row=6, column=1, sticky="w", pady=(16, 0))

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
        )
        problems = candidate.validate()
        if problems:
            messagebox.showerror("Refuge", "\n".join(problems))
            return
        restart_server = self.server.running and (
            candidate.port != self.config.port or
            candidate.dest_dir != self.config.dest_dir)
        for field, value in vars(candidate).items():
            setattr(self.config, field, value)
        self.config.save()
        self.bus.success("Settings saved.")
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
        self.hotspot_btn.state(["disabled"])
        target = self.hotspot.stop if self.hotspot.active else self.hotspot.start
        import threading
        threading.Thread(target=target, daemon=True).start()

    def _open_folder(self):
        os.makedirs(self.config.dest_dir, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(self.config.dest_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self.config.dest_dir])
        else:
            subprocess.Popen(["xdg-open", self.config.dest_dir])

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
        for event in self.bus.drain():
            handler = getattr(self, f"_on_{event.kind}", None)
            if handler:
                handler(event)
        self.root.after(POLL_MS, self._poll_events)

    def _on_log(self, event):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {event.data['message']}\n",
                        event.data["level"])
        self.log.see("end")
        self.log.configure(state="disabled")

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
        self.net_dot.configure(fg=colors.get(state, MUTED))
        self.net_label.configure(text=labels.get(state, state))
        self._refresh_urls(event.data.get("addresses"))

    def _on_hotspot_state(self, event):
        active = event.data["active"]
        self.hotspot_btn.state(["!disabled"])
        self.hotspot_btn.configure(text="Stop Hotspot" if active else "Start Hotspot")

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
        state, addresses = network_state(False)
        self.bus.emit("network_state", state=state, addresses=addresses)
        if self.config.autostart_server:
            self.server.start()
        self.monitor.start()
        self._poll_events()
        self.root.mainloop()


def main():
    RefugeApp().run()
