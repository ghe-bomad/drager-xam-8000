"""Configuration and credential loading for Dräger X-am 8000."""

import json
from pathlib import Path

_DIR = Path(__file__).parent

def load_config(path: Path | str | None = None) -> dict:
    """Load settings from config.json. Returns dict with defaults for missing keys."""
    defaults = {
        "port": "auto",
        "baudrate": 115200,
        "timeout": 3.0,
        "security_mode": 5,
        "polling_interval": 2.0,
    }
    cfg_path = Path(path) if path else _DIR / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            defaults.update(json.load(f))
    return defaults


def load_credentials(path: Path | str | None = None) -> dict[int, str]:
    """Load security passwords from credentials.json.

    Returns dict mapping mode (int) -> password (str).
    Falls back to empty dict if file missing.
    """
    cred_path = Path(path) if path else _DIR / "credentials.json"
    if not cred_path.exists():
        return {}
    with open(cred_path) as f:
        data = json.load(f)
    return {int(k): v for k, v in data.get("passwords", {}).items()}
