from __future__ import annotations

import ctypes
import math
import os
import tkinter as tk
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PIL import Image, ImageTk

from . import APP_NAME, __version__
from .config import AppConfig, ConfigStore
from .diagnostics import diagnostics_json, safe_diagnostics
from .localization import assert_translation_parity, tr
from .model import (
    CLAUDE_KEYS,
    Metric,
    MetricStore,
    ProviderState,
    LaneVisibility,
    resolve_provider_visibility,
    select_visible_hud_metrics,
)
from .native_menu import NativeMenu, build_context_menu
from .notifications import NotificationDecider, WindowsBalloonNotifier, notification_text


TRANSPARENT_KEY = "#010203"
REAR_BACKGROUND = "#252b34"
BORDER_COLOR = "#667383"
TEXT = "#ffffff"
MUTED = "#b8b8b8"
STATUS_AMBER = "#f6c85f"
STATUS_RED = "#ff6b6b"
STATUS_GREY = "#aeb6c2"
ACTION_BLUE = "#60a5fa"
# A fully transparent layered window is also transparent to Win32 hit testing.
# One alpha step is visually imperceptible (at most one RGB level) but keeps the
# rear input surface eligible for pointer routing on blank HUD pixels.
REAR_ALPHA_BYTE = 1
FOREGROUND_ALPHA_BYTE = 255
BASE_ICON_SIZE = 28
BASE_VALUE_FONT = 17
BASE_RESET_FONT = 12
BASE_VALUE_Y = 31
BASE_RESET_Y = 56
BASE_LANE_GAP = 8
BASE_ACTION_PADDING = 3
BASE_CORNER_DIAMETER = 36
DRAG_THRESHOLD_DIP = 5.0
VALUE_FONT_FAMILY = "Segoe UI Semibold"
RESET_FONT_FAMILY = "Segoe UI"
PROVIDER_ICON_NAMES = {"codex": "chatgpt.png", "claude": "claude.png"}


@dataclass(frozen=True)
class ModeSpec:
    name: str
    size: tuple[int, int]
    icon_anchors: tuple[tuple[str, int, int], ...]
    text_anchors: tuple[int, ...]
    hit_rects: tuple[tuple[int, int, int, int], ...]


MODE_SPECS: dict[str, ModeSpec] = {
    "both-compact": ModeSpec(
        "both-compact", (364, 84), (("codex", 20, 42), ("claude", 124, 42)),
        (41, 145, 217, 289), ((2, 12, 120, 72), (106, 12, 362, 72)),
    ),
    "both-wide": ModeSpec(
        "both-wide", (436, 84), (("codex", 20, 42), ("claude", 196, 42)),
        (41, 113, 217, 289, 361), ((2, 12, 192, 72), (178, 12, 434, 72)),
    ),
    "claude-only": ModeSpec(
        "claude-only", (260, 84), (("claude", 20, 42),),
        (41, 113, 185), ((2, 12, 258, 72),),
    ),
    "codex-one-window": ModeSpec(
        "codex-one-window", (124, 84), (("codex", 20, 42),),
        (41,), ((2, 12, 122, 72),),
    ),
    "codex-two-window": ModeSpec(
        "codex-two-window", (196, 84), (("codex", 20, 42),),
        (41, 113), ((2, 12, 194, 72),),
    ),
}


@dataclass(frozen=True)
class PhysicalLayout:
    mode: str
    scale: float
    width: int
    height: int
    logical_width: int
    logical_height: int
    icon_size: int
    value_font_size: int
    reset_font_size: int
    value_y: int
    reset_y: int
    icon_anchors: tuple[tuple[str, int, int], ...]
    text_anchors: tuple[int, ...]
    hit_rects: tuple[tuple[int, int, int, int], ...]


@dataclass(frozen=True)
class NativeClampResult:
    x: int
    y: int
    work_area: tuple[int, int, int, int]
    moved: bool
    fully_contained: bool


def layout_mode(
    preset: str,
    *,
    codex_visible: bool,
    claude_visible: bool,
    codex_window_count: int,
) -> str | None:
    if preset not in {"compact", "wide"}:
        raise ValueError("unsupported preset")
    if codex_window_count not in {0, 1, 2}:
        raise ValueError("unsupported Codex window count")
    if codex_visible and claude_visible:
        return "both-wide" if preset == "wide" and codex_window_count == 2 else "both-compact"
    if claude_visible:
        return "claude-only"
    if codex_visible:
        return "codex-two-window" if preset == "wide" and codex_window_count == 2 else "codex-one-window"
    return None


def compute_layout(mode_or_preset: str, scale: float, *, codex_visible: bool = True, claude_visible: bool = True, codex_window_count: int = 2) -> PhysicalLayout:
    if not 1.0 <= scale <= 3.0:
        raise ValueError("unsupported scale")
    mode = mode_or_preset if mode_or_preset in MODE_SPECS else layout_mode(
        mode_or_preset,
        codex_visible=codex_visible,
        claude_visible=claude_visible,
        codex_window_count=codex_window_count,
    )
    if mode is None or mode not in MODE_SPECS:
        raise ValueError("no visible layout")
    spec = MODE_SPECS[mode]

    def scaled(value: int) -> int:
        return round(value * scale)

    return PhysicalLayout(
        mode=mode,
        scale=scale,
        width=scaled(spec.size[0]),
        height=scaled(spec.size[1]),
        logical_width=spec.size[0],
        logical_height=spec.size[1],
        icon_size=max(26, scaled(BASE_ICON_SIZE)),
        value_font_size=max(15, scaled(BASE_VALUE_FONT)),
        reset_font_size=max(11, scaled(BASE_RESET_FONT)),
        value_y=scaled(BASE_VALUE_Y),
        reset_y=scaled(BASE_RESET_Y),
        icon_anchors=tuple((provider, scaled(x), scaled(y)) for provider, x, y in spec.icon_anchors),
        text_anchors=tuple(scaled(value) for value in spec.text_anchors),
        hit_rects=tuple(tuple(scaled(value) for value in rect) for rect in spec.hit_rects),
    )


