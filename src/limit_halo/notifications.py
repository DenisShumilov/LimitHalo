from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .config import AppConfig
from .model import Metric, ProviderState


@dataclass(frozen=True)
class QuotaNotification:
    provider: str
    window_key: str
    window_duration_mins: int
    remaining_percent: int
    resets_at: int


def in_quiet_hours(local_minutes: int, start: int, end: int) -> bool:
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (local_minutes, start, end)):
        raise ValueError("quiet-hour value must be an integer")
    if not all(0 <= value <= 1_439 for value in (local_minutes, start, end)):
        raise ValueError("quiet-hour value out of range")
    if start == end:
        return False
    if start < end:
        return start <= local_minutes < end
    return local_minutes >= start or local_minutes < end


class NotificationDecider:
    """Memory-only per-reset-cycle alert dedupe; no provider values persist."""

    def __init__(self) -> None:
        self._emitted: set[tuple[str, str, int, int]] = set()

    def evaluate(
        self,
        config: AppConfig,
        metrics: Iterable[Metric],
        *,
        now: datetime | None = None,
    ) -> tuple[QuotaNotification, ...]:
        if not config.alertsEnabled:
            return ()
        current = datetime.now().astimezone() if now is None else now.astimezone()
        minutes = current.hour * 60 + current.minute
        if config.quietHoursEnabled and in_quiet_hours(
            minutes, config.quietStartMinutes, config.quietEndMinutes
        ):
            return ()
        notifications: list[QuotaNotification] = []
        for metric in metrics:
            if metric.state is not ProviderState.READY or metric.used_percent is None or metric.resets_at is None:
                continue
            remaining = 100 - metric.used_percent
            if remaining > config.alertThresholdPercent:
                self._emitted = {
                    emitted
                    for emitted in self._emitted
                    if emitted[:3] != (metric.provider, metric.key, config.alertThresholdPercent)
                }
                continue
            key = (
                metric.provider,
                metric.key,
                config.alertThresholdPercent,
                metric.resets_at,
            )
            if key in self._emitted:
                continue
            self._emitted.add(key)
            notifications.append(
                QuotaNotification(
                    metric.provider,
                    metric.key,
                    metric.window_duration_mins,
                    remaining,
                    metric.resets_at,
                )
            )
        return tuple(notifications)


def quota_window_label(value: QuotaNotification, language: str) -> str:
    minutes = value.window_duration_mins
    if value.window_key == "claude_fable_10080":
        return "Fable · 7 днів" if language == "uk" else "Fable · 7-day"
    provider = "Codex" if value.provider == "codex" else "Claude"
    if minutes % 1_440 == 0:
        count = minutes // 1_440
        duration = f"{count} днів" if language == "uk" else f"{count}-day"
    elif minutes % 60 == 0:
        count = minutes // 60
        duration = f"{count} год" if language == "uk" else f"{count}-hour"
    else:
        duration = f"{minutes} хв" if language == "uk" else f"{minutes}-minute"
    return f"{provider} · {duration}"


def notification_text(value: QuotaNotification, language: str) -> tuple[str, str]:
    if language not in {"uk", "en"}:
        raise ValueError("unsupported language")
    provider = "Codex" if value.provider == "codex" else "Claude"
    window = quota_window_label(value, language)
    reset = datetime.fromtimestamp(value.resets_at).astimezone().strftime("%d.%m · %H:%M")
    if language == "uk":
        return f"Низький залишок {provider}", f"{window}: {value.remaining_percent}% залишилось · скидання {reset}"
    return f"Low {provider} allowance", f"{window}: {value.remaining_percent}% remaining · resets {reset}"


class WindowsBalloonNotifier:
    """A temporary Shell_NotifyIcon balloon; it is not a permanent tray icon."""

    def __init__(self, owner_hwnd: int, schedule: Callable[[int, Callable[[], None]], object]) -> None:
        self.owner_hwnd = owner_hwnd
        self.schedule = schedule
        self._serial = 0

    def show(self, title: str, message: str) -> bool:
        if os.name != "nt" or not title or not message:
            return False
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("hWnd", ctypes.c_void_p),
                ("uID", ctypes.c_uint32),
                ("uFlags", ctypes.c_uint32),
                ("uCallbackMessage", ctypes.c_uint32),
                ("hIcon", ctypes.c_void_p),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", ctypes.c_uint32),
                ("dwStateMask", ctypes.c_uint32),
                ("szInfo", ctypes.c_wchar * 256),
                ("uTimeoutOrVersion", ctypes.c_uint32),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", ctypes.c_uint32),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", ctypes.c_void_p),
            ]

        shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint32, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = ctypes.c_int
        self._serial = (self._serial + 1) & 0xFFFF
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self.owner_hwnd
        data.uID = ((os.getpid() & 0xFFFF) << 16) | self._serial
        user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.LoadIconW.restype = ctypes.c_void_p
        data.hIcon = user32.LoadIconW(None, ctypes.c_void_p(32516))  # IDI_INFORMATION
        data.uFlags = 0x00000002 | 0x00000010  # NIF_ICON | NIF_INFO
        data.szInfo = message[:255]
        data.szInfoTitle = title[:63]
        data.dwInfoFlags = 0x00000001 | 0x00000010  # NIIF_INFO | NIIF_NOSOUND
        if not shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(data)):
            return False
        shell32.Shell_NotifyIconW(0x00000001, ctypes.byref(data))

        def remove() -> None:
            shell32.Shell_NotifyIconW(0x00000002, ctypes.byref(data))

        self.schedule(10_000, remove)
        return True
