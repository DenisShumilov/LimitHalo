from __future__ import annotations

import copy
import json
import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from limit_halo.config import (
    FORBIDDEN_PERSISTED_FRAGMENTS,
    AppConfig,
    ConfigStore,
    decode_config,
)
from limit_halo.localization import tr
from limit_halo.model import Metric, MetricStore, ProviderState
from limit_halo.ui import (
    RESET_FONT_FAMILY,
    VALUE_FONT_FAMILY,
    WidgetWindow,
    compute_layout,
    metric_visible_text,
    place_recovery_action,
)
from limit_halo.ui_contract import (
    G04_MUTANTS,
    production_g04_snapshot,
    validate_g04_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "g04-ui-native-v1.json"


class G04ReleaseContractTests(unittest.TestCase):
    def test_frozen_five_mode_four_dpi_ui_native_snapshot(self) -> None:
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        actual = production_g04_snapshot()
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual["layouts"]), 20)
        self.assertTrue(
            all(row["contourAcceptedCells"] > 0 for row in actual["layouts"])
        )
        self.assertEqual(validate_g04_snapshot(actual), ())

    def test_every_named_g04_mutant_is_applied_and_rejected(self) -> None:
        catalog = json.loads(
            (ROOT / "tests" / "mutants" / "mutant-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(tuple(catalog["V7-G04"]), G04_MUTANTS)
        baseline = production_g04_snapshot()

        def apply_mutant(name: str, value: dict[str, object]) -> None:
            if name == "GLYPH-CHANGED":
                value["assets"]["codex"] = "0" * 64
            elif name == "TYPOGRAPHY-CHANGED":
                value["typography"]["valueFamily"] = "Arial"
            elif name == "COMPACT-LAYOUT-CHANGED":
                value["layouts"][0]["size"][0] += 1
            elif name == "MONOCHROME-CHANGED":
                value["readyAppearance"]["defaultMode"] = "status"
            elif name == "BLANK-DRAG-REMOVED":
                value["interaction"]["wholeContourDrag"] = False
            elif name == "ENGLISH-DEFAULT-REMOVED":
                value["languages"]["freshDefault"] = "uk-UA"
            elif name == "UKRAINIAN-REMOVED":
                value["languages"]["ukrainianSelectable"] = False
            elif name == "PROVIDER-DISCOVERY-REMOVED":
                value["providers"]["installedLaterVisible"] = False
            elif name == "UNUSED-LANE-SHOWN":
                value["providers"]["unusedHidden"] = False
            elif name == "STARTUP-PREFERENCE-DROPPED":
                value["startup"]["userOffRoundTrips"] = False
            elif name == "CONFIG-FIELD-DROPPED":
                value["config"]["safeUnknownTopLevelRoundTrips"] = False
            elif name == "AI-ENTRY-REMOVED":
                value["aiEntry"]["actions"].remove("Auto")
            else:
                raise AssertionError(f"unbound G04 mutant: {name}")

        for name in G04_MUTANTS:
            with self.subTest(mutant=name):
                mutant = copy.deepcopy(baseline)
                apply_mutant(name, mutant)
                self.assertNotEqual(mutant, baseline)
                self.assertIn(name, validate_g04_snapshot(mutant))

    def test_all_known_and_safe_future_config_fields_round_trip(self) -> None:
        full = AppConfig(
            locale="uk-UA",
            claudeMode="enabled",
            codexMode="disabled",
            claudeConsent="granted",
            topmost=False,
            startWithWindows=False,
            displayPreset="wide",
            colorMode="status",
            alertsEnabled=True,
            alertThresholdPercent=5,
            quietHoursEnabled=True,
            quietStartMinutes=23 * 60 + 15,
            quietEndMinutes=7 * 60 + 45,
            x=-1600,
            y=240,
        )
        with tempfile.TemporaryDirectory() as directory:
            first = ConfigStore(Path(directory))
            first.save(full)
            self.assertEqual(first.load(), full)

            future = full.to_json_object()
            future["futureUi"] = {
                "density": "small",
                "variants": ["a", "b"],
                "nested": {"enabled": True},
            }
            first.path.write_text(json.dumps(future), encoding="utf-8")
            loaded = first.load()
            changed = replace(loaded, locale="en-US", x=-1400)
            first.save(changed)
            second = ConfigStore(Path(directory))
            reloaded = second.load()
            self.assertEqual(reloaded, changed)
            self.assertEqual(
                reloaded.to_json_object()["futureUi"], future["futureUi"]
            )

        base = AppConfig().to_json_object()
        for fragment in FORBIDDEN_PERSISTED_FRAGMENTS:
            punctuated = ".".join(fragment)
            for extension in (
                {punctuated: "blocked"},
                {"futureUi": {punctuated: "blocked"}},
                {"futureUi": [{punctuated: "blocked"}]},
            ):
                with self.subTest(forbidden=fragment, extension=extension):
                    candidate = copy.deepcopy(base)
                    candidate.update(extension)
                    self.assertEqual(decode_config(candidate), AppConfig())

    def test_provider_visibility_matrix_uses_the_widget_production_path(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        present: set[str] = set()
        succeeded: set[str] = set()
        window.provider_present = lambda provider: provider in present
        window.store = mock.Mock()
        window.store.ever_succeeded.side_effect = lambda provider: provider in succeeded

        automatic = AppConfig()
        self.assertFalse(window._provider_visible_for_config("codex", automatic))
        self.assertFalse(window._provider_visible_for_config("claude", automatic))
        present.add("codex")
        self.assertTrue(window._provider_visible_for_config("codex", automatic))
        self.assertFalse(window._provider_visible_for_config("claude", automatic))
        present.add("claude")
        self.assertTrue(window._provider_visible_for_config("claude", automatic))
        hidden = replace(automatic, codexMode="disabled", claudeMode="disabled")
        self.assertFalse(window._provider_visible_for_config("codex", hidden))
        self.assertFalse(window._provider_visible_for_config("claude", hidden))

        present.clear()
        succeeded.add("claude")
        self.assertTrue(window._provider_visible_for_config("claude", automatic))
        succeeded.clear()
        self.assertFalse(window._provider_visible_for_config("claude", automatic))
        present.add("claude")
        self.assertTrue(window._provider_visible_for_config("claude", automatic))
        self.assertEqual(automatic, AppConfig(), "automatic discovery must not mutate settings")

    def test_every_action_hit_box_routes_to_its_exact_action(self) -> None:
        root = tk.Tk()
        root.withdraw()
        lanes = {
            "both-compact": (
                (0, (("codex:sign-in", "sign_in"), ("codex:retry", "retry"), ("codex:setup", "setup"))),
                (1, (("claude:setup", "connect"), ("claude:sign-in", "sign_in"), ("claude:retry", "retry"))),
            ),
            "both-wide": (
                (0, (("codex:sign-in", "sign_in"), ("codex:retry", "retry"), ("codex:setup", "setup"))),
                (2, (("claude:setup", "connect"), ("claude:sign-in", "sign_in"), ("claude:retry", "retry"))),
            ),
            "claude-only": (
                (0, (("claude:setup", "connect"), ("claude:sign-in", "sign_in"), ("claude:retry", "retry"))),
            ),
            "codex-one-window": (
                (0, (("codex:sign-in", "sign_in"), ("codex:retry", "retry"), ("codex:setup", "setup"))),
            ),
            "codex-two-window": (
                (0, (("codex:sign-in", "sign_in"), ("codex:retry", "retry"), ("codex:setup", "setup"))),
            ),
        }
        try:
            for mode, lane_specs in lanes.items():
                for dpi in (96, 120, 144, 192):
                    layout = compute_layout(mode, dpi / 96)
                    for index, actions in lane_specs:
                        lane_right = (
                            layout.text_anchors[index + 1] - round(8 * layout.scale)
                            if index + 1 < len(layout.text_anchors)
                            else layout.width - round(8 * layout.scale)
                        )
                        for language in ("en", "uk"):
                            for action, label_key in actions:
                                with self.subTest(
                                    mode=mode,
                                    dpi=dpi,
                                    language=language,
                                    action=action,
                                ):
                                    canvas = tk.Canvas(
                                        root,
                                        width=layout.width,
                                        height=layout.height,
                                        borderwidth=0,
                                        highlightthickness=0,
                                    )
                                    font = (RESET_FONT_FAMILY, -layout.reset_font_size)
                                    status = canvas.create_text(
                                        layout.text_anchors[index],
                                        layout.reset_y,
                                        anchor="w",
                                        font=font,
                                        text=tr(language, "provider_error_short"),
                                    )
                                    item = canvas.create_text(
                                        layout.text_anchors[index],
                                        layout.reset_y,
                                        anchor="w",
                                        font=font,
                                        text=tr(language, label_key),
                                    )
                                    box, _action_only = place_recovery_action(
                                        canvas,
                                        status,
                                        item,
                                        x=layout.text_anchors[index],
                                        reset_y=layout.reset_y,
                                        scale=layout.scale,
                                        lane_right=lane_right,
                                        canvas_height=layout.height,
                                    )
                                    center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
                                    window = WidgetWindow.__new__(WidgetWindow)
                                    window._action_boxes = {action: box}
                                    self.assertEqual(window._action_at(*center), action)
                                    window._armed_action = action
                                    window._dragging = False
                                    window._drag_origin = center
                                    window._drag_window_origin = (10, 10)
                                    window._invoke_action = mock.Mock()
                                    window._drag_end(mock.Mock())
                                    window._invoke_action.assert_called_once_with(action)
                                    canvas.destroy()
        finally:
            root.destroy()

    def test_five_dip_threshold_keeps_action_precedence(self) -> None:
        window = WidgetWindow.__new__(WidgetWindow)
        window.layout = compute_layout("codex-one-window", 1.0)
        window.dpi = 96
        window._set_window_pair_position = mock.Mock(return_value=True)
        window._sync_z_order = mock.Mock()
        window._repair_and_persist_native_position = mock.Mock()
        window._persist_native_position = mock.Mock(return_value=True)
        window._invoke_action = mock.Mock()

        window._drag_origin = (0, 0)
        window._drag_window_origin = (10, 20)
        window._dragging = False
        window._armed_action = "codex:retry"
        window._drag_move(mock.Mock(x_root=3, y_root=4))
        self.assertFalse(window._dragging)
        window._set_window_pair_position.assert_not_called()
        window._drag_end(mock.Mock())
        window._invoke_action.assert_called_once_with("codex:retry")

        window._drag_origin = (0, 0)
        window._drag_window_origin = (10, 20)
        window._dragging = False
        window._armed_action = "codex:retry"
        window._drag_move(mock.Mock(x_root=4, y_root=4))
        self.assertTrue(window._dragging)
        self.assertIsNone(window._armed_action)
        window._drag_end(mock.Mock())
        window._repair_and_persist_native_position.assert_called_once_with()
        self.assertEqual(window._invoke_action.call_count, 1)

    def test_newer_claude_value_is_rendered_on_the_next_widget_tick(self) -> None:
        now = 2_000_000_000.0
        store = MetricStore(now=now)

        def snapshot(used: int, observed: float) -> tuple[Metric, ...]:
            return (
                Metric("claude_300", "claude", 300, used, None, ProviderState.READY, observed),
                Metric("claude_10080", "claude", 10_080, 50, int(now) + 400_000, ProviderState.READY, observed),
                Metric("claude_fable_10080", "claude", 10_080, None, None, ProviderState.UNAVAILABLE, observed),
            )

        self.assertTrue(store.update_claude(snapshot(75, now), sequence=1))
        window = WidgetWindow.__new__(WidgetWindow)
        window._closing = False
        window._drag_origin = (0, 0)
        window.store = store
        window.config = AppConfig(codexMode="disabled", claudeMode="enabled", claudeConsent="granted")
        window.provider_present = lambda provider: provider == "claude"
        window.layout = compute_layout("claude-only", 1.0)
        window.scale = 1.0
        window.root = mock.Mock()
        rendered: list[str] = []

        def capture_redraw() -> None:
            metrics, _codex_visible, _claude_visible = window._current_metrics()
            rendered.append(metric_visible_text(metrics[0], "en")[0])

        window._redraw = capture_redraw
        window._sync_z_order = mock.Mock()
        window._emit_notifications = mock.Mock()
        window._tick()
        self.assertEqual(rendered[-1], "25%·5h")

        self.assertTrue(store.update_claude(snapshot(10, now + 1), sequence=2))
        window._tick()
        self.assertEqual(rendered[-1], "90%·5h")

    def test_english_startup_and_ai_entry_are_bound_to_real_sources(self) -> None:
        self.assertEqual(AppConfig().locale, "en-US")
        self.assertTrue(AppConfig().startWithWindows)
        self.assertEqual(AppConfig(locale="uk-UA").locale, "uk-UA")
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory))
            store.save(replace(AppConfig(), startWithWindows=False))
            self.assertFalse(ConfigStore(Path(directory)).load().startWithWindows)

        demo = (ROOT / "src" / "limit_halo" / "demo.py").read_text(encoding="utf-8")
        self.assertIn('default="en-US"', demo)
        helper = (ROOT / "LimitHalo-Agent.ps1").read_text(encoding="utf-8")
        guide = (ROOT / "INSTALL_WITH_AI.md").read_text(encoding="utf-8")
        self.assertIn("@('Auto','Status','Install','Repair')", helper)
        self.assertIn("'/OPENAFTERINSTALL=0'", helper)
        self.assertNotIn("python", helper.casefold())
        self.assertEqual(
            guide.count("powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\LimitHalo-Agent.ps1 Auto"),
            2,
        )
        self.assertIn("downloads nothing and needs no Python", guide)


if __name__ == "__main__":
    unittest.main()