def point_in_rounded_rect(x: int, y: int, rect: Sequence[int], radius: int) -> bool:
    left, top, right, bottom = rect
    if x < left or x > right or y < top or y > bottom:
        return False
    radius = max(0, min(radius, (right - left) // 2, (bottom - top) // 2))
    inner_left, inner_right = left + radius, right - radius
    inner_top, inner_bottom = top + radius, bottom - radius
    if inner_left <= x <= inner_right or inner_top <= y <= inner_bottom:
        return True
    center_x = inner_left if x < inner_left else inner_right
    center_y = inner_top if y < inner_top else inner_bottom
    return (x - center_x) ** 2 + (y - center_y) ** 2 <= radius**2


def hit_contour_contains(layout: PhysicalLayout, x: int, y: int) -> bool:
    return any(point_in_rounded_rect(x, y, rect, round(16 * layout.scale)) for rect in layout.hit_rects)


def action_lane_right(layout: PhysicalLayout, index: int) -> int:
    """Return the exclusive visual boundary for one metric/action lane."""

    if not 0 <= index < len(layout.text_anchors):
        raise ValueError("metric lane index out of range")
    if index + 1 < len(layout.text_anchors):
        return layout.text_anchors[index + 1] - round(BASE_LANE_GAP * layout.scale)
    return layout.width - round(BASE_LANE_GAP * layout.scale)


def place_recovery_action(
    canvas: tk.Canvas,
    status_item: int,
    action_item: int,
    *,
    x: int,
    reset_y: int,
    scale: float,
    lane_right: int,
    canvas_height: int,
) -> tuple[tuple[int, int, int, int], bool]:
    """Keep the recovery action visible without resizing the original HUD."""

    padding = max(1, round(BASE_ACTION_PADDING * scale))
    status_box = canvas.bbox(status_item)
    if status_box is None:
        raise ValueError("recovery status has no bounds")
    canvas.coords(action_item, status_box[2] + padding, reset_y)
    action_box = canvas.bbox(action_item)
    if action_box is None:
        raise ValueError("recovery action has no bounds")

    action_only = action_box[2] + padding > lane_right
    if action_only:
        canvas.itemconfigure(status_item, state="hidden")
        canvas.coords(action_item, x, reset_y)
        action_box = canvas.bbox(action_item)
        if action_box is None:
            raise ValueError("recovery action has no bounds")

    if (
        (not action_only and (
            status_box[0] < 0
            or status_box[2] > lane_right
            or status_box[1] < 0
            or status_box[3] > canvas_height
        ))
        or action_box[0] < 0
        or action_box[2] > lane_right
        or action_box[1] < 0
        or action_box[3] > canvas_height
    ):
        raise ValueError("recovery text does not fit its lane")

    expanded = (
        max(0, action_box[0] - padding),
        max(0, action_box[1] - padding),
        min(lane_right, action_box[2] + padding),
        min(canvas_height, action_box[3] + padding),
    )
    if expanded[0] >= expanded[2] or expanded[1] >= expanded[3]:
        raise ValueError("recovery action hit target is empty")
    return expanded, action_only


def drag_distance_dip(dx: int, dy: int, dpi: int) -> float:
    if dpi <= 0:
        raise ValueError("invalid DPI")
    return math.hypot(dx, dy) * 96.0 / dpi


def recover_position(
    x: int,
    y: int,
    width: int,
    height: int,
    work_areas: Iterable[tuple[int, int, int, int]],
    *,
    scale: float,
) -> tuple[int, int]:
    areas = tuple(work_areas)
    if not areas:
        return x, y
    minimum_width, minimum_height = round(48 * scale), round(24 * scale)
    for left, top, right, bottom in areas:
        visible_width = max(0, min(x + width, right) - max(x, left))
        visible_height = max(0, min(y + height, bottom) - max(y, top))
        if visible_width >= minimum_width and visible_height >= minimum_height:
            return x, y
    left, top, right, _bottom = areas[0]
    margin = round(24 * scale)
    return max(left, right - width - margin), top + margin


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def screen_pointer_position(root: tk.Misc, *, user32: object | None = None) -> tuple[int, int]:
    """Return physical screen coordinates, with Tk as a safe fallback."""
    if user32 is None and os.name == "nt":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except (AttributeError, OSError):
            user32 = None
    if user32 is not None:
        try:
            get_cursor_pos = user32.GetCursorPos
            get_cursor_pos.argtypes = [ctypes.POINTER(_POINT)]
            get_cursor_pos.restype = ctypes.c_int
            point = _POINT()
            if get_cursor_pos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    return int(root.winfo_pointerx()), int(root.winfo_pointery())


def attention_position(
    pointer_x: int,
    pointer_y: int,
    width: int,
    height: int,
    work_areas: Iterable[tuple[int, int, int, int]],
    *,
    scale: float,
) -> tuple[int, int]:
    areas = tuple(work_areas)
    if not areas:
        return pointer_x, pointer_y
    area = next(
        (
            candidate
            for candidate in areas
            if candidate[0] <= pointer_x < candidate[2] and candidate[1] <= pointer_y < candidate[3]
        ),
        areas[0],
    )
    left, top, right, bottom = area
    margin = round(24 * scale)
    preferred_x = pointer_x + margin
    if preferred_x + width > right - margin:
        preferred_x = pointer_x - width - margin
    minimum_x = left + margin
    maximum_x = max(minimum_x, right - width - margin)
    minimum_y = top + margin
    maximum_y = max(minimum_y, bottom - height - margin)
    return (
        min(max(preferred_x, minimum_x), maximum_x),
        min(max(pointer_y - height // 2, minimum_y), maximum_y),
    )


def geometry_string(width: int, height: int, x: int, y: int) -> str:
    return f"{width}x{height}{x:+d}{y:+d}"


def clamp_origin_to_work_area(
    x: int,
    y: int,
    width: int,
    height: int,
    work_area: tuple[int, int, int, int],
) -> tuple[int, int, bool]:
    """Clamp a physical window origin without producing inverted coordinates.

    An oversized synthetic window cannot be fully contained.  In that case it
    is aligned to the work area's top-left corner so the maximum useful portion
    remains visible and the result explicitly reports that containment was
    impossible.  Every production HUD layout is smaller than a supported
    monitor work area.
    """

    left, top, right, bottom = work_area
    work_width = right - left
    work_height = bottom - top
    if width <= 0 or height <= 0 or work_width <= 0 or work_height <= 0:
        raise ValueError("invalid native window or work-area rectangle")

    if width <= work_width:
        x = min(max(x, left), right - width)
    else:
        x = left
    if height <= work_height:
        y = min(max(y, top), bottom - height)
    else:
        y = top
    return x, y, width <= work_width and height <= work_height


class _NATIVE_RECT(ctypes.Structure):
    _fields_ = [(name, ctypes.c_long) for name in ("left", "top", "right", "bottom")]


class _NATIVE_MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _NATIVE_RECT),
        ("rcWork", _NATIVE_RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


def _native_pointer_value(value: object) -> int:
    if isinstance(value, ctypes.c_void_p):
        return int(value.value or 0)
    return int(value or 0)


def native_move_aligned_windows(
    rear_hwnd: int,
    foreground_hwnd: int,
    x: int,
    y: int,
    *,
    user32: object | None = None,
) -> bool:
    """Move both HUD HWNDs to one exact physical origin and verify the move."""

    if (
        not rear_hwnd
        or not foreground_hwnd
        or isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, int)
        or not isinstance(y, int)
        or not -(2**31) <= x < 2**31
        or not -(2**31) <= y < 2**31
    ):
        return False
    if user32 is None:
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except (AttributeError, OSError):
            return False
    try:
        set_window_pos = user32.SetWindowPos
        get_window_rect = user32.GetWindowRect
        set_window_pos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        set_window_pos.restype = ctypes.c_int
        get_window_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NATIVE_RECT)]
        get_window_rect.restype = ctypes.c_int
        flags = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE
        for hwnd in (rear_hwnd, foreground_hwnd):
            if not set_window_pos(
                ctypes.c_void_p(hwnd), ctypes.c_void_p(), x, y, 0, 0, flags
            ):
                return False
        for hwnd in (rear_hwnd, foreground_hwnd):
            rect = _NATIVE_RECT()
            if not get_window_rect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                return False
            if int(rect.left) != x or int(rect.top) != y:
                return False
        return True
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return False


def native_aligned_window_origin(
    rear_hwnd: int,
    foreground_hwnd: int,
    *,
    user32: object | None = None,
) -> tuple[int, int] | None:
    """Read one verified physical origin for the aligned HUD pair."""

    if not rear_hwnd or not foreground_hwnd:
        return None
    if user32 is None:
        if os.name != "nt":
            return None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except (AttributeError, OSError):
            return None
    try:
        get_window_rect = user32.GetWindowRect
        get_window_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NATIVE_RECT)]
        get_window_rect.restype = ctypes.c_int
        origins: list[tuple[int, int]] = []
        for hwnd in (rear_hwnd, foreground_hwnd):
            rect = _NATIVE_RECT()
            if not get_window_rect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                return None
            if rect.right <= rect.left or rect.bottom <= rect.top:
                return None
            origins.append((int(rect.left), int(rect.top)))
        return origins[0] if origins[0] == origins[1] else None
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def native_window_dpi(hwnd: int, *, user32: object | None = None) -> int | None:
    """Return a bounded native DPI sample for a real top-level HWND."""

    if not hwnd:
        return None
    if user32 is None:
        if os.name != "nt":
            return None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except (AttributeError, OSError):
            return None
    try:
        get_dpi = user32.GetDpiForWindow
        get_dpi.argtypes = [ctypes.c_void_p]
        get_dpi.restype = ctypes.c_uint32
        dpi = int(get_dpi(ctypes.c_void_p(hwnd)))
        return dpi if 96 <= dpi <= 288 else None
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def native_post_placement_clamp(
    rear_hwnd: int,
    foreground_hwnd: int,
    *,
    user32: object | None = None,
) -> NativeClampResult | None:
    """Align and contain both physical HUD windows in their native work area."""

    if not rear_hwnd or not foreground_hwnd:
        return None
    if user32 is None:
        if os.name != "nt":
            return None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except (AttributeError, OSError):
            return None

    try:
        get_window_rect = user32.GetWindowRect
        monitor_from_window = user32.MonitorFromWindow
        get_monitor_info = user32.GetMonitorInfoW
        set_window_pos = user32.SetWindowPos
        get_window_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NATIVE_RECT)]
        get_window_rect.restype = ctypes.c_int
        monitor_from_window.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        monitor_from_window.restype = ctypes.c_void_p
        get_monitor_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NATIVE_MONITORINFO)]
        get_monitor_info.restype = ctypes.c_int
        set_window_pos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        set_window_pos.restype = ctypes.c_int

        def read_rect(hwnd: int) -> tuple[int, int, int, int] | None:
            value = _NATIVE_RECT()
            if not get_window_rect(ctypes.c_void_p(hwnd), ctypes.byref(value)):
                return None
            rect = (int(value.left), int(value.top), int(value.right), int(value.bottom))
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                return None
            return rect

        rear_rect = read_rect(rear_hwnd)
        foreground_rect = read_rect(foreground_hwnd)
        if rear_rect is None or foreground_rect is None:
            return None

        # The foreground contains the visible pixels, so its nearest native
        # monitor is authoritative if an old bug left the pair misaligned.
        monitor = monitor_from_window(ctypes.c_void_p(foreground_hwnd), 0x00000002)
        monitor_value = _native_pointer_value(monitor)
        if not monitor_value:
            return None
        info = _NATIVE_MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not get_monitor_info(ctypes.c_void_p(monitor_value), ctypes.byref(info)):
            return None
        work_area = (
            int(info.rcWork.left),
            int(info.rcWork.top),
            int(info.rcWork.right),
            int(info.rcWork.bottom),
        )

        width = max(rear_rect[2] - rear_rect[0], foreground_rect[2] - foreground_rect[0])
        height = max(rear_rect[3] - rear_rect[1], foreground_rect[3] - foreground_rect[1])
        x, y, fits = clamp_origin_to_work_area(
            foreground_rect[0], foreground_rect[1], width, height, work_area
        )
        moved = any(
            rect[0] != x or rect[1] != y
            for rect in (rear_rect, foreground_rect)
        )
        if moved:
            flags = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE
            rear_moved = bool(
                set_window_pos(
                    ctypes.c_void_p(rear_hwnd), ctypes.c_void_p(), x, y, 0, 0, flags
                )
            )
            foreground_moved = bool(
                set_window_pos(
                    ctypes.c_void_p(foreground_hwnd), ctypes.c_void_p(), x, y, 0, 0, flags
                )
            )
            if not rear_moved or not foreground_moved:
                return None

            # Measure the actual post-call rectangles instead of treating a
            # successful API return as proof of placement.
            rear_rect = read_rect(rear_hwnd)
            foreground_rect = read_rect(foreground_hwnd)
            if rear_rect is None or foreground_rect is None:
                return None
            if any(rect[0] != x or rect[1] != y for rect in (rear_rect, foreground_rect)):
                return None

        fully_contained = all(
            work_area[0] <= rect[0]
            and work_area[1] <= rect[1]
            and rect[2] <= work_area[2]
            and rect[3] <= work_area[3]
            for rect in (rear_rect, foreground_rect)
        )
        if fits and not fully_contained:
            return None
        return NativeClampResult(x, y, work_area, moved, fully_contained)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def _dpi_scale(root: tk.Misc) -> float:
    if os.name != "nt":
        return 1.0
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(root.winfo_id()))
        return min(2.0, max(1.0, dpi / 96.0))
    except (AttributeError, OSError):
        return 1.0


