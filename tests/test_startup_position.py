from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from limit_halo.config import AppConfig, ConfigStore
from limit_halo.ui import WidgetWindow, NativeClampResult, clamp_origin_to_work_area, compute_layout


class StartupPositionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=os.environ.get("LIMIT_HALO_TEST_ROOT"))
        self.addCleanup(self.folder.cleanup)

    def window(self, preferred=(1600, 900), scale=1.0):
        w = WidgetWindow.__new__(WidgetWindow)
        w.config_store = ConfigStore(Path(self.folder.name))
        w.config = AppConfig(x=preferred[0], y=preferred[1])
        w.config_store.save(w.config)
        w.layout = compute_layout("both-compact", scale)
        w.scale, w.dpi = scale, round(scale * 96)
        w.areas = ((0, 0, 1920, 1080),)
        w.origin = (10, 10)
        w._work_areas = lambda: w.areas
        w._native_pair_origin = lambda: w.origin

        def move(x, y):
            w.origin = (x, y)
            return True

        def clamp():
            area = min(w.areas, key=lambda a: abs(a[0] - w.origin[0]))
            x, y, contained = clamp_origin_to_work_area(*w.origin, w.layout.width, w.layout.height, area)
            moved = (x, y) != w.origin
            w.origin = (x, y)
            return NativeClampResult(x, y, area, moved, contained)

        w._set_window_pair_position = mock.Mock(side_effect=move)
        w._native_post_placement_clamp = clamp
        return w

    def test_boot_at_smaller_resolution_then_restores_saved_position(self):
        for scale in (1.0, 1.25, 1.5, 2.0):
            with self.subTest(scale=scale), mock.patch("limit_halo.ui.os.name", "nt"):
                w = self.window((1100, 700), scale)
                w.areas = ((0, 0, 1024, 720),)
                self.assertTrue(w._reconcile_preferred_position())
                self.assertNotEqual(w.origin, (1100, 700))
                self.assertEqual((w.config_store.load().x, w.config_store.load().y), (1100, 700))
                w.areas = ((0, 0, 1920, 1080),)
                self.assertTrue(w._reconcile_preferred_position())
                self.assertEqual(w.origin, (1100, 700))
                w._set_window_pair_position.reset_mock()
                self.assertTrue(w._reconcile_preferred_position())
                w._set_window_pair_position.assert_not_called()

    def test_missing_negative_monitor_returns_even_after_restart(self):
        with mock.patch("limit_halo.ui.os.name", "nt"):
            w = self.window((-1000, 300))
            self.assertTrue(w._reconcile_preferred_position())
            self.assertGreaterEqual(w.origin[0], 0)
            saved = ConfigStore(Path(self.folder.name)).load()
            restarted = self.window((saved.x, saved.y))
            restarted.areas = ((-1920, 0, 0, 1080), (0, 0, 1920, 1080))
            self.assertTrue(restarted._reconcile_preferred_position())
            self.assertEqual(restarted.origin, (-1000, 300))

    def test_drag_during_fallback_adopts_new_position_and_disk_failure_keeps_old(self):
        with mock.patch("limit_halo.ui.os.name", "nt"):
            w = self.window((-1000, 300))
            w._reconcile_preferred_position()
            self.assertTrue(w._persist_native_position(220, 180, remember=True))
            w.areas = ((-1920, 0, 0, 1080), (0, 0, 1920, 1080))
            self.assertTrue(w._reconcile_preferred_position())
            self.assertEqual(w.origin, (220, 180))
            with mock.patch.object(w.config_store, "save", side_effect=OSError("disk full")):
                self.assertFalse(w._persist_native_position(500, 400, remember=True))
            self.assertEqual((w.config.x, w.config.y), (220, 180))
            self.assertEqual(ConfigStore(Path(self.folder.name)).load(), w.config)

    def test_first_run_initializes_once_and_system_move_is_not_user_intent(self):
        with mock.patch("limit_halo.ui.os.name", "nt"):
            w = self.window((None, None))
            self.assertTrue(w._reconcile_preferred_position())
            preferred = (w.config.x, w.config.y)
            self.assertEqual(preferred, w.origin)
            w.origin = (0, 0)
            self.assertTrue(w._reconcile_preferred_position())
            self.assertEqual(w.origin, preferred)
            w.config = replace(w.config, x=600, y=200)
            w._set_window_pair_position.side_effect = lambda *_: False
            self.assertFalse(w._reconcile_preferred_position())
            self.assertEqual((w.config_store.load().x, w.config_store.load().y), preferred)
