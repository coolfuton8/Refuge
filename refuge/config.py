"""Configuration persisted as JSON next to the application (travels with the USB drive)."""

import json
from dataclasses import dataclass, asdict, fields
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "refuge_config.json"


@dataclass
class Config:
    dest_dir: str = str(APP_DIR / "rescued_files")
    port: int = 8080
    bind_address: str = "0.0.0.0"
    hotspot_ssid: str = "REFUGE-RESCUE"
    hotspot_password: str = "rescue-me-now"
    auto_hotspot: bool = True
    check_interval_seconds: int = 10
    autostart_server: bool = True

    @classmethod
    def load(cls):
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return cfg
            valid = {f.name: f.type for f in fields(cls)}
            for key, value in raw.items():
                if key in valid:
                    setattr(cfg, key, value)
        return cfg

    def save(self):
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )

    def validate(self):
        """Return a list of human-readable problems (empty list = valid)."""
        problems = []
        if not (1 <= int(self.port) <= 65535):
            problems.append("Port must be between 1 and 65535.")
        if not self.dest_dir.strip():
            problems.append("Destination folder is required.")
        if self.auto_hotspot or self.hotspot_ssid:
            if not (1 <= len(self.hotspot_ssid) <= 32):
                problems.append("Hotspot SSID must be 1-32 characters.")
            if not (8 <= len(self.hotspot_password) <= 63):
                problems.append("Hotspot password must be 8-63 characters (WPA2 requirement).")
        return problems
