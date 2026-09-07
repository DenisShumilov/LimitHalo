from __future__ import annotations

"""Provider-free setup route.  Importing this module starts no collector or auth flow."""

import os
import sys
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from . import APP_NAME, PRODUCT_ID
from .config import ALERT_THRESHOLDS, AppConfig, ConfigStore, decode_config
from .localization import assert_translation_parity, tr
from .startup import set_start_with_windows, start_with_windows_active


def configuration_state_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA unavailable")
    return Path(local) / PRODUCT_ID


def configuration_executable() -> Path:
    value = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    return Path(value).resolve(strict=True)


def configuration_is_portable() -> bool:
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return (root / "portable.flag").is_file()


def grant_claude_quota_access() -> int:
    """Persist the installer's explicit quota-only consent without providers."""

    try:
        store = ConfigStore(configuration_state_root())
        current = store.load()
        store.save(replace(current, claudeMode="enabled", claudeConsent="granted"))
        return 0
    except (OSError, RuntimeError, ValueError):
        return 1


def parse_local_time(value: str) -> int:
    if len(value) != 5 or value[2] != ":" or not (value[:2] + value[3:]).isascii() or not (value[:2] + value[3:]).isdecimal():
        raise ValueError("invalid local time")
    hours, minutes = int(value[:2]), int(value[3:])
    if hours > 23 or minutes > 59:
        raise ValueError("invalid local time")
    return hours * 60 + minutes