def _asset(name: str) -> Path:
    return Path(__file__).resolve().parent / "assets" / name


def _remaining_percent(metric: Metric) -> int | None:
    return None if metric.used_percent is None else 100 - metric.used_percent


def _duration_suffix(metric: Metric, language: str) -> str:
    if metric.key == "claude_fable_10080":
        return "·F"
    minutes = metric.window_duration_mins
    if minutes % 10_080 == 0:
        return f"·{minutes // 1_440}{'д' if language == 'uk' else 'd'}"
    if minutes % 60 == 0:
        return f"·{minutes // 60}{'г' if language == 'uk' else 'h'}"
    return f"·{minutes}{'хв' if language == 'uk' else 'm'}"


def _format_reset(metric: Metric, language: str, now: datetime | None = None) -> str:
    if metric.resets_at is None:
        return tr(language, "window_not_provided_short")
    current = datetime.now().astimezone() if now is None else now.astimezone()
    reset = datetime.fromtimestamp(metric.resets_at).astimezone()
    if metric.provider == "claude" and metric.key == "claude_300":
        seconds = max(0, int((reset - current).total_seconds()))
        hours, remainder = divmod(seconds, 3_600)
        minutes = remainder // 60
        return f"{hours}{'г' if language == 'uk' else 'h'}{minutes:02d}{'хв' if language == 'uk' else 'm'}"
    return reset.strftime("%d.%m·%H:%M")


def metric_visible_text(metric: Metric, language: str, now: datetime | None = None) -> tuple[str, str]:
    remaining = _remaining_percent(metric)
    value = "—" if remaining is None else f"{remaining}%{_duration_suffix(metric, language)}"
    if metric.state is ProviderState.REFRESHING:
        subline = tr(language, "refreshing")
    elif metric.state is ProviderState.STALE:
        subline = tr(language, "last_updated")
    elif metric.state is ProviderState.OFFLINE:
        subline = tr(language, "offline_short")
    elif metric.state is ProviderState.AUTHORIZATION_REQUIRED:
        subline = tr(language, "authorization_short")
    elif metric.state is ProviderState.PROVIDER_ERROR:
        subline = tr(language, "provider_error_short")
    elif metric.state is ProviderState.UNAVAILABLE:
        subline = tr(language, "window_not_provided_short")
    elif metric.state is ProviderState.DISABLED:
        subline = tr(language, "claude_disabled")
    elif metric.state is ProviderState.READY:
        subline = _format_reset(metric, language, now)
    else:  # Metric validates this enum; retain an exhaustive defensive guard.
        raise ValueError("unsupported provider state")
    return value, subline


def metric_color(metric: Metric, color_mode: str) -> str:
    if metric.state not in {ProviderState.READY, ProviderState.REFRESHING}:
        return STATUS_GREY
    if color_mode == "monochrome":
        return TEXT
    if color_mode != "status":
        raise ValueError("invalid color mode")
    remaining = _remaining_percent(metric)
    if remaining is None:
        return STATUS_GREY
    if remaining <= 5:
        return STATUS_RED
    if remaining <= 20:
        return STATUS_AMBER
    return TEXT


def diagnostics_status_explanations(language: str) -> tuple[str, str]:
    return tr(language, "authorization_required"), tr(language, "offline")


def _placeholder_metrics(provider: str, state: ProviderState, now: float) -> tuple[Metric, ...]:
    if provider == "codex":
        return (Metric("codex_10080", "codex", 10_080, None, None, state, now),)
    return tuple(
        Metric(key, "claude", 300 if key == "claude_300" else 10_080, None, None, state, now)
        for key in CLAUDE_KEYS
    )


def merge_settings_changes(current: AppConfig, opened: AppConfig, edited: AppConfig) -> AppConfig:
    """Apply only the user's edits, never an old dialog's position or metadata."""

    protected = {"schemaVersion", "x", "y", "_preserved_unknown_json"}
    changes = {
        field.name: getattr(edited, field.name)
        for field in fields(AppConfig)
        if field.name not in protected
        and getattr(edited, field.name) != getattr(opened, field.name)
    }
    return replace(current, **changes)


