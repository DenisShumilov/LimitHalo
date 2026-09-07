from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from .config import reject_reparse_components


_HRESULT = wintypes.LONG


class StartupFailure(RuntimeError):
    pass


STARTUP_DESCRIPTION = "LimitHalo owned startup shortcut v1"
STARTUP_ARGUMENTS = "--startup"


@dataclass(frozen=True)
class StartupRegistration:
    state: str
    path: Path

    @property
    def active(self) -> bool:
        return self.state == "owned"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    import uuid

    value = uuid.UUID(text)
    result = _GUID()
    ctypes.memmove(ctypes.byref(result), value.bytes_le, 16)
    return result


def _method(pointer: ctypes.c_void_p, index: int, restype: object, *argtypes: object):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])


def _release(pointer: ctypes.c_void_p) -> None:
    if pointer.value:
        _method(pointer, 2, wintypes.ULONG)(pointer)
        pointer.value = None


class ShellLink:
    CLSID = _guid("00021401-0000-0000-C000-000000000046")
    IID_LINK = _guid("000214F9-0000-0000-C000-000000000046")
    IID_PERSIST = _guid("0000010B-0000-0000-C000-000000000046")

    def __init__(self) -> None:
        if os.name != "nt":
            raise StartupFailure("Windows required")
        self.ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        status = self.ole32.CoInitializeEx(None, 2)
        self._uninitialize = status in (0, 1)
        if status < 0 and ctypes.c_uint32(status).value != 0x80010106:
            raise StartupFailure("COM unavailable")
        self.link = ctypes.c_void_p()
        self.persist = ctypes.c_void_p()
        self.ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        self.ole32.CoCreateInstance.restype = _HRESULT
        if self.ole32.CoCreateInstance(
            ctypes.byref(self.CLSID), None, 1, ctypes.byref(self.IID_LINK), ctypes.byref(self.link)
        ) < 0:
            self.close()
            raise StartupFailure("Shell Link unavailable")
        query = _method(self.link, 0, _HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
        if query(self.link, ctypes.byref(self.IID_PERSIST), ctypes.byref(self.persist)) < 0:
            self.close()
            raise StartupFailure("IPersistFile unavailable")

    def set_path(self, path: Path) -> None:
        if _method(self.link, 20, _HRESULT, wintypes.LPCWSTR)(self.link, str(path)) < 0:
            raise StartupFailure("shortcut target rejected")

    def set_working_directory(self, path: Path) -> None:
        if _method(self.link, 9, _HRESULT, wintypes.LPCWSTR)(self.link, str(path)) < 0:
            raise StartupFailure("shortcut directory rejected")

    def set_description(self, value: str) -> None:
        if _method(self.link, 7, _HRESULT, wintypes.LPCWSTR)(self.link, value) < 0:
            raise StartupFailure("shortcut description rejected")

    def set_arguments(self, value: str) -> None:
        if _method(self.link, 11, _HRESULT, wintypes.LPCWSTR)(self.link, value) < 0:
            raise StartupFailure("shortcut arguments rejected")

    @staticmethod
    def _read_text(pointer: ctypes.c_void_p, index: int, label: str) -> str:
        buffer = ctypes.create_unicode_buffer(32_768)
        if _method(pointer, index, _HRESULT, wintypes.LPWSTR, ctypes.c_int)(
            pointer, buffer, len(buffer)
        ) < 0:
            raise StartupFailure(f"shortcut {label} unavailable")
        return buffer.value

    def description(self) -> str:
        return self._read_text(self.link, 6, "description")

    def working_directory(self) -> Path:
        return Path(self._read_text(self.link, 8, "working directory"))

    def arguments(self) -> str:
        return self._read_text(self.link, 10, "arguments")

    def target(self) -> Path:
        buffer = ctypes.create_unicode_buffer(32_768)
        if _method(
            self.link, 3, _HRESULT,
            wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        )(self.link, buffer, len(buffer), None, 0) < 0:
            raise StartupFailure("shortcut target unavailable")
        return Path(buffer.value)

    def load(self, path: Path) -> None:
        if _method(self.persist, 5, _HRESULT, wintypes.LPCWSTR, wintypes.DWORD)(
            self.persist, str(path), 0
        ) < 0:
            raise StartupFailure("shortcut load failed")

    def save(self, path: Path) -> None:
        if _method(self.persist, 6, _HRESULT, wintypes.LPCWSTR, wintypes.BOOL)(
            self.persist, str(path), True
        ) < 0:
            raise StartupFailure("shortcut save failed")

    def close(self) -> None:
        _release(getattr(self, "persist", ctypes.c_void_p()))
        _release(getattr(self, "link", ctypes.c_void_p()))
        if getattr(self, "_uninitialize", False):
            self.ole32.CoUninitialize()
            self._uninitialize = False

    def __enter__(self) -> "ShellLink":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise StartupFailure("APPDATA unavailable")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "LimitHalo.lnk"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve(strict=True))) == os.path.normcase(str(right.resolve(strict=True)))
    except OSError:
        return os.path.normcase(str(left.absolute())) == os.path.normcase(str(right.absolute()))


