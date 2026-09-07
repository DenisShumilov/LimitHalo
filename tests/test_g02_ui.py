from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import os
import tempfile
import tkinter as tk
import unicodedata
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from limit_halo import demo as fixture_demo
from limit_halo.config import AppConfig
from limit_halo.configure import build_config_from_answers, settings_surface
from limit_halo.localization import STRINGS, assert_translation_parity, tr
from limit_halo.model import Metric, MetricStore, ProviderState
from limit_halo.native_menu import build_context_menu, flatten_actions
from limit_halo.notifications import NotificationDecider, in_quiet_hours, notification_text
from limit_halo.ui import (
    ACTION_BLUE,
    BASE_CORNER_DIAMETER,
    DRAG_THRESHOLD_DIP,
    FOREGROUND_ALPHA_BYTE,
    MODE_SPECS,
    REAR_ALPHA_BYTE,
    TRANSPARENT_KEY,
    WidgetWindow,
    action_lane_right,
    attention_position,
    compute_layout,
    drag_distance_dip,
    geometry_string,
    hit_contour_contains,
    layout_mode,
    metric_color,
    metric_visible_text,
    place_recovery_action,
    recover_position,
    screen_pointer_position,
)
from limit_halo.ui_contract import production_ui_contract, validate_ui_contract


ROOT = Path(__file__).resolve().parents[1]


