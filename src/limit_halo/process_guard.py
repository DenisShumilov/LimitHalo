from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path


def safe_child_environment() -> dict[str, str]:
    permitted = (
        "SystemRoot",
        "WINDIR",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
    )
    clean = {key: os.environ[key] for key in permitted if key in os.environ}
    clean["NoDefaultCurrentDirectoryInExePath"] = "1"
    return clean


def system_directory() -> str:
    if os.name != "nt":
        return str(Path.cwd().resolve())
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel32.GetSystemDirectoryW.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32_768)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise OSError(ctypes.get_last_error(), "GetSystemDirectoryW")
    path = Path(buffer.value).resolve(strict=True)
    if not path.is_dir():
        raise OSError("System32 unavailable")
    return str(path)


class WindowsJob:
    """Close-to-kill ownership for the exact permitted child process tree."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "SetInformationJobObject")
        if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(int(process._handle))):  # type: ignore[attr-defined]
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "AssignProcessToJobObject")
        self.handle = int(job)

    def close(self) -> None:
        if self.handle and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


def child_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_DEFAULT_ERROR_MODE
