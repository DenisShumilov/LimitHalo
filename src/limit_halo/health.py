from __future__ import annotations

import hashlib
from pathlib import Path

from .config import AppConfig, V3_KEYS, decode_config
from .ui import MODE_SPECS
from .ui_contract import production_ui_contract, validate_ui_contract


ASSET_SHA256 = {
    "app.ico": "0fa0239e5e4d0dc347eee1bbe6a953654d805bb52d2bbc9e54bbb27f019a1284",
    "chatgpt.png": "375016542ebe78b63c7f36b8f6aee3058f2b706d861e5153fd74f9c27f63b8b7",
    "claude.png": "4dbf0aa77200058e8cb152e990597a95a99963db85632bf8abd8343a249e898e",
}


def offline_health_check() -> bool:
    """Read-only package self-check; never loads app, collectors, or auth."""

    config = AppConfig()
    encoded = config.to_json_object()
    if set(encoded) != set(V3_KEYS) or decode_config(encoded) != config:
        return False
    if validate_ui_contract(production_ui_contract()):
        return False
    if MODE_SPECS["both-compact"].size != (364, 84):
        return False
    assets = Path(__file__).resolve().parent / "assets"
    for name, expected in ASSET_SHA256.items():
        path = assets / name
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        if digest != expected:
            return False
    return True