class G02UiContractTests(unittest.TestCase):
    def test_exact_modes_anchors_and_dpi_rounding(self) -> None:
        expected = {
            "both-compact": ((364, 84), ((20, 42), (124, 42)), (41, 145, 217, 289)),
            "both-wide": ((436, 84), ((20, 42), (196, 42)), (41, 113, 217, 289, 361)),
            "claude-only": ((260, 84), ((20, 42),), (41, 113, 185)),
            "codex-one-window": ((124, 84), ((20, 42),), (41,)),
            "codex-two-window": ((196, 84), ((20, 42),), (41, 113)),
        }
        for mode, (size, icons, text_x) in expected.items():
            spec = MODE_SPECS[mode]
            self.assertEqual(spec.size, size)
            self.assertEqual(tuple((x, y) for _provider, x, y in spec.icon_anchors), icons)
            self.assertEqual(spec.text_anchors, text_x)
            for dpi in (96, 120, 144, 192):
                scale = dpi / 96
                layout = compute_layout(mode, scale)
                self.assertEqual((layout.width, layout.height), tuple(round(value * scale) for value in size))
                self.assertEqual(layout.text_anchors, tuple(round(value * scale) for value in text_x))

    def test_mode_resolution_has_no_disabled_provider_gap(self) -> None:
        self.assertEqual(layout_mode("compact", codex_visible=True, claude_visible=True, codex_window_count=2), "both-compact")
        self.assertEqual(layout_mode("wide", codex_visible=True, claude_visible=True, codex_window_count=2), "both-wide")
        self.assertEqual(layout_mode("compact", codex_visible=False, claude_visible=True, codex_window_count=0), "claude-only")
        self.assertEqual(layout_mode("compact", codex_visible=True, claude_visible=False, codex_window_count=2), "codex-one-window")
        self.assertEqual(layout_mode("wide", codex_visible=True, claude_visible=False, codex_window_count=2), "codex-two-window")
        self.assertIsNone(layout_mode("compact", codex_visible=False, claude_visible=False, codex_window_count=0))

    def test_default_native_properties_assets_and_current_look_constants(self) -> None:
        self.assertEqual((REAR_ALPHA_BYTE, FOREGROUND_ALPHA_BYTE, TRANSPARENT_KEY), (1, 255, "#010203"))
        assets = ROOT / "src" / "limit_halo" / "assets"
        expected = {
            "app.ico": "0fa0239e5e4d0dc347eee1bbe6a953654d805bb52d2bbc9e54bbb27f019a1284",
            "chatgpt.png": "375016542ebe78b63c7f36b8f6aee3058f2b706d861e5153fd74f9c27f63b8b7",
            "claude.png": "4dbf0aa77200058e8cb152e990597a95a99963db85632bf8abd8343a249e898e",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((assets / name).read_bytes()).hexdigest(), digest)
        source = inspect.getsource(WidgetWindow._apply_layered_attributes)
        self.assertIn("REAR_ALPHA_BYTE", source)
        self.assertIn("FOREGROUND_ALPHA_BYTE", source)
        self.assertIn("TRANSPARENT_KEY", source)
        region_source = inspect.getsource(WidgetWindow._make_region)
        self.assertEqual(BASE_CORNER_DIAMETER, 36)
        self.assertIn("self.layout.width + 1", region_source)
        self.assertIn("self.layout.height + 1", region_source)
        self.assertIn("self.layout.hit_rects", region_source)
        self.assertIn("CombineRgn", region_source)

    def test_frozen_fixture_text_is_remaining_not_used_and_has_exact_suffixes(self) -> None:
        now = datetime.fromtimestamp(2_000_000_000).astimezone()
        codex = Metric("codex_10080", "codex", 10_080, 48, int(now.timestamp()) + 86_400, ProviderState.READY, now.timestamp())
        claude = Metric("claude_300", "claude", 300, 38, int(now.timestamp()) + 13_440, ProviderState.READY, now.timestamp())
        self.assertEqual(metric_visible_text(codex, "uk", now)[0], "52%·7д")
        self.assertEqual(metric_visible_text(codex, "en", now)[0], "52%·7d")
        self.assertEqual(metric_visible_text(claude, "uk", now), ("62%·5г", "3г44хв"))
        self.assertEqual(metric_visible_text(claude, "en", now), ("62%·5h", "3h44m"))

    def test_exhausted_and_not_provided_are_truthfully_distinct_in_both_locales(self) -> None:
        now = datetime.fromtimestamp(2_000_000_000).astimezone()
        reset = int(now.timestamp()) + 3_600
        exhausted = Metric(
            "codex_300", "codex", 300, 100, reset, ProviderState.READY, now.timestamp()
        )
        not_provided = Metric(
            "claude_fable_10080",
            "claude",
            10_080,
            None,
            None,
            ProviderState.UNAVAILABLE,
            now.timestamp(),
        )
        for language, suffix, missing_text in (
            ("en", "5h", "not provided"),
            ("uk", "5г", "не надано"),
        ):
            with self.subTest(language=language):
                exhausted_text = metric_visible_text(exhausted, language, now)
                self.assertEqual(exhausted_text[0], f"0%·{suffix}")
                self.assertEqual(
                    exhausted_text[1],
                    datetime.fromtimestamp(reset).astimezone().strftime("%d.%m·%H:%M"),
                )
                self.assertEqual(metric_visible_text(not_provided, language, now), ("—", missing_text))

        production_text = "\n".join(
            text.casefold()
            for translations in STRINGS.values()
            for text in translations.values()
        )
        self.assertNotIn("no quota", production_text)
        self.assertNotIn("немає квоти", production_text)

    def test_detected_providers_use_refreshing_until_the_first_snapshot(self) -> None:
        now = 2_000_000_000.0
        initial_claude = tuple(
            Metric(
                key,
                "claude",
                300 if key == "claude_300" else 10_080,
                None,
                None,
                ProviderState.UNAVAILABLE,
                now,
            )
            for key in ("claude_300", "claude_10080", "claude_fable_10080")
        )
        window = WidgetWindow.__new__(WidgetWindow)
        window.config = AppConfig(
            codexMode="auto", claudeMode="auto", claudeConsent="granted"
        )
        window.store = mock.Mock()
        window.store.snapshot.return_value = ((), initial_claude)
        window.store.ever_succeeded.return_value = False
        window.provider_present = mock.Mock(return_value=True)

        metrics, codex_visible, claude_visible = window._current_metrics()

        self.assertTrue(codex_visible)
        self.assertTrue(claude_visible)
        self.assertEqual(len(metrics), 4)
        self.assertTrue(all(metric.state is ProviderState.REFRESHING for metric in metrics))
        for language in ("en", "uk"):
            self.assertTrue(
                all(
                    metric_visible_text(metric, language)[1] == STRINGS[language]["refreshing"]
                    for metric in metrics
                )
            )

    def test_monochrome_default_and_optional_status_colors(self) -> None:
        ready = Metric("codex_300", "codex", 300, 91, 2_000_010_000, ProviderState.READY, 2_000_000_000)
        offline = Metric("codex_300", "codex", 300, 91, 2_000_010_000, ProviderState.OFFLINE, 2_000_000_000)
        self.assertEqual(AppConfig().colorMode, "monochrome")
        self.assertEqual(metric_color(ready, "monochrome"), "#ffffff")
        self.assertNotEqual(metric_color(ready, "status"), "#ffffff")
        self.assertEqual(metric_color(offline, "monochrome"), "#aeb6c2")
        self.assertEqual(ACTION_BLUE, "#60a5fa")

    def test_whole_contour_hit_testing_and_exclusive_five_dip_threshold(self) -> None:
        layout = compute_layout("both-compact", 1.0)
        for point in ((2, 28), (20, 42), (110, 42), (124, 42), (300, 42), (362, 42)):
            self.assertTrue(hit_contour_contains(layout, *point), point)
        for point in ((0, 0), (1, 12), (363, 83), (200, 5)):
            self.assertFalse(hit_contour_contains(layout, *point), point)
        self.assertEqual(DRAG_THRESHOLD_DIP, 5.0)
        self.assertEqual(drag_distance_dip(3, 4, 96), 5.0)
        self.assertGreater(drag_distance_dip(4, 4, 96), 5.0)
        source = inspect.getsource(WidgetWindow._drag_move)
        self.assertIn("<= DRAG_THRESHOLD_DIP", source)
        self.assertIn("self._armed_action = None", source)

    def test_native_menu_recomputes_real_checks_and_has_complete_actions(self) -> None:
        for locale in ("en-US", "uk-UA"):
            with self.subTest(locale=locale):
                config = AppConfig(locale=locale, colorMode="status")
                entries = build_context_menu(
                    config, actual_topmost=False, actual_startup=True
                )
                self.assertEqual(
                    flatten_actions(entries),
                    ("refresh", "colors:toggle", "check-updates", "startup", "topmost", "configure", "exit"),
                )
                flat = [item for item in entries if item.action is not None]
                self.assertTrue(next(item for item in flat if item.action == "colors:toggle").checked)
                self.assertTrue(next(item for item in flat if item.action == "startup").checked)
                self.assertFalse(next(item for item in flat if item.action == "topmost").checked)
                joined = " ".join(item.label for item in flat)
                for forbidden in ("Provider", "Постачальник"):
                    self.assertNotIn(forbidden, joined)

    def test_progressive_settings_surface_has_only_detected_and_needed_controls(self) -> None:
        fresh = settings_surface(AppConfig(), codex_present=False, claude_present=False)
        self.assertEqual(fresh.tabs, ("settings", "help"))
        self.assertEqual(fresh.provider_rows, ())
        self.assertIsNone(fresh.claude_access_action)
        self.assertEqual(fresh.alert_details, ())
        self.assertFalse(fresh.advanced_visible)
        self.assertEqual(fresh.daily_duplicates, ())
        self.assertEqual(fresh.help_actions, ("copy-ai-repair-report", "privacy-about"))

        codex = settings_surface(AppConfig(), codex_present=True, claude_present=False)
        self.assertEqual(codex.provider_rows, ("codex",))
        claude_connect = settings_surface(AppConfig(), codex_present=False, claude_present=True)
        self.assertEqual(claude_connect.provider_rows, ("claude",))
        self.assertEqual(claude_connect.claude_access_action, "connect")
        claude_revoke = settings_surface(
            AppConfig(claudeConsent="granted"), codex_present=False, claude_present=True
        )
        self.assertEqual(claude_revoke.claude_access_action, "revoke")
        claude_reconnect = settings_surface(
            AppConfig(claudeConsent="denied"), codex_present=False, claude_present=True
        )
        self.assertEqual(claude_reconnect.claude_access_action, "connect")

        alerts = settings_surface(
            AppConfig(alertsEnabled=True), codex_present=True, claude_present=True
        )
        self.assertEqual(alerts.alert_details, ("threshold", "quiet-hours"))
        legacy = settings_surface(
            AppConfig(displayPreset="wide"), codex_present=False, claude_present=False
        )
        self.assertTrue(legacy.advanced_visible)

    def test_notifications_are_off_by_default_quiet_and_deduped_by_reset_cycle(self) -> None:
        now = datetime(2026, 8, 13, 12, 0).astimezone()
        metric = Metric("codex_300", "codex", 300, 85, int(now.timestamp()) + 1_000, ProviderState.READY, now.timestamp())
        decider = NotificationDecider()
        self.assertEqual(decider.evaluate(AppConfig(), (metric,), now=now), ())
        enabled = AppConfig(alertsEnabled=True, alertThresholdPercent=20)
        first = decider.evaluate(enabled, (metric,), now=now)
        self.assertEqual(len(first), 1)
        title, message = notification_text(first[0], "en")
        self.assertIn("Codex · 5-hour", message)
        self.assertNotIn("daily", (title + message).casefold())
        self.assertEqual(decider.evaluate(enabled, (metric,), now=now), ())
        recovered = Metric("codex_300", "codex", 300, 70, metric.resets_at, ProviderState.READY, now.timestamp())
        self.assertEqual(decider.evaluate(enabled, (recovered,), now=now), ())
        self.assertEqual(len(decider.evaluate(enabled, (metric,), now=now)), 1)
        next_cycle = Metric("codex_300", "codex", 300, 85, metric.resets_at + 10_000, ProviderState.READY, now.timestamp())
        self.assertEqual(len(decider.evaluate(enabled, (next_cycle,), now=now)), 1)
        quiet = AppConfig(alertsEnabled=True, quietHoursEnabled=True, quietStartMinutes=11 * 60, quietEndMinutes=13 * 60)
        self.assertEqual(NotificationDecider().evaluate(quiet, (metric,), now=now), ())
        self.assertTrue(in_quiet_hours(23 * 60, 22 * 60, 8 * 60))
        self.assertFalse(in_quiet_hours(12 * 60, 22 * 60, 8 * 60))

    def test_position_recovery_and_negative_geometry_are_deterministic(self) -> None:
        areas = ((0, 0, 1920, 1080),)
        self.assertEqual(recover_position(100, 100, 364, 84, areas, scale=1.0), (100, 100))
        self.assertEqual(recover_position(5000, 5000, 364, 84, areas, scale=1.0), (1532, 24))
        self.assertEqual(geometry_string(364, 84, -100, -20), "364x84-100-20")

    def test_shortcut_activation_places_widget_beside_pointer_inside_its_work_area(self) -> None:
        areas = ((-1920, 0, 0, 1080), (0, 0, 5120, 1392))
        self.assertEqual(attention_position(2500, 700, 124, 84, areas, scale=1.0), (2524, 658))
        self.assertEqual(attention_position(5100, 20, 124, 84, areas, scale=1.0), (4952, 24))
        self.assertEqual(attention_position(-1910, 1050, 364, 84, areas, scale=1.0), (-1886, 972))

    def test_shortcut_activation_uses_native_physical_cursor_coordinates(self) -> None:
        class RootThatMustNotBeUsed:
            def winfo_pointerx(self) -> int:
                raise AssertionError("Tk fallback was used")

            def winfo_pointery(self) -> int:
                raise AssertionError("Tk fallback was used")

        class GetCursorPos:
            argtypes = None
            restype = None

            def __call__(self, pointer: object) -> int:
                pointer._obj.x = 2560
                pointer._obj.y = 696
                return 1

        user32 = mock.Mock()
        user32.GetCursorPos = GetCursorPos()
        self.assertEqual(
            screen_pointer_position(RootThatMustNotBeUsed(), user32=user32),
            (2560, 696),
        )

        areas = ((-1920, 0, 0, 1080), (0, 0, 5120, 1392))
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
        window.config_store = mock.Mock()
        window._work_areas = mock.Mock(return_value=areas)
        window._sync_z_order = mock.Mock()
        window.root.winfo_x.return_value = 2524
        window.root.winfo_y.return_value = 658

        with (
            mock.patch("limit_halo.ui.screen_pointer_position", return_value=(2500, 700)) as pointer_position,
            mock.patch("limit_halo.ui.os.name", "posix"),
        ):
            window.activate_from_shortcut()

        pointer_position.assert_not_called()
        self.assertEqual(window._work_areas.call_count, 2)
        window.root.geometry.assert_not_called()
        window.foreground.geometry.assert_not_called()
        window.config_store.save.assert_called_once()
        committed = window.config_store.save.call_args.args[0]
        self.assertEqual((committed.x, committed.y), (2524, 658))
        self.assertEqual(window._attention_generation, 1)

        source = inspect.getsource(WidgetWindow.activate_from_shortcut)
        self.assertNotIn("screen_pointer_position(self.root)", source)
        self.assertIn("_repair_and_persist_native_position", source)
        self.assertNotIn("winfo_pointer", source)

    def test_native_cursor_failure_falls_back_to_tk_coordinates(self) -> None:
        root = mock.Mock()
        root.winfo_pointerx.return_value = -320
        root.winfo_pointery.return_value = 840
        user32 = mock.Mock()
        user32.GetCursorPos.return_value = 0
        self.assertEqual(screen_pointer_position(root, user32=user32), (-320, 840))
        root.winfo_pointerx.assert_called_once_with()
        root.winfo_pointery.assert_called_once_with()

    def test_contract_mutants_fail(self) -> None:
        self.assertEqual(validate_ui_contract(production_ui_contract()), ())
        cases = (
            (lambda value: value["sizes"]["both-compact"].__setitem__(0, 365), "geometry"),
            (lambda value: value["anchors"]["both-compact"]["textX"].__setitem__(0, 42), "anchor"),
            (lambda value: value["native"].__setitem__("rearAlphaByte", 0), "native-window"),
            (lambda value: value["interaction"].__setitem__("ordinaryClick", "popup"), "click-popup"),
            (lambda value: value["interaction"].__setitem__("dragThresholdExclusive", False), "drag-threshold"),
            (lambda value: value["defaults"].__setitem__("alerts", True), "defaults"),
            (lambda value: value["menuActions"].pop(), "menu"),
            (lambda value: value["menuActions"].append("diagnostics"), "menu"),
            (lambda value: value["translations"]["en"].__setitem__("app_name", "Codex · daily"), "daily-label"),
        )
        for mutate, expected in cases:
            mutant = copy.deepcopy(production_ui_contract())
            mutate(mutant)
            self.assertIn(expected, validate_ui_contract(mutant))

    def test_no_ordinary_popup_and_events_are_bound_once(self) -> None:
        source = inspect.getsource(WidgetWindow._bind_actions)
        self.assertNotIn("<Double-1>", source)
        self.assertNotIn("_show_settings", source)
        self.assertIn("<ButtonPress-1>", source)
        self.assertIn("<Button-3>", source)
        self.assertIn("<Shift-F10>", source)

    def test_claude_setup_is_explicit_and_provider_free_answers_remain_bounded(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window.config = AppConfig(claudeMode="enabled", claudeConsent="not-asked")
        metric = Metric("claude_300", "claude", 300, None, None, ProviderState.UNAVAILABLE, 2_000_000_000)
        self.assertEqual(window._provider_action("claude", (metric,)), "claude:setup")
        window.config = AppConfig(claudeMode="auto", claudeConsent="granted")
        ready = Metric("claude_300", "claude", 300, 2, 2_000_010_000, ProviderState.READY, 2_000_000_000)
        optional = Metric("claude_fable_10080", "claude", 10_080, None, None, ProviderState.UNAVAILABLE, 2_000_000_000)
        self.assertIsNone(window._provider_action("claude", (ready, optional)))
        configured = build_config_from_answers(
            AppConfig(), locale="en-US", claude_mode="enabled", codex_mode="disabled",
            claude_access=True, view="wide", colors="status", alerts=True,
            threshold=10, quiet_hours=True, quiet_start="23:00", quiet_end="07:30", startup=False,
        )
        self.assertEqual((configured.locale, configured.claudeMode, configured.codexMode), ("en-US", "enabled", "disabled"))
        self.assertTrue(configured.claudeConsentCompleted)
        self.assertEqual((configured.displayPreset, configured.colorMode, configured.alertThresholdPercent), ("wide", "status", 10))
        self.assertEqual((configured.quietHoursEnabled, configured.quietStartMinutes, configured.quietEndMinutes), (True, 23 * 60, 7 * 60 + 30))
        untouched = build_config_from_answers(
            AppConfig(), locale="en-US", claude_mode="auto", codex_mode="auto",
            claude_access=False, view="compact", colors="monochrome", alerts=False,
            threshold=20, quiet_hours=False, quiet_start="22:00", quiet_end="08:00", startup=False,
        )
        self.assertEqual(untouched.claudeConsent, "not-asked")

    def test_claude_setup_action_opens_the_direct_consent_surface(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window.config = AppConfig(claudeMode="auto", claudeConsent="not-asked")
        window._request_claude_consent = mock.Mock()
        window._open_configure = mock.Mock()
        window._invoke_action("claude:setup")
        window._request_claude_consent.assert_called_once_with()
        window._open_configure.assert_not_called()

    def test_declining_the_only_auto_claude_lane_does_not_redraw_an_empty_hud(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window.config = AppConfig(codexMode="disabled", claudeMode="auto", claudeConsent="not-asked")
        window.foreground = mock.Mock()
        window.config_store = mock.Mock()
        window.store = mock.Mock()
        window.store.ever_succeeded.return_value = False
        window.provider_present = mock.Mock(return_value=False)
        window.on_config_changed = mock.Mock()
        window._redraw = mock.Mock(side_effect=RuntimeError("providerless redraw"))
        window._show_settings_failure = mock.Mock()
        with mock.patch("tkinter.messagebox.askyesno", return_value=False):
            window._request_claude_consent()
        self.assertEqual(window.config.claudeConsent, "denied")
        window.config_store.save.assert_called_once_with(window.config)
        window.on_config_changed.assert_called_once()
        window._redraw.assert_not_called()
        window._show_settings_failure.assert_not_called()

    def test_non_ready_values_are_specific_and_never_generic_na(self) -> None:
        expected = {
            "en": {
                ProviderState.REFRESHING: "Refreshing…",
                ProviderState.AUTHORIZATION_REQUIRED: "auth",
                ProviderState.OFFLINE: "offline",
                ProviderState.PROVIDER_ERROR: "not updated",
                ProviderState.UNAVAILABLE: "not provided",
                ProviderState.DISABLED: "Claude is off",
            },
            "uk": {
                ProviderState.REFRESHING: "Оновлюємо…",
                ProviderState.AUTHORIZATION_REQUIRED: "вхід",
                ProviderState.OFFLINE: "офлайн",
                ProviderState.PROVIDER_ERROR: "не оновлено",
                ProviderState.UNAVAILABLE: "не надано",
                ProviderState.DISABLED: "Claude вимкнено",
            },
        }
        for language in ("en", "uk"):
            for state, expected_subline in expected[language].items():
                with self.subTest(language=language, state=state.value):
                    metric = Metric("claude_300", "claude", 300, None, None, state, 2_000_000_000)
                    value, subline = metric_visible_text(metric, language)
                    self.assertEqual(value, "—")
                    self.assertEqual(subline, expected_subline)

        for language, missing in (("en", "not provided"), ("uk", "не надано")):
            ready_without_reset = Metric(
                "claude_300",
                "claude",
                300,
                5,
                None,
                ProviderState.READY,
                2_000_000_000,
            )
            self.assertEqual(
                metric_visible_text(ready_without_reset, language),
                ("95%·5h" if language == "en" else "95%·5г", missing),
            )

        with self.assertRaisesRegex(ValueError, "provider state"):
            Metric("claude_300", "claude", 300, 5, None, "unknown", 2_000_000_000)  # type: ignore[arg-type]

        production_text = " ".join(
            unicodedata.normalize("NFKC", text).casefold()
            for catalog in STRINGS.values()
            for text in catalog.values()
        )
        generic_aliases = (
            "needs attention",
            "потрібна увага",
            "action required",
            "check status",
        )
        for generic in generic_aliases:
            self.assertNotIn(generic, production_text)
        for language, value in (("en", "ＮＥＥＤＳ ATTENTION"), ("uk", "ПОТРІБНА УВАГА")):
            mutant = copy.deepcopy(STRINGS)
            mutant[language]["provider_error_short"] = value
            mutated_text = " ".join(
                unicodedata.normalize("NFKC", text).casefold()
                for catalog in mutant.values()
                for text in catalog.values()
            )
            self.assertTrue(any(alias in mutated_text for alias in generic_aliases))
        ui_source = inspect.getsource(WidgetWindow)
        self.assertNotIn(":help", ui_source)
        self.assertNotIn("_show_recovery_help", ui_source)

        catalog = json.loads(
            (ROOT / "tests" / "mutants" / "mutant-catalog.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(catalog["V7-G01"]),
            {
                "GENERIC-ATTENTION-EN",
                "GENERIC-ATTENTION-UK",
                "SEMANTIC-GENERIC-ALIAS",
                "CONSTRUCTED-GENERIC",
                "UNICODE-CASE-GENERIC",
                "UNKNOWN-STATE",
                "LOCALIZATION-FALLBACK",
                "HIGHER-LIMIT-UNAVAILABLE",
                "LAST-GOOD-DROPPED",
                "ZERO-AS-UNAVAILABLE",
                "GOOD-MALFORMED-GOOD-BROKEN",
            },
        )

    def test_every_named_g01_control_is_executed(self) -> None:
        catalog = json.loads(
            (ROOT / "tests" / "mutants" / "mutant-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        expected = set(catalog["V7-G01"])
        executed: set[str] = set()

        aliases = tuple(
            "".join(
                character
                for character in unicodedata.normalize("NFKC", text).casefold()
                if character.isalnum()
            )
            for text in (
                "needs attention",
                "потрібна увага",
                "action required",
                "check status",
            )
        )

        def contains_generic(text: str) -> bool:
            normalized = "".join(
                character
                for character in unicodedata.normalize("NFKC", text).casefold()
                if character.isalnum()
            )
            return any(alias in normalized for alias in aliases)

        production_surfaces = []
        for path in sorted((ROOT / "src" / "limit_halo").glob("*.py")):
            production_surfaces.append(path.read_text(encoding="utf-8"))
        for pattern in ("*.cpp", "*.h"):
            for path in sorted((ROOT / "native").rglob(pattern)):
                production_surfaces.append(path.read_text(encoding="utf-8"))
        self.assertFalse(contains_generic("\n".join(production_surfaces)))

        alias_mutants = {
            "GENERIC-ATTENTION-EN": "Needs attention",
            "GENERIC-ATTENTION-UK": "Потрібна увага",
            "SEMANTIC-GENERIC-ALIAS": "check-status",
            "CONSTRUCTED-GENERIC": "needs" + " attention",
            "UNICODE-CASE-GENERIC": "ＮＥＥＤＳ ATTENTION",
        }
        for name, text in alias_mutants.items():
            with self.subTest(control=name):
                self.assertTrue(contains_generic(text))
                executed.add(name)

        with self.assertRaisesRegex(ValueError, "provider state"):
            Metric(
                "claude_300",
                "claude",
                300,
                1,
                None,
                "future-state",  # type: ignore[arg-type]
                2_000_000_000.0,
            )
        executed.add("UNKNOWN-STATE")
        with self.assertRaisesRegex(KeyError, "missing translation"):
            tr("future-locale", "provider_error_short")
        executed.add("LOCALIZATION-FALLBACK")

        now = 2_000_000_000.0
        store = MetricStore(now=now)

        def snapshot(session_used: int, observed: float) -> tuple[Metric, ...]:
            return (
                Metric("claude_300", "claude", 300, session_used, None, ProviderState.READY, observed),
                Metric("claude_10080", "claude", 10_080, 50, int(now) + 500_000, ProviderState.READY, observed),
                Metric("claude_fable_10080", "claude", 10_080, None, None, ProviderState.UNAVAILABLE, observed),
            )

        self.assertTrue(store.update_claude(snapshot(75, now), sequence=1))
        self.assertTrue(store.update_claude(snapshot(10, now + 1), sequence=2))
        _codex, increased = store.snapshot(now=now + 1)
        self.assertEqual(metric_visible_text(increased[0], "en")[0], "90%·5h")
        executed.add("HIGHER-LIMIT-UNAVAILABLE")

        store.mark_provider(
            "claude",
            ProviderState.PROVIDER_ERROR,
            now=now + 2,
            error_code="synthetic_malformed",
        )
        _codex, retained = store.snapshot(now=now + 2)
        self.assertEqual(retained[0].used_percent, 10)
        self.assertEqual(metric_visible_text(retained[0], "en")[0], "90%·5h")
        executed.add("LAST-GOOD-DROPPED")

        exhausted = Metric(
            "claude_300",
            "claude",
            300,
            100,
            None,
            ProviderState.READY,
            now + 3,
        )
        self.assertEqual(metric_visible_text(exhausted, "en"), ("0%·5h", "not provided"))
        executed.add("ZERO-AS-UNAVAILABLE")

        self.assertTrue(store.update_claude(snapshot(5, now + 3), sequence=3))
        _codex, recovered = store.snapshot(now=now + 3)
        self.assertIs(recovered[0].state, ProviderState.READY)
        self.assertEqual(metric_visible_text(recovered[0], "en")[0], "95%·5h")
        executed.add("GOOD-MALFORMED-GOOD-BROKEN")

        self.assertEqual(executed, expected)

    def test_config_is_durable_before_consent_side_effects(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        old = AppConfig(claudeMode="auto", claudeConsent="not-asked")
        new = AppConfig(claudeMode="auto", claudeConsent="granted")
        window.config = old
        events: list[str] = []
        window.config_store = mock.Mock()
        window.config_store.save.side_effect = lambda value: events.append(f"save:{value.claudeConsent}")
        window.on_config_changed = lambda _old, value: events.append(f"apply:{value.claudeConsent}")
        self.assertTrue(window._commit_config(new))
        self.assertEqual(events[:2], ["save:granted", "apply:granted"])
        self.assertEqual(window.config, new)

        blocked = WidgetWindow.__new__(WidgetWindow)
        blocked.config = old
        blocked.config_store = mock.Mock()
        blocked.config_store.save.side_effect = OSError("fixture write failure")
        blocked.on_config_changed = mock.Mock()
        self.assertFalse(blocked._commit_config(new))
        blocked.on_config_changed.assert_not_called()

    def test_inline_status_and_action_are_separate_draw_items(self) -> None:
        source = inspect.getsource(WidgetWindow._redraw)
        self.assertIn("action_item = self._scene_item", source)
        self.assertIn('f"metric-{index}-action", "text"', source)
        self.assertIn('f"metric-{index}-reset", "text"', source)
        self.assertEqual(source.count("place_recovery_action("), 1)
        self.assertNotIn('if metric.provider == "codex":', source)
        helper = inspect.getsource(place_recovery_action)
        self.assertIn("canvas.bbox(status_item)", helper)
        self.assertIn("canvas.bbox(action_item)", helper)
        self.assertNotIn("subline = tr(self.config.language, label_key)", source)

    def test_compact_recovery_action_fits_inside_canvas(self) -> None:
        root = tk.Tk()
        root.withdraw()
        old_overflow_observed = False
        try:
            states = (
                (ProviderState.AUTHORIZATION_REQUIRED, "sign_in"),
                (ProviderState.OFFLINE, "retry"),
                (ProviderState.PROVIDER_ERROR, "retry"),
                (ProviderState.UNAVAILABLE, "setup"),
            )
            for dpi in (96, 120, 144, 192):
                scale = dpi / 96
                layout = compute_layout("codex-one-window", scale)
                self.assertEqual((layout.logical_width, layout.logical_height), (124, 84))
                self.assertEqual(
                    (layout.text_anchors[0], layout.value_y, layout.reset_y),
                    tuple(round(value * scale) for value in (41, 31, 56)),
                )
                self.assertEqual(
                    (layout.value_font_size, layout.reset_font_size),
                    (max(15, round(17 * scale)), max(11, round(12 * scale))),
                )
                for language in ("en", "uk"):
                    ready_now = datetime.fromtimestamp(2_000_000_000).astimezone()
                    ready = Metric(
                        "codex_10080", "codex", 10_080, 48,
                        int(ready_now.timestamp()) + 86_400,
                        ProviderState.READY, ready_now.timestamp(),
                    )
                    self.assertEqual(
                        metric_visible_text(ready, language, ready_now),
                        (
                            "52%·7д" if language == "uk" else "52%·7d",
                            datetime.fromtimestamp(ready.resets_at).astimezone().strftime("%d.%m·%H:%M"),
                        ),
                    )
                    self.assertIsNone(WidgetWindow.__new__(WidgetWindow)._provider_action("codex", (ready,)))
                    for state, action_key in states:
                        with self.subTest(dpi=dpi, language=language, state=state.value):
                            canvas = tk.Canvas(
                                root,
                                width=layout.width,
                                height=layout.height,
                                borderwidth=0,
                                highlightthickness=0,
                            )
                            metric = Metric("codex_10080", "codex", 10_080, None, None, state, 2_000_000_000)
                            expected_operation = {
                                ProviderState.AUTHORIZATION_REQUIRED: "sign-in",
                                ProviderState.OFFLINE: "retry",
                                ProviderState.PROVIDER_ERROR: "retry",
                                ProviderState.UNAVAILABLE: "setup",
                            }[state]
                            self.assertEqual(
                                WidgetWindow.__new__(WidgetWindow)._provider_action("codex", (metric,)),
                                f"codex:{expected_operation}",
                            )
                            _value, status_text = metric_visible_text(metric, language)
                            font = ("Segoe UI", -layout.reset_font_size)
                            x = layout.text_anchors[0]
                            status_item = canvas.create_text(
                                x, layout.reset_y, anchor="w", font=font, text=status_text
                            )
                            action_item = canvas.create_text(
                                x, layout.reset_y, anchor="w", font=font, text=tr(language, action_key)
                            )
                            lane_right = action_lane_right(layout, 0)

                            old_status = canvas.bbox(status_item)
                            self.assertIsNotNone(old_status)
                            assert old_status is not None
                            canvas.coords(action_item, old_status[2] + round(3 * scale), layout.reset_y)
                            old_action = canvas.bbox(action_item)
                            self.assertIsNotNone(old_action)
                            assert old_action is not None
                            old_overflow_observed |= old_action[2] + round(3 * scale) > lane_right
                            canvas.coords(action_item, x, layout.reset_y)

                            hit_box, action_only = place_recovery_action(
                                canvas,
                                status_item,
                                action_item,
                                x=x,
                                reset_y=layout.reset_y,
                                scale=scale,
                                lane_right=lane_right,
                                canvas_height=layout.height,
                            )
                            status_box = canvas.bbox(status_item)
                            if action_only:
                                self.assertIsNone(status_box)
                            else:
                                self.assertIsNotNone(status_box)
                            boxes = [canvas.bbox(action_item), hit_box]
                            if status_box is not None:
                                boxes.append(status_box)
                            for box in boxes:
                                self.assertIsNotNone(box)
                                assert box is not None
                                self.assertGreaterEqual(box[0], 0)
                                self.assertGreaterEqual(box[1], 0)
                                self.assertLessEqual(box[2], lane_right)
                                self.assertLessEqual(box[3], layout.height)
                                self.assertGreater(box[2], box[0])
                                self.assertGreater(box[3], box[1])
                            left, top, right, bottom = hit_box
                            for point in (
                                (left, top), (right, top), (left, bottom), (right, bottom),
                                ((left + right) // 2, (top + bottom) // 2),
                            ):
                                self.assertTrue(hit_contour_contains(layout, *point), point)
                            canvas.destroy()
            self.assertTrue(old_overflow_observed, "old inline Ukrainian recovery text must be a negative control")
        finally:
            root.destroy()

    def test_every_inline_action_fits_its_actual_provider_lane(self) -> None:
        root = tk.Tk()
        root.withdraw()
        actual_lanes = {
            "both-compact": ((0, ("sign_in", "retry", "setup")), (1, ("connect", "sign_in", "retry"))),
            "both-wide": ((0, ("sign_in", "retry", "setup")), (2, ("connect", "sign_in", "retry"))),
            "claude-only": ((0, ("connect", "sign_in", "retry")),),
            "codex-one-window": ((0, ("sign_in", "retry", "setup")),),
            "codex-two-window": ((0, ("sign_in", "retry", "setup")),),
        }
        try:
            for mode, lanes in actual_lanes.items():
                for dpi in (96, 120, 144, 192):
                    layout = compute_layout(mode, dpi / 96)
                    for index, actions in lanes:
                        x = layout.text_anchors[index]
                        for language in ("en", "uk"):
                            for action_key in actions:
                                with self.subTest(
                                    mode=mode,
                                    dpi=dpi,
                                    index=index,
                                    language=language,
                                    action=action_key,
                                ):
                                    canvas = tk.Canvas(
                                        root,
                                        width=layout.width,
                                        height=layout.height,
                                        borderwidth=0,
                                        highlightthickness=0,
                                    )
                                    font = ("Segoe UI", -layout.reset_font_size)
                                    status_item = canvas.create_text(
                                        x,
                                        layout.reset_y,
                                        anchor="w",
                                        font=font,
                                        text=tr(language, "provider_error_short"),
                                    )
                                    action_item = canvas.create_text(
                                        x,
                                        layout.reset_y,
                                        anchor="w",
                                        font=font,
                                        text=tr(language, action_key),
                                    )
                                    hit_box, _action_only = place_recovery_action(
                                        canvas,
                                        status_item,
                                        action_item,
                                        x=x,
                                        reset_y=layout.reset_y,
                                        scale=layout.scale,
                                        lane_right=action_lane_right(layout, index),
                                        canvas_height=layout.height,
                                    )
                                    self.assertLess(hit_box[0], hit_box[2])
                                    self.assertLessEqual(hit_box[2], action_lane_right(layout, index))
                                    canvas.destroy()
        finally:
            root.destroy()

    def test_demo_fixture_has_every_required_typed_state(self) -> None:
        for state, expected in (
            ("ready", ProviderState.READY),
            ("refreshing", ProviderState.REFRESHING),
            ("offline", ProviderState.OFFLINE),
            ("authorization-required", ProviderState.AUTHORIZATION_REQUIRED),
            ("provider-error", ProviderState.PROVIDER_ERROR),
            ("unavailable", ProviderState.UNAVAILABLE),
        ):
            codex, claude = fixture_demo.fixture_store(now=2_000_000_000, state=state).snapshot(now=2_000_000_001)
            self.assertTrue(codex)
            self.assertTrue(claude)
            self.assertTrue(all(item.state is expected for item in codex + claude))
            if state == "provider-error":
                self.assertTrue(all(item.error_code == "provider_error" for item in codex + claude))
        for state, remaining in (("low", 15), ("critical", 4)):
            codex, _claude = fixture_demo.fixture_store(now=2_000_000_000, state=state).snapshot(now=2_000_000_001)
            self.assertEqual(100 - codex[0].used_percent, remaining)

    def test_demo_is_confined_and_import_closure_has_no_production_route(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            state = Path(temporary) / "state"
            config = state / "demo"
            outside = Path(temporary) / "outside"
            config.mkdir(parents=True)
            outside.mkdir()
            self.assertEqual(fixture_demo.validated_fixture_roots(state, config), (state.resolve(), config.resolve()))
            with self.assertRaises(ValueError):
                fixture_demo.validated_fixture_roots(state, state)
            with self.assertRaises(ValueError):
                fixture_demo.validated_fixture_roots(state, outside)
        tree = ast.parse((ROOT / "src" / "limit_halo" / "demo.py").read_text(encoding="utf-8"))
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        forbidden = {"subprocess", "socket", "limit_halo.app", "limit_halo.codex_protocol", "limit_halo.codex_discovery", "limit_halo.claude_protocol", "limit_halo.auth"}
        self.assertTrue(imports.isdisjoint(forbidden), imports & forbidden)

    def test_translation_parity(self) -> None:
        assert_translation_parity()
        self.assertEqual(set(STRINGS["uk"]), set(STRINGS["en"]))


if __name__ == "__main__":
    unittest.main()
