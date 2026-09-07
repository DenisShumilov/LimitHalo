from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from pathlib import Path

from . import PRODUCT_ID
from .auth import AuthLaunchFailure, claude_client_installed, launch_provider_sign_in
from .claude_protocol import BrokerFailure, ClaudeRuntime, production_client
from .codex_discovery import CodexCandidate, DiscoveryFailure, discover_public_codex
from .codex_protocol import CodexCollector
from .config import AppConfig, ConfigStore
from .model import MetricStore, ProviderState
from .startup import StartupFailure, set_start_with_windows, start_with_windows_active
from .ui import WidgetWindow, enable_dpi_awareness
from .localization import tr
from .update_controller import UpdateController


MUTEX_NAME = r"Local\AILimitsWidget-HUD-v1"
ACTIVATION_EVENT_NAME = r"Local\AILimitsWidget-HUD-Activate-v1"
POSTINSTALL_OPEN_ENVIRONMENT = "LIMIT_HALO_POSTINSTALL_OPEN"


def package_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def state_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA unavailable")
    return Path(local) / PRODUCT_ID


def portable_mode() -> bool:
    return (package_root() / "portable.flag").is_file()


class SingleInstance:
    def __init__(
        self,
        mutex_name: str = MUTEX_NAME,
        activation_event_name: str = ACTIVATION_EVENT_NAME,
    ) -> None:
        self.handle: int | None = None
        self.activation_handle: int | None = None
        self.acquired = True
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateEventW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, True, mutex_name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW")
        self.handle = int(handle)
        self.acquired = ctypes.get_last_error() != 183

        event = kernel32.CreateEventW(None, True, False, activation_event_name)
        if not event:
            error = ctypes.get_last_error()
            if self.acquired:
                kernel32.ReleaseMutex(ctypes.c_void_p(self.handle))
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None
            raise OSError(error, "CreateEventW")
        self.activation_handle = int(event)

    def request_activation(self) -> None:
        if not self.activation_handle or os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.SetEvent.restype = ctypes.c_int
        if not kernel32.SetEvent(ctypes.c_void_p(self.activation_handle)):
            raise OSError(ctypes.get_last_error(), "SetEvent")

    def consume_activation_request(self) -> bool:
        if not self.activation_handle or os.name != "nt":
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
        kernel32.ResetEvent.restype = ctypes.c_int
        result = kernel32.WaitForSingleObject(ctypes.c_void_p(self.activation_handle), 0)
        if result == 0x00000102:
            return False
        if result != 0:
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject")
        if not kernel32.ResetEvent(ctypes.c_void_p(self.activation_handle)):
            raise OSError(ctypes.get_last_error(), "ResetEvent")
        return True

    def close(self) -> None:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if self.activation_handle:
                kernel32.CloseHandle(ctypes.c_void_p(self.activation_handle))
                self.activation_handle = None
            if self.handle:
                if self.acquired:
                    kernel32.ReleaseMutex(ctypes.c_void_p(self.handle))
                kernel32.CloseHandle(ctypes.c_void_p(self.handle))
                self.handle = None


def executable_path() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve(strict=True)


def provider_should_be_visible(mode: str, present: bool) -> bool:
    if mode == "disabled":
        return False
    if mode == "enabled":
        return True
    if mode == "auto":
        return present
    raise ValueError("invalid provider mode")


def trusted_claude_client_present() -> bool:
    # Presence keeps Auto from becoming a hidden dead end.  Actual execution
    # still performs the stricter signature verification in auth.py.
    return claude_client_installed()


def _open_provider_free_configuration(
    *, codex_present: bool = False, claude_present: bool = False
) -> int:
    from .configure import main as configure_main

    return configure_main(
        codex_present=codex_present,
        claude_present=claude_present,
    )


