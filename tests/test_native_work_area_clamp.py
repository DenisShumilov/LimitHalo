from __future__ import annotations

import ast
import ctypes
import inspect
import json
import textwrap
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from limit_halo.config import AppConfig, ConfigStore
from limit_halo.ui import (
    NativeClampResult,
    WidgetWindow,
    clamp_origin_to_work_area,
    compute_layout,
    native_aligned_window_origin,
    native_move_aligned_windows,
    native_post_placement_clamp,
    native_window_dpi,
)


POST_STARTUP_CLAMP_MUTANT = "native post-startup work-area clamp omitted"
POST_ATTENTION_CLAMP_MUTANT = "native post-attention work-area clamp omitted"
V7_GEOMETRY_MUTANTS = {
    "SHOW-BEFORE-CLAMP",
    "SAVE-BEFORE-CLAMP",
    "REPAIR-NOT-PERSISTED",
    "SECOND-HWND-UNCLAMPED",
    "NEW-SAVE-CALLSITE",
    "STARTUP-CLAMP-OMITTED",
    "ACTIVATION-CLAMP-OMITTED",
    "DRAG-CLAMP-OMITTED",
    "RELAYOUT-CLAMP-OMITTED",
    "PERIODIC-REPAIR-OMITTED",
    "PERSIST-LOGICAL-INSTEAD-OF-NATIVE",
    "DISPLAY-CHANGE-IGNORED",
    "MONITOR-DISCONNECT-IGNORED",
}


def _pointer_value(value: object) -> int:
    if isinstance(value, ctypes.c_void_p):
        return int(value.value or 0)
    return int(value or 0)


class _FakeFunction:
    def __init__(self, implementation: object) -> None:
        self.implementation = implementation
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        return self.implementation(*args)


class _FakeUser32:
    def __init__(
        self,
        rects: dict[int, tuple[int, int, int, int]],
        *,
        foreground_monitor: int,
        work_areas: dict[int, tuple[int, int, int, int]],
        dpi: int = 96,
    ) -> None:
        self.rects = dict(rects)
        self.foreground_monitor = foreground_monitor
        self.work_areas = dict(work_areas)
        self.dpi = dpi
        self.get_rect_calls: list[int] = []
        self.monitor_calls: list[tuple[int, int]] = []
        self.set_calls: list[tuple[int, int, int, int, int, int]] = []
        self.GetWindowRect = _FakeFunction(self._get_window_rect)
        self.MonitorFromWindow = _FakeFunction(self._monitor_from_window)
        self.GetMonitorInfoW = _FakeFunction(self._get_monitor_info)
        self.SetWindowPos = _FakeFunction(self._set_window_pos)
        self.GetDpiForWindow = _FakeFunction(self._get_dpi_for_window)

    def _get_dpi_for_window(self, hwnd: object) -> int:
        return self.dpi if _pointer_value(hwnd) in self.rects else 0

    def _get_window_rect(self, hwnd: object, output: object) -> int:
        handle = _pointer_value(hwnd)
        self.get_rect_calls.append(handle)
        rect = self.rects.get(handle)
        if rect is None:
            return 0
        target = output._obj
        target.left, target.top, target.right, target.bottom = rect
        return 1

    def _monitor_from_window(self, hwnd: object, flags: object) -> int:
        self.monitor_calls.append((_pointer_value(hwnd), int(flags)))
        return self.foreground_monitor

    def _get_monitor_info(self, monitor: object, output: object) -> int:
        area = self.work_areas.get(_pointer_value(monitor))
        if area is None:
            return 0
        target = output._obj
        target.rcMonitor.left, target.rcMonitor.top, target.rcMonitor.right, target.rcMonitor.bottom = area
        target.rcWork.left, target.rcWork.top, target.rcWork.right, target.rcWork.bottom = area
        return 1

    def _set_window_pos(
        self,
        hwnd: object,
        _insert_after: object,
        x: object,
        y: object,
        width: object,
        height: object,
        flags: object,
    ) -> int:
        handle = _pointer_value(hwnd)
        old = self.rects.get(handle)
        if old is None:
            return 0
        old_width, old_height = old[2] - old[0], old[3] - old[1]
        new_x, new_y = int(x), int(y)
        self.rects[handle] = (new_x, new_y, new_x + old_width, new_y + old_height)
        self.set_calls.append((handle, new_x, new_y, int(width), int(height), int(flags)))
        return 1