def _loaded_shortcut_is_owned(link: ShellLink, target: Path) -> bool:
    return (
        _same_path(link.target(), target)
        and _same_path(link.working_directory(), target.parent)
        and link.arguments() in {"", STARTUP_ARGUMENTS}
        and link.description() == STARTUP_DESCRIPTION
    )


def inspect_startup_registration(executable: Path) -> StartupRegistration:
    """Classify the real shortcut from all ownership fields, never cached config."""

    target = executable.resolve(strict=True)
    shortcut = startup_shortcut_path()
    reject_reparse_components(shortcut)
    if not shortcut.exists():
        return StartupRegistration("missing", shortcut)
    if not shortcut.is_file():
        return StartupRegistration("foreign", shortcut)
    try:
        with ShellLink() as existing:
            existing.load(shortcut)
            state = "owned" if _loaded_shortcut_is_owned(existing, target) else "foreign"
    except (OSError, StartupFailure):
        state = "unreadable"
    return StartupRegistration(state, shortcut)


def start_with_windows_active(executable: Path) -> bool:
    """Read the exact owned registration; never trust a cached checkmark."""

    return inspect_startup_registration(executable).active


def set_start_with_windows(enabled: bool, executable: Path) -> None:
    """Atomically create/remove only an exact, fully verified owned shortcut."""

    reject_reparse_components(executable)
    target = executable.resolve(strict=True)
    registration = inspect_startup_registration(target)
    shortcut = registration.path
    if registration.state in {"foreign", "unreadable"}:
        raise StartupFailure("refusing to modify an unowned startup shortcut")
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    reject_reparse_components(shortcut)
    suffix = uuid.uuid4().hex
    temporary = shortcut.with_name(f".{shortcut.name}.{suffix}.tmp")
    backup = shortcut.with_name(f".{shortcut.name}.{suffix}.bak")
    moved_existing = False
    try:
        if registration.active:
            reject_reparse_components(backup)
            os.replace(shortcut, backup)
            moved_existing = True
        if not enabled:
            if shortcut.exists() or inspect_startup_registration(target).state != "missing":
                raise StartupFailure("startup shortcut removal could not be verified")
            if moved_existing:
                backup.unlink()
                moved_existing = False
            return
        reject_reparse_components(temporary)
        with ShellLink() as link:
            link.set_path(target)
            link.set_working_directory(target.parent)
            link.set_arguments(STARTUP_ARGUMENTS)
            link.set_description(STARTUP_DESCRIPTION)
            link.save(temporary)
        reject_reparse_components(temporary)
        reject_reparse_components(shortcut)
        os.replace(temporary, shortcut)
        if not inspect_startup_registration(target).active:
            raise StartupFailure("startup shortcut verification failed")
        if moved_existing:
            backup.unlink()
            moved_existing = False
    except BaseException:
        try:
            if shortcut.exists():
                shortcut.unlink()
            if moved_existing and backup.exists():
                os.replace(backup, shortcut)
                moved_existing = False
        except OSError as rollback_error:
            raise StartupFailure("startup change failed and rollback was incomplete") from rollback_error
        raise
    finally:
        residues = (temporary,) if moved_existing else (temporary, backup)
        for residue in residues:
            try:
                residue.unlink()
            except FileNotFoundError:
                pass