def main(*, startup_launch: bool = False) -> int:
    instance = SingleInstance()
    if not instance.acquired:
        try:
            if not startup_launch:
                instance.request_activation()
            return 0
        finally:
            instance.close()
    postinstall_open = os.environ.pop(POSTINSTALL_OPEN_ENVIRONMENT, "") == "1"
    config_store = ConfigStore(state_root())
    current_config = config_store.load()
    portable = portable_mode()
    if portable and current_config.startWithWindows:
        current_config = __import__("dataclasses").replace(current_config, startWithWindows=False)
        if getattr(config_store, "write_blocked", False) is not True:
            config_store.save(current_config)

    codex_candidate: CodexCandidate | None = None
    if current_config.codexMode != "disabled":
        try:
            codex_candidate = discover_public_codex()
        except (OSError, DiscoveryFailure):
            codex_candidate = None
    claude_client_present = False
    if current_config.claudeMode != "disabled":
        claude_client_present = trusted_claude_client_present()
    claude_broker_present = False
    if current_config.claudeMode != "disabled" and current_config.claudeConsent == "granted":
        try:
            production_client(package_root())
            claude_broker_present = True
        except (OSError, BrokerFailure):
            claude_broker_present = False
    if not (
        provider_should_be_visible(current_config.codexMode, codex_candidate is not None)
        or provider_should_be_visible(
            current_config.claudeMode,
            claude_broker_present
            or (claude_client_present and current_config.claudeConsent != "denied"),
        )
    ):
        if startup_launch:
            instance.close()
            return 0
        try:
            configure_result = _open_provider_free_configuration(
                codex_present=codex_candidate is not None,
                claude_present=claude_client_present or claude_broker_present,
            )
        except BaseException:
            instance.close()
            raise
        if configure_result != 0:
            instance.close()
            return configure_result
        current_config = config_store.load()
        if portable and current_config.startWithWindows:
            current_config = __import__("dataclasses").replace(current_config, startWithWindows=False)
            if getattr(config_store, "write_blocked", False) is not True:
                config_store.save(current_config)
        if current_config.codexMode != "disabled":
            try:
                codex_candidate = discover_public_codex()
            except (OSError, DiscoveryFailure):
                codex_candidate = None
        else:
            codex_candidate = None
        claude_client_present = (
            current_config.claudeMode != "disabled" and trusted_claude_client_present()
        )
        claude_broker_present = False
        if current_config.claudeMode != "disabled" and current_config.claudeConsent == "granted":
            try:
                production_client(package_root())
                claude_broker_present = True
            except (OSError, BrokerFailure):
                claude_broker_present = False
        if not (
            provider_should_be_visible(current_config.codexMode, codex_candidate is not None)
            or provider_should_be_visible(
                current_config.claudeMode,
                claude_broker_present
                or (claude_client_present and current_config.claudeConsent != "denied"),
            )
        ):
            instance.close()
            return 0

    enable_dpi_awareness()
    store = MetricStore()
    codex: CodexCollector | None = None
    claude = ClaudeRuntime(store, lambda: current_config, lambda: production_client(package_root()))
    root = tk.Tk()
    shutdown_started = False
    window_ref: list[WidgetWindow] = []
    update_controller: UpdateController | None = None
    auth_recovery_generation = {"codex": 0, "claude": 0}

    def poll_activation() -> None:
        if shutdown_started:
            return
        try:
            requested = instance.consume_activation_request()
        except OSError:
            requested = False
        if requested and window_ref:
            window_ref[0].activate_from_shortcut()
        root.after(200, poll_activation)

    def start_codex_if_available() -> bool:
        nonlocal codex_candidate, codex
        if current_config.codexMode == "disabled":
            return False
        if codex_candidate is None:
            try:
                codex_candidate = discover_public_codex()
            except (OSError, DiscoveryFailure):
                return False
        if codex is None:
            selected = codex_candidate
            codex = CodexCollector(store, discovery=lambda: selected)
        codex.start()
        return True

    def stop_codex() -> None:
        nonlocal codex
        if codex is not None:
            codex.request_stop()
            codex.join(5.0)
            codex = None

    def present(provider: str) -> bool:
        if provider == "codex":
            return codex_candidate is not None
        if provider == "claude":
            return claude_broker_present or (
                claude_client_present and current_config.claudeConsent != "denied"
            )
        raise ValueError("invalid provider")

    def detected(provider: str) -> bool:
        if provider == "codex":
            return codex_candidate is not None
        if provider == "claude":
            return claude_client_present or claude_broker_present
        raise ValueError("invalid provider")

    def update_config(old: AppConfig, new: AppConfig) -> None:
        nonlocal current_config, claude_client_present, claude_broker_present
        if portable and new.startWithWindows:
            raise RuntimeError("startup is unavailable in portable mode")
        before = False if portable else start_with_windows_active(executable_path())
        if before != new.startWithWindows:
            try:
                set_start_with_windows(new.startWithWindows, executable_path())
                if start_with_windows_active(executable_path()) != new.startWithWindows:
                    raise StartupFailure("startup verification failed")
            except (OSError, StartupFailure):
                try:
                    if start_with_windows_active(executable_path()) != before:
                        set_start_with_windows(before, executable_path())
                except (OSError, StartupFailure):
                    pass
                raise RuntimeError("startup setting failed")
        current_config = new
        if new.claudeMode != "disabled" and not claude_client_present:
            claude_client_present = trusted_claude_client_present()
        if old.codexMode != "disabled" and new.codexMode == "disabled":
            auth_recovery_generation["codex"] += 1
        if new.codexMode == "disabled":
            stop_codex()
        else:
            start_codex_if_available()
        if (
            old.claudeMode != "disabled" and old.claudeConsent == "granted"
            and (new.claudeMode == "disabled" or new.claudeConsent != "granted")
        ):
            auth_recovery_generation["claude"] += 1
        if new.claudeMode == "disabled" or new.claudeConsent != "granted":
            claude.disable()
            claude_broker_present = False
        elif new.claudeConsent == "granted":
            try:
                claude.start_if_consented()
                claude_broker_present = True
            except (OSError, BrokerFailure):
                claude_broker_present = False
                store.mark_provider("claude", ProviderState.PROVIDER_ERROR, error_code="broker_failure")
        visible = (
            provider_should_be_visible(new.codexMode, codex_candidate is not None or store.ever_succeeded("codex"))
            or provider_should_be_visible(
                new.claudeMode,
                claude_broker_present
                or (claude_client_present and new.claudeConsent != "denied")
                or store.ever_succeeded("claude"),
            )
        )
        if not visible and window_ref:
            root.after(0, window_ref[0].exit)

    def refresh(provider: str) -> None:
        nonlocal claude_client_present, claude_broker_present
        if provider == "codex":
            if start_codex_if_available() and codex is not None:
                codex.request_refresh()
        elif provider == "claude":
            if current_config.claudeMode == "disabled":
                return
            if not claude_client_present:
                claude_client_present = trusted_claude_client_present()
            if current_config.claudeConsent == "granted":
                try:
                    claude.start_if_consented()
                    claude_broker_present = True
                except (OSError, BrokerFailure):
                    claude_broker_present = False
                    store.mark_provider("claude", ProviderState.PROVIDER_ERROR, error_code="broker_failure")
                    return
                claude.request_refresh()

    def provider_ready(provider: str) -> bool:
        codex_metrics, claude_metrics = store.snapshot()
        values = codex_metrics if provider == "codex" else claude_metrics
        return any(metric.state is ProviderState.READY for metric in values)

    def sign_in(provider: str) -> None:
        try:
            process = launch_provider_sign_in(provider)
        except (OSError, AuthLaunchFailure):
            store.mark_provider(provider, ProviderState.PROVIDER_ERROR, error_code="auth_client_unavailable")
            return
        store.mark_provider(provider, ProviderState.REFRESHING)
        auth_recovery_generation[provider] += 1
        generation = auth_recovery_generation[provider]

        def recover(attempt: int = 0, exited_observations: int = 0) -> None:
            if shutdown_started or generation != auth_recovery_generation[provider]:
                return
            if provider_ready(provider):
                return
            refresh(provider)
            if provider_ready(provider):
                return
            # Coalesced refreshes continue while the official client owns the
            # human sign-in flow, then allow two final observations after exit.
            exited = process.poll() is not None
            next_exited_observations = exited_observations + 1 if exited else 0
            if attempt >= 47 or next_exited_observations >= 3:
                return
            root.after(2_500, lambda: recover(attempt + 1, next_exited_observations))

        root.after(750, recover)

    def poll_provider_presence() -> None:
        nonlocal claude_client_present
        if shutdown_started:
            return
        if current_config.codexMode != "disabled" and codex_candidate is None:
            start_codex_if_available()
        if current_config.claudeMode != "disabled" and not claude_client_present:
            claude_client_present = trusted_claude_client_present()
        root.after(30_000, poll_provider_presence)

    def shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            return
        shutdown_started = True
        if update_controller is not None:
            update_controller.close()
        claude.shutdown()
        stop_codex()

    def check_updates() -> None:
        if update_controller is not None and not shutdown_started:
            update_controller.click()

    def update_menu_state() -> tuple[str, bool]:
        if update_controller is not None:
            return update_controller.menu_state()
        return tr(current_config.language, "check_updates"), True

    def poll_updates() -> None:
        if shutdown_started:
            return
        if update_controller is not None:
            update_controller.tick()
        root.after(150, poll_updates)

    try:
        if codex_candidate is not None and current_config.codexMode != "disabled":
            store.mark_provider("codex", ProviderState.REFRESHING)
            start_codex_if_available()
        if current_config.claudeConsent == "granted" and current_config.claudeMode != "disabled":
            try:
                claude.start_if_consented()
                claude_broker_present = True
            except (OSError, BrokerFailure):
                claude_broker_present = False
                store.mark_provider("claude", ProviderState.PROVIDER_ERROR, error_code="broker_failure")
        window = WidgetWindow(
            root,
            store,
            config_store,
            on_refresh=refresh,
            on_config_changed=update_config,
            on_exit=shutdown,
            on_sign_in=sign_in,
            provider_present=present,
            provider_detected=detected,
            startup_active=lambda: False if portable else start_with_windows_active(executable_path()),
            portable=portable,
            on_check_updates=check_updates,
            update_menu_state=update_menu_state,
        )
        window_ref.append(window)
        from tkinter import messagebox
        update_controller = UpdateController(
            update_root=state_root() / "updates",
            language=lambda: window.config.language,
            portable=portable,
            ask=lambda title, body: messagebox.askyesno(title, body, parent=window.foreground, default="no"),
            inform=lambda title, body: messagebox.showinfo(title, body, parent=window.foreground),
        )
        root.after(150, poll_updates)
        if postinstall_open:
            window.activate_from_shortcut()
        root.after(200, poll_activation)
        root.after(1_000, poll_provider_presence)
        root.mainloop()
        return 0
    finally:
        shutdown()
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
