from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Callable, Iterable

from .config import AppConfig
from .localization import tr


@dataclass(frozen=True)
class MenuEntry:
    label: str = ""
    action: str | None = None
    kind: str = "command"
    checked: bool = False
    enabled: bool = True
    children: tuple["MenuEntry", ...] = ()


def build_context_menu(
    config: AppConfig,
    *,
    actual_topmost: bool,
    actual_startup: bool,
    codex_present: bool | None = None,
    claude_present: bool | None = None,
    update_label: str | None = None,
    update_enabled: bool = True,
) -> tuple[MenuEntry, ...]:
    language = config.language
    return (
        MenuEntry(tr(language, "refresh_now"), "refresh"),
        MenuEntry(tr(language, "status_colors"), "colors:toggle", "check", config.colorMode == "status"),
        MenuEntry(update_label or tr(language, "check_updates"), "check-updates", enabled=update_enabled),
        MenuEntry(kind="separator"),
        MenuEntry(tr(language, "startup"), "startup", "check", actual_startup),
        MenuEntry(tr(language, "topmost"), "topmost", "check", actual_topmost),
        MenuEntry(tr(language, "settings_title"), "configure"),
        MenuEntry(kind="separator"),
        MenuEntry(tr(language, "exit"), "exit"),
    )


def flatten_actions(entries: Iterable[MenuEntry]) -> tuple[str, ...]:
    result: list[str] = []
    for entry in entries:
        if entry.action is not None:
            result.append(entry.action)
        result.extend(flatten_actions(entry.children))
    return tuple(result)


class NativeMenu:
    """Win32 TrackPopupMenuEx with the foreground widget HWND as owner."""

    def __init__(self, owner_hwnd: int) -> None:
        if os.name != "nt" or not owner_hwnd:
            raise OSError("native Windows menu unavailable")
        self.owner_hwnd = owner_hwnd

    def show(
        self,
        entries: Iterable[MenuEntry],
        x: int,
        y: int,
        actions: dict[str, Callable[[], None]],
    ) -> str | None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreatePopupMenu.restype = ctypes.c_void_p
        user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_wchar_p]
        user32.AppendMenuW.restype = ctypes.c_int
        user32.TrackPopupMenuEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        user32.TrackPopupMenuEx.restype = ctypes.c_uint32
        user32.DestroyMenu.argtypes = [ctypes.c_void_p]
        root = user32.CreatePopupMenu()
        if not root:
            raise OSError(ctypes.get_last_error(), "CreatePopupMenu")
        command_actions: dict[int, str] = {}
        next_id = 1

        def append(handle: int, values: Iterable[MenuEntry]) -> None:
            nonlocal next_id
            for entry in values:
                if entry.kind == "separator":
                    if not user32.AppendMenuW(handle, 0x00000800, 0, None):
                        raise OSError(ctypes.get_last_error(), "AppendMenuW")
                    continue
                flags = 0x00000000
                if not entry.enabled:
                    flags |= 0x00000001
                if entry.checked:
                    flags |= 0x00000008
                if entry.kind == "radio":
                    flags |= 0x00000200
                identifier: int
                if entry.kind == "submenu":
                    child = user32.CreatePopupMenu()
                    if not child:
                        raise OSError(ctypes.get_last_error(), "CreatePopupMenu")
                    append(child, entry.children)
                    flags |= 0x00000010
                    identifier = int(child)
                elif entry.action is not None:
                    identifier = next_id
                    command_actions[identifier] = entry.action
                    next_id += 1
                else:
                    identifier = 0
                if not user32.AppendMenuW(handle, flags, identifier, entry.label):
                    raise OSError(ctypes.get_last_error(), "AppendMenuW")

        try:
            append(root, entries)
            user32.SetForegroundWindow(ctypes.c_void_p(self.owner_hwnd))
            command_id = user32.TrackPopupMenuEx(
                root,
                0x0100 | 0x0002 | 0x0080,  # RETURNCMD | RIGHTBUTTON | NONOTIFY
                int(x),
                int(y),
                ctypes.c_void_p(self.owner_hwnd),
                None,
            )
            user32.PostMessageW(ctypes.c_void_p(self.owner_hwnd), 0, 0, 0)
            action = command_actions.get(command_id)
            if action is not None and action in actions:
                actions[action]()
            return action
        finally:
            user32.DestroyMenu(root)
