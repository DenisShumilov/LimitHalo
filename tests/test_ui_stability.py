from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from limit_halo.config import AppConfig, ConfigStore
from limit_halo.configure import persist_configuration
from limit_halo.ui import WidgetWindow, NativeClampResult, compute_layout, merge_settings_changes
from limit_halo.ui_contract import production_g04_snapshot, validate_g04_snapshot


class UiStabilityTests(unittest.TestCase):
    def test_rejected_settings_cannot_read_or_change_startup(self):
        with tempfile.TemporaryDirectory(prefix="blocked-settings-") as folder:
            store = ConfigStore(Path(folder))
            rejected = b'{synthetic-invalid-existing'
            store.path.write_bytes(rejected)
            old = store.load()
            reader = mock.Mock(side_effect=AssertionError("startup read must not happen"))
            writer = mock.Mock(side_effect=AssertionError("startup write must not happen"))
            with self.assertRaises(OSError):
                persist_configuration(
                    store, old, replace(old, startWithWindows=True),
                    executable=Path("synthetic.exe"), portable=False,
                    startup_reader=reader, startup_writer=writer,
                )
            reader.assert_not_called()
            writer.assert_not_called()
            self.assertEqual(store.path.read_bytes(), rejected)
            self.assertEqual(list(Path(folder).iterdir()), [store.path])

    def window(self, *, offscreen=False):
        window = WidgetWindow.__new__(WidgetWindow)
        window.config = AppConfig(x=4, y=1374 if offscreen else 1308)
        window.config_store = mock.Mock()
        window.root = mock.Mock()
        window.foreground = mock.Mock()
        window.canvas = mock.Mock()
        window.root.state.return_value = window.foreground.state.return_value = "normal"
        window.layout = compute_layout("both-compact", 1.0)
        window.scale, window.dpi = 1.0, 96
        window._closing = False
        window._attention_generation = 0
        window._attention_topmost = False
        window._native_post_placement_clamp = mock.Mock(
            return_value=NativeClampResult(4, 1308, (0, 0, 5120, 1392), True, True)
        )
        window._current_layout = lambda config=None: compute_layout(
            "both-wide" if (config or window.config).displayPreset == "wide" else "both-compact", 1.0
        )
        window._set_window_pair_position = mock.Mock(return_value=True)
        window._apply_input_regions = mock.Mock()
        window._sync_z_order = mock.Mock()
        window._redraw = mock.Mock()
        window.on_config_changed = mock.Mock()
        return window

    def test_failed_relayout_preserves_preferred_coordinate_during_temporary_clamp(self):
        window = self.window(offscreen=True)
        saved = []
        window.config_store.save.side_effect = lambda value: saved.append(value)
        window.on_config_changed.side_effect = RuntimeError("synthetic apply failure")
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertFalse(window._commit_config(replace(window.config, displayPreset="wide")))
        self.assertEqual([(value.x, value.y) for value in saved], [(4, 1374)] * 2)
        self.assertEqual((window.config.x, window.config.y), (4, 1374))
        self.assertEqual(window.layout.mode, "both-compact")

    def test_same_layout_stale_dialog_cannot_restore_old_position(self):
        window = self.window()
        stale = replace(window.config, x=900, y=25, colorMode="status")
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertTrue(window._commit_config(stale))
        self.assertEqual((window.config.x, window.config.y), (4, 1308))
        self.assertEqual(window.config.colorMode, "status")

    def test_settings_apply_only_edited_fields_and_keep_live_metadata(self):
        opened = AppConfig(x=10, y=20)
        current = replace(opened, x=200, y=300, topmost=False, claudeConsent="granted")
        edited = replace(opened, locale="uk-UA")
        self.assertEqual(merge_settings_changes(current, opened, edited), replace(current, locale="uk-UA"))

    def test_rollback_restores_geometry_before_persisting_old_settings(self):
        window = self.window()
        states = []
        window.config_store.save.side_effect = lambda value: states.append((value.displayPreset, window.layout.mode))
        window.on_config_changed.side_effect = ValueError("synthetic callback")
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertFalse(window._commit_config(replace(window.config, displayPreset="wide")))
        self.assertEqual(states, [("wide", "both-wide"), ("compact", "both-compact")])

    def test_failed_initial_settings_save_is_not_followed_by_an_overwrite(self):
        window = self.window()
        window.config_store.save.side_effect = OSError("synthetic full disk")
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertFalse(window._commit_config(replace(window.config, colorMode="status")))
        self.assertEqual(window.config_store.save.call_count, 1)
        window.on_config_changed.assert_not_called()

    def test_failed_rollback_save_keeps_safe_session_position(self):
        window = self.window()
        window.config_store.save.side_effect = [None, OSError("synthetic rollback disk failure")]
        window.on_config_changed.side_effect = RuntimeError("synthetic callback")
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertFalse(window._commit_config(replace(window.config, displayPreset="wide")))
        self.assertEqual((window.config.x, window.config.y, window.layout.mode), (4, 1308, "both-compact"))

    def test_read_only_settings_allow_safe_session_position_without_writing(self):
        window = self.window(offscreen=True)
        window.config_store.write_blocked = True
        with mock.patch("limit_halo.ui.os.name", "nt"):
            self.assertIsNotNone(window._repair_and_persist_native_position())
            self.assertFalse(window._commit_config(replace(window.config, colorMode="status")))
        self.assertEqual((window.config.x, window.config.y), (4, 1374))
        window.config_store.save.assert_not_called()

    def test_healthy_activation_does_not_relocate_or_remap_windows(self):
        window = self.window()
        window._hwnd = mock.Mock(return_value=100)
        with mock.patch("limit_halo.ui.os.name", "nt"), mock.patch("limit_halo.ui.ctypes.WinDLL"):
            window.activate_from_shortcut()
        window._set_window_pair_position.assert_not_called()
        window.config_store.save.assert_not_called()
        window.root.deiconify.assert_not_called()
        window.foreground.deiconify.assert_not_called()

    def test_retained_text_item_is_created_once_for_two_hundred_updates(self):
        window = self.window()
        window._scene_items, window._scene_options = {}, {}
        window._active_scene_keys = set()
        window.canvas.create_text.return_value = 11
        for value in range(200):
            self.assertEqual(window._scene_item("metric-0-value", "text", 41, 31, text=str(value)), 11)
        window.canvas.create_text.assert_called_once()
        window.canvas.delete.assert_not_called()

    def test_refresh_has_no_full_canvas_or_icon_teardown(self):
        for method in (WidgetWindow._redraw, WidgetWindow._draw_rear):
            source = inspect.getsource(method)
            self.assertNotIn('.delete("all")', source)
            self.assertNotIn("_image_refs.clear", source)
            self.assertNotIn("withdraw(", source)
            self.assertNotIn("deiconify(", source)
        self.assertIn("_icon_cache", inspect.getsource(WidgetWindow._load_icon))

    def test_transient_render_failure_does_not_cancel_future_ticks(self):
        window = self.window()
        window._drag_origin = None
        window._refresh_display_state = mock.Mock(return_value=True)
        window._redraw.side_effect = RuntimeError("synthetic transient display error")
        window._tick()
        window.root.after.assert_called_once_with(1000, window._tick)
        window.root.withdraw.assert_not_called()

    def test_native_snapshot_negative_controls_are_not_ignored(self):
        for field, bad in (("windowCount", 1), ("alignedPhysicalRectangles", False), ("rearAlphaByte", 255)):
            value = production_g04_snapshot()
            self.assertEqual(validate_g04_snapshot(value), ())
            value["native"][field] = bad
            self.assertIn("NATIVE-STATE-CHANGED", validate_g04_snapshot(value))


if __name__ == "__main__":
    unittest.main()
