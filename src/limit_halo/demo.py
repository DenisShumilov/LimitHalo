from __future__ import annotations

"""Fixture-only demo; no production collector, auth, browser, or provider import."""

import argparse
import json
import os
import time
import tkinter as tk
from pathlib import Path

from .config import AppConfig, ConfigStore, reject_reparse_components
from .model import Metric, MetricStore, ProviderState
from .ui import WidgetWindow, enable_dpi_awareness


DEMO_STATES = (
    "ready",
    "refreshing",
    "offline",
    "authorization-required",
    "provider-error",
    "unavailable",
    "low",
    "critical",
)


def fixture_store(now: float | None = None, *, state: str = "ready") -> MetricStore:
    if state not in DEMO_STATES:
        raise ValueError("invalid fixture state")
    current = 2_000_000_000.0 if now is None else now
    store = MetricStore(now=current)
    if state == "unavailable":
        store.mark_provider("codex", ProviderState.UNAVAILABLE, now=current, error_code="trusted_client_missing")
        store.mark_provider("claude", ProviderState.UNAVAILABLE, now=current, error_code="trusted_client_missing")
        return store
    codex_used = 85 if state == "low" else (96 if state == "critical" else 24)
    claude_used = 86 if state == "low" else (97 if state == "critical" else 38)
    store.update_codex(
        (
            Metric("codex_300", "codex", 300, codex_used, int(current) + 12_000, ProviderState.READY, current),
            Metric("codex_10080", "codex", 10_080, 48, int(current) + 430_000, ProviderState.READY, current),
        )
    )
    store.update_claude(
        (
            Metric("claude_300", "claude", 300, claude_used, int(current) + 13_440, ProviderState.READY, current),
            Metric("claude_10080", "claude", 10_080, 82, int(current) + 86_400, ProviderState.READY, current),
            Metric("claude_fable_10080", "claude", 10_080, 85, int(current) + 86_400, ProviderState.READY, current),
        )
    )
    transition = {
        "refreshing": ProviderState.REFRESHING,
        "offline": ProviderState.OFFLINE,
        "authorization-required": ProviderState.AUTHORIZATION_REQUIRED,
        "provider-error": ProviderState.PROVIDER_ERROR,
    }.get(state)
    if transition is not None:
        error = {
            ProviderState.AUTHORIZATION_REQUIRED: "authorization_required",
            ProviderState.PROVIDER_ERROR: "provider_error",
        }.get(transition, "transport_timeout")
        store.mark_provider("codex", transition, now=current + 1, error_code=error)
        store.mark_provider("claude", transition, now=current + 1, error_code=error)
    return store


class FixtureController:
    def __init__(self) -> None:
        self.refreshes: list[str] = []
        self.sign_ins: list[str] = []

    def refresh(self, provider: str) -> None:
        if provider not in {"codex", "claude"}:
            raise ValueError("invalid fixture provider")
        self.refreshes.append(provider)

    def sign_in(self, provider: str) -> None:
        if provider not in {"codex", "claude"}:
            raise ValueError("invalid fixture provider")
        self.sign_ins.append(provider)


def validated_fixture_roots(authorized_state_root: Path, config_root: Path) -> tuple[Path, Path]:
    def existing_directory(value: Path, label: str) -> Path:
        if not value.is_absolute():
            raise ValueError(f"{label} must be an absolute path")
        absolute = Path(os.path.abspath(value))
        reject_reparse_components(absolute)
        try:
            canonical = absolute.resolve(strict=True)
        except OSError as exc:
            raise OSError(f"{label} must be an existing directory") from exc
        if not canonical.is_dir():
            raise ValueError(f"{label} must be a directory")
        reject_reparse_components(canonical)
        return canonical

    state = existing_directory(authorized_state_root, "--authorized-state-root")
    config = existing_directory(config_root, "--config-root")
    try:
        common = Path(os.path.commonpath((str(state), str(config))))
    except ValueError as exc:
        raise ValueError("--config-root must be strictly beneath --authorized-state-root") from exc
    if os.path.normcase(str(common)) != os.path.normcase(str(state)) or os.path.normcase(str(config)) == os.path.normcase(str(state)):
        raise ValueError("--config-root must be strictly beneath --authorized-state-root")
    return state, config


def _mode_config(mode: str, locale: str) -> AppConfig:
    if mode == "both-compact":
        return AppConfig(locale=locale, claudeMode="enabled", codexMode="enabled", claudeConsent="granted", displayPreset="compact")
    if mode == "both-wide":
        return AppConfig(locale=locale, claudeMode="enabled", codexMode="enabled", claudeConsent="granted", displayPreset="wide")
    if mode == "claude-only":
        return AppConfig(locale=locale, claudeMode="enabled", codexMode="disabled", claudeConsent="granted", displayPreset="compact")
    if mode == "codex-one-window":
        return AppConfig(locale=locale, claudeMode="disabled", codexMode="enabled", displayPreset="compact")
    if mode == "codex-two-window":
        return AppConfig(locale=locale, claudeMode="disabled", codexMode="enabled", displayPreset="wide")
    raise ValueError("invalid fixture mode")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline AILimitsWidget fixture demo")
    parser.add_argument("--authorized-state-root", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--exit-after-ms", type=int, default=None)
    parser.add_argument("--locale", choices=("uk-UA", "en-US"), default="en-US")
    parser.add_argument(
        "--mode",
        choices=("both-compact", "both-wide", "claude-only", "codex-one-window", "codex-two-window"),
        default="both-compact",
    )
    parser.add_argument("--status-colors", action="store_true")
    parser.add_argument("--state", choices=DEMO_STATES, default="ready")
    parser.add_argument("--x", type=int, default=None)
    parser.add_argument("--y", type=int, default=None)
    parser.add_argument("--diagnostics-json", action="store_true")
    args = parser.parse_args(argv)
    enable_dpi_awareness()
    if args.exit_after_ms is not None and not 100 <= args.exit_after_ms <= 30_000:
        parser.error("--exit-after-ms must be between 100 and 30000")
    try:
        _state, config_root = validated_fixture_roots(args.authorized_state_root, args.config_root)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    config_store = ConfigStore(config_root)
    config = _mode_config(args.mode, args.locale)
    if (args.x is None) != (args.y is None):
        parser.error("--x and --y must be supplied together")
    if args.x is not None:
        if not -100_000 <= args.x <= 100_000 or not -100_000 <= args.y <= 100_000:
            parser.error("fixture position is out of range")
        config = __import__("dataclasses").replace(config, x=args.x, y=args.y)
    if args.status_colors:
        config = __import__("dataclasses").replace(config, colorMode="status")
    config_store.save(config)
    store = fixture_store(state=args.state)
    controller = FixtureController()
    root = tk.Tk()
    window = WidgetWindow(
        root,
        store,
        config_store,
        on_refresh=controller.refresh,
        on_config_changed=lambda _old, _new: None,
        on_exit=lambda: None,
        on_sign_in=controller.sign_in,
        provider_present=lambda provider: (
            provider == "codex" and config.codexMode != "disabled"
        ) or (provider == "claude" and config.claudeMode != "disabled"),
        startup_active=lambda: False,
    )
    if args.diagnostics_json:
        print(json.dumps(window.diagnostics_snapshot(), ensure_ascii=False, sort_keys=True))
        window.exit()
        return 0
    if args.exit_after_ms is not None:
        root.after(args.exit_after_ms, window.exit)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
