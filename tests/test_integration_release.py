from __future__ import annotations

import os
import ast
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from limit_halo import __main__ as launcher
from limit_halo import app


class IntegratedReleaseTests(unittest.TestCase):
    def run_provider_free_route(
        self,
        codex_mode: str,
        claude_mode: str,
        arguments: list[str] | None = None,
        *,
        claude_consent: str = "not-asked",
        claude_detected: bool = False,
    ) -> tuple[int, mock.Mock, mock.Mock]:
        config = app.AppConfig(
            codexMode=codex_mode,
            claudeMode=claude_mode,
            claudeConsent=claude_consent,
        )
        instance = mock.Mock(acquired=True)
        config_store = mock.Mock()
        config_store.load.return_value = config
        configure = mock.Mock(return_value=31)
        with (
            mock.patch.object(app, "SingleInstance", return_value=instance),
            mock.patch.object(app, "enable_dpi_awareness"),
            mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
            mock.patch.object(app, "ConfigStore", return_value=config_store),
            mock.patch.object(app, "portable_mode", return_value=False),
            mock.patch.object(app, "discover_public_codex", return_value=None),
            mock.patch.object(app, "trusted_claude_client_present", return_value=claude_detected),
            mock.patch.object(app, "production_client") as production_client,
            mock.patch.object(app, "_open_provider_free_configuration", configure),
            mock.patch.object(app, "MetricStore") as metric_store,
            mock.patch.object(app, "CodexCollector") as codex_collector,
            mock.patch.object(app, "ClaudeRuntime") as claude_runtime,
            mock.patch.object(app.tk, "Tk") as tk_root,
            mock.patch.object(app, "WidgetWindow") as widget_window,
            mock.patch.object(app, "launch_provider_sign_in") as sign_in,
        ):
            result = launcher.main([] if arguments is None else arguments)
        production_client.assert_not_called()
        metric_store.assert_not_called()
        codex_collector.assert_not_called()
        claude_runtime.assert_not_called()
        tk_root.assert_not_called()
        widget_window.assert_not_called()
        sign_in.assert_not_called()
        return result, instance, configure

    def test_exact_offline_health_check_has_no_production_app_or_provider_start(self) -> None:
        loaded_app = sys.modules.pop("limit_halo.app", None)
        try:
            self.assertEqual(launcher.main(["--health-check", "--offline", "--no-provider-start"]), 0)
            self.assertNotIn("limit_halo.app", sys.modules)
            for arguments in (
                ["--health-check"],
                ["--health-check", "--offline"],
                ["--offline", "--no-provider-start", "--health-check"],
                ["--health-check", "--offline", "--no-provider-start", "--demo-fixture"],
            ):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    launcher.main(arguments)
        finally:
            if loaded_app is not None:
                sys.modules["limit_halo.app"] = loaded_app

    def test_demo_route_is_explicit_and_does_not_import_production_app(self) -> None:
        called: list[list[str]] = []
        fake = types.ModuleType("limit_halo.demo")
        fake.main = lambda argv: called.append(list(argv)) or 17
        arguments = [
            "--demo-fixture",
            r"--authorized-state-root=C:\fixture\state",
            r"--config-root=C:\fixture\state\demo",
            "--exit-after-ms=1500",
        ]
        with mock.patch.dict(sys.modules, {"limit_halo.demo": fake}):
            self.assertEqual(launcher.main(arguments), 17)
        self.assertEqual(called, [[
            r"--authorized-state-root=C:\fixture\state",
            r"--config-root=C:\fixture\state\demo",
            "--exit-after-ms=1500",
        ]])

    def test_exact_configure_route_is_lazy_and_provider_free(self) -> None:
        called: list[str] = []
        fake = types.ModuleType("limit_halo.configure")
        fake.main = lambda: called.append("configure") or 23
        provider_modules = (
            "limit_halo.codex_protocol", "limit_halo.codex_discovery",
            "limit_halo.claude_protocol", "limit_halo.auth",
        )
        saved = {name: sys.modules.pop(name, None) for name in provider_modules}
        try:
            with mock.patch.dict(sys.modules, {"limit_halo.configure": fake}):
                self.assertEqual(launcher.main(["--configure"]), 23)
            self.assertEqual(called, ["configure"])
            self.assertTrue(all(name not in sys.modules for name in provider_modules))
        finally:
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module

        source = (ROOT / "src" / "limit_halo" / "configure.py").read_text(encoding="utf-8")
        imports = {
            node.module or ""
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertTrue(imports.isdisjoint({
            "limit_halo.codex_protocol", "limit_halo.codex_discovery",
            "limit_halo.claude_protocol", "limit_halo.auth",
        }))

    def test_exact_installer_consent_route_is_lazy_and_provider_free(self) -> None:
        called: list[str] = []
        fake = types.ModuleType("limit_halo.configure")
        fake.grant_claude_quota_access = lambda: called.append("grant") or 0
        provider_modules = (
            "limit_halo.codex_protocol", "limit_halo.codex_discovery",
            "limit_halo.claude_protocol", "limit_halo.auth",
        )
        saved = {name: sys.modules.pop(name, None) for name in provider_modules}
        try:
            with mock.patch.dict(sys.modules, {"limit_halo.configure": fake}):
                self.assertEqual(launcher.main(["--grant-claude-quota-access"]), 0)
            self.assertEqual(called, ["grant"])
            self.assertTrue(all(name not in sys.modules for name in provider_modules))
        finally:
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module

    def test_duplicate_or_unknown_routes_fail_closed(self) -> None:
        with self.assertRaises(SystemExit):
            launcher.main(["--demo-fixture", "--demo-fixture"])
        with self.assertRaises(SystemExit):
            launcher.main(["--unknown"])
        with self.assertRaises(SystemExit):
            launcher.main(["--configure", "--unknown"])
        with self.assertRaises(SystemExit):
            launcher.main(["--grant-claude-quota-access", "--unknown"])

    def test_portable_state_stays_in_local_appdata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "portable.flag").write_text("portable\n", encoding="ascii")
            local_app_data = root / "local-app-data"
            with mock.patch.object(app, "package_root", return_value=root), mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                self.assertEqual(app.state_root(), local_app_data / "AILimitsWidget")
                self.assertTrue(app.portable_mode())

    def test_mutex_matches_installer(self) -> None:
        installer = (ROOT / "packaging" / "installer" / "LimitHalo.iss").read_text(encoding="utf-8")
        self.assertEqual(app.MUTEX_NAME, r"Local\AILimitsWidget-HUD-v1")
        self.assertIn("AppMutex=" + app.MUTEX_NAME, installer)

    @unittest.skipUnless(os.name == "nt", "Windows named-object contract")
    def test_duplicate_instance_signals_the_existing_widget(self) -> None:
        suffix = uuid.uuid4().hex
        primary = app.SingleInstance(
            rf"Local\LimitHalo-test-mutex-{suffix}",
            rf"Local\LimitHalo-test-activation-{suffix}",
        )
        duplicate = app.SingleInstance(
            rf"Local\LimitHalo-test-mutex-{suffix}",
            rf"Local\LimitHalo-test-activation-{suffix}",
        )
        try:
            self.assertTrue(primary.acquired)
            self.assertFalse(duplicate.acquired)
            self.assertFalse(primary.consume_activation_request())
            duplicate.request_activation()
            self.assertTrue(primary.consume_activation_request())
            self.assertFalse(primary.consume_activation_request())
        finally:
            duplicate.close()
            primary.close()

    def test_provider_free_explicit_launch_opens_configure_in_same_process(self) -> None:
        for codex_mode, claude_mode in (
            ("auto", "auto"),
            ("auto", "disabled"),
            ("disabled", "auto"),
            ("disabled", "disabled"),
        ):
            with self.subTest(codex=codex_mode, claude=claude_mode):
                result, instance, configure = self.run_provider_free_route(codex_mode, claude_mode)
                self.assertEqual(result, 31)
                configure.assert_called_once_with(
                    codex_present=False, claude_present=False
                )
                instance.close.assert_called_once_with()

    def test_provider_free_startup_exits_quietly_without_opening_setup(self) -> None:
        result, instance, configure = self.run_provider_free_route(
            "auto", "auto", ["--startup"]
        )
        self.assertEqual(result, 0)
        configure.assert_not_called()
        instance.close.assert_called_once_with()

    def test_denied_detected_claude_remains_reconnectable_in_settings(self) -> None:
        result, _instance, configure = self.run_provider_free_route(
            "disabled",
            "auto",
            claude_consent="denied",
            claude_detected=True,
        )
        self.assertEqual(result, 31)
        configure.assert_called_once_with(
            codex_present=False, claude_present=True
        )

    def test_exact_startup_route_reaches_production_startup_mode(self) -> None:
        with mock.patch.object(app, "main", return_value=17) as production:
            self.assertEqual(launcher.main(["--startup"]), 17)
        production.assert_called_once_with(startup_launch=True)
        for arguments in (("--startup", "--startup"), ("--startup", "--configure")):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                launcher.main(list(arguments))

    def test_provider_free_configure_error_still_closes_single_instance(self) -> None:
        config = app.AppConfig(codexMode="disabled", claudeMode="disabled")
        instance = mock.Mock(acquired=True)
        config_store = mock.Mock()
        config_store.load.return_value = config
        with (
            mock.patch.object(app, "SingleInstance", return_value=instance),
            mock.patch.object(app, "enable_dpi_awareness"),
            mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
            mock.patch.object(app, "ConfigStore", return_value=config_store),
            mock.patch.object(app, "portable_mode", return_value=False),
            mock.patch.object(app, "_open_provider_free_configuration", side_effect=RuntimeError("fixture configure failure")),
            self.assertRaisesRegex(RuntimeError, "fixture configure failure"),
        ):
            launcher.main([])
        instance.close.assert_called_once_with()

    def test_auto_detected_claude_shows_setup_lane_before_consent(self) -> None:
        config = app.AppConfig(codexMode="disabled", claudeMode="auto", claudeConsent="not-asked")
        instance = mock.Mock(acquired=True)
        config_store = mock.Mock()
        config_store.load.return_value = config
        root = mock.Mock()
        store = mock.Mock()
        claude = mock.Mock()
        with (
            mock.patch.object(app, "SingleInstance", return_value=instance),
            mock.patch.object(app, "enable_dpi_awareness"),
            mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
            mock.patch.object(app, "ConfigStore", return_value=config_store),
            mock.patch.object(app, "portable_mode", return_value=False),
            mock.patch.object(app, "trusted_claude_client_present", return_value=True),
            mock.patch.object(app, "production_client") as production_client,
            mock.patch.object(app, "_open_provider_free_configuration") as configure,
            mock.patch.object(app, "MetricStore", return_value=store),
            mock.patch.object(app, "ClaudeRuntime", return_value=claude),
            mock.patch.object(app.tk, "Tk", return_value=root),
            mock.patch.object(app, "WidgetWindow") as widget_window,
        ):
            self.assertEqual(launcher.main([]), 0)
        configure.assert_not_called()
        production_client.assert_not_called()
        widget_window.assert_called_once()
        provider_present = widget_window.call_args.kwargs["provider_present"]
        self.assertTrue(provider_present("claude"))
        claude.start_if_consented.assert_not_called()
        root.mainloop.assert_called_once_with()

    def test_provider_free_consent_resumes_into_hud_without_relaunch(self) -> None:
        initial = app.AppConfig(codexMode="disabled", claudeMode="auto", claudeConsent="not-asked")
        configured = app.AppConfig(codexMode="disabled", claudeMode="enabled", claudeConsent="granted")
        instance = mock.Mock(acquired=True)
        config_store = mock.Mock()
        config_store.load.side_effect = (initial, configured)
        root = mock.Mock()
        store = mock.Mock()
        claude = mock.Mock()
        with (
            mock.patch.object(app, "SingleInstance", return_value=instance),
            mock.patch.object(app, "enable_dpi_awareness"),
            mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
            mock.patch.object(app, "ConfigStore", return_value=config_store),
            mock.patch.object(app, "portable_mode", return_value=False),
            mock.patch.object(app, "trusted_claude_client_present", return_value=False),
            mock.patch.object(app, "production_client"),
            mock.patch.object(app, "_open_provider_free_configuration", return_value=0) as configure,
            mock.patch.object(app, "MetricStore", return_value=store),
            mock.patch.object(app, "ClaudeRuntime", return_value=claude),
            mock.patch.object(app.tk, "Tk", return_value=root),
            mock.patch.object(app, "WidgetWindow") as widget_window,
        ):
            self.assertEqual(launcher.main([]), 0)
        configure.assert_called_once_with(
            codex_present=False, claude_present=False
        )
        widget_window.assert_called_once()
        claude.start_if_consented.assert_called_once_with()
        root.mainloop.assert_called_once_with()

    def test_consented_claude_starts_before_the_first_hud_frame(self) -> None:
        config = app.AppConfig(codexMode="disabled", claudeMode="enabled", claudeConsent="granted")
        instance = mock.Mock(acquired=True)
        config_store = mock.Mock()
        config_store.load.return_value = config
        root = mock.Mock()
        store = mock.Mock()
        claude = mock.Mock()
        events: list[str] = []
        claude.start_if_consented.side_effect = lambda: events.append("claude-start") or True

        def construct_window(*_args: object, **_kwargs: object) -> mock.Mock:
            events.append("hud-frame")
            return mock.Mock()

        with (
            mock.patch.object(app, "SingleInstance", return_value=instance),
            mock.patch.object(app, "enable_dpi_awareness"),
            mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
            mock.patch.object(app, "ConfigStore", return_value=config_store),
            mock.patch.object(app, "portable_mode", return_value=False),
            mock.patch.object(app, "trusted_claude_client_present", return_value=True),
            mock.patch.object(app, "production_client"),
            mock.patch.object(app, "MetricStore", return_value=store),
            mock.patch.object(app, "ClaudeRuntime", return_value=claude),
            mock.patch.object(app.tk, "Tk", return_value=root),
            mock.patch.object(app, "WidgetWindow", side_effect=construct_window),
        ):
            self.assertEqual(launcher.main([]), 0)

        self.assertEqual(events[:2], ["claude-start", "hud-frame"])
        claude.start_if_consented.assert_called_once_with()

    def test_first_consented_claude_frame_is_really_refreshing(self) -> None:
        config = app.AppConfig(codexMode="disabled", claudeMode="enabled", claudeConsent="granted")
        instance = mock.Mock(acquired=True)
        config_store = mock.Mock()
        config_store.load.return_value = config
        root = mock.Mock()
        client = mock.Mock()
        inert_thread = mock.Mock()
        inert_thread.is_alive.return_value = False
        observed_states: list[app.ProviderState] = []

        def inspect_first_frame(_root: object, store: object, *_args: object, **_kwargs: object) -> mock.Mock:
            _codex, claude_metrics = store.snapshot()
            observed_states.extend(metric.state for metric in claude_metrics)
            return mock.Mock()

        with (
            mock.patch.object(app, "SingleInstance", return_value=instance),
            mock.patch.object(app, "enable_dpi_awareness"),
            mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
            mock.patch.object(app, "ConfigStore", return_value=config_store),
            mock.patch.object(app, "portable_mode", return_value=False),
            mock.patch.object(app, "trusted_claude_client_present", return_value=True),
            mock.patch.object(app, "production_client", return_value=client),
            mock.patch.object(app.tk, "Tk", return_value=root),
            mock.patch.object(app, "WidgetWindow", side_effect=inspect_first_frame),
            mock.patch("limit_halo.claude_protocol.threading.Thread", return_value=inert_thread),
        ):
            self.assertEqual(launcher.main([]), 0)

        self.assertEqual(observed_states, [app.ProviderState.REFRESHING] * 3)
        inert_thread.start.assert_called_once_with()

    def test_claude_sign_in_automatically_refreshes_and_launch_failure_is_typed(self) -> None:
        config = app.AppConfig(codexMode="disabled", claudeMode="enabled", claudeConsent="granted")

        for launch_fails in (False, True):
            with self.subTest(launch_fails=launch_fails):
                instance = mock.Mock(acquired=True)
                config_store = mock.Mock()
                config_store.load.return_value = config
                root = mock.Mock()
                scheduled: dict[int, list[object]] = {}
                root.after.side_effect = lambda delay, callback: scheduled.setdefault(delay, []).append(callback)
                store = mock.Mock()
                store.snapshot.return_value = ((), ())
                claude = mock.Mock()
                process = mock.Mock()
                process.poll.return_value = None
                widget_window = mock.Mock()

                def run_one_ui_turn() -> None:
                    sign_in = widget_window.call_args.kwargs["on_sign_in"]
                    sign_in("claude")
                    if not launch_fails:
                        self.assertEqual(len(scheduled.get(750, ())), 1)
                        scheduled[750].pop(0)()

                root.mainloop.side_effect = run_one_ui_turn
                launch = mock.Mock(
                    side_effect=app.AuthLaunchFailure("fixture unavailable") if launch_fails else None,
                    return_value=process,
                )
                with (
                    mock.patch.object(app, "SingleInstance", return_value=instance),
                    mock.patch.object(app, "enable_dpi_awareness"),
                    mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
                    mock.patch.object(app, "ConfigStore", return_value=config_store),
                    mock.patch.object(app, "portable_mode", return_value=False),
                    mock.patch.object(app, "trusted_claude_client_present", return_value=True),
                    mock.patch.object(app, "production_client"),
                    mock.patch.object(app, "MetricStore", return_value=store),
                    mock.patch.object(app, "ClaudeRuntime", return_value=claude),
                    mock.patch.object(app.tk, "Tk", return_value=root),
                    mock.patch.object(app, "WidgetWindow", widget_window),
                    mock.patch.object(app, "launch_provider_sign_in", launch),
                ):
                    self.assertEqual(launcher.main([]), 0)

                launch.assert_called_once_with("claude")
                if launch_fails:
                    store.mark_provider.assert_any_call(
                        "claude", app.ProviderState.PROVIDER_ERROR,
                        error_code="auth_client_unavailable",
                    )
                    claude.request_refresh.assert_not_called()
                else:
                    store.mark_provider.assert_any_call("claude", app.ProviderState.REFRESHING)
                    claude.request_refresh.assert_called_once_with()
                    process.poll.assert_called_once_with()

    def test_sign_in_recovery_does_not_refresh_an_already_ready_provider(self) -> None:
        config = app.AppConfig(codexMode="disabled", claudeMode="enabled", claudeConsent="granted")
        instance = mock.Mock(acquired=True)
        config_store = mock.Mock()
        config_store.load.return_value = config
        root = mock.Mock()
        scheduled: dict[int, list[object]] = {}
        root.after.side_effect = lambda delay, callback: scheduled.setdefault(delay, []).append(callback)
        store = mock.Mock()
        store.snapshot.return_value = ((), (mock.Mock(state=app.ProviderState.READY),))
        claude = mock.Mock()
        process = mock.Mock()
        widget_window = mock.Mock()

        def run_one_ui_turn() -> None:
            widget_window.call_args.kwargs["on_sign_in"]("claude")
            scheduled[750].pop(0)()

        root.mainloop.side_effect = run_one_ui_turn
        with (
            mock.patch.object(app, "SingleInstance", return_value=instance),
            mock.patch.object(app, "enable_dpi_awareness"),
            mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
            mock.patch.object(app, "ConfigStore", return_value=config_store),
            mock.patch.object(app, "portable_mode", return_value=False),
            mock.patch.object(app, "trusted_claude_client_present", return_value=True),
            mock.patch.object(app, "production_client"),
            mock.patch.object(app, "MetricStore", return_value=store),
            mock.patch.object(app, "ClaudeRuntime", return_value=claude),
            mock.patch.object(app.tk, "Tk", return_value=root),
            mock.patch.object(app, "WidgetWindow", widget_window),
            mock.patch.object(app, "launch_provider_sign_in", return_value=process),
        ):
            self.assertEqual(launcher.main([]), 0)

        claude.start_if_consented.assert_called_once_with()
        claude.request_refresh.assert_not_called()
        process.poll.assert_not_called()

    def test_duplicate_app_launch_only_signals_and_never_opens_configure(self) -> None:
        instance = mock.Mock(acquired=False)
        with (
            mock.patch.object(app, "SingleInstance", return_value=instance),
            mock.patch.object(app, "enable_dpi_awareness") as dpi,
            mock.patch.object(app, "_open_provider_free_configuration") as configure,
        ):
            self.assertEqual(launcher.main([]), 0)
        instance.request_activation.assert_called_once_with()
        instance.close.assert_called_once_with()
        dpi.assert_not_called()
        configure.assert_not_called()

        startup_instance = mock.Mock(acquired=False)
        with (
            mock.patch.object(app, "SingleInstance", return_value=startup_instance),
            mock.patch.object(app, "enable_dpi_awareness") as startup_dpi,
            mock.patch.object(app, "_open_provider_free_configuration") as startup_configure,
        ):
            self.assertEqual(launcher.main(["--startup"]), 0)
        startup_instance.request_activation.assert_not_called()
        startup_instance.close.assert_called_once_with()
        startup_dpi.assert_not_called()
        startup_configure.assert_not_called()

    def test_enabled_unavailable_and_present_provider_keep_hud_route(self) -> None:
        for codex_mode, candidate, postinstall_signal, expected_activation, arguments in (
            ("enabled", None, "1", True, []),
            ("auto", object(), None, False, []),
            ("auto", object(), "", False, []),
            ("auto", object(), "0", False, []),
            ("auto", object(), "true", False, []),
            ("auto", object(), "01", False, []),
            ("auto", object(), " 1", False, []),
            ("auto", object(), None, False, ["--startup"]),
        ):
            with self.subTest(codex=codex_mode, present=candidate is not None, signal=postinstall_signal):
                config = app.AppConfig(codexMode=codex_mode, claudeMode="disabled")
                instance = mock.Mock(acquired=True)
                config_store = mock.Mock()
                config_store.load.return_value = config
                root = mock.Mock()
                store = mock.Mock()
                claude = mock.Mock()
                collector = mock.Mock()
                with (
                    mock.patch.object(app, "SingleInstance", return_value=instance),
                    mock.patch.object(app, "enable_dpi_awareness"),
                    mock.patch.object(app, "state_root", return_value=ROOT / "fixture-state"),
                    mock.patch.object(app, "ConfigStore", return_value=config_store),
                    mock.patch.object(app, "portable_mode", return_value=False),
                    mock.patch.object(app, "discover_public_codex", return_value=candidate),
                    mock.patch.object(app, "_open_provider_free_configuration") as configure,
                    mock.patch.object(app, "MetricStore", return_value=store),
                    mock.patch.object(app, "ClaudeRuntime", return_value=claude),
                    mock.patch.object(app, "CodexCollector", return_value=collector),
                    mock.patch.object(app.tk, "Tk", return_value=root) as tk_root,
                    mock.patch.object(app, "WidgetWindow") as widget_window,
                    mock.patch.dict(os.environ, {}),
                ):
                    if postinstall_signal is None:
                        os.environ.pop(app.POSTINSTALL_OPEN_ENVIRONMENT, None)
                    else:
                        os.environ[app.POSTINSTALL_OPEN_ENVIRONMENT] = postinstall_signal
                    self.assertEqual(launcher.main(arguments), 0)
                configure.assert_not_called()
                tk_root.assert_called_once_with()
                widget_window.assert_called_once()
                root.mainloop.assert_called_once_with()
                claude.shutdown.assert_called_once_with()
                instance.close.assert_called_once_with()
                if expected_activation:
                    widget_window.return_value.activate_from_shortcut.assert_called_once_with()
                else:
                    widget_window.return_value.activate_from_shortcut.assert_not_called()
                self.assertNotIn(app.POSTINSTALL_OPEN_ENVIRONMENT, os.environ)
                if candidate is None:
                    collector.start.assert_not_called()
                else:
                    collector.start.assert_called_once_with()

    def test_only_explicit_startup_may_take_the_provider_free_quiet_exit(self) -> None:
        source = (ROOT / "src" / "limit_halo" / "app.py").read_text(encoding="utf-8")
        fallback = source[
            source.index("    if not (", source.index("def main(*,")):
            source.index("    enable_dpi_awareness()")
        ]
        self.assertIn("if startup_launch:", fallback)
        self.assertIn("configure_result = _open_provider_free_configuration(", fallback)
        self.assertIn("current_config = config_store.load()", fallback)
        self.assertIn("return configure_result", fallback)
        self.assertEqual(fallback.count("if startup_launch:"), 1)

    def test_installer_selects_exactly_one_applicable_finish_action_by_default(self) -> None:
        installer = (ROOT / "packaging" / "installer" / "LimitHalo.iss").read_text(encoding="utf-8")
        run_lines = [line for line in installer.splitlines() if line.startswith('Filename: "{app}\\versions\\{#CandidateId}')]
        self.assertEqual(len(run_lines), 3)
        consent, hud, configure = run_lines
        self.assertIn('Parameters: "--grant-claude-quota-access"', consent)
        self.assertIn("runhidden waituntilterminated", consent)
        self.assertIn("Check: ShouldApplyClaudeConsent", consent)
        self.assertTrue(all("postinstall" in line and "skipifsilent" not in line for line in (hud, configure)))
        self.assertTrue(all("unchecked" not in line for line in (hud, configure)))
        self.assertIn("Check: ShouldOfferHudLaunch", hud)
        self.assertIn("Check: ShouldOfferConfigureLaunch", configure)
        self.assertIn("BeforeInstall: PrepareHudPostInstallLaunch", hud)
        self.assertIn("AfterInstall: ClearPostInstallLaunchSignal", hud)
        self.assertIn("BeforeInstall: PrepareConfigurePostInstallLaunch", configure)
        self.assertIn("SetEnvironmentVariable('LIMIT_HALO_POSTINSTALL_OPEN', '1')", installer)
        self.assertGreaterEqual(installer.count("SetEnvironmentVariable('LIMIT_HALO_POSTINSTALL_OPEN', '0')"), 2)
        self.assertEqual(installer.count("LIMIT_HALO_POSTINSTALL_ROUTE=HUD"), 1)
        self.assertEqual(installer.count("LIMIT_HALO_POSTINSTALL_ROUTE=CONFIGURE"), 1)

    def test_installer_offers_explicit_quota_consent_and_connects_on_first_launch(self) -> None:
        installer = (ROOT / "packaging" / "installer" / "LimitHalo.iss").read_text(encoding="utf-8")
        self.assertIn("ClaudeAccessPage := CreateInputOptionPage(ClaudePage.ID", installer)
        self.assertIn("ClaudeAccessPage.Values[0] := False;", installer)
        self.assertIn("I allow quota-only Claude access.", installer)
        self.assertIn("Я дозволяю доступ лише до квоти Claude.", installer)
        self.assertIn("function ClaudeConsentValue(): string;", installer)
        self.assertIn("'  \"claudeConsent\": \"' + ClaudeConsentValue() + '\",'", installer)
        self.assertIn("the first widget launch connects immediately", installer)
        self.assertIn("api.anthropic.com/api/oauth/usage", installer)
        self.assertIn("platform.claude.com/v1/oauth/token", installer)
        self.assertIn('Parameters: "--grant-claude-quota-access"', installer)
        self.assertIn("function ShouldApplyClaudeConsent(): Boolean;", installer)
        self.assertNotIn("param:PLANCLAUDECONSENT", installer)

    def test_installer_postinstall_gate_has_exact_silent_truth_table(self) -> None:
        installer = (ROOT / "packaging" / "installer" / "LimitHalo.iss").read_text(encoding="utf-8")
        self.assertIn(
            "Result := (not WizardSilent) or\n"
            "    (SilentDefaultsAccepted() and (ExpandConstant('{param:OPENAFTERINSTALL|0}') = '1'));",
            installer,
        )
        self.assertIn(
            "Result := CandidateHealthPassed and PostInstallOpenPermitted() and HudTargetSelected();",
            installer,
        )
        self.assertIn(
            "Result := CandidateHealthPassed and PostInstallOpenPermitted() and not HudTargetSelected();",
            installer,
        )
        self.assertNotIn("not ShouldOfferHudLaunch()", installer)

        def branches(*, silent: bool, accepted: bool, open_value: str, hud: bool) -> tuple[bool, bool]:
            permitted = (not silent) or (silent and accepted and open_value == "1")
            return permitted and hud, permitted and not hud

        for silent, accepted, open_value, expected_count in (
            (False, False, "0", 1),
            (True, True, "1", 1),
            (True, True, "0", 0),
            (True, True, "true", 0),
            (True, True, "01", 0),
            (True, False, "1", 0),
        ):
            for hud in (False, True):
                with self.subTest(silent=silent, accepted=accepted, value=open_value, hud=hud):
                    self.assertEqual(sum(branches(silent=silent, accepted=accepted, open_value=open_value, hud=hud)), expected_count)

    def test_installer_uses_supported_input_option_properties(self) -> None:
        installer = (ROOT / "packaging" / "installer" / "LimitHalo.iss").read_text(encoding="utf-8")
        self.assertNotIn(".Selected[", installer)
        self.assertIn(".SelectedValueIndex", installer)
        self.assertIn("PreferencesPage.Values[0]", installer)
        self.assertIn("(PageID = ConfirmPage.ID)", installer[installer.index("function ShouldSkipPage"):])
        self.assertNotIn("  ConfigPath: string;", installer)

    def test_silent_install_requires_explicit_default_acceptance_without_second_interactive_checkbox(self) -> None:
        installer = (ROOT / "packaging" / "installer" / "LimitHalo.iss").read_text(encoding="utf-8")
        helper = installer.index("function SilentDefaultsAccepted(): Boolean;")
        handler = installer.index("function PrepareToInstall(var NeedsRestart: Boolean): String;")
        self.assertLess(helper, handler)
        self.assertIn("Result := WizardSilent and (ExpandConstant('{param:ACCEPTDEFAULTS|0}') = '1');", installer)
        silent_gate = installer[handler:installer.index("function ReadCandidateMarker", handler)]
        self.assertIn("if WizardSilent and not SilentDefaultsAccepted() then", silent_gate)
        self.assertIn("LIMIT_HALO_SILENT_DEFAULTS_ACCEPTANCE_REQUIRED", silent_gate)
        skip = installer[installer.index("function ShouldSkipPage"):handler]
        self.assertIn("(PageID = ConfirmPage.ID)", skip)
        self.assertIn("(PageID = ReviewPage.ID)", skip)
        self.assertNotIn("A human must review and confirm the plan before installation.", installer)

    def test_fresh_installed_defaults_autostart_and_remove_redundant_settings_shortcut(self) -> None:
        installer = (ROOT / "packaging" / "installer" / "LimitHalo.iss").read_text(encoding="utf-8")
        self.assertIn("{param:PLANAUTOSTART|1}'), True", installer)
        shortcut_writer = installer[
            installer.index("procedure WriteOwnedShortcuts"):
            installer.index("function HudTargetSelected")
        ]
        self.assertIn("RunOwnedShortcut('Remove', 'Configure', Candidate);", shortcut_writer)
        self.assertNotIn("RunOwnedShortcut('Ensure', 'Configure', Candidate);", shortcut_writer)

    def test_installer_threshold_copy_and_language_default_are_explicit(self) -> None:
        installer = (ROOT / "packaging" / "installer" / "LimitHalo.iss").read_text(encoding="utf-8")
        for text in (
            "Low-limit warning",
            "When should the widget warn you?",
            "The widget will show one notification when the selected amount remains. It can notify you again after the limit resets.",
            "20% remaining (recommended)",
            "10% remaining",
            "5% remaining",
            "Попередження про малий залишок",
            "Коли попереджати про малий залишок?",
            "Віджет один раз покаже сповіщення, коли залишиться вибрана кількість ліміту. Після оновлення ліміту він зможе попередити знову.",
            "20% залишилось (рекомендовано)",
            "10% залишилось",
            "5% залишилось",
        ):
            with self.subTest(text=text):
                self.assertIn(text, installer)
        for old_text in (
            "Notification threshold",
            "Поріг сповіщення",
            "Save a threshold for a future downward crossing.",
            "Збережіть поріг для наступного перетину вниз.",
            "Only one notification may be shown per provider, quota window, threshold, and reset cycle.",
            "Для кожного постачальника, вікна квоти, порога й циклу скидання дозволено лише одне сповіщення.",
        ):
            with self.subTest(old_text=old_text):
                self.assertNotIn(old_text, installer)

        self.assertIn("LanguageDetectionMethod=none", installer)
        self.assertIn("UsePreviousLanguage=no", installer)
        self.assertIn("ShowLanguageDialog=yes", installer)
        self.assertLess(installer.index('Name: "english"'), installer.index('Name: "ukrainian"'))
        self.assertIn("if Value = '10' then Result := 1", installer)
        self.assertIn("else if Value = '5' then Result := 2", installer)
        self.assertIn("if ThresholdPage.SelectedValueIndex = 1 then Result := 10", installer)
        self.assertIn("else if ThresholdPage.SelectedValueIndex = 2 then Result := 5", installer)
        self.assertIn("else Result := 20;", installer)

    def test_assets_keep_package_relative_layout(self) -> None:
        spec = (ROOT / "packaging" / "pyinstaller" / "AILimitsWidget.spec").read_text(encoding="utf-8")
        self.assertIn('datas.append((str(candidate), "limit_halo/assets"))', spec)
        self.assertIn('("app.ico", "chatgpt.png", "claude.png",', spec)

    def test_installer_validation_stays_windows_powershell_compatible(self) -> None:
        cleanup = (ROOT / "packaging" / "installer" / "Invoke-ManifestOwnedCleanup.ps1").read_text(encoding="utf-8")
        self.assertNotIn("IsPathFullyQualified", cleanup)
        self.assertNotIn("Get-FileHash", cleanup)
        self.assertIn("IsPathRooted", cleanup)
        self.assertIn("function Get-Sha256", cleanup)

        verifier = (ROOT / "packaging" / "scripts" / "Verify-Release.ps1").read_text(encoding="utf-8")
        self.assertNotIn("IsPathFullyQualified", verifier)
        self.assertIn("IsPathRooted", verifier)


if __name__ == "__main__":
    unittest.main()