def format_local_time(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_439:
        raise ValueError("invalid local time")
    return f"{value // 60:02d}:{value % 60:02d}"


def build_config_from_answers(
    current: AppConfig,
    *,
    locale: str,
    claude_mode: str,
    codex_mode: str,
    claude_access: bool,
    view: str,
    colors: str,
    alerts: bool,
    threshold: int,
    quiet_hours: bool,
    quiet_start: str,
    quiet_end: str,
    startup: bool,
) -> AppConfig:
    quiet_start_minutes = parse_local_time(quiet_start)
    quiet_end_minutes = parse_local_time(quiet_end)
    if quiet_start_minutes == quiet_end_minutes:
        raise ValueError("quiet-hours interval is empty")
    candidate = replace(
        current,
        locale=locale,
        claudeMode=claude_mode,
        codexMode=codex_mode,
        claudeConsent=(
            "granted"
            if claude_access
            else ("denied" if current.claudeConsentCompleted else current.claudeConsent)
        ),
        displayPreset=view,
        colorMode=colors,
        alertsEnabled=alerts,
        alertThresholdPercent=threshold,
        quietHoursEnabled=quiet_hours,
        quietStartMinutes=quiet_start_minutes,
        quietEndMinutes=quiet_end_minutes,
        startWithWindows=startup,
    )
    if decode_config(candidate.to_json_object()) != candidate:
        raise ValueError("invalid setup answers")
    return candidate


def persist_configuration(
    store: ConfigStore,
    old: AppConfig,
    new: AppConfig,
    *,
    executable: Path,
    portable: bool,
    startup_reader: Callable[[Path], bool] = start_with_windows_active,
    startup_writer: Callable[[bool, Path], None] = set_start_with_windows,
) -> None:
    """Apply external startup truth first, persist only after verification, and roll back."""

    if getattr(store, "write_blocked", False) is True:
        raise OSError("existing settings require recovery before changes can be applied")

    before = False if portable else startup_reader(executable)
    requested = False if portable else new.startWithWindows
    applied = False
    try:
        if before != requested:
            startup_writer(requested, executable)
            applied = True
        if (False if portable else startup_reader(executable)) != requested:
            raise RuntimeError("startup state did not match the requested value")
        store.save(replace(new, startWithWindows=requested))
    except BaseException:
        if applied:
            try:
                startup_writer(before, executable)
            except (OSError, RuntimeError):
                pass
        try:
            store.save(replace(old, startWithWindows=before))
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class SettingsSurface:
    tabs: tuple[str, ...]
    provider_rows: tuple[str, ...]
    claude_access_action: str | None
    alert_details: tuple[str, ...]
    advanced_visible: bool
    daily_duplicates: tuple[str, ...]
    help_actions: tuple[str, ...]


def settings_surface(
    config: AppConfig, *, codex_present: bool, claude_present: bool
) -> SettingsSurface:
    """Pure, deterministic projection used by the UI and release snapshots."""

    providers = tuple(
        provider
        for provider, present in (("claude", claude_present), ("codex", codex_present))
        if present
    )
    return SettingsSurface(
        tabs=("settings", "help"),
        provider_rows=providers,
        claude_access_action=(
            "revoke" if config.claudeConsent == "granted" else "connect"
        ) if claude_present else None,
        alert_details=("threshold", "quiet-hours") if config.alertsEnabled else (),
        advanced_visible=config.displayPreset == "wide" or config.quietHoursEnabled,
        daily_duplicates=(),
        help_actions=("copy-ai-repair-report", "privacy-about"),
    )


class ConfigureDialog:
    def __init__(
        self,
        parent: tk.Misc,
        initial: AppConfig,
        commit: Callable[[AppConfig], bool],
        *,
        startup_actual: bool,
        portable: bool,
        codex_present: bool = False,
        claude_present: bool = False,
        copy_repair_report: Callable[[], None] | None = None,
        show_about: Callable[[], None] | None = None,
    ) -> None:
        assert_translation_parity()
        self.initial = initial
        self.commit = commit
        self.portable = portable
        self.codex_present = codex_present
        self.claude_present = claude_present
        self.copy_repair_report = copy_repair_report
        self.show_about = show_about
        self.window = tk.Toplevel(parent)
        self.window.title(f"{APP_NAME} · {tr(initial.language, 'settings_title')}")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.attributes("-topmost", True)
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

        self.locale = tk.StringVar(value=initial.locale)
        self.show_claude = tk.BooleanVar(value=initial.claudeMode != "disabled")
        self.show_codex = tk.BooleanVar(value=initial.codexMode != "disabled")
        self.claude_access = tk.BooleanVar(value=initial.claudeConsent == "granted")
        self.keep_wide = tk.BooleanVar(value=initial.displayPreset == "wide")
        self.status_colors = tk.BooleanVar(value=initial.colorMode == "status")
        self.alerts = tk.BooleanVar(value=initial.alertsEnabled)
        self.threshold = tk.IntVar(value=initial.alertThresholdPercent)
        self.quiet_hours = tk.BooleanVar(value=initial.quietHoursEnabled)
        self.quiet_start = tk.StringVar(value=format_local_time(initial.quietStartMinutes))
        self.quiet_end = tk.StringVar(value=format_local_time(initial.quietEndMinutes))
        self.startup = tk.BooleanVar(value=False if portable else startup_actual)
        self.advanced = tk.BooleanVar(
            value=False
        )
        self._build()
        self.window.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.window.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.window.winfo_reqheight()) // 2)
        self.window.geometry(f"+{x}+{y}")
        self.window.grab_set()
        self.window.focus_force()

    def _build(self) -> None:
        language = self.initial.language
        surface = settings_surface(
            self.initial,
            codex_present=self.codex_present,
            claude_present=self.claude_present,
        )
        body = ttk.Frame(self.window, padding=12)
        body.grid(sticky="nsew")

        tabs = ttk.Notebook(body)
        tabs.grid(row=0, column=0, columnspan=2, sticky="nsew")
        settings = ttk.Frame(tabs, padding=14)
        help_page = ttk.Frame(tabs, padding=14)
        tabs.add(settings, text=tr(language, "settings_title"))
        tabs.add(help_page, text=tr(language, "help"))

        ttk.Label(settings, text=tr(language, "setup_intro"), wraplength=430, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )

        ttk.Label(settings, text=tr(language, "language")).grid(
            row=1, column=0, sticky="w", padx=(0, 14), pady=4
        )
        language_options = (("en-US", "english"), ("uk-UA", "ukrainian"))
        language_labels = [tr(language, key) for _value, key in language_options]
        language_values = {
            label: value
            for label, (value, _key) in zip(language_labels, language_options, strict=True)
        }
        language_reverse = {value: label for label, value in language_values.items()}
        language_shown = tk.StringVar(value=language_reverse[self.locale.get()])
        language_combo = ttk.Combobox(
            settings,
            textvariable=language_shown,
            values=language_labels,
            state="readonly",
            width=28,
        )
        language_combo.grid(row=1, column=1, sticky="ew", pady=4)
        language_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.locale.set(language_values[language_shown.get()]),
        )

        row = 2
        if self.claude_present:
            ttk.Checkbutton(
                settings, text=tr(language, "show_claude"), variable=self.show_claude
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
            row += 1
            consent_key = (
                "claude_access_revoke"
                if self.initial.claudeConsent == "granted"
                else "claude_access"
            )
            ttk.Checkbutton(
                settings, text=tr(language, consent_key), variable=self.claude_access
            ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(18, 0), pady=4)
            row += 1
        if self.codex_present:
            ttk.Checkbutton(
                settings, text=tr(language, "show_codex"), variable=self.show_codex
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
            row += 1
        if not self.claude_present and not self.codex_present:
            ttk.Label(
                settings,
                text=tr(language, "no_providers_detected"),
                wraplength=430,
                justify="left",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
            row += 1

        ttk.Checkbutton(
            settings, text=tr(language, "status_colors"), variable=self.status_colors
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 4))
        row += 1
        ttk.Checkbutton(
            settings,
            text=tr(language, "low_quota_alerts"),
            variable=self.alerts,
            command=self._update_optional_controls,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
        row += 1

        self.alert_controls = ttk.Frame(settings)
        self.alert_controls.grid(row=row, column=0, columnspan=2, sticky="ew", padx=(18, 0))
        ttk.Label(self.alert_controls, text=tr(language, "alert_threshold")).grid(
            row=0, column=0, sticky="w", padx=(0, 14), pady=4
        )
        ttk.Combobox(
            self.alert_controls,
            textvariable=self.threshold,
            values=sorted(ALERT_THRESHOLDS, reverse=True),
            state="readonly",
            width=8,
        ).grid(row=0, column=1, sticky="w", pady=4)
        row += 1

        advanced_needed = surface.advanced_visible
        self.advanced_toggle = ttk.Checkbutton(
            settings,
            text=tr(language, "advanced"),
            variable=self.advanced,
            command=self._update_optional_controls,
        )
        self.advanced_toggle.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        if not advanced_needed:
            self.advanced_toggle.grid_remove()
        row += 1

        self.advanced_controls = ttk.Frame(settings)
        self.advanced_controls.grid(row=row, column=0, columnspan=2, sticky="ew", padx=(18, 0))
        ttk.Checkbutton(
            self.advanced_controls,
            text=f"{tr(language, 'display')}: {tr(language, 'wide')}",
            variable=self.keep_wide,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        self.quiet_controls = ttk.Frame(self.alert_controls)
        self.quiet_controls.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Checkbutton(
            self.quiet_controls,
            text=tr(language, "quiet_hours"),
            variable=self.quiet_hours,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(self.quiet_controls, text=tr(language, "quiet_from")).grid(
            row=2, column=0, sticky="w", pady=4
        )
        quiet_times = ttk.Frame(self.quiet_controls)
        quiet_times.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Entry(quiet_times, textvariable=self.quiet_start, width=6).pack(side="left")
        ttk.Label(quiet_times, text=f"  {tr(language, 'quiet_to')}  ").pack(side="left")
        ttk.Entry(quiet_times, textvariable=self.quiet_end, width=6).pack(side="left")

        ttk.Label(
            help_page,
            text=tr(language, "repair_help"),
            wraplength=430,
            justify="left",
        ).pack(anchor="w", fill="x")
        ttk.Button(
            help_page, text=tr(language, "fix_with_ai"), command=self._copy_repair
        ).pack(anchor="w", pady=(10, 16))
        ttk.Separator(help_page, orient="horizontal").pack(fill="x", pady=(0, 12))
        ttk.Label(
            help_page,
            text=f"{tr(language, 'privacy_text')}\n\n{tr(language, 'security_text')}\n\n{tr(language, 'license_text')}",
            wraplength=430,
            justify="left",
        ).pack(anchor="w", fill="x")
        ttk.Button(
            help_page, text=tr(language, "privacy_about"), command=self._show_about
        ).pack(anchor="w", pady=(10, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text=tr(language, "cancel"), command=self.window.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text=tr(language, "save"), command=self._save, default="active").pack(side="left")
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<Return>", lambda _event: self._save())
        self._update_optional_controls()

    def _update_optional_controls(self) -> None:
        if self.alerts.get():
            self.alert_controls.grid()
        else:
            self.alert_controls.grid_remove()
        if self.alerts.get():
            self.quiet_controls.grid()
        else:
            self.quiet_controls.grid_remove()
        if self.advanced.get():
            self.advanced_controls.grid()
        else:
            self.advanced_controls.grid_remove()
        self.window.update_idletasks()

    def _copy_repair(self) -> None:
        try:
            if self.copy_repair_report is not None:
                self.copy_repair_report()
            else:
                self.window.clipboard_clear()
                self.window.clipboard_append(tr(self.initial.language, "repair_help"))
        except (OSError, RuntimeError, tk.TclError):
            messagebox.showerror(
                tr(self.initial.language, "settings_title"),
                tr(self.initial.language, "setup_failed"),
                parent=self.window,
            )
            return
        messagebox.showinfo(
            tr(self.initial.language, "help"),
            f"{tr(self.initial.language, 'repair_report_copied')}\n\n{tr(self.initial.language, 'repair_help')}",
            parent=self.window,
        )

    def _show_about(self) -> None:
        if self.show_about is not None:
            self.show_about()
            return
        messagebox.showinfo(
            tr(self.initial.language, "privacy_about"),
            f"{tr(self.initial.language, 'privacy_text')}\n\n{tr(self.initial.language, 'license_text')}",
            parent=self.window,
        )

    def _save(self) -> None:
        try:
            candidate = build_config_from_answers(
                self.initial,
                locale=self.locale.get(),
                claude_mode=(
                    self.initial.claudeMode
                    if self.show_claude.get() and self.initial.claudeMode != "disabled"
                    else "auto" if self.show_claude.get() else "disabled"
                ),
                codex_mode=(
                    self.initial.codexMode
                    if self.show_codex.get() and self.initial.codexMode != "disabled"
                    else "auto" if self.show_codex.get() else "disabled"
                ),
                claude_access=self.claude_access.get(),
                view="wide" if self.keep_wide.get() else "compact",
                colors="status" if self.status_colors.get() else "monochrome",
                alerts=self.alerts.get(),
                threshold=self.threshold.get(),
                quiet_hours=self.quiet_hours.get(),
                quiet_start=self.quiet_start.get(),
                quiet_end=self.quiet_end.get(),
                startup=self.startup.get(),
            )
            if not self.commit(candidate):
                raise RuntimeError("configuration commit rejected")
        except (OSError, RuntimeError, ValueError):
            messagebox.showerror(
                tr(self.initial.language, "settings_title"),
                tr(self.initial.language, "setup_failed"),
                parent=self.window,
            )
            return
        self.window.destroy()


def open_configure_dialog(
    parent: tk.Misc,
    initial: AppConfig,
    commit: Callable[[AppConfig], bool],
    *,
    startup_actual: bool,
    portable: bool,
    codex_present: bool = False,
    claude_present: bool = False,
    copy_repair_report: Callable[[], None] | None = None,
    show_about: Callable[[], None] | None = None,
) -> ConfigureDialog:
    return ConfigureDialog(
        parent,
        initial,
        commit,
        startup_actual=startup_actual,
        portable=portable,
        codex_present=codex_present,
        claude_present=claude_present,
        copy_repair_report=copy_repair_report,
        show_about=show_about,
    )


def main(*, codex_present: bool = False, claude_present: bool = False) -> int:
    store = ConfigStore(configuration_state_root())
    initial = store.load()
    executable = configuration_executable()
    portable = configuration_is_portable()
    actual = False if portable else start_with_windows_active(executable)
    root = tk.Tk()
    root.withdraw()

    def commit(candidate: AppConfig) -> bool:
        try:
            persist_configuration(
                store,
                initial,
                candidate,
                executable=executable,
                portable=portable,
            )
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    dialog = open_configure_dialog(
        root,
        initial,
        commit,
        startup_actual=actual,
        portable=portable,
        codex_present=codex_present,
        claude_present=claude_present,
    )
    root.wait_window(dialog.window)
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