class WidgetWindow:
    def __init__(
        self,
        root: tk.Tk,
        store: MetricStore,
        config_store: ConfigStore,
        *,
        on_refresh: Callable[[str], None],
        on_config_changed: Callable[[AppConfig, AppConfig], None],
        on_exit: Callable[[], None],
        on_sign_in: Callable[[str], None] | None = None,
        provider_present: Callable[[str], bool] | None = None,
        provider_detected: Callable[[str], bool] | None = None,
        startup_active: Callable[[], bool] | None = None,
        on_check_updates: Callable[[], None] | None = None,
        update_menu_state: Callable[[], tuple[str, bool]] | None = None,
        scale_override: float | None = None,
        portable: bool = False,
    ) -> None:
        assert_translation_parity()
        self.root = root
        self.store = store
        self.config_store = config_store
        self.on_refresh = on_refresh
        self.on_config_changed = on_config_changed
        self.on_exit = on_exit
        self.on_sign_in = on_sign_in or (lambda _provider: None)
        self.provider_present = provider_present or (lambda provider: store.ever_succeeded(provider))
        self.provider_detected = provider_detected or self.provider_present
        self.startup_active = startup_active or (lambda: self.config.startWithWindows)
        self.on_check_updates = on_check_updates or (lambda: None)
        self.update_menu_state = update_menu_state or (lambda: (tr(self.config.language, "check_updates"), True))
        self.portable = portable
        self.config = config_store.load()
        self._closing = False
        self._image_refs: list[ImageTk.PhotoImage] = []
        self._icon_cache: dict[tuple[str, int], ImageTk.PhotoImage] = {}
        self._scene_items: dict[str, int] = {}
        self._scene_options: dict[str, dict[str, object]] = {}
        self._active_scene_keys: set[str] = set()
        self._rear_item: int | None = None
        self._shown = False
        self._applied_window_size: tuple[int, int] | None = None
        self._region_signature: tuple[object, ...] | None = None
        self._drag_origin: tuple[int, int] | None = None
        self._drag_window_origin: tuple[int, int] | None = None
        self._dragging = False
        self._armed_action: str | None = None
        self._action_boxes: dict[str, tuple[int, int, int, int]] = {}
        self._action_order: list[str] = []
        self._focused_action = -1
        self._notification_decider = NotificationDecider()
        self._notifier: WindowsBalloonNotifier | None = None
        self.rounded_region_applied = False
        self._attention_generation = 0
        self._attention_topmost = False

        root.withdraw()
        root.title(APP_NAME)
        root.configure(bg=REAR_BACKGROUND)
        root.overrideredirect(True)
        self.scale = scale_override if scale_override is not None else _dpi_scale(root)
        self.scale = min(2.0, max(1.0, round(self.scale * 4) / 4))
        self.dpi = round(self.scale * 96)
        self.layout = self._current_layout()
        root.attributes("-topmost", self.config.topmost)
        root.attributes("-alpha", 0.0)
        root.resizable(False, False)
        self.rear = tk.Canvas(root, bg=REAR_BACKGROUND, bd=0, highlightthickness=0, cursor="fleur")
        self.rear.pack(fill="both", expand=True)

        self.foreground = tk.Toplevel(root)
        self.foreground.withdraw()
        self.foreground.title(f"{APP_NAME} HUD")
        self.foreground.configure(bg=TRANSPARENT_KEY)
        self.foreground.overrideredirect(True)
        self.foreground.attributes("-topmost", self.config.topmost)
        self.foreground.attributes("-alpha", 1.0)
        if os.name == "nt":
            self.foreground.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.foreground.resizable(False, False)
        self.canvas = tk.Canvas(
            self.foreground,
            bg=TRANSPARENT_KEY,
            bd=0,
            highlightthickness=0,
            cursor="fleur",
            takefocus=True,
        )
        self.canvas.pack(fill="both", expand=True)
        self._bind_actions()
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self.foreground.protocol("WM_DELETE_WINDOW", lambda: None)
        initial_placement = self._apply_geometry(
            reset=self.config.x is None or self.config.y is None
        )
        if os.name == "nt" and initial_placement is None:
            raise RuntimeError("HUD native placement could not be verified")
        self.root.deiconify()
        self.foreground.deiconify()
        self.root.update_idletasks()
        self.foreground.update_idletasks()
        # Re-measure after the two native windows become visible.  Tk's
        # pre-deiconify screen fallback can include the taskbar on some mixed
        # DPI desktops; the native monitor work area is the final authority.
        visible_placement = self._repair_and_persist_native_position()
        if os.name == "nt" and visible_placement is None:
            self.root.withdraw()
            self.foreground.withdraw()
            raise RuntimeError("HUD visible placement could not be verified")
        self._apply_layered_attributes()
        self._shown = True
        self._apply_input_regions()
        self._sync_z_order()
        self._notifier = WindowsBalloonNotifier(self._hwnd(self.foreground), self.root.after)
        self._redraw()
        self._tick()

    def _provider_visible(self, provider: str) -> bool:
        return self._provider_visible_for_config(provider, self.config)

    def _provider_visible_for_config(
        self, provider: str, config: AppConfig
    ) -> bool:
        mode = config.codexMode if provider == "codex" else config.claudeMode
        resolution = resolve_provider_visibility(
            mode,
            trusted_client_present=self.provider_present(provider),
            ever_succeeded=self.store.ever_succeeded(provider),
        )
        return resolution.lane is LaneVisibility.VISIBLE

    def _current_metrics(self) -> tuple[tuple[Metric, ...], bool, bool]:
        codex, claude = self.store.snapshot()
        codex_visible = self._provider_visible("codex")
        claude_visible = self._provider_visible("claude")
        now = __import__("time").time()
        if codex_visible and not codex:
            state = (
                ProviderState.REFRESHING
                if self.provider_present("codex") and not self.store.ever_succeeded("codex")
                else ProviderState.UNAVAILABLE
            )
            codex = _placeholder_metrics("codex", state, now)
        if (
            claude_visible
            and self.provider_present("claude")
            and not self.store.ever_succeeded("claude")
            and claude
            and all(metric.state is ProviderState.UNAVAILABLE for metric in claude)
        ):
            claude = _placeholder_metrics("claude", ProviderState.REFRESHING, now)
        if claude_visible and not claude:
            state = (
                ProviderState.REFRESHING
                if self.provider_present("claude") and not self.store.ever_succeeded("claude")
                else ProviderState.UNAVAILABLE
            )
            claude = _placeholder_metrics("claude", state, now)
        selected = select_visible_hud_metrics(
            self.config.displayPreset,
            codex,
            claude,
            codex_visible=codex_visible,
            claude_visible=claude_visible,
        )
        return selected, codex_visible, claude_visible

    def _current_layout(
        self,
        config: AppConfig | None = None,
        *,
        scale: float | None = None,
    ) -> PhysicalLayout:
        selected_config = self.config if config is None else config
        selected_scale = self.scale if scale is None else scale
        codex, _claude = self.store.snapshot()
        codex_visible = self._provider_visible_for_config("codex", selected_config)
        claude_visible = self._provider_visible_for_config("claude", selected_config)
        count = min(2, max(1 if codex_visible else 0, len(codex)))
        mode = layout_mode(
            selected_config.displayPreset,
            codex_visible=codex_visible,
            claude_visible=claude_visible,
            codex_window_count=count,
        )
        if mode is None:
            raise RuntimeError("HUD requires at least one visible provider")
        return compute_layout(mode, selected_scale)

    def _bind_actions(self) -> None:
        for widget in (self.rear, self.canvas):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", self._open_context_menu)
            widget.bind("<Motion>", self._update_cursor)
            widget.bind("<Shift-F10>", self._open_context_menu_keyboard)
            widget.bind("<KeyPress-Menu>", self._open_context_menu_keyboard)
            widget.bind("<Control-r>", self._manual_refresh)
        self.canvas.bind("<Tab>", self._focus_next_action)
        self.canvas.bind("<Shift-Tab>", self._focus_previous_action)
        self.canvas.bind("<Return>", self._invoke_focused_action)
        self.canvas.bind("<space>", self._invoke_focused_action)

    def _logical(self, value: int) -> int:
        return round(value * self.scale)

    def _work_areas(self) -> tuple[tuple[int, int, int, int], ...]:
        if os.name != "nt":
            return ((0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()),)
        areas: list[tuple[int, int, int, int]] = []

        class RECT(ctypes.Structure):
            _fields_ = [(name, ctypes.c_long) for name in ("left", "top", "right", "bottom")]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_uint32)]

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_long)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        @callback_type
        def collect(monitor: int, _dc: int, _rect: object, _data: int) -> int:
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
                work = info.rcWork
                areas.append((work.left, work.top, work.right, work.bottom))
            return 1

        user32.EnumDisplayMonitors(None, None, collect, 0)
        return tuple(areas) or ((0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()),)

    def _default_position(self) -> tuple[int, int]:
        area = self._work_areas()[0]
        return area[2] - self.layout.width - self._logical(24), area[1] + self._logical(24)

    def _apply_geometry(self, *, reset: bool = False) -> NativeClampResult | None:
        old_layout = getattr(self, "layout", None)
        target_layout = self._current_layout()
        self.layout = target_layout
        if reset:
            x, y = self._default_position()
        else:
            current_origin = self._native_pair_origin() if getattr(self, "_shown", False) else None
            if current_origin is not None:
                x = current_origin[0] if self.config.x is None else self.config.x
                y = current_origin[1] if self.config.y is None else self.config.y
            else:
                default_x, default_y = self._default_position()
                x = default_x if self.config.x is None else self.config.x
                y = default_y if self.config.y is None else self.config.y
                x, y = recover_position(x, y, self.layout.width, self.layout.height, self._work_areas(), scale=self.scale)
        placement = self._place_repair_and_persist(x, y)
        if placement is None:
            if old_layout is not None:
                self.layout = old_layout
                rollback_x = x if self.config.x is None else self.config.x
                rollback_y = y if self.config.y is None else self.config.y
                self._set_window_pair_position(rollback_x, rollback_y)
                self._repair_and_persist_native_position()
            return None
        self._apply_input_regions()
        self._sync_z_order()
        self._redraw()
        return placement

    def _set_window_pair_position(self, x: int, y: int) -> bool:
        if os.name == "nt":
            dimensions = (self.layout.width, self.layout.height)
            if getattr(self, "_applied_window_size", None) != dimensions:
                size = f"{dimensions[0]}x{dimensions[1]}"
                self.root.geometry(size)
                self.foreground.geometry(size)
                self.root.update_idletasks()
                self.foreground.update_idletasks()
                self._applied_window_size = dimensions
            if self._native_pair_origin() == (x, y):
                return True
            moved = native_move_aligned_windows(
                self._hwnd(self.root),
                self._hwnd(self.foreground),
                x,
                y,
            )
            if moved:
                self.root.update_idletasks()
                self.foreground.update_idletasks()
            return moved
        geometry = geometry_string(self.layout.width, self.layout.height, x, y)
        self.root.geometry(geometry)
        self.foreground.geometry(geometry)
        self.root.update_idletasks()
        self.foreground.update_idletasks()
        return True

    def _persist_native_position(self, x: int, y: int, *, remember: bool = False) -> bool:
        # x/y are the user's preferred position, not a temporary Windows
        # correction while monitors, DPI or the taskbar are still initializing.
        # Only first placement and a completed user drag may change them.
        if not remember and self.config.x is not None and self.config.y is not None:
            return True
        if self.config.x == x and self.config.y == y:
            return True
        new = replace(self.config, x=x, y=y)
        if getattr(self.config_store, "write_blocked", False) is True:
            if remember:
                return False
            # A malformed/unreadable existing file is never overwritten by
            # automatic placement. The visible fallback is session-only.
            self.config = new
            return True
        try:
            self.config_store.save(new)
        except (OSError, RuntimeError, ValueError):
            return False
        self.config = new
        return True

    def _repair_and_persist_native_position(self) -> NativeClampResult | None:
        if os.name != "nt":
            x, y = int(self.root.winfo_x()), int(self.root.winfo_y())
            if not self._persist_native_position(x, y):
                return None
            areas = self._work_areas()
            area = areas[0] if areas else (x, y, x + self.layout.width, y + self.layout.height)
            return NativeClampResult(x, y, area, False, True)
        result = self._native_post_placement_clamp()
        if result is None or not result.fully_contained:
            return None
        return result if self._persist_native_position(result.x, result.y) else None

    def _place_repair_and_persist(self, x: int, y: int) -> NativeClampResult | None:
        if not self._set_window_pair_position(x, y):
            return None
        return self._repair_and_persist_native_position()

    def _native_post_placement_clamp(self) -> NativeClampResult | None:
        if os.name != "nt":
            return None
        result = native_post_placement_clamp(
            self._hwnd(self.root),
            self._hwnd(self.foreground),
        )
        if result is not None and result.moved:
            self.root.update_idletasks()
            self.foreground.update_idletasks()
        return result

    def _native_pair_origin(self) -> tuple[int, int] | None:
        if os.name != "nt":
            return int(self.root.winfo_x()), int(self.root.winfo_y())
        return native_aligned_window_origin(
            self._hwnd(self.root), self._hwnd(self.foreground)
        )

    @staticmethod
    def _scale_from_dpi(dpi: int) -> float:
        return min(2.0, max(1.0, round((dpi / 96.0) * 4) / 4))

    def _restore_layout(
        self,
        layout: PhysicalLayout,
        scale: float,
        dpi: int,
        origin: tuple[int, int],
    ) -> NativeClampResult | None:
        self.layout = layout
        self.scale = scale
        self.dpi = dpi
        if not self._set_window_pair_position(*origin):
            return None
        result = self._native_post_placement_clamp()
        if result is None or not result.fully_contained:
            return None
        self._apply_input_regions()
        self._sync_z_order()
        self._redraw()
        return result

    def _refresh_display_state(self) -> bool:
        """Repair display drift and atomically adopt a changed native DPI."""

        if not self._reconcile_preferred_position():
            return False
        if os.name != "nt":
            return self._repair_and_persist_native_position() is not None
        current = self._repair_and_persist_native_position()
        if current is None:
            return False
        sampled_dpi = native_window_dpi(self._hwnd(self.foreground))
        if sampled_dpi is None:
            return True
        target_scale = self._scale_from_dpi(sampled_dpi)
        if target_scale == self.scale and sampled_dpi == self.dpi:
            return True

        old_layout, old_scale, old_dpi = self.layout, self.scale, self.dpi
        self.scale = target_scale
        self.dpi = sampled_dpi
        try:
            self.layout = self._current_layout(scale=target_scale)
        except (RuntimeError, ValueError):
            self._restore_layout(
                old_layout, old_scale, old_dpi, (current.x, current.y)
            )
            return False
        placement = self._place_repair_and_persist(
            current.x if self.config.x is None else self.config.x,
            current.y if self.config.y is None else self.config.y,
        )
        if placement is None:
            self._restore_layout(
                old_layout, old_scale, old_dpi, (current.x, current.y)
            )
            return False
        self._apply_input_regions()
        self._sync_z_order()
        self._redraw()
        return True

    def _reconcile_preferred_position(self) -> bool:
        """Restore user intent when a startup/display fallback is no longer needed."""
        areas = self._work_areas()
        signature = (areas, self.layout.width, self.layout.height, self.config.x, self.config.y)
        origin = self._native_pair_origin()
        if (signature == getattr(self, "_display_signature", None)
                and origin == getattr(self, "_display_origin", None) and origin is not None):
            return True
        x, y = self.config.x, self.config.y
        if x is None or y is None:
            x, y = self._default_position()
        if areas:
            # Find the nearest screen to the preferred point. A missing screen
            # gets a visible session fallback; its saved coordinates survive.
            area = min(areas, key=lambda a: (
                max(a[0] - x, 0, x - (a[2] - 1)) ** 2
                + max(a[1] - y, 0, y - (a[3] - 1)) ** 2
            ))
            x, y, _ = clamp_origin_to_work_area(x, y, self.layout.width, self.layout.height, area)
        placement = self._place_repair_and_persist(x, y)
        if placement is None:
            return False
        self._display_signature = signature
        self._display_origin = (placement.x, placement.y)
        return True

    @staticmethod
    def _hwnd(window: tk.Misc) -> int:
        window.update_idletasks()
        inner = int(window.winfo_id())
        if os.name != "nt":
            return inner
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        user32.GetAncestor.restype = ctypes.c_void_p
        outer = user32.GetAncestor(ctypes.c_void_p(inner), 2)
        return int(outer) if outer else inner

    def _actual_topmost(self) -> bool:
        if os.name != "nt":
            return self.config.topmost
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        style = user32.GetWindowLongPtrW(ctypes.c_void_p(self._hwnd(self.foreground)), -20)
        return bool(style & 0x00000008)

    def _sync_z_order(self) -> None:
        if os.name != "nt":
            self.foreground.lift(self.root)
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        flags = 0x0001 | 0x0002 | 0x0010
        foreground = ctypes.c_void_p(self._hwnd(self.foreground))
        rear = ctypes.c_void_p(self._hwnd(self.root))
        effective_topmost = self.config.topmost or self._attention_topmost
        user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        user32.GetWindow.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        front_topmost = bool(user32.GetWindowLongPtrW(foreground, -20) & 0x8)
        rear_topmost = bool(user32.GetWindowLongPtrW(rear, -20) & 0x8)
        if (
            front_topmost == effective_topmost
            and rear_topmost == effective_topmost
            and _native_pointer_value(user32.GetWindow(foreground, 2)) == rear.value
        ):
            return
        user32.SetWindowPos(foreground, ctypes.c_void_p(-1 if effective_topmost else -2), 0, 0, 0, 0, flags)
        if not effective_topmost:
            user32.SetWindowPos(foreground, ctypes.c_void_p(0), 0, 0, 0, 0, flags)
        user32.SetWindowPos(rear, foreground, 0, 0, 0, 0, flags)

    def activate_from_shortcut(self) -> None:
        if self._closing:
            return
        # Launch/upgrade activation reveals the chosen location. Only an
        # explicit placement action is allowed to relocate a healthy HUD.
        native_placement = self._repair_and_persist_native_position()
        if os.name == "nt" and native_placement is None:
            return
        if self.root.state() != "normal" or self.foreground.state() != "normal":
            self.root.deiconify()
            self.foreground.deiconify()
            self.root.update_idletasks()
            self.foreground.update_idletasks()
            native_placement = self._repair_and_persist_native_position()
            if os.name == "nt" and native_placement is None:
                return
        self._attention_generation += 1
        generation = self._attention_generation
        self._attention_topmost = True
        self._sync_z_order()
        try:
            self.foreground.lift()
            self.canvas.focus_force()
            if os.name == "nt":
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SetForegroundWindow(ctypes.c_void_p(self._hwnd(self.foreground)))
        except (OSError, tk.TclError):
            pass

        def finish_attention() -> None:
            if self._closing or generation != self._attention_generation:
                return
            self._attention_topmost = False
            self._sync_z_order()

        self.root.after(2_500, finish_attention)

    @staticmethod
    def _colorref(value: str) -> int:
        red, green, blue = (int(value[index:index + 2], 16) for index in (1, 3, 5))
        return red | green << 8 | blue << 16

    def _apply_layered_attributes(self) -> None:
        if os.name != "nt":
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetLayeredWindowAttributes(
            ctypes.c_void_p(self._hwnd(self.root)), 0, REAR_ALPHA_BYTE, 0x00000002
        )
        user32.SetLayeredWindowAttributes(
            ctypes.c_void_p(self._hwnd(self.foreground)), self._colorref(TRANSPARENT_KEY), FOREGROUND_ALPHA_BYTE, 0x00000001 | 0x00000002
        )

    def _make_region(self, *, input_contour: bool = False) -> int:
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = ctypes.c_int
        if input_contour:
            gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
            gdi32.CreateRectRgn.restype = ctypes.c_void_p
            gdi32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
            gdi32.CombineRgn.restype = ctypes.c_int
            combined = gdi32.CreateRectRgn(0, 0, 0, 0)
            if not combined:
                return 0
            radius = round(16 * self.scale)
            # CreateRoundRectRgn has subtly different discrete edge rounding
            # from our public pointer contract.  Build exact one-pixel-high
            # scanlines so every integer point accepted by
            # hit_contour_contains is also routed by Win32.
            for rect in self.layout.hit_rects:
                left, top, right, bottom = rect
                for y in range(top, bottom + 1):
                    accepted = [x for x in range(left, right + 1) if point_in_rounded_rect(x, y, rect, radius)]
                    if not accepted:
                        continue
                    piece = gdi32.CreateRectRgn(accepted[0], y, accepted[-1] + 1, y + 1)
                    if not piece or gdi32.CombineRgn(combined, combined, piece, 2) == 0:
                        if piece:
                            gdi32.DeleteObject(piece)
                        gdi32.DeleteObject(combined)
                        return 0
                    gdi32.DeleteObject(piece)
            return int(combined)
        region = gdi32.CreateRoundRectRgn(
            0,
            0,
            self.layout.width + 1,
            self.layout.height + 1,
            round(BASE_CORNER_DIAMETER * self.scale),
            round(BASE_CORNER_DIAMETER * self.scale),
        )
        return int(region) if region else 0

    def _apply_input_regions(self) -> None:
        signature = (self.layout.width, self.layout.height, self.layout.hit_rects)
        if getattr(self, "_region_signature", None) == signature and self.rounded_region_applied:
            return
        if os.name != "nt" or not hasattr(self, "foreground"):
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowRgn.restype = ctypes.c_int
        success = True
        for window, input_contour in ((self.root, True), (self.foreground, False)):
            region = self._make_region(input_contour=input_contour)
            if not region or not user32.SetWindowRgn(ctypes.c_void_p(self._hwnd(window)), ctypes.c_void_p(region), 1):
                success = False
                if region:
                    ctypes.WinDLL("gdi32", use_last_error=True).DeleteObject(ctypes.c_void_p(region))
        self.rounded_region_applied = success
        if success:
            self._region_signature = signature

    def _load_icon(self, provider: str) -> ImageTk.PhotoImage:
        key = (provider, self.layout.icon_size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        try:
            name = PROVIDER_ICON_NAMES[provider]
        except KeyError as exc:
            raise ValueError("unsupported provider icon") from exc
        image = Image.open(_asset(name)).convert("RGBA").resize(
            (self.layout.icon_size, self.layout.icon_size), Image.Resampling.LANCZOS
        )
        photo = ImageTk.PhotoImage(image, master=self.canvas)
        self._image_refs.append(photo)
        self._icon_cache[key] = photo
        return photo

    def _draw_rear(self) -> None:
        dimensions = (self.layout.width, self.layout.height)
        if getattr(self, "_rear_dimensions", None) == dimensions:
            return
        self.rear.configure(width=dimensions[0], height=dimensions[1])
        # One native alpha step keeps this input surface hit-testable while the
        # original glyph-only appearance remains visually unchanged in SDR.
        if self._rear_item is None:
            self._rear_item = self.rear.create_rectangle(0, 0, *dimensions, fill=REAR_BACKGROUND, outline=BORDER_COLOR)
        else:
            self.rear.coords(self._rear_item, 0, 0, *dimensions)
        self._rear_dimensions = dimensions

    def _scene_item(self, key: str, kind: str, *coordinates: int, **options: object) -> int:
        """Retain one canvas object; commit changed properties within this UI turn."""

        self._active_scene_keys.add(key)
        options.setdefault("tags", (key,))
        options.setdefault("state", "normal")
        item = self._scene_items.get(key)
        if item is None:
            item = getattr(self.canvas, f"create_{kind}")(*coordinates, **options)
            self._scene_items[key] = item
        else:
            self.canvas.coords(item, *coordinates)
            previous = self._scene_options.get(key, {})
            changed = {name: value for name, value in options.items() if previous.get(name) != value}
            if changed:
                self.canvas.itemconfigure(item, **changed)
        self._scene_options[key] = dict(options)
        return item

    def _provider_action(self, provider: str, metrics: tuple[Metric, ...]) -> str | None:
        if provider == "claude" and self.config.claudeConsent != "granted":
            return "claude:setup"
        states = {metric.state for metric in metrics if metric.provider == provider}
        if ProviderState.AUTHORIZATION_REQUIRED in states:
            return f"{provider}:sign-in"
        if ProviderState.OFFLINE in states:
            return f"{provider}:retry"
        if ProviderState.PROVIDER_ERROR in states:
            return f"{provider}:retry"
        if provider == "codex" and states == {ProviderState.UNAVAILABLE}:
            return "codex:setup"
        return None

    def _redraw(self) -> None:
        if not hasattr(self, "canvas"):
            return
        desired = self._current_layout()
        if desired.mode != self.layout.mode or desired.width != self.layout.width:
            self._apply_geometry()
            return
        dimensions = (self.layout.width, self.layout.height)
        if getattr(self, "_canvas_dimensions", None) != dimensions:
            self.canvas.configure(width=dimensions[0], height=dimensions[1])
            self._canvas_dimensions = dimensions
        self._draw_rear()
        self._active_scene_keys.clear()
        self._action_boxes.clear()
        self._action_order.clear()
        metrics, codex_visible, claude_visible = self._current_metrics()
        by_provider = {
            provider: tuple(metric for metric in metrics if metric.provider == provider)
            for provider in ("codex", "claude")
        }
        for provider, x, y in self.layout.icon_anchors:
            if (provider == "codex" and codex_visible) or (provider == "claude" and claude_visible):
                self._scene_item(f"{provider}-icon", "image", x, y, image=self._load_icon(provider))
        value_font = (VALUE_FONT_FAMILY, -self.layout.value_font_size)
        reset_font = (RESET_FONT_FAMILY, -self.layout.reset_font_size)
        actions = {
            provider: self._provider_action(provider, values)
            for provider, values in by_provider.items()
        }
        provider_first_lane: set[str] = set()
        for index, (metric, x) in enumerate(zip(metrics, self.layout.text_anchors, strict=True)):
            value, subline = metric_visible_text(metric, self.config.language)
            value_color = metric_color(metric, self.config.colorMode)
            action = actions[metric.provider]
            if action and metric.provider not in provider_first_lane:
                provider_first_lane.add(metric.provider)
                label_key = (
                    "connect" if action == "claude:setup"
                    else "setup" if action.endswith("setup")
                    else "sign_in" if action.endswith("sign-in")
                    else "retry"
                )
                action_text = tr(self.config.language, label_key)
                reset_color = MUTED
            else:
                action = None
                action_text = ""
                reset_color = MUTED
            self._scene_item(
                f"metric-{index}-value", "text",
                x, self.layout.value_y, anchor="w", fill=value_color, font=value_font,
                text=value, tags=(f"metric-{index}-value",),
            )
            reset_item = self._scene_item(
                f"metric-{index}-reset", "text",
                x, self.layout.reset_y, anchor="w", fill=reset_color, font=reset_font,
                text=subline, tags=(f"metric-{index}-reset",),
            )
            # Action fitting may have hidden the previous subline. Restore it
            # before computing this frame; no event/idle flush occurs here.
            self.canvas.itemconfigure(reset_item, state="normal")
            if action is not None:
                action_item = self._scene_item(
                    f"metric-{index}-action", "text",
                    x,
                    self.layout.reset_y,
                    anchor="w",
                    fill=ACTION_BLUE,
                    font=reset_font,
                    text=action_text,
                    tags=(f"metric-{index}-action",),
                )
                expanded, _action_only = place_recovery_action(
                    self.canvas,
                    reset_item,
                    action_item,
                    x=x,
                    reset_y=self.layout.reset_y,
                    scale=self.scale,
                    lane_right=action_lane_right(self.layout, index),
                    canvas_height=self.layout.height,
                )
                self._action_boxes[action] = expanded
                self._action_order.append(action)
                if self._focused_action == len(self._action_order) - 1:
                    self._scene_item(
                        f"metric-{index}-focus", "rectangle",
                        *expanded, outline=ACTION_BLUE, width=max(1, self._logical(1)), tags=("action-focus",)
                    )
            else:
                self._scene_item(
                    f"metric-{index}-action", "text", x, self.layout.reset_y,
                    anchor="w", fill=ACTION_BLUE, font=reset_font, text="", state="hidden",
                )
            if action is None or self._focused_action != len(self._action_order) - 1:
                self._scene_item(
                    f"metric-{index}-focus", "rectangle", 0, 0, 0, 0,
                    outline=ACTION_BLUE, width=max(1, self._logical(1)),
                    tags=("action-focus",), state="hidden",
                )
        for key, item in self._scene_items.items():
            if key not in self._active_scene_keys and self._scene_options[key].get("state") != "hidden":
                self.canvas.itemconfigure(item, state="hidden")
                self._scene_options[key]["state"] = "hidden"
        if self._focused_action >= len(self._action_order):
            self._focused_action = -1

    def _action_at(self, x: int, y: int) -> str | None:
        for action, (left, top, right, bottom) in self._action_boxes.items():
            if left <= x <= right and top <= y <= bottom:
                return action
        return None

    def _update_cursor(self, event: tk.Event) -> None:
        cursor = "hand2" if self._action_at(event.x, event.y) is not None else "fleur"
        event.widget.configure(cursor=cursor)

    def _drag_start(self, event: tk.Event) -> str:
        if not hit_contour_contains(self.layout, event.x, event.y):
            return "break"
        self.canvas.focus_set()
        self._drag_origin = (event.x_root, event.y_root)
        self._drag_window_origin = self._native_pair_origin()
        if self._drag_window_origin is None:
            repaired = self._repair_and_persist_native_position()
            self._drag_window_origin = (
                None if repaired is None else (repaired.x, repaired.y)
            )
        if self._drag_window_origin is None:
            self._drag_origin = None
            return "break"
        self._dragging = False
        self._armed_action = self._action_at(event.x, event.y)
        return "break"

    def _drag_move(self, event: tk.Event) -> str:
        if self._drag_origin is None or self._drag_window_origin is None:
            return "break"
        dx, dy = event.x_root - self._drag_origin[0], event.y_root - self._drag_origin[1]
        if not self._dragging and drag_distance_dip(dx, dy, self.dpi) <= DRAG_THRESHOLD_DIP:
            return "break"
        x, y = self._drag_window_origin[0] + dx, self._drag_window_origin[1] + dy
        if not self._set_window_pair_position(x, y):
            return "break"
        self._dragging = True
        self._armed_action = None
        self._sync_z_order()
        return "break"

    def _drag_end(self, _event: tk.Event) -> str:
        action = self._armed_action
        save_failed = False
        if self._dragging:
            placement = self._repair_and_persist_native_position()
            save_failed = placement is None or not self._persist_native_position(
                placement.x, placement.y, remember=True
            )
        elif action is not None:
            self._invoke_action(action)
        self._drag_origin = None
        self._drag_window_origin = None
        self._dragging = False
        self._armed_action = None
        if save_failed:
            self._reconcile_preferred_position()
            self._show_settings_failure()
        return "break"

    def _focus_next_action(self, _event: tk.Event | None = None) -> str:
        if self._action_order:
            self._focused_action = (self._focused_action + 1) % len(self._action_order)
            self._redraw()
        return "break"

    def _focus_previous_action(self, _event: tk.Event | None = None) -> str:
        if self._action_order:
            self._focused_action = (self._focused_action - 1) % len(self._action_order)
            self._redraw()
        return "break"

    def _invoke_focused_action(self, _event: tk.Event | None = None) -> str:
        if 0 <= self._focused_action < len(self._action_order):
            self._invoke_action(self._action_order[self._focused_action])
        return "break"

    def _invoke_action(self, action: str) -> None:
        provider, operation = action.split(":", 1)
        if operation == "retry":
            self.on_refresh(provider)
        elif operation == "sign-in":
            try:
                self.on_sign_in(provider)
            except RuntimeError:
                return
        elif operation == "setup":
            if provider == "claude" and self.config.claudeConsent != "granted":
                self._request_claude_consent()
            else:
                self._open_configure()

    def _request_claude_consent(self) -> None:
        from tkinter import messagebox

        approved = messagebox.askyesno(
            tr(self.config.language, "claude_consent_title"),
            tr(self.config.language, "claude_disclosure_body"),
            parent=self.foreground,
            icon="question",
            default="no",
        )
        consent = "granted" if approved else "denied"
        mode = "enabled" if approved else self.config.claudeMode
        if self._commit_config(replace(self.config, claudeConsent=consent, claudeMode=mode)):
            # Declining can hide the only Auto lane and schedule app exit.
            # Do not ask layout code to render an impossible providerless HUD.
            if self._provider_visible("codex") or self._provider_visible("claude"):
                self._redraw()
        else:
            self._show_settings_failure()

    def _manual_refresh(self, _event: tk.Event | None = None) -> str:
        if self.config.codexMode != "disabled":
            self.on_refresh("codex")
        if self.config.claudeMode != "disabled":
            self.on_refresh("claude")
        return "break"

    def _open_context_menu_keyboard(self, _event: tk.Event | None = None) -> str:
        self._post_context_menu(self.foreground.winfo_rootx() + self._logical(12), self.foreground.winfo_rooty() + self.layout.height)
        return "break"

    def _open_context_menu(self, event: tk.Event) -> str:
        self._post_context_menu(event.x_root, event.y_root)
        return "break"

    def _post_context_menu(self, x: int, y: int) -> None:
        update_label, update_enabled = self.update_menu_state()
        entries = build_context_menu(
            self.config,
            actual_topmost=self._actual_topmost(),
            actual_startup=self.startup_active(),
            update_label=update_label,
            update_enabled=update_enabled,
        )
        actions: dict[str, Callable[[], None]] = {
            "refresh": lambda: self._manual_refresh(),
            "configure": self._open_configure,
            "colors:toggle": lambda: self._set_config(
                colorMode="monochrome" if self.config.colorMode == "status" else "status"
            ),
            "check-updates": self.on_check_updates,
            "topmost": self._toggle_topmost,
            "startup": lambda: self._set_config(startWithWindows=not self.startup_active()),
            "exit": self.exit,
        }
        NativeMenu(self._hwnd(self.foreground)).show(entries, x, y, actions)
        self._sync_z_order()

    def _set_config(self, **values: object) -> None:
        if not self._commit_config(replace(self.config, **values)):
            self._show_settings_failure()
            return
        self._redraw()

    def _toggle_topmost(self) -> None:
        if not self._commit_config(replace(self.config, topmost=not self._actual_topmost())):
            self._show_settings_failure()
            return
        self.root.attributes("-topmost", self.config.topmost)
        self.foreground.attributes("-topmost", self.config.topmost)
        self._sync_z_order()

    def _show_settings_failure(self) -> None:
        from tkinter import messagebox

        messagebox.showerror(
            tr(self.config.language, "settings_title"),
            tr(self.config.language, "settings_preserved" if getattr(self.config_store, "write_blocked", False) is True else "setup_failed"),
            parent=self.foreground,
        )

    def _commit_config(self, new: AppConfig) -> bool:
        if getattr(self.config_store, "write_blocked", False) is True:
            return False
        old = self.config
        old_layout = getattr(self, "layout", None)
        old_scale = getattr(self, "scale", 1.0)
        old_dpi = getattr(self, "dpi", 96)
        old_origin: tuple[int, int] | None = None
        layout_prepared = False

        if old_layout is not None and os.name == "nt":
            safe_old = self._repair_and_persist_native_position()
            if safe_old is None:
                return False
            # Settings must preserve the preferred coordinates even while the
            # actual HWND is temporarily clamped to a smaller work area.
            old = self.config
            old_origin = (safe_old.x, safe_old.y)
            new = replace(new, x=old.x, y=old.y, _preserved_unknown_json=old._preserved_unknown_json)
            try:
                target_layout = self._current_layout(new)
            except (RuntimeError, ValueError):
                target_layout = None
            if target_layout is not None and (
                target_layout.mode != old_layout.mode
                or target_layout.width != old_layout.width
                or target_layout.height != old_layout.height
            ):
                self.layout = target_layout
                if not self._set_window_pair_position(*old_origin):
                    self._restore_layout(
                        old_layout, old_scale, old_dpi, old_origin
                    )
                    return False
                prepared = self._native_post_placement_clamp()
                if prepared is None or not prepared.fully_contained:
                    self._restore_layout(
                        old_layout, old_scale, old_dpi, old_origin
                    )
                    return False
                layout_prepared = True

        persisted = False
        try:
            self.config_store.save(new)
            persisted = True
            self.on_config_changed(old, new)
            self.config = new
            if layout_prepared:
                self._apply_input_regions()
                self._sync_z_order()
                self._redraw()
            return True
        except (OSError, RuntimeError, ValueError):
            if persisted:
                try:
                    self.on_config_changed(new, old)
                except (OSError, RuntimeError, ValueError):
                    pass
            self.config = old
            if layout_prepared and old_layout is not None and old_origin is not None:
                try:
                    restored = self._restore_layout(old_layout, old_scale, old_dpi, old_origin)
                except (OSError, RuntimeError, ValueError, tk.TclError):
                    restored = None
                if restored is None or not restored.fully_contained:
                    return False
                self.config = old
            if persisted:
                try:
                    self.config_store.save(old)
                except (OSError, RuntimeError, ValueError):
                    pass
            return False

    def _open_configure(self) -> None:
        from .configure import open_configure_dialog

        opened = self.config

        def commit_edited(edited: AppConfig) -> bool:
            return self._commit_config(merge_settings_changes(self.config, opened, edited))

        open_configure_dialog(
            self.foreground,
            opened,
            commit_edited,
            startup_actual=self.startup_active(),
            portable=self.portable,
            codex_present=self.provider_detected("codex"),
            claude_present=self.provider_detected("claude"),
            copy_repair_report=self._copy_safe_diagnostics,
            show_about=self._show_privacy_about,
        )

    def _copy_safe_diagnostics(self) -> None:
        codex, claude = self.store.snapshot()
        payload = diagnostics_json(safe_diagnostics(self.config, codex, claude, dpi=self.dpi))
        self.foreground.clipboard_clear()
        self.foreground.clipboard_append(payload)

    def _show_privacy_about(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo(
            tr(self.config.language, "privacy_about"),
            f"{APP_NAME} {__version__}\n\n{tr(self.config.language, 'privacy_text')}\n\n{tr(self.config.language, 'license_text')}",
            parent=self.foreground,
        )

    def _emit_notifications(self) -> None:
        if self._notifier is None:
            return
        metrics, _codex_visible, _claude_visible = self._current_metrics()
        for value in self._notification_decider.evaluate(self.config, metrics):
            title, message = notification_text(value, self.config.language)
            self._notifier.show(title, message)

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            if self._drag_origin is None:
                self._refresh_display_state()
            desired = self._current_layout()
            if desired.mode != self.layout.mode and self._drag_origin is None:
                self._apply_geometry()
            else:
                self._redraw()
            self._sync_z_order()
            self._emit_notifications()
        except (OSError, RuntimeError, tk.TclError):
            # Keep the last complete frame during a transient display/config
            # failure. One failed callback must not permanently stop refresh.
            pass
        finally:
            if not self._closing:
                self.root.after(1_000, self._tick)

    def diagnostics_snapshot(self) -> dict[str, object]:
        codex, claude = self.store.snapshot()
        return {
            "logicalSize": [self.layout.logical_width, self.layout.logical_height],
            "physicalSize": [self.layout.width, self.layout.height],
            "mode": self.layout.mode,
            "dpi": self.dpi,
            "rearAlphaByte": REAR_ALPHA_BYTE,
            "foregroundAlphaByte": FOREGROUND_ALPHA_BYTE,
            "transparentKey": TRANSPARENT_KEY,
            "twoAlignedWindows": self.root.winfo_x() == self.foreground.winfo_x() and self.root.winfo_y() == self.foreground.winfo_y(),
            "roundedRegionApplied": self.rounded_region_applied,
            "ordinaryClickAction": "none",
            "dragThresholdDipExclusive": DRAG_THRESHOLD_DIP,
            "safeDiagnostics": safe_diagnostics(self.config, codex, claude, dpi=self.dpi),
        }

    def exit(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._repair_and_persist_native_position() is None:
            self._closing = False
            self._show_settings_failure()
            return
        self.on_exit()
        self.root.destroy()


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