def _has_ordered_native_clamp(source: str, *, function_name: str) -> bool:
    tree = ast.parse(textwrap.dedent(source))
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]

    def lines_for(attribute: str) -> list[int]:
        return sorted(
            node.lineno
            for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == attribute
        )

    clamp_lines = lines_for("_native_post_placement_clamp")
    update_lines = lines_for("update_idletasks")
    if not clamp_lines or not update_lines or min(clamp_lines) <= max(update_lines):
        return False
    if function_name == "activate_from_shortcut":
        commit_lines = lines_for("_commit_config")
        return bool(commit_lines and min(clamp_lines) < min(commit_lines))
    apply_layer_lines = lines_for("_apply_layered_attributes")
    return bool(apply_layer_lines and min(clamp_lines) < min(apply_layer_lines))


def _remove_startup_native_clamp(source: str) -> str:
    """Remove only the post-deiconify startup clamp from a full ui.py source."""

    tree = ast.parse(source)
    widget = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WidgetWindow"
    )
    initializer = next(
        node for node in widget.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    calls = [node for node in ast.walk(initializer) if isinstance(node, ast.Call)]
    layered_line = min(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "_apply_layered_attributes"
    )
    last_visible_update = max(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "update_idletasks"
        and node.lineno < layered_line
    )
    candidates = [
        node
        for node in initializer.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "_native_post_placement_clamp"
        and last_visible_update < node.lineno < layered_line
    ]
    if len(candidates) != 1:
        raise AssertionError("startup native clamp is not uniquely identified")
    candidate = candidates[0]
    lines = source.splitlines(keepends=True)
    del lines[candidate.lineno - 1:candidate.end_lineno]
    return "".join(lines)


def _catalog_g02_mutants() -> list[str]:
    catalog = json.loads(
        (Path(__file__).resolve().parent / "mutants" / "mutant-catalog.json").read_text(
            encoding="utf-8"
        )
    )
    return catalog["G02"]


class NativeWorkAreaClampTests(unittest.TestCase):
    def test_bottom_edge_clamps_in_physical_pixels_at_every_supported_dpi(self) -> None:
        work_area = (0, 0, 5120, 1392)
        for dpi in (96, 120, 144, 192):
            with self.subTest(dpi=dpi):
                layout = compute_layout("both-compact", dpi / 96)
                x = 4105
                y = work_area[3] - layout.height + round(65 * dpi / 96)
                self.assertEqual(
                    clamp_origin_to_work_area(
                        x, y, layout.width, layout.height, work_area
                    ),
                    (x, work_area[3] - layout.height, True),
                )

        # Exact live v4 failure signature: only 19 of 84 pixels were visible.
        self.assertEqual(
            clamp_origin_to_work_area(4105, 1373, 364, 84, work_area),
            (4105, 1308, True),
        )

    def test_negative_coordinate_monitor_and_oversized_edges_are_deterministic(self) -> None:
        negative_work_area = (-2560, -120, -640, 1040)
        self.assertEqual(
            clamp_origin_to_work_area(-2800, 1000, 728, 168, negative_work_area),
            (-2560, 872, True),
        )
        self.assertEqual(
            clamp_origin_to_work_area(-900, -300, 364, 84, negative_work_area),
            (-1004, -120, True),
        )
        self.assertEqual(
            clamp_origin_to_work_area(500, 500, 1200, 900, (100, -200, 1000, 600)),
            (100, -200, False),
        )
        with self.assertRaises(ValueError):
            clamp_origin_to_work_area(0, 0, 0, 84, (0, 0, 1920, 1040))

    def test_native_clamp_measures_and_moves_both_aligned_windows(self) -> None:
        rear, foreground = 101, 202
        api = _FakeUser32(
            {
                rear: (4105, 1373, 4469, 1457),
                foreground: (4105, 1373, 4469, 1457),
            },
            foreground_monitor=77,
            work_areas={77: (0, 0, 5120, 1392)},
        )
        result = native_post_placement_clamp(rear, foreground, user32=api)
        self.assertEqual(
            result,
            NativeClampResult(4105, 1308, (0, 0, 5120, 1392), True, True),
        )
        self.assertEqual(api.rects[rear], (4105, 1308, 4469, 1392))
        self.assertEqual(api.rects[foreground], (4105, 1308, 4469, 1392))
        self.assertEqual(api.monitor_calls, [(foreground, 0x00000002)])
        self.assertEqual([call[0] for call in api.set_calls], [rear, foreground])
        self.assertTrue(all(call[3:] == (0, 0, 0x0015) for call in api.set_calls))
        self.assertEqual(api.get_rect_calls, [rear, foreground, rear, foreground])

    def test_native_clamp_uses_foreground_monitor_on_negative_multi_monitor_layout(self) -> None:
        rear, foreground = 303, 404
        api = _FakeUser32(
            {
                rear: (-700, 1020, -336, 1104),
                foreground: (-700, 1020, -336, 1104),
            },
            foreground_monitor=22,
            work_areas={
                11: (0, 0, 5120, 1392),
                22: (-1920, 0, 0, 1040),
            },
        )
        result = native_post_placement_clamp(rear, foreground, user32=api)
        self.assertEqual(
            result,
            NativeClampResult(-700, 956, (-1920, 0, 0, 1040), True, True),
        )
        self.assertEqual(api.rects[rear], (-700, 956, -336, 1040))
        self.assertEqual(api.rects[foreground], (-700, 956, -336, 1040))

    def test_contained_pair_is_measured_but_not_needlessly_moved(self) -> None:
        rear, foreground = 505, 606
        api = _FakeUser32(
            {
                rear: (120, 80, 484, 164),
                foreground: (120, 80, 484, 164),
            },
            foreground_monitor=33,
            work_areas={33: (0, 0, 1920, 1040)},
        )
        result = native_post_placement_clamp(rear, foreground, user32=api)
        self.assertEqual(
            result,
            NativeClampResult(120, 80, (0, 0, 1920, 1040), False, True),
        )
        self.assertEqual(api.set_calls, [])
        self.assertEqual(api.get_rect_calls, [rear, foreground])

    def test_native_exact_move_supports_negative_physical_origins(self) -> None:
        rear, foreground = 707, 808
        api = _FakeUser32(
            {
                rear: (0, 0, 364, 84),
                foreground: (0, 0, 364, 84),
            },
            foreground_monitor=44,
            work_areas={44: (-1920, 0, 0, 1040)},
        )
        self.assertTrue(
            native_move_aligned_windows(
                rear, foreground, -1886, 972, user32=api
            )
        )
        self.assertEqual(api.rects[rear], (-1886, 972, -1522, 1056))
        self.assertEqual(api.rects[foreground], (-1886, 972, -1522, 1056))
        self.assertEqual([call[0] for call in api.set_calls], [rear, foreground])

    def test_native_drag_origin_and_dpi_are_read_from_the_real_hwnds(self) -> None:
        rear, foreground = 909, 1001
        api = _FakeUser32(
            {
                rear: (-1886, 972, -1522, 1056),
                foreground: (-1886, 972, -1522, 1056),
            },
            foreground_monitor=55,
            work_areas={55: (-1920, 0, 0, 1040)},
            dpi=144,
        )
        self.assertEqual(
            native_aligned_window_origin(rear, foreground, user32=api),
            (-1886, 972),
        )
        self.assertEqual(native_window_dpi(foreground, user32=api), 144)

        api.rects[rear] = (-1800, 900, -1436, 984)
        self.assertIsNone(
            native_aligned_window_origin(rear, foreground, user32=api)
        )
        api.dpi = 0
        self.assertIsNone(native_window_dpi(foreground, user32=api))

    def test_work_area_change_and_monitor_disconnect_repair_without_focus(self) -> None:
        rear, foreground = 1101, 1202
        api = _FakeUser32(
            {
                rear: (4105, 1308, 4469, 1392),
                foreground: (4105, 1308, 4469, 1392),
            },
            foreground_monitor=77,
            work_areas={77: (0, 0, 5120, 1392)},
        )
        self.assertTrue(
            native_post_placement_clamp(rear, foreground, user32=api).fully_contained
        )

        # A taller taskbar shrinks the same monitor's work area.
        api.work_areas[77] = (0, 0, 5120, 1320)
        taskbar_result = native_post_placement_clamp(
            rear, foreground, user32=api
        )
        self.assertEqual((taskbar_result.x, taskbar_result.y), (4105, 1236))

        # The old monitor disappears; MonitorFromWindow now selects the
        # remaining negative-origin display and the pair is contained there.
        api.foreground_monitor = 88
        api.work_areas = {88: (-1920, -120, 0, 920)}
        disconnected = native_post_placement_clamp(
            rear, foreground, user32=api
        )
        self.assertTrue(disconnected.fully_contained)
        self.assertEqual((disconnected.x, disconnected.y), (-364, 836))
        self.assertTrue(
            all(call[-1] & 0x0010 for call in api.set_calls),
            "periodic repairs must use NOACTIVATE",
        )

    def test_automatic_native_clamp_preserves_user_preference(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window.root = mock.Mock()
        window.foreground = mock.Mock()
        window.layout = compute_layout("both-compact", 1.0)
        window.config = AppConfig(x=4, y=1374)
        window.config_store = mock.Mock()
        window._native_post_placement_clamp = mock.Mock(
            return_value=NativeClampResult(
                4, 1308, (0, 0, 5120, 1392), True, True
            )
        )
        with mock.patch("limit_halo.ui.os.name", "nt"):
            result = window._repair_and_persist_native_position()
        self.assertEqual(result.x if result else None, 4)
        self.assertEqual((window.config.x, window.config.y), (4, 1374))
        window.config_store.save.assert_not_called()

        # A second healthy guard tick is a no-op on disk.
        window._native_post_placement_clamp.return_value = NativeClampResult(
            4, 1308, (0, 0, 5120, 1392), False, True
        )
        with mock.patch("limit_halo.ui.os.name", "nt"):
            window._repair_and_persist_native_position()
        window.config_store.save.assert_not_called()

    def test_failed_native_verification_never_overwrites_last_safe_position(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window.root = mock.Mock()
        window.foreground = mock.Mock()
        window.layout = compute_layout("both-compact", 1.0)
        window.config = AppConfig(x=40, y=80)
        window.config_store = mock.Mock()
        window._native_post_placement_clamp = mock.Mock(return_value=None)
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertIsNone(window._repair_and_persist_native_position())
        window.config_store.save.assert_not_called()
        self.assertEqual((window.config.x, window.config.y), (40, 80))

        window._native_post_placement_clamp.return_value = NativeClampResult(
            4, 1308, (0, 0, 5120, 1392), True, True
        )
        window.config_store.save.side_effect = OSError("synthetic full disk")
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertIsNotNone(window._repair_and_persist_native_position())
            self.assertFalse(window._persist_native_position(4, 1308, remember=True))
        self.assertEqual((window.config.x, window.config.y), (40, 80))

    def test_repaired_origin_round_trips_through_isolated_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            original = AppConfig(x=4, y=1374)
            store.save(original)
            repaired = AppConfig(x=4, y=1308)
            store.save(repaired)
            self.assertEqual((store.load().x, store.load().y), (4, 1308))

    def test_periodic_guard_adopts_native_dpi_and_rolls_back_on_failure(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window._reconcile_preferred_position = mock.Mock(return_value=True)
        window.root = mock.Mock()
        window.foreground = mock.Mock()
        window.config = AppConfig(x=4, y=1308)
        window.scale = 1.0
        window.dpi = 96
        window.layout = compute_layout("both-compact", 1.0)
        current = NativeClampResult(4, 1308, (0, 0, 5120, 1392), False, True)
        changed = NativeClampResult(4, 1266, (0, 0, 5120, 1392), True, True)
        window._repair_and_persist_native_position = mock.Mock(return_value=current)
        window._hwnd = mock.Mock(return_value=100)
        window._current_layout = mock.Mock(
            side_effect=lambda _config=None, scale=None: compute_layout(
                "both-compact", window.scale if scale is None else scale
            )
        )
        window._place_repair_and_persist = mock.Mock(return_value=changed)
        window._apply_input_regions = mock.Mock()
        window._sync_z_order = mock.Mock()
        window._redraw = mock.Mock()
        window._restore_layout = mock.Mock()

        with (
            mock.patch("limit_halo.ui.os.name", "nt"),
            mock.patch("limit_halo.ui.native_window_dpi", return_value=144),
        ):
            self.assertTrue(window._refresh_display_state())
        self.assertEqual((window.scale, window.dpi), (1.5, 144))
        self.assertEqual((window.layout.width, window.layout.height), (546, 126))
        window._place_repair_and_persist.assert_called_once_with(4, 1308)
        window._restore_layout.assert_not_called()

        prior_layout = window.layout
        prior_scale, prior_dpi = window.scale, window.dpi
        window._place_repair_and_persist.return_value = None
        with (
            mock.patch("limit_halo.ui.os.name", "nt"),
            mock.patch("limit_halo.ui.native_window_dpi", return_value=192),
        ):
            self.assertFalse(window._refresh_display_state())
        window._restore_layout.assert_called_once_with(
            prior_layout, prior_scale, prior_dpi, (4, 1308)
        )

    def test_layout_configuration_is_clamped_before_it_is_saved(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        old = AppConfig(displayPreset="compact", x=4, y=1308)
        new = AppConfig(displayPreset="wide", x=4, y=1308)
        window.config = old
        window.scale = 1.0
        window.dpi = 96
        window.layout = compute_layout("both-compact", 1.0)
        target = compute_layout("both-wide", 1.0)
        events: list[str] = []
        window._current_layout = mock.Mock(return_value=target)
        window._repair_and_persist_native_position = mock.Mock(
            return_value=NativeClampResult(
                4, 1308, (0, 0, 5120, 1392), False, True
            )
        )
        window._set_window_pair_position = mock.Mock(
            side_effect=lambda *_args: events.append("resize") or True
        )
        window._native_post_placement_clamp = mock.Mock(
            side_effect=lambda: events.append("clamp")
            or NativeClampResult(4, 1308, (0, 0, 5120, 1392), False, True)
        )
        window.config_store = mock.Mock()
        window.config_store.save.side_effect = lambda _value: events.append("save")
        window.on_config_changed = lambda _old, _new: events.append("apply")
        window._apply_input_regions = mock.Mock()
        window._sync_z_order = mock.Mock()
        window._redraw = mock.Mock()
        window._restore_layout = mock.Mock()

        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertTrue(window._commit_config(new))
        self.assertLess(events.index("clamp"), events.index("save"))
        self.assertEqual(window.config, new)
        self.assertEqual(window.layout, target)

        blocked = WidgetWindow.__new__(WidgetWindow)
        blocked.config = old
        blocked.scale = 1.0
        blocked.dpi = 96
        blocked.layout = compute_layout("both-compact", 1.0)
        blocked._current_layout = mock.Mock(return_value=target)
        blocked._repair_and_persist_native_position = mock.Mock(
            return_value=NativeClampResult(4, 1308, (0, 0, 5120, 1392), False, True)
        )
        blocked._set_window_pair_position = mock.Mock(return_value=True)
        blocked._native_post_placement_clamp = mock.Mock(return_value=None)
        blocked.config_store = mock.Mock()
        blocked.on_config_changed = mock.Mock()
        blocked._restore_layout = mock.Mock()
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertFalse(blocked._commit_config(new))
        blocked.config_store.save.assert_not_called()
        blocked._restore_layout.assert_called_once()

    def test_exit_refuses_to_destroy_the_only_unsaved_repaired_position(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window._closing = False
        window.root = mock.Mock()
        window._repair_and_persist_native_position = mock.Mock(return_value=None)
        window._show_settings_failure = mock.Mock()
        window.on_exit = mock.Mock()
        window.exit()
        self.assertFalse(window._closing)
        window._show_settings_failure.assert_called_once_with()
        window.on_exit.assert_not_called()
        window.root.destroy.assert_not_called()

    def test_all_position_routes_use_verified_repair_before_save_or_show(self) -> None:
        initializer = inspect.getsource(WidgetWindow.__init__)
        activation = inspect.getsource(WidgetWindow.activate_from_shortcut)
        apply_geometry = inspect.getsource(WidgetWindow._apply_geometry)
        drag_move = inspect.getsource(WidgetWindow._drag_move)
        drag_start = inspect.getsource(WidgetWindow._drag_start)
        drag_end = inspect.getsource(WidgetWindow._drag_end)
        tick = inspect.getsource(WidgetWindow._tick)
        exit_source = inspect.getsource(WidgetWindow.exit)

        self.assertLess(initializer.index("initial_placement"), initializer.index("deiconify()"))
        self.assertIn("_repair_and_persist_native_position", activation)
        self.assertLess(activation.index("_repair_and_persist_native_position"), activation.index("deiconify()"))
        self.assertNotIn("attention_position(", activation)
        self.assertIn("_place_repair_and_persist", apply_geometry)
        self.assertIn("_set_window_pair_position", drag_move)
        self.assertIn("_native_pair_origin", drag_start)
        self.assertNotIn("winfo_x", drag_start)
        self.assertNotIn("winfo_y", drag_start)
        self.assertNotIn("_repair_and_persist_native_position", drag_move)
        self.assertIn("_repair_and_persist_native_position", drag_end)
        self.assertIn("_refresh_display_state", tick)
        self.assertIn("self._drag_origin is None", tick)
        self.assertIn("_repair_and_persist_native_position", exit_source)
        for unsafe in (drag_end, exit_source):
            self.assertNotIn("winfo_x", unsafe)
            self.assertNotIn("winfo_y", unsafe)
            self.assertNotIn("_commit_config", unsafe)

    def test_shortcut_uses_verified_pre_show_and_post_show_repair(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window._closing = False
        window._attention_generation = 0
        window._attention_topmost = False
        window.root = mock.Mock()
        window.foreground = mock.Mock()
        window.canvas = mock.Mock()
        window.layout = compute_layout("codex-one-window", 1.0)
        window.scale = 1.0
        window.config = AppConfig()
        window._work_areas = mock.Mock(return_value=((0, 0, 5120, 1392),))
        window._sync_z_order = mock.Mock()
        placement = NativeClampResult(
            4952, 1308, (0, 0, 5120, 1392), False, True
        )
        window._place_repair_and_persist = mock.Mock(return_value=placement)
        window._repair_and_persist_native_position = mock.Mock(return_value=placement)

        with (
            mock.patch("limit_halo.ui.screen_pointer_position", return_value=(5100, 1400)),
            mock.patch("limit_halo.ui.os.name", "nt"),
            mock.patch("limit_halo.ui.ctypes.WinDLL", side_effect=OSError),
        ):
            window.activate_from_shortcut()

        window._place_repair_and_persist.assert_not_called()
        self.assertEqual(window._repair_and_persist_native_position.call_count, 2)
        window.root.deiconify.assert_called_once_with()
        window.foreground.deiconify.assert_called_once_with()

    def test_catalog_binds_every_v7_geometry_omission_control(self) -> None:
        catalog = json.loads(
            (Path(__file__).resolve().parent / "mutants" / "mutant-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(catalog["V7-G03"]), V7_GEOMETRY_MUTANTS)

    def test_every_v7_geometry_omission_is_applied_to_source_and_rejected(self) -> None:
        source_path = Path(inspect.getsourcefile(WidgetWindow) or "")
        source = source_path.read_text(encoding="utf-8")

        def parts(value: str) -> tuple[dict[str, str], dict[str, str]]:
            lines = value.splitlines(keepends=True)
            tree = ast.parse(value)
            functions: dict[str, str] = {}
            methods: dict[str, str] = {}
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    functions[node.name] = "".join(lines[node.lineno - 1 : node.end_lineno])
                if isinstance(node, ast.ClassDef) and node.name == "WidgetWindow":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            methods[item.name] = "".join(
                                lines[item.lineno - 1 : item.end_lineno]
                            )
            return functions, methods

        def save_inventory(value: str) -> Counter[tuple[str, str]]:
            tree = ast.parse(value)
            result: Counter[tuple[str, str]] = Counter()

            class Visitor(ast.NodeVisitor):
                def __init__(self) -> None:
                    self.stack: list[str] = []

                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    self.stack.append(node.name)
                    self.generic_visit(node)
                    self.stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node: ast.Call) -> None:
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "save":
                        result[(self.stack[-1] if self.stack else "<module>", "save")] += 1
                    self.generic_visit(node)

            Visitor().visit(tree)
            return result

        baseline_inventory = save_inventory(source)

        def violations(value: str) -> set[str]:
            functions, methods = parts(value)
            found: set[str] = set()
            initializer = methods["__init__"]
            if "initial_placement = self._apply_geometry(" not in initializer or initializer.index(
                "initial_placement = self._apply_geometry("
            ) > initializer.index("self.root.deiconify()"):
                found.add("SHOW-BEFORE-CLAMP")
            if "visible_placement = self._repair_and_persist_native_position()" not in initializer:
                found.add("STARTUP-CLAMP-OMITTED")

            repair = methods["_repair_and_persist_native_position"]
            if "return result if self._persist_native_position(result.x, result.y) else None" not in repair:
                found.add("REPAIR-NOT-PERSISTED")
            native = functions["native_post_placement_clamp"]
            if "for rect in (rear_rect, foreground_rect)" not in native or "foreground_moved" not in native:
                found.add("SECOND-HWND-UNCLAMPED")
            if save_inventory(value) != baseline_inventory:
                found.add("NEW-SAVE-CALLSITE")

            activation = methods["activate_from_shortcut"]
            if "_repair_and_persist_native_position" not in activation or activation.index(
                "_repair_and_persist_native_position"
            ) > activation.index("deiconify()"):
                found.add("ACTIVATION-CLAMP-OMITTED")
            if "_repair_and_persist_native_position" not in methods["_drag_end"]:
                found.add("DRAG-CLAMP-OMITTED")

            commit = methods["_commit_config"]
            if "prepared = self._native_post_placement_clamp()" not in commit:
                found.add("RELAYOUT-CLAMP-OMITTED")
            if "_refresh_display_state" not in methods["_tick"]:
                found.add("PERIODIC-REPAIR-OMITTED")
            if "self._persist_native_position(result.x, result.y)" not in repair:
                found.add("PERSIST-LOGICAL-INSTEAD-OF-NATIVE")
            if "native_window_dpi" not in methods["_refresh_display_state"]:
                found.add("DISPLAY-CHANGE-IGNORED")
            if "MonitorFromWindow" not in native:
                found.add("MONITOR-DISCONNECT-IGNORED")

            native_index = repair.find("self._native_post_placement_clamp()")
            persist_index = repair.find(
                "self._persist_native_position(result.x, result.y)"
            )
            if native_index < 0 or persist_index < 0 or persist_index < native_index:
                found.add("SAVE-BEFORE-CLAMP")
            if "self._persist_native_position(self.config.x or 0" in repair:
                found.add("SAVE-BEFORE-CLAMP")
            return found

        self.assertEqual(violations(source), set())

        def replace_once(value: str, old: str, new: str) -> str:
            self.assertEqual(value.count(old), 1, old)
            return value.replace(old, new, 1)

        mutators = {
            "SHOW-BEFORE-CLAMP": lambda value: replace_once(
                value,
                "initial_placement = self._apply_geometry(",
                "initial_placement = self._native_post_placement_clamp(",
            ),
            "SAVE-BEFORE-CLAMP": lambda value: replace_once(
                value,
                "        result = self._native_post_placement_clamp()\n        if result is None or not result.fully_contained:\n            return None\n        return result if",
                "        self._persist_native_position(self.config.x or 0, self.config.y or 0)\n        result = self._native_post_placement_clamp()\n        if result is None or not result.fully_contained:\n            return None\n        return result if",
            ),
            "REPAIR-NOT-PERSISTED": lambda value: replace_once(
                value,
                "        return result if self._persist_native_position(result.x, result.y) else None\n",
                "        return result\n",
            ),
            "SECOND-HWND-UNCLAMPED": lambda value: value.replace(
                "for rect in (rear_rect, foreground_rect)",
                "for rect in (rear_rect,)",
            ),
            "NEW-SAVE-CALLSITE": lambda value: value
            + "\ndef synthetic_new_save(store, config):\n    store.save(config)\n",
            "STARTUP-CLAMP-OMITTED": lambda value: replace_once(
                value,
                "visible_placement = self._repair_and_persist_native_position()",
                "visible_placement = initial_placement",
            ),
            "ACTIVATION-CLAMP-OMITTED": lambda value: replace_once(
                value,
                "        native_placement = self._repair_and_persist_native_position()\n        if os.name",
                "        native_placement = None\n        if os.name",
            ),
            "DRAG-CLAMP-OMITTED": lambda value: replace_once(
                value,
                "            placement = self._repair_and_persist_native_position()\n",
                "            placement = None\n",
            ),
            "RELAYOUT-CLAMP-OMITTED": lambda value: replace_once(
                value,
                "prepared = self._native_post_placement_clamp()",
                "prepared = None",
            ),
            "PERIODIC-REPAIR-OMITTED": lambda value: replace_once(
                value,
                "            self._refresh_display_state()\n",
                "            pass\n",
            ),
            "PERSIST-LOGICAL-INSTEAD-OF-NATIVE": lambda value: replace_once(
                value,
                "self._persist_native_position(result.x, result.y)",
                "self._persist_native_position(int(self.root.winfo_x()), int(self.root.winfo_y()))",
            ),
            "DISPLAY-CHANGE-IGNORED": lambda value: replace_once(
                value,
                "sampled_dpi = native_window_dpi(self._hwnd(self.foreground))",
                "sampled_dpi = self.dpi",
            ),
            "MONITOR-DISCONNECT-IGNORED": lambda value: replace_once(
                value,
                "monitor_from_window = user32.MonitorFromWindow",
                "monitor_from_window = user32.GetForegroundWindow",
            ),
        }
        self.assertEqual(set(mutators), V7_GEOMETRY_MUTANTS)
        for name, mutate in mutators.items():
            with self.subTest(mutant=name):
                mutated = mutate(source)
                self.assertNotEqual(mutated, source)
                self.assertIn(name, violations(mutated))

    def test_static_position_inventory_rejects_new_save_route(self) -> None:
        source_root = Path(inspect.getsourcefile(WidgetWindow) or "").parent
        wanted = {
            "save",
            "deiconify",
            "geometry",
            "SetWindowPos",
            "native_move_aligned_windows",
            "native_post_placement_clamp",
        }

        def inventory(sources: dict[str, str]) -> Counter[tuple[str, str, str]]:
            found: Counter[tuple[str, str, str]] = Counter()

            class Visitor(ast.NodeVisitor):
                def __init__(self, filename: str) -> None:
                    self.filename = filename
                    self.stack: list[str] = []

                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    self.stack.append(node.name)
                    self.generic_visit(node)
                    self.stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node: ast.Call) -> None:
                    name = (
                        node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else node.func.id if isinstance(node.func, ast.Name) else ""
                    )
                    if name in wanted:
                        found[(self.filename, self.stack[-1] if self.stack else "<module>", name)] += 1
                    self.generic_visit(node)

            for filename, source in sources.items():
                Visitor(filename).visit(ast.parse(source))
            return found

        sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in source_root.glob("*.py")
        }
        expected = Counter(
            {
                ("app.py", "main", "save"): 2,
                ("config.py", "load", "save"): 1,
                ("configure.py", "__init__", "geometry"): 1,
                ("configure.py", "grant_claude_quota_access", "save"): 1,
                ("configure.py", "persist_configuration", "save"): 2,
                ("demo.py", "main", "save"): 1,
                ("startup.py", "set_start_with_windows", "save"): 1,
                ("ui.py", "__init__", "deiconify"): 2,
                ("ui.py", "_commit_config", "save"): 2,
                ("ui.py", "_native_post_placement_clamp", "native_post_placement_clamp"): 1,
                ("ui.py", "_persist_native_position", "save"): 1,
                ("ui.py", "_set_window_pair_position", "geometry"): 4,
                ("ui.py", "_set_window_pair_position", "native_move_aligned_windows"): 1,
                ("ui.py", "_sync_z_order", "SetWindowPos"): 3,
                ("ui.py", "activate_from_shortcut", "deiconify"): 2,
            }
        )
        self.assertEqual(inventory(sources), expected)

        mutated = dict(sources)
        mutated["app.py"] += "\ndef _position_mutant(store, config):\n    store.save(config)\n"
        self.assertNotEqual(inventory(mutated), expected)


if __name__ == "__main__":
    unittest.main()
