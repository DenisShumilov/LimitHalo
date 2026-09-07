from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from limit_halo.ui import MODE_SPECS, point_in_rounded_rect


RUN_NATIVE = os.name == "nt" and os.environ.get("LIMIT_HALO_RUN_NATIVE_INPUT_ORACLE") == "1"
if RUN_NATIVE:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


class POINT(ctypes.Structure):
    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


class RECT(ctypes.Structure):
    _fields_ = (("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG))


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )


class INPUT_UNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT),)


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = (("type", wintypes.DWORD), ("value", INPUT_UNION))


class NativeWindowApi:
    GA_ROOT = 2
    LWA_ALPHA = 2
    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    MOUSEEVENTF_ABSOLUTE = 0x8000
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.WindowFromPhysicalPoint.argtypes = [POINT]
        self.user32.WindowFromPhysicalPoint.restype = wintypes.HWND
        self.user32.GetLayeredWindowAttributes.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetLayeredWindowAttributes.restype = wintypes.BOOL
        self.user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.user32.GetDpiForWindow.restype = wintypes.UINT
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int

    def windows_for_pid(self, pid: int) -> dict[str, int]:
        result: dict[str, int] = {}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def collect(hwnd: int, _parameter: int) -> bool:
            owner = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid or not self.user32.IsWindowVisible(hwnd):
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            text = ctypes.create_unicode_buffer(max(1, length + 1))
            self.user32.GetWindowTextW(hwnd, text, len(text))
            if text.value in {"LimitHalo", "LimitHalo HUD"}:
                result[text.value] = int(hwnd)
            return True

        if not self.user32.EnumWindows(collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        value = RECT()
        if not self.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.left, value.top, value.right, value.bottom

    def owner_pid_at(self, x: int, y: int) -> int:
        child = self.user32.WindowFromPhysicalPoint(POINT(x, y))
        if not child:
            return 0
        root = self.user32.GetAncestor(child, self.GA_ROOT) or child
        owner = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(root, ctypes.byref(owner))
        return int(owner.value)

    def alpha(self, hwnd: int) -> int:
        color = wintypes.DWORD()
        alpha = ctypes.c_ubyte()
        flags = wintypes.DWORD()
        if not self.user32.GetLayeredWindowAttributes(hwnd, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not flags.value & self.LWA_ALPHA:
            raise AssertionError("window has no constant-alpha attribute")
        return int(alpha.value)

    def dpi(self, hwnd: int) -> int:
        return int(self.user32.GetDpiForWindow(hwnd))

    def cursor(self) -> tuple[int, int]:
        point = POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        return point.x, point.y

    def _absolute(self, x: int, y: int) -> tuple[int, int]:
        left = self.user32.GetSystemMetrics(self.SM_XVIRTUALSCREEN)
        top = self.user32.GetSystemMetrics(self.SM_YVIRTUALSCREEN)
        width = self.user32.GetSystemMetrics(self.SM_CXVIRTUALSCREEN)
        height = self.user32.GetSystemMetrics(self.SM_CYVIRTUALSCREEN)
        if width <= 1 or height <= 1:
            raise RuntimeError("interactive virtual desktop unavailable")
        return (
            round((x - left) * 65_535 / (width - 1)),
            round((y - top) * 65_535 / (height - 1)),
        )

    def send_mouse(self, x: int, y: int, flags: int) -> None:
        absolute_x, absolute_y = self._absolute(x, y)
        value = INPUT(
            type=self.INPUT_MOUSE,
            value=INPUT_UNION(
                mi=MOUSEINPUT(
                    absolute_x,
                    absolute_y,
                    0,
                    flags | self.MOUSEEVENTF_ABSOLUTE | self.MOUSEEVENTF_VIRTUALDESK,
                    0,
                    None,
                )
            ),
        )
        if self.user32.SendInput(1, ctypes.byref(value), ctypes.sizeof(INPUT)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        if flags & self.MOUSEEVENTF_MOVE:
            time.sleep(0.02)
            actual = self.cursor()
            if abs(actual[0] - x) > 1 or abs(actual[1] - y) > 1:
                raise AssertionError(f"SendInput cursor mismatch: expected {(x, y)}, observed {actual}")

    def drag(self, start: tuple[int, int], delta: tuple[int, int]) -> None:
        self.send_mouse(*start, self.MOUSEEVENTF_MOVE)
        time.sleep(0.08)
        self.send_mouse(*start, self.MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.08)
        destination = start[0] + delta[0], start[1] + delta[1]
        self.send_mouse(*destination, self.MOUSEEVENTF_MOVE)
        time.sleep(0.12)
        self.send_mouse(*destination, self.MOUSEEVENTF_LEFTUP)
        time.sleep(0.15)


# This critical oracle is intentionally absent from ordinary/headless test
# discovery.  Release certification invokes it explicitly on an unlocked
# Windows desktop; an unavailable desktop is NOT_VERIFIED rather than a skip.
class NativeDragHitRoutingTests(unittest.TestCase if RUN_NATIVE else object):
    def setUp(self) -> None:
        self.api = NativeWindowApi()
        self.original_cursor = self.api.cursor()

    def tearDown(self) -> None:
        self.api.user32.SetCursorPos(*self.original_cursor)

    def command(self, state: Path, config: Path, mode: str) -> list[str]:
        executable = os.environ.get("LIMIT_HALO_NATIVE_ORACLE_EXE")
        prefix = [str(Path(executable).resolve())] if executable else [sys.executable, "-m", "limit_halo"]
        return [
            *prefix,
            "--demo-fixture",
            f"--authorized-state-root={state}",
            f"--config-root={config}",
            "--exit-after-ms=30000",
            "--locale=en-US",
            f"--mode={mode}",
            "--state=ready",
            "--x=80",
            "--y=180",
        ]

    def wait_for_windows(self, pid: int) -> dict[str, int]:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            values = self.api.windows_for_pid(pid)
            if set(values) == {"LimitHalo", "LimitHalo HUD"}:
                return values
            time.sleep(0.05)
        self.fail("fixture did not expose its two aligned HUD windows")

    def test_every_rounded_contour_routes_and_blank_pixels_drag(self) -> None:
        for mode, spec in MODE_SPECS.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
                state = Path(temporary) / "state"
                config = state / "config"
                config.mkdir(parents=True)
                environment = dict(os.environ)
                if not environment.get("LIMIT_HALO_NATIVE_ORACLE_EXE"):
                    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
                process = subprocess.Popen(
                    self.command(state, config, mode),
                    cwd=ROOT,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    windows = self.wait_for_windows(process.pid)
                    rear, foreground = windows["LimitHalo"], windows["LimitHalo HUD"]
                    rear_rect = self.api.rect(rear)
                    foreground_rect = self.api.rect(foreground)
                    self.assertEqual(rear_rect, foreground_rect)
                    self.assertEqual(self.api.alpha(rear), 1)
                    dpi = self.api.dpi(rear)
                    scale = dpi / 96.0
                    observed_size = rear_rect[2] - rear_rect[0], rear_rect[3] - rear_rect[1]
                    expected_size = round(spec.size[0] * scale), round(spec.size[1] * scale)
                    self.assertEqual(observed_size, expected_size)

                    scaled_rects = tuple(
                        tuple(round(value * scale) for value in rect)
                        for rect in spec.hit_rects
                    )
                    radius = round(16 * scale)
                    lattice_points = 0
                    for x in range(0, observed_size[0], 4):
                        for y in range(0, observed_size[1], 4):
                            if not any(point_in_rounded_rect(x, y, rect, radius) for rect in scaled_rects):
                                continue
                            lattice_points += 1
                            self.assertEqual(
                                self.api.owner_pid_at(rear_rect[0] + x, rear_rect[1] + y),
                                process.pid,
                                (mode, x, y),
                            )
                    self.assertGreater(lattice_points, 100)
                    for left, top, right, _bottom in scaled_rects:
                        blank = (rear_rect[0] + (left + right) // 2, rear_rect[1] + top + max(2, round(scale)))
                        self.assertEqual(self.api.owner_pid_at(*blank), process.pid, (mode, "blank", blank))
                    for outside in ((rear_rect[0] + observed_size[0] // 2, rear_rect[1] + 4), (rear_rect[0] + 1, rear_rect[1] + 1)):
                        self.assertNotEqual(self.api.owner_pid_at(*outside), process.pid, (mode, "outside", outside))

                    first = scaled_rects[0]
                    blank = (rear_rect[0] + (first[0] + first[2]) // 2, rear_rect[1] + first[1] + max(2, round(scale)))
                    threshold_pixels = max(1, int(5 * dpi / 96))
                    self.api.drag(blank, (threshold_pixels, 0))
                    self.assertEqual(self.api.rect(rear), rear_rect, (mode, "threshold"))

                    drag_pixels = round(24 * dpi / 96)
                    self.api.drag(blank, (drag_pixels, 0))
                    moved_rear = self.api.rect(rear)
                    moved_foreground = self.api.rect(foreground)
                    self.assertEqual(moved_rear, moved_foreground)
                    self.assertLessEqual(abs((moved_rear[0] - rear_rect[0]) - drag_pixels), 2)
                    self.assertLessEqual(abs(moved_rear[1] - rear_rect[1]), 2)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
