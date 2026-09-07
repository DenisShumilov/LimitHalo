from __future__ import annotations

import ast
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from limit_halo.auth import AuthLaunchFailure, TrustedAuthClient, _default_claude_candidates, claude_client_installed, launch_provider_sign_in, launch_trusted_client, trusted_claude_client
from limit_halo.claude_protocol import BROKER_METRIC_KEYS, BrokerFailure, BrokerResult, ClaudeRuntime, parse_broker_line
from limit_halo.codex_discovery import (
    APPX_EXE_RELATIVE,
    CODE_SIGNING_EKU,
    ERROR_RESOURCE_TYPE_NOT_FOUND,
    EXPECTED_SUBJECT,
    NPM_EXE_RELATIVE,
    NPM_NATIVE_NAME,
    PACKAGE_PUBLISHER,
    PACKAGE_PUBLISHER_ID,
    STORE_PACKAGE_PUBLISHER,
    STORE_PACKAGE_PUBLISHER_ID,
    AppxRecord,
    CodexCandidate,
    DiscoveryFailure,
    TrustRecord,
    Version,
    _compare_string_ordinal_ignore_case,
    _reject_existing_reparse_chain,
    _require_pe_version,
    appx_runnable_mirror,
    appx_candidate,
    discover_from_sources,
    executable_sha256,
    npm_candidate,
    read_pe_file_version,
    select_candidate,
    verify_candidate_unchanged,
)
from limit_halo.codex_protocol import (
    AuthorizationRequired,
    CodexCollector,
    CodexSession,
    MAX_JSON_NESTING,
    ProtocolFailure,
    SubprocessTransport,
    _load_object,
    account_is_usable,
    app_server_command,
    normalize_rate_limits,
)
from limit_halo.config import AppConfig, ConfigStore, decode_config, safe_defaults
from limit_halo.configure import persist_configuration
from limit_halo.diagnostics import SAFE_FIELDS, diagnostics_json, safe_diagnostics
from limit_halo.model import (
    CLAUDE_FAILURE_BACKOFF_SECONDS,
    CLAUDE_POLL_SECONDS,
    CLAUDE_STALE_SECONDS,
    CODEX_POLL_SECONDS,
    CODEX_RESTART_BACKOFF_SECONDS,
    CODEX_STALE_SECONDS,
    Metric,
    MetricStore,
    ProviderState,
    RefreshCoalescer,
    LaneVisibility,
    resolve_provider_visibility,
)
from limit_halo.ui import WidgetWindow, compute_layout, metric_visible_text


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class FakeTransport:
    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self.incoming = list(incoming)
        self.sent: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        del timeout
        return self.incoming.pop(0) if self.incoming else None

    def close(self) -> None:
        return


def valid_trust() -> TrustRecord:
    return TrustRecord(True, True, dict(EXPECTED_SUBJECT), frozenset({CODE_SIGNING_EKU}))


class G03ProtocolTests(unittest.TestCase):
    @staticmethod
    def account_frame(request_id: int = 2, *, present: bool = False, required: bool = False) -> dict[str, Any]:
        return {
            "id": request_id,
            "result": {
                "account": {"type": "chatgpt"} if present else None,
                "requiresOpenaiAuth": required,
            },
        }

    def test_frozen_codex_fixture_corpus(self) -> None:
        corpus = json.loads((FIXTURES / "codex_frames.json").read_text(encoding="utf-8"))
        now = corpus["observedAt"]
        for case in corpus["accepted"]:
            with self.subTest(case=case["name"]):
                metrics = normalize_rate_limits(case["result"], observed_at=now)
                self.assertEqual([item.window_duration_mins for item in metrics], case["durations"])
        for case in corpus["rejected"]:
            with self.subTest(case=case["name"]):
                with self.assertRaises(ProtocolFailure):
                    normalize_rate_limits(case["result"], observed_at=now)

    def test_codex_protocol_is_one_in_session_handshake_without_probe(self) -> None:
        now = int(time.time())
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"windowDurationMins": 300, "usedPercent": 25, "resetsAt": now + 10_000},
                "secondary": {"windowDurationMins": 10_080, "usedPercent": 50, "resetsAt": now + 500_000},
            }
        }
        transport = FakeTransport([{"id": 1, "result": {}}, self.account_frame(), {"id": 3, "result": result}])
        metrics = CodexSession(transport).initialize_and_read()
        self.assertEqual([message["method"] for message in transport.sent], [
            "initialize", "initialized", "account/read", "account/rateLimits/read"
        ])
        self.assertEqual(transport.sent[2]["params"], {"refreshToken": False})
        self.assertEqual(len(metrics), 2)
        self.assertFalse(any(message["method"].startswith(("thread/", "turn/")) for message in transport.sent))
        bad = FakeTransport([{"method": "thread/started", "params": {}}, {"id": 1, "result": {}}])
        with self.assertRaises(ProtocolFailure):
            CodexSession(bad).initialize_and_read()

    def test_exact_account_read_classification_without_retaining_account_data(self) -> None:
        self.assertFalse(account_is_usable({"account": None, "requiresOpenaiAuth": True}))
        self.assertTrue(account_is_usable({"account": None, "requiresOpenaiAuth": False}))
        self.assertTrue(account_is_usable({"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}))
        self.assertTrue(account_is_usable({
            "account": {"type": "chatgpt", "ignored": "metadata"},
            "requiresOpenaiAuth": False,
            "email": "ignored@example.invalid",
        }))
        for malformed in (
            {"account": None},
            {"account": None, "requiresOpenaiAuth": 1},
            {"account": "identifier", "requiresOpenaiAuth": False},
        ):
            with self.assertRaises(ProtocolFailure):
                account_is_usable(malformed)
        session = CodexSession(FakeTransport([{"id": 1, "result": {}}, self.account_frame(required=True)]))
        with self.assertRaises(AuthorizationRequired):
            session.initialize_and_read()
        store = mock.Mock()
        candidate = CodexCandidate("npm", Version.parse("1.0.0"), Path("codex.exe"), Path("."))
        collector = CodexCollector(
            store,
            discovery=lambda: candidate,
            transport_factory=lambda _candidate: FakeTransport([
                {"id": 1, "result": {}}, self.account_frame(required=True)
            ]),
        )
        store.mark_provider.side_effect = lambda *_args, **_kwargs: collector.request_stop()
        collector._run()
        store.mark_provider.assert_called_once_with(
            "codex", ProviderState.AUTHORIZATION_REQUIRED, error_code="authorization_required"
        )

    def test_only_exact_full_rate_limit_update_is_accepted_in_every_phase(self) -> None:
        now = int(time.time())
        snapshot = {
            "limitId": "codex",
            "primary": {"windowDurationMins": 300, "usedPercent": 25, "resetsAt": now + 10_000},
            "secondary": {"windowDurationMins": 10_080, "usedPercent": 50, "resetsAt": now + 500_000},
        }
        update = {"method": "account/rateLimits/updated", "params": {"rateLimits": snapshot}}
        result = {"rateLimits": snapshot}

        initialize_phase = FakeTransport([update, {"id": 1, "result": {}}, self.account_frame(), {"id": 3, "result": result}])
        session = CodexSession(initialize_phase)
        self.assertEqual(len(session.initialize_and_read()), 2)
        self.assertTrue(session.wait_for_update(0))

        read_phase = FakeTransport([{"id": 1, "result": {}}, self.account_frame(), update, {"id": 3, "result": result}])
        self.assertEqual(len(CodexSession(read_phase).initialize_and_read()), 2)

        steady = CodexSession(FakeTransport([update]))
        self.assertTrue(steady.wait_for_update(0.1))

        malformed = [
            {"method": "account/updated", "params": {}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {**snapshot, "primary": None}}},
            {
                "method": "account/rateLimits/updated",
                "params": {"rateLimits": {**snapshot, "primary": {**snapshot["primary"], "usedPercent": "25"}}},
            },
            {"method": "remoteControl/status/changed", "params": {}, "emittedAtMs": -1},
            {"id": 9, "method": "server/request", "params": {}},
        ]
        for phase in ("initialize", "read", "steady"):
            for message in malformed:
                with self.subTest(phase=phase, message=message):
                    if phase == "initialize":
                        incoming = [message, {"id": 1, "result": {}}, self.account_frame(), {"id": 3, "result": result}]
                        action = lambda incoming=incoming: CodexSession(FakeTransport(incoming)).initialize_and_read()
                    elif phase == "read":
                        incoming = [{"id": 1, "result": {}}, self.account_frame(), message, {"id": 3, "result": result}]
                        action = lambda incoming=incoming: CodexSession(FakeTransport(incoming)).initialize_and_read()
                    else:
                        action = lambda message=message: CodexSession(FakeTransport([message])).wait_for_update(0.1)
                    with self.assertRaises(ProtocolFailure):
                        action()

    def test_current_additive_codex_protocol_projection(self) -> None:
        now = int(time.time())
        primary = {
            "windowDurationMins": 300,
            "usedPercent": 25,
            "resetsAt": now + 10_000,
            "planType": "ignored-additive-metadata",
        }
        snapshot = {
            "limitId": "codex",
            "primary": primary,
            "secondary": None,
            "ignoredSnapshotField": {"nested": True},
        }
        result = {
            "rateLimits": snapshot,
            "rateLimitsByLimitId": {
                "codex": snapshot,
                "codex_bengalfox": {
                    "limitId": "codex_bengalfox",
                    "primary": None,
                },
            },
            "rateLimitResetCredits": {"available": 0},
        }
        remote_status = {
            "method": "remoteControl/status/changed",
            "params": {"ignored": {"deep": "metadata"}},
            "emittedAtMs": now * 1_000,
        }
        transport = FakeTransport([
            remote_status,
            {"id": 1, "result": {}},
            remote_status,
            {
                "id": 2,
                "result": {
                    "account": {"type": "chatgpt", "ignored": "metadata"},
                    "requiresOpenaiAuth": False,
                    "email": "ignored@example.invalid",
                },
            },
            remote_status,
            {"id": 3, "result": result},
        ])
        metrics = CodexSession(transport).initialize_and_read()
        self.assertEqual(
            [(metric.key, metric.used_percent, 100 - metric.used_percent) for metric in metrics],
            [("codex_300", 25, 75)],
        )

        update = {
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": snapshot, "ignoredUpdateMetadata": True},
        }
        self.assertTrue(CodexSession(FakeTransport([remote_status, update])).wait_for_update(0.1))

        conflicting = {
            **result,
            "rateLimitsByLimitId": {
                **result["rateLimitsByLimitId"],
                "codex": {
                    **snapshot,
                    "primary": {**primary, "usedPercent": 26},
                },
            },
        }
        with self.assertRaises(ProtocolFailure):
            normalize_rate_limits(conflicting, observed_at=now)
        with self.assertRaises(ProtocolFailure):
            CodexSession(FakeTransport([{
                **remote_status,
                "unexpectedEnvelopeField": True,
            }])).wait_for_update(0.1)

    def test_frame_relaxation_mutants_fail(self) -> None:
        with self.assertRaises(ProtocolFailure):
            _load_object(b'{"id":1,"id":2}')
        with self.assertRaises(ProtocolFailure):
            _load_object(b"{" + b" " * 65_536)
        with self.assertRaises(ProtocolFailure):
            _load_object(b'{"nested":' + b"[" * 3_000 + b"0" + b"]" * 3_000 + b"}")
        with self.assertRaises(ProtocolFailure):
            normalize_rate_limits({"rateLimits": {"limitId": "codex", "primary": {"windowDurationMins": True, "usedPercent": 1, "resetsAt": 2_000_001_000}}}, observed_at=2_000_000_000)

    def test_json_nesting_bound_is_exact_and_ignores_string_content(self) -> None:
        exact = b'{"value":' + (b"[" * (MAX_JSON_NESTING - 1)) + b"0" + (b"]" * (MAX_JSON_NESTING - 1)) + b"}"
        self.assertIsInstance(_load_object(exact), dict)
        over = b'{"value":' + (b"[" * MAX_JSON_NESTING) + b"0" + (b"]" * MAX_JSON_NESTING) + b"}"
        with self.assertRaises(ProtocolFailure):
            _load_object(over)
        string_heavy = json.dumps({"value": r'plain [{ }] and escaped quote \" still [{ }]'}).encode("utf-8")
        self.assertEqual(_load_object(string_heavy)["value"], r'plain [{ }] and escaped quote \" still [{ }]')

    def test_oversized_json_integer_is_typed_in_every_codex_receive_phase(self) -> None:
        oversized = b'{"oversized":' + (b"9" * 4_301) + b"}"
        with self.assertRaises(ProtocolFailure):
            _load_object(oversized)

        class RawTransport:
            def __init__(self, frames: list[bytes]) -> None:
                self.frames = list(frames)
                self.sent: list[dict[str, object]] = []

            def send(self, message: dict[str, object]) -> None:
                self.sent.append(message)

            def receive(self, _timeout: float) -> dict[str, object] | None:
                if not self.frames:
                    return None
                return _load_object(self.frames.pop(0))

            def close(self) -> None:
                return

        valid_initialize = b'{"id":1,"result":{}}'
        valid_account = b'{"id":2,"result":{"account":null,"requiresOpenaiAuth":false}}'
        valid_snapshot = (
            b'{"id":3,"result":{"rateLimits":{"limitId":"codex",'
            b'"primary":{"windowDurationMins":300,"usedPercent":25,"resetsAt":2000010000}}}}'
        )
        for name, frames, action in (
            ("initialize", [oversized], lambda session: session.initialize_and_read()),
            ("account", [valid_initialize, oversized], lambda session: session.initialize_and_read()),
            ("read", [valid_initialize, valid_account, oversized], lambda session: session.initialize_and_read()),
            ("steady", [oversized], lambda session: session.wait_for_update(0.1)),
        ):
            with self.subTest(phase=name):
                with self.assertRaises(ProtocolFailure):
                    action(CodexSession(RawTransport(frames)))

        # A valid frame after the hostile one documents that the parser failure
        # is local to the frame and does not corrupt global decoder state.
        parsed = _load_object(valid_snapshot)
        self.assertEqual(parsed["id"], 3)

    def test_used_percent_is_range_checked_before_conversion_and_stays_protocol_safe(self) -> None:
        now = 2_000_000_000

        def result(used: object) -> dict[str, object]:
            return {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"windowDurationMins": 300, "usedPercent": used, "resetsAt": now + 10_000},
                }
            }

        for used in (-1, 101, 10**10_000, -(10**10_000), float("inf"), float("nan"), 1.5):
            with self.subTest(kind=type(used).__name__):
                with self.assertRaises(ProtocolFailure):
                    normalize_rate_limits(result(used), observed_at=now)
        self.assertEqual(normalize_rate_limits(result(100.0), observed_at=now)[0].used_percent, 100)

        transport = FakeTransport([{"id": 1, "result": {}}, self.account_frame(), {"id": 3, "result": result(10**10_000)}])
        with self.assertRaises(ProtocolFailure):
            CodexSession(transport).initialize_and_read()

        store = mock.Mock()
        candidate = CodexCandidate("npm", Version.parse("1.0.0"), Path("codex.exe"), Path("."))
        collector_transport = FakeTransport([{"id": 1, "result": {}}, self.account_frame(), {"id": 3, "result": result(10**10_000)}])
        collector = CodexCollector(
            store,
            discovery=lambda: candidate,
            transport_factory=lambda _candidate: collector_transport,
        )
        store.mark_provider.side_effect = lambda *_args, **_kwargs: collector.request_stop()
        collector._run()
        store.mark_provider.assert_called_once_with(
            "codex", ProviderState.PROVIDER_ERROR, error_code="codex_unavailable"
        )

    def test_semver_bounds_fail_as_discovery_errors_before_integer_conversion(self) -> None:
        self.assertEqual(Version.parse("65535.65535.65535.65535-rc.1").numbers, (65535, 65535, 65535, 65535))
        rejected = (
            "65536.0.0",
            ("9" * 300) + ".0.0",
            "1.0.0-" + ("9" * 250),
            "1.0.0-" + ".".join("a" for _ in range(17)),
        )
        for text in rejected:
            with self.subTest(length=len(text)):
                with self.assertRaises(DiscoveryFailure):
                    Version.parse(text)
        with self.assertRaises(DiscoveryFailure):
            Version((65_536, 0, 0, 0))

    def test_claude_broker_corpus_is_sanitized(self) -> None:
        corpus = json.loads((FIXTURES / "claude_frames.json").read_text(encoding="utf-8"))
        request = corpus["requestId"]
        sequence = corpus["sequence"]
        encoded = (json.dumps(corpus["accepted"], separators=(",", ":")) + "\n").encode("utf-8")
        result = parse_broker_line(encoded, request_id=request, acquisition_sequence=sequence, received_at=corpus["receivedAt"])
        self.assertEqual(result.status, "OK")
        self.assertEqual([metric.key for metric in result.metrics], ["claude_300", "claude_10080", "claude_fable_10080"])
        self.assertEqual(
            BROKER_METRIC_KEYS,
            {
                "claude_session": "claude_300",
                "claude_weekly_all": "claude_10080",
                "claude_fable_weekly": "claude_fable_10080",
            },
        )
        native_source = (ROOT / "native" / "claude_broker" / "broker_core.cpp").read_text(encoding="utf-8")
        for wire_key in BROKER_METRIC_KEYS:
            self.assertIn(f'appendMetric("{wire_key}"', native_source)
        failure = parse_broker_line(
            (json.dumps(corpus["failure"], separators=(",", ":")) + "\n").encode("utf-8"),
            request_id=request,
            acquisition_sequence=sequence,
            received_at=corpus["receivedAt"],
        )
        self.assertEqual(failure.provider_state, ProviderState.OFFLINE)
        self.assertEqual(failure.redacted_error_code, "transport_timeout")
        mutant = dict(corpus["failure"])
        mutant["rawResponse"] = "secret"
        with self.assertRaises(BrokerFailure):
            parse_broker_line((json.dumps(mutant) + "\n").encode(), request_id=request, acquisition_sequence=sequence, received_at=corpus["receivedAt"])
        old_python_only = json.loads(json.dumps(corpus["accepted"]))
        old_python_only["metrics"] = {
            internal_key: old_python_only["metrics"][wire_key]
            for wire_key, internal_key in BROKER_METRIC_KEYS.items()
        }
        with self.assertRaises(BrokerFailure):
            parse_broker_line(
                (json.dumps(old_python_only) + "\n").encode(),
                request_id=request,
                acquisition_sequence=sequence,
                received_at=corpus["receivedAt"],
            )

    def test_claude_quota_growth_and_missing_window_states_are_not_conflated(self) -> None:
        corpus = json.loads((FIXTURES / "claude_frames.json").read_text(encoding="utf-8"))
        request = corpus["requestId"]
        sequence = corpus["sequence"]
        received_at = corpus["receivedAt"]

        baseline_missing = json.loads(json.dumps(corpus["accepted"]))
        baseline_missing["metrics"]["claude_session"] = None
        with self.assertRaisesRegex(BrokerFailure, "baseline metric unavailable"):
            parse_broker_line(
                (json.dumps(baseline_missing, separators=(",", ":")) + "\n").encode("utf-8"),
                request_id=request,
                acquisition_sequence=sequence,
                received_at=received_at,
            )

        optional_missing = json.loads(json.dumps(corpus["accepted"]))
        optional_missing["metrics"]["claude_fable_weekly"] = None
        partial = parse_broker_line(
            (json.dumps(optional_missing, separators=(",", ":")) + "\n").encode("utf-8"),
            request_id=request,
            acquisition_sequence=sequence,
            received_at=received_at,
        )
        self.assertEqual(
            [metric.state for metric in partial.metrics],
            [ProviderState.READY, ProviderState.READY, ProviderState.UNAVAILABLE],
        )

        store = MetricStore(now=float(received_at))
        self.assertTrue(store.update_claude(partial.metrics, sequence=sequence))
        increased = json.loads(json.dumps(corpus["accepted"]))
        increased["acquisitionSequence"] = sequence + 1
        increased["acquiredAtUnixSeconds"] = received_at + 1
        increased["metrics"]["claude_session"]["remainingMicros"] = 90_000_000
        refreshed = parse_broker_line(
            (json.dumps(increased, separators=(",", ":")) + "\n").encode("utf-8"),
            request_id=request,
            acquisition_sequence=sequence + 1,
            received_at=received_at + 1,
        )
        self.assertTrue(store.update_claude(refreshed.metrics, sequence=sequence + 1))
        _codex, claude = store.snapshot(now=float(received_at + 1))
        self.assertEqual(claude[0].used_percent, 10)
        self.assertIs(claude[0].state, ProviderState.READY)

        older = json.loads(json.dumps(corpus["accepted"]))
        older["acquisitionSequence"] = sequence + 2
        older["acquiredAtUnixSeconds"] = received_at
        older["metrics"]["claude_session"]["remainingMicros"] = 1_000_000
        older_result = parse_broker_line(
            (json.dumps(older, separators=(",", ":")) + "\n").encode("utf-8"),
            request_id=request,
            acquisition_sequence=sequence + 2,
            received_at=received_at + 2,
        )
        self.assertFalse(store.update_claude(older_result.metrics, sequence=sequence + 2))
        self.assertFalse(store.update_claude(refreshed.metrics, sequence=sequence + 1))
        _codex, still_fresh = store.snapshot(now=float(received_at + 2))
        self.assertEqual(still_fresh[0].used_percent, 10)

        exhausted = json.loads(json.dumps(corpus["accepted"]))
        exhausted["metrics"]["claude_session"]["remainingMicros"] = 0
        exhausted_result = parse_broker_line(
            (json.dumps(exhausted, separators=(",", ":")) + "\n").encode("utf-8"),
            request_id=request,
            acquisition_sequence=sequence,
            received_at=received_at,
        )
        self.assertEqual(exhausted_result.metrics[0].used_percent, 100)
        self.assertIs(exhausted_result.metrics[0].state, ProviderState.READY)

        no_reset = json.loads(json.dumps(corpus["accepted"]))
        no_reset["metrics"]["claude_session"]["resetUnixSeconds"] = None
        no_reset_result = parse_broker_line(
            (json.dumps(no_reset, separators=(",", ":")) + "\n").encode("utf-8"),
            request_id=request,
            acquisition_sequence=sequence,
            received_at=received_at,
        )
        self.assertEqual(no_reset_result.metrics[0].used_percent, 25)
        self.assertIsNone(no_reset_result.metrics[0].resets_at)
        malformed_weekly_reset = json.loads(json.dumps(no_reset))
        malformed_weekly_reset["metrics"]["claude_weekly_all"]["resetUnixSeconds"] = None
        with self.assertRaisesRegex(BrokerFailure, "reset unavailable"):
            parse_broker_line(
                (json.dumps(malformed_weekly_reset, separators=(",", ":")) + "\n").encode("utf-8"),
                request_id=request,
                acquisition_sequence=sequence,
                received_at=received_at,
            )

    def test_metric_store_rejects_duplicate_and_non_monotonic_snapshots(self) -> None:
        now = 2_000_000_000.0
        store = MetricStore(now=now)
        first = (
            Metric("claude_300", "claude", 300, 25, None, ProviderState.READY, now),
            Metric("claude_10080", "claude", 10_080, 50, int(now) + 500_000, ProviderState.READY, now),
            Metric("claude_fable_10080", "claude", 10_080, None, None, ProviderState.UNAVAILABLE, now),
        )
        self.assertTrue(store.update_claude(first, sequence=1))
        duplicate = first + (first[-1],)
        with self.assertRaisesRegex(ValueError, "incomplete Claude"):
            store.update_claude(duplicate, sequence=2)
        same_time_new_sequence = tuple(
            replace(metric, used_percent=10 if metric.key == "claude_300" else metric.used_percent)
            for metric in first
        )
        self.assertTrue(store.update_claude(same_time_new_sequence, sequence=2))
        self.assertFalse(store.update_claude(first, sequence=2))
        older = tuple(replace(metric, observed_at=now - 1) for metric in first)
        self.assertFalse(store.update_claude(older, sequence=99))

        prior_with_fable = tuple(
            replace(
                metric,
                used_percent=60,
                resets_at=int(now) + 500_000,
                state=ProviderState.READY,
            )
            if metric.key == "claude_fable_10080"
            else metric
            for metric in same_time_new_sequence
        )
        self.assertTrue(store.update_claude(prior_with_fable, sequence=3))
        optional_absent = tuple(
            replace(
                metric,
                used_percent=None,
                resets_at=None,
                state=ProviderState.UNAVAILABLE,
                observed_at=now + 1,
            )
            if metric.key == "claude_fable_10080"
            else replace(metric, observed_at=now + 1)
            for metric in prior_with_fable
        )
        self.assertTrue(store.update_claude(optional_absent, sequence=4))
        _codex, retained = store.snapshot(now=now + 1)
        retained_fable = next(
            metric for metric in retained if metric.key == "claude_fable_10080"
        )
        self.assertEqual(retained_fable.used_percent, 60)
        self.assertIs(retained_fable.state, ProviderState.STALE)
        self.assertEqual(retained_fable.observed_at, now)

        recovered = tuple(
            replace(
                metric,
                used_percent=40,
                resets_at=int(now) + 600_000,
                state=ProviderState.READY,
                observed_at=now + 2,
                error_code=None,
            )
            if metric.key == "claude_fable_10080"
            else replace(metric, observed_at=now + 2)
            for metric in prior_with_fable
        )
        self.assertTrue(store.update_claude(recovered, sequence=5))
        _codex, current = store.snapshot(now=now + 2)
        current_fable = next(
            metric for metric in current if metric.key == "claude_fable_10080"
        )
        self.assertEqual(current_fable.used_percent, 40)
        self.assertIs(current_fable.state, ProviderState.READY)

        catalog = json.loads(
            (ROOT / "tests" / "mutants" / "mutant-catalog.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(catalog["V7-G02"]),
            {
                "ALL-NULL",
                "DUPLICATE-CANONICAL",
                "MALFORMED",
                "PARTIAL-BASELINE",
                "STALE-SEQUENCE",
                "EQUAL-SEQUENCE-OVERWRITE",
                "CRASH",
                "TIMEOUT",
                "RECOVERY-HIGHER",
                "NEWER-HIGHER-NOT-RENDERED-IN-ONE-TICK",
                "TRUE-ZERO",
                "OPTIONAL-ABSENT",
            },
        )

    def test_codex_store_rejects_equal_and_older_snapshots(self) -> None:
        now = 2_000_000_000.0
        store = MetricStore(now=now)
        first = (
            Metric(
                "codex_300",
                "codex",
                300,
                40,
                int(now) + 3_600,
                ProviderState.READY,
                now,
            ),
        )
        newer_same_clock = tuple(replace(metric, used_percent=30) for metric in first)
        older_late_sequence = tuple(
            replace(metric, used_percent=99, observed_at=now - 1) for metric in first
        )

        self.assertTrue(store.update_codex(first, sequence=1))
        self.assertTrue(store.update_codex(newer_same_clock, sequence=2))
        self.assertFalse(store.update_codex(first, sequence=2))
        self.assertFalse(store.update_codex(older_late_sequence, sequence=99))

        codex, _claude = store.snapshot(now=now)
        self.assertEqual(len(codex), 1)
        self.assertEqual(codex[0].used_percent, 30)

    def test_codex_collector_supplies_a_real_monotonic_acquisition_sequence(self) -> None:
        store = mock.Mock()
        store.update_codex.side_effect = (True, True, False)
        collector = CodexCollector(store, discovery=mock.Mock())
        metric = Metric(
            "codex_300",
            "codex",
            300,
            40,
            2_000_003_600,
            ProviderState.READY,
            2_000_000_000.0,
        )

        collector._accept_snapshot((metric,))
        collector._accept_snapshot((replace(metric, observed_at=2_000_000_001.0),))
        with self.assertRaisesRegex(ProtocolFailure, "non-monotonic"):
            collector._accept_snapshot((metric,))

        self.assertEqual(
            [call.kwargs["sequence"] for call in store.update_codex.call_args_list],
            [1, 2, 3],
        )

    def test_every_named_g02_control_is_executed(self) -> None:
        catalog = json.loads(
            (ROOT / "tests" / "mutants" / "mutant-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        expected = set(catalog["V7-G02"])
        executed: set[str] = set()
        now = 2_000_000_000.0

        def metrics(
            session_used: int,
            observed: float,
            *,
            fable_state: ProviderState = ProviderState.READY,
        ) -> tuple[Metric, ...]:
            return (
                Metric("claude_300", "claude", 300, session_used, None, ProviderState.READY, observed),
                Metric("claude_10080", "claude", 10_080, 50, int(now) + 500_000, ProviderState.READY, observed),
                Metric(
                    "claude_fable_10080",
                    "claude",
                    10_080,
                    60 if fable_state is ProviderState.READY else None,
                    int(now) + 500_000 if fable_state is ProviderState.READY else None,
                    fable_state,
                    observed,
                ),
            )

        store = MetricStore(now=now)
        first = metrics(75, now)
        self.assertTrue(store.update_claude(first, sequence=1))

        all_null = tuple(
            replace(
                metric,
                used_percent=None,
                resets_at=None,
                state=ProviderState.UNAVAILABLE,
                observed_at=now + 1,
            )
            for metric in first
        )
        with self.assertRaisesRegex(ValueError, "baseline"):
            store.update_claude(all_null, sequence=2)
        executed.add("ALL-NULL")

        with self.assertRaisesRegex(ValueError, "incomplete Claude"):
            store.update_claude(first + (first[-1],), sequence=2)
        executed.add("DUPLICATE-CANONICAL")

        corpus = json.loads(
            (FIXTURES / "claude_frames.json").read_text(encoding="utf-8")
        )
        with self.assertRaises(BrokerFailure):
            parse_broker_line(
                b"{malformed\n",
                request_id=corpus["requestId"],
                acquisition_sequence=corpus["sequence"],
                received_at=corpus["receivedAt"],
            )
        executed.add("MALFORMED")

        partial = json.loads(json.dumps(corpus["accepted"]))
        partial["metrics"]["claude_session"] = None
        with self.assertRaisesRegex(BrokerFailure, "baseline metric unavailable"):
            parse_broker_line(
                (json.dumps(partial, separators=(",", ":")) + "\n").encode(),
                request_id=corpus["requestId"],
                acquisition_sequence=corpus["sequence"],
                received_at=corpus["receivedAt"],
            )
        executed.add("PARTIAL-BASELINE")

        older = tuple(replace(metric, observed_at=now - 1) for metric in first)
        self.assertFalse(store.update_claude(older, sequence=99))
        executed.add("STALE-SEQUENCE")
        self.assertFalse(store.update_claude(first, sequence=1))
        executed.add("EQUAL-SEQUENCE-OVERWRITE")

        store.mark_provider(
            "claude", ProviderState.PROVIDER_ERROR, now=now + 1, error_code="broker_crash"
        )
        _codex, after_crash = store.snapshot(now=now + 1)
        self.assertEqual(after_crash[0].used_percent, 75)
        self.assertIs(after_crash[0].state, ProviderState.PROVIDER_ERROR)
        executed.add("CRASH")
        store.mark_provider(
            "claude", ProviderState.OFFLINE, now=now + 1, error_code="timeout"
        )
        _codex, after_timeout = store.snapshot(now=now + 1)
        self.assertEqual(after_timeout[0].used_percent, 75)
        self.assertIs(after_timeout[0].state, ProviderState.OFFLINE)
        executed.add("TIMEOUT")

        recovered = metrics(10, now + 1)
        self.assertTrue(store.update_claude(recovered, sequence=2))
        _codex, current = store.snapshot(now=now + 1)
        self.assertEqual(current[0].used_percent, 10)
        self.assertIs(current[0].state, ProviderState.READY)
        executed.add("RECOVERY-HIGHER")

        window = WidgetWindow.__new__(WidgetWindow)
        window._closing = False
        window._drag_origin = (0, 0)
        window.store = store
        window.config = AppConfig(
            codexMode="disabled", claudeMode="enabled", claudeConsent="granted"
        )
        window.provider_present = lambda provider: provider == "claude"
        window.layout = compute_layout("claude-only", 1.0)
        window.scale = 1.0
        window.root = mock.Mock()
        rendered: list[str] = []

        def capture() -> None:
            visible, _codex_visible, _claude_visible = window._current_metrics()
            rendered.append(metric_visible_text(visible[0], "en")[0])

        window._redraw = capture
        window._sync_z_order = mock.Mock()
        window._emit_notifications = mock.Mock()
        window._tick()
        self.assertEqual(rendered, ["90%·5h"])
        executed.add("NEWER-HIGHER-NOT-RENDERED-IN-ONE-TICK")

        exhausted = metrics(100, now + 2)
        self.assertTrue(store.update_claude(exhausted, sequence=3))
        _codex, zero = store.snapshot(now=now + 2)
        self.assertEqual(metric_visible_text(zero[0], "en")[0], "0%·5h")
        self.assertIs(zero[0].state, ProviderState.READY)
        executed.add("TRUE-ZERO")

        optional_absent = metrics(
            20, now + 3, fable_state=ProviderState.UNAVAILABLE
        )
        self.assertTrue(store.update_claude(optional_absent, sequence=4))
        _codex, retained_optional = store.snapshot(now=now + 3)
        fable = next(
            metric
            for metric in retained_optional
            if metric.key == "claude_fable_10080"
        )
        self.assertEqual(fable.used_percent, 60)
        self.assertIs(fable.state, ProviderState.STALE)
        executed.add("OPTIONAL-ABSENT")

        self.assertEqual(executed, expected)

    def test_native_credential_replace_keeps_a_verified_recovery_copy(self) -> None:
        source = (ROOT / "native" / "claude_broker" / "production_adapter.cpp").read_text(
            encoding="utf-8"
        )
        write_credential = source[source.index("Status ProductionAdapter::WriteCredential(") :]
        self.assertIn('BuildRandomSiblingPath(credentialPath_, L"tmp", temporaryPath)', write_credential)
        self.assertIn('BuildRandomSiblingPath(credentialPath_, L"bak", backupPath)', write_credential)
        self.assertIn(
            "credentialPath_.c_str(), temporaryPath.c_str(), backupPath.c_str()",
            write_credential,
        )
        self.assertNotIn(
            "ReplaceFileW(credentialPath_.c_str(), temporaryPath.c_str(), nullptr",
            write_credential,
        )
        self.assertIn("CopyFileExW(credentialPath.c_str(), temporaryPath.c_str()", source)
        self.assertIn("COPY_FILE_FAIL_IF_EXISTS", source)
        self.assertIn("EnsureNewCredentialAtCanonical(", write_credential)
        self.assertIn("InspectExactFile(temporaryPath, replacement)", source)
        self.assertIn("MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH", source)
        self.assertIn("DeleteExactFileIfPresent(temporaryPath, nextFullDocument)", write_credential)
        self.assertIn("DeleteExactFileIfPresent(backupPath, expected.storageDocument)", write_credential)

    def test_oversized_json_integer_is_typed_by_claude_broker_decoder(self) -> None:
        request_id = "0123456789abcdef0123456789abcdef"
        line = b'{"oversized":' + (b"9" * 4_301) + b"}\n"
        with self.assertRaises(BrokerFailure):
            parse_broker_line(
                line,
                request_id=request_id,
                acquisition_sequence=1,
                received_at=2_000_000_000,
            )
        nested = b'{"nested":' + b"[" * 3_000 + b"0" + b"]" * 3_000 + b"}\n"
        with self.assertRaises(BrokerFailure):
            parse_broker_line(nested, request_id=request_id, acquisition_sequence=1)

    def test_public_discovery_fixtures_and_tie_break(self) -> None:
        self.assertGreater(Version.parse("2.3.4"), Version.parse("2.3.4-rc.1"))
        self.assertGreater(Version.parse("2.3.4-rc.2"), Version.parse("2.3.4-rc.1"))
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            root = Path(temporary)
            npm_root = root / "npm" / "node_modules" / "@openai" / "codex"
            native_root = npm_root / "node_modules" / "@openai" / "codex-win32-x64"
            executable = native_root / NPM_EXE_RELATIVE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture-pe")
            npm_root.mkdir(parents=True, exist_ok=True)
            (npm_root / "package.json").write_text(json.dumps({
                "name": "@openai/codex", "version": "2.3.4",
                "optionalDependencies": {"@openai/codex-win32-x64": "=2.3.4"},
            }), encoding="utf-8")
            (native_root / "package.json").write_text(json.dumps({
                "name": "@openai/codex-win32-x64", "version": "2.3.4"
            }), encoding="utf-8")
            npm = npm_candidate(npm_root, trust_verifier=lambda _p: valid_trust(), pe_version_reader=lambda _p: (2, 3, 4, 0))
            self.assertEqual(npm.source, "npm")

            appx_root = root / "appx"
            appx_executable = appx_root / APPX_EXE_RELATIVE
            appx_executable.parent.mkdir(parents=True)
            appx_executable.write_bytes(b"fixture-pe")
            record = AppxRecord(
                "OpenAI.Codex",
                PACKAGE_PUBLISHER,
                PACKAGE_PUBLISHER_ID,
                f"OpenAI.Codex_{PACKAGE_PUBLISHER_ID}",
                f"OpenAI.Codex_2.3.4.0_x64__{PACKAGE_PUBLISHER_ID}",
                "2.3.4.0",
                appx_root,
            )
            appx = appx_candidate(record, trust_verifier=lambda _p: valid_trust(), pe_version_reader=lambda _p: (2, 3, 4, 0))
            with self.assertRaises(DiscoveryFailure):
                select_candidate((npm, appx))
            newer = replace(npm, version=Version.parse("3.0.0"))
            self.assertEqual(select_candidate((npm, newer)).version, Version.parse("3.0.0"))

            invalid = TrustRecord(True, False, dict(EXPECTED_SUBJECT), frozenset({CODE_SIGNING_EKU}))
            with self.assertRaises(DiscoveryFailure):
                discover_from_sources(
                    npm_codex_root=npm_root,
                    appx_records=(record,),
                    trust_verifier=lambda _p: invalid,
                    pe_version_reader=lambda _p: (2, 3, 4, 0),
                )

    def test_current_codex_metadata_profiles_are_exact_and_version_absence_is_narrow(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            root = Path(temporary)
            npm_root = root / "npm" / "node_modules" / "@openai" / "codex"
            native_root = npm_root / "node_modules" / "@openai" / "codex-win32-x64"
            executable = native_root / NPM_EXE_RELATIVE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture-pe-without-version-resource")
            npm_root.mkdir(parents=True, exist_ok=True)

            def write_current_npm(
                *,
                declaration: str = "npm:@openai/codex@0.139.0-win32-x64",
                native_overrides: dict[str, Any] | None = None,
                duplicate_declaration: bool = False,
            ) -> None:
                package: dict[str, Any] = {
                    "name": "@openai/codex",
                    "version": "0.139.0",
                    "optionalDependencies": {NPM_NATIVE_NAME: declaration},
                }
                if duplicate_declaration:
                    package["dependencies"] = {NPM_NATIVE_NAME: declaration}
                native: dict[str, Any] = {
                    "name": "@openai/codex",
                    "version": "0.139.0-win32-x64",
                    "os": ["win32"],
                    "cpu": ["x64"],
                }
                native.update(native_overrides or {})
                (npm_root / "package.json").write_text(json.dumps(package), encoding="utf-8")
                (native_root / "package.json").write_text(json.dumps(native), encoding="utf-8")

            write_current_npm()
            trust_calls: list[Path] = []

            def trusted(path: Path) -> TrustRecord:
                trust_calls.append(path)
                return valid_trust()

            current_npm = npm_candidate(
                npm_root,
                trust_verifier=trusted,
                pe_version_reader=lambda _p: None,
            )
            self.assertEqual(current_npm.version, Version.parse("0.139.0"))
            self.assertEqual(trust_calls, [executable.resolve(strict=True)])
            _require_pe_version(executable, Version.parse("0.139.0"), lambda _p: None)
            with self.assertRaises(DiscoveryFailure):
                _require_pe_version(executable, Version.parse("0.139.0"), lambda _p: (0, 138, 0, 0))

            def raising_reader(_path: Path) -> tuple[int, int, int, int]:
                raise OSError("not the explicit absent-version sentinel")

            with self.assertRaises(DiscoveryFailure):
                npm_candidate(npm_root, trust_verifier=lambda _p: valid_trust(), pe_version_reader=raising_reader)

            invalid_trust = TrustRecord(True, False, dict(EXPECTED_SUBJECT), frozenset({CODE_SIGNING_EKU}))
            with self.assertRaises(DiscoveryFailure):
                npm_candidate(npm_root, trust_verifier=lambda _p: invalid_trust, pe_version_reader=lambda _p: None)

            npm_mutants = (
                ("npm:@openai/not-codex@0.139.0-win32-x64", {}),
                ("^0.139.0", {}),
                ("npm:@openai/codex@0.139.0-win32-arm64", {}),
                ("npm:@openai/codex@0.139.0-win32-x64", {"name": NPM_NATIVE_NAME}),
                ("npm:@openai/codex@0.139.0-win32-x64", {"version": "0.140.0-win32-x64"}),
                ("npm:@openai/codex@0.139.0-win32-x64", {"os": ["linux"]}),
                ("npm:@openai/codex@0.139.0-win32-x64", {"cpu": ["arm64"]}),
            )
            for declaration, overrides in npm_mutants:
                with self.subTest(declaration=declaration, overrides=overrides):
                    write_current_npm(declaration=declaration, native_overrides=overrides)
                    with self.assertRaises(DiscoveryFailure):
                        npm_candidate(npm_root, trust_verifier=lambda _p: valid_trust(), pe_version_reader=lambda _p: None)
            write_current_npm(duplicate_declaration=True)
            with self.assertRaises(DiscoveryFailure):
                npm_candidate(npm_root, trust_verifier=lambda _p: valid_trust(), pe_version_reader=lambda _p: None)

            appx_root = root / "appx"
            appx_executable = appx_root / APPX_EXE_RELATIVE
            appx_executable.parent.mkdir(parents=True)
            appx_executable.write_bytes(b"fixture-pe-without-version-resource")
            current_record = AppxRecord(
                "OpenAI.Codex",
                STORE_PACKAGE_PUBLISHER,
                STORE_PACKAGE_PUBLISHER_ID,
                f"OpenAI.Codex_{STORE_PACKAGE_PUBLISHER_ID}",
                f"OpenAI.Codex_26.820.9563.0_x64__{STORE_PACKAGE_PUBLISHER_ID}",
                "26.820.9563.0",
                appx_root,
            )
            current_appx = appx_candidate(
                current_record,
                trust_verifier=lambda _p: valid_trust(),
                pe_version_reader=lambda _p: None,
            )
            self.assertEqual(current_appx.source, "appx")
            appx_mutants = (
                replace(current_record, publisher="CN=Unexpected"),
                replace(current_record, publisher_id="aaaaaaaaaaaaa"),
                replace(current_record, family_name="OpenAI.Codex_aaaaaaaaaaaaa"),
                replace(current_record, full_name=f"OpenAI.Codex_26.820.9563.0_arm64__{STORE_PACKAGE_PUBLISHER_ID}"),
                replace(current_record, full_name=f"OpenAI.Codex_26.820.9563.1_x64__{STORE_PACKAGE_PUBLISHER_ID}"),
            )
            for mutant in appx_mutants:
                with self.subTest(mutant=mutant):
                    with self.assertRaises(DiscoveryFailure):
                        appx_candidate(mutant, trust_verifier=lambda _p: valid_trust(), pe_version_reader=lambda _p: None)
            with self.assertRaises(DiscoveryFailure):
                appx_candidate(current_record, trust_verifier=lambda _p: invalid_trust, pe_version_reader=lambda _p: None)
            with self.assertRaises(DiscoveryFailure):
                appx_candidate(current_record, trust_verifier=lambda _p: valid_trust(), pe_version_reader=lambda _p: (26, 820, 9562, 0))

        class FakeFunction:
            def __init__(self) -> None:
                self.argtypes = None
                self.restype = None

            def __call__(self, *_args: object) -> int:
                return 0

        class FakeVersionDll:
            def __init__(self) -> None:
                self.GetFileVersionInfoSizeW = FakeFunction()

        with mock.patch("limit_halo.codex_discovery.ctypes.WinDLL", return_value=FakeVersionDll()):
            with mock.patch("limit_halo.codex_discovery.ctypes.get_last_error", return_value=ERROR_RESOURCE_TYPE_NOT_FOUND):
                self.assertIsNone(read_pe_file_version(Path("codex.exe")))
            with mock.patch("limit_halo.codex_discovery.ctypes.get_last_error", return_value=5):
                with self.assertRaises(OSError):
                    read_pe_file_version(Path("codex.exe"))

    def test_appx_runnable_mirror_requires_exact_bytes_and_precedes_npm(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            root = Path(temporary)
            payload = b"official-codex-fixture-bytes"
            appx_root = root / "appx"
            appx_executable = appx_root / APPX_EXE_RELATIVE
            appx_executable.parent.mkdir(parents=True)
            appx_executable.write_bytes(payload)
            record = AppxRecord(
                "OpenAI.Codex",
                STORE_PACKAGE_PUBLISHER,
                STORE_PACKAGE_PUBLISHER_ID,
                f"OpenAI.Codex_{STORE_PACKAGE_PUBLISHER_ID}",
                f"OpenAI.Codex_26.825.6671.0_x64__{STORE_PACKAGE_PUBLISHER_ID}",
                "26.825.6671.0",
                appx_root,
            )
            reference = appx_candidate(
                record,
                trust_verifier=lambda _path: valid_trust(),
                pe_version_reader=lambda _path: None,
                trusted_root=root,
            )
            with mock.patch("limit_halo.codex_discovery.read_pe_file_version", return_value=None), mock.patch(
                "limit_halo.codex_discovery.verify_authenticode_cache_only", return_value=valid_trust()
            ):
                with self.assertRaises(DiscoveryFailure):
                    verify_candidate_unchanged(reference)

            local_root = root / "local"
            mirror_root = local_root / "OpenAI" / "Codex" / "bin"
            mirror_executable = mirror_root / "b99306303521e97e" / "codex.exe"
            mirror_executable.parent.mkdir(parents=True)
            mirror_executable.write_bytes(payload)
            mirror = appx_runnable_mirror(
                reference,
                mirror_root,
                trust_verifier=lambda _path: valid_trust(),
                pe_version_reader=lambda _path: None,
                trusted_root=local_root,
            )
            self.assertEqual(mirror.source, "appx-mirror")
            self.assertEqual(mirror.executable, mirror_executable.resolve(strict=True))
            self.assertEqual(mirror.executable_sha256, reference.executable_sha256)

            npm_root = root / "npm" / "node_modules" / "@openai" / "codex"
            native_root = npm_root / "node_modules" / "@openai" / "codex-win32-x64"
            npm_executable = native_root / NPM_EXE_RELATIVE
            npm_executable.parent.mkdir(parents=True)
            npm_executable.write_bytes(b"npm-codex-fixture")
            npm_root.mkdir(parents=True, exist_ok=True)
            (npm_root / "package.json").write_text(json.dumps({
                "name": "@openai/codex",
                "version": "99.0.0",
                "optionalDependencies": {NPM_NATIVE_NAME: "=99.0.0"},
            }), encoding="utf-8")
            (native_root / "package.json").write_text(json.dumps({
                "name": NPM_NATIVE_NAME,
                "version": "99.0.0",
            }), encoding="utf-8")
            selected = discover_from_sources(
                npm_codex_root=npm_root,
                appx_records=(record,),
                trust_verifier=lambda _path: valid_trust(),
                pe_version_reader=lambda _path: None,
                npm_trusted_root=root,
                appx_trusted_root=root,
                appx_mirror_root=mirror_root,
                appx_mirror_trusted_root=local_root,
            )
            self.assertEqual((selected.source, selected.executable), ("appx-mirror", mirror.executable))
            self.assertEqual(selected.executable_sha256, reference.executable_sha256)

            with self.assertRaises(DiscoveryFailure):
                discover_from_sources(
                    npm_codex_root=None,
                    appx_records=(record,),
                    trust_verifier=lambda _path: valid_trust(),
                    pe_version_reader=lambda _path: None,
                    appx_trusted_root=root,
                )

            mirror_executable.write_bytes(b"X" + payload[1:])
            fallback = discover_from_sources(
                npm_codex_root=npm_root,
                appx_records=(record,),
                trust_verifier=lambda _path: valid_trust(),
                pe_version_reader=lambda _path: None,
                npm_trusted_root=root,
                appx_trusted_root=root,
                appx_mirror_root=mirror_root,
                appx_mirror_trusted_root=local_root,
            )
            self.assertEqual((fallback.source, fallback.executable), ("npm", npm_executable.resolve(strict=True)))
            with self.assertRaises(DiscoveryFailure):
                appx_runnable_mirror(
                    reference,
                    mirror_root,
                    trust_verifier=lambda _path: valid_trust(),
                    pe_version_reader=lambda _path: None,
                    trusted_root=local_root,
                )
            mirror_executable.write_bytes(payload)
            invalid = TrustRecord(True, False, dict(EXPECTED_SUBJECT), frozenset({CODE_SIGNING_EKU}))
            with self.assertRaises(DiscoveryFailure):
                appx_runnable_mirror(
                    reference,
                    mirror_root,
                    trust_verifier=lambda _path: invalid,
                    pe_version_reader=lambda _path: None,
                    trusted_root=local_root,
                )

    def test_candidate_identity_accepts_absent_pe_version_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            package_root = Path(temporary) / "package"
            package_root.mkdir()
            executable = package_root / "codex.exe"
            payload = b"signed-codex-fixture"
            executable.write_bytes(payload)
            candidate = CodexCandidate(
                "npm",
                Version.parse("0.139.0"),
                executable.resolve(strict=True),
                package_root.resolve(strict=True),
                executable_sha256=executable_sha256(executable),
            )
            with mock.patch("limit_halo.codex_discovery.read_pe_file_version", return_value=None), mock.patch(
                "limit_halo.codex_discovery.verify_authenticode_cache_only", return_value=valid_trust()
            ):
                self.assertEqual(verify_candidate_unchanged(candidate), executable.resolve(strict=True))
                with self.assertRaises(DiscoveryFailure):
                    verify_candidate_unchanged(replace(candidate, executable_sha256="0" * 64))

            with mock.patch("limit_halo.codex_discovery.read_pe_file_version", return_value=(0, 138, 0, 0)), mock.patch(
                "limit_halo.codex_discovery.verify_authenticode_cache_only", return_value=valid_trust()
            ):
                with self.assertRaises(DiscoveryFailure):
                    verify_candidate_unchanged(candidate)

            executable.write_bytes(b"X" + payload[1:])
            with mock.patch("limit_halo.codex_discovery.read_pe_file_version", return_value=None), mock.patch(
                "limit_halo.codex_discovery.verify_authenticode_cache_only", return_value=valid_trust()
            ):
                with self.assertRaises(DiscoveryFailure):
                    verify_candidate_unchanged(candidate)
                with mock.patch("limit_halo.codex_protocol.subprocess.Popen") as popen:
                    with self.assertRaises(ProtocolFailure):
                        SubprocessTransport(candidate)
                    popen.assert_not_called()

            executable.write_bytes(payload)
            invalid = TrustRecord(True, False, dict(EXPECTED_SUBJECT), frozenset({CODE_SIGNING_EKU}))
            with mock.patch("limit_halo.codex_discovery.read_pe_file_version", return_value=None), mock.patch(
                "limit_halo.codex_discovery.verify_authenticode_cache_only", return_value=invalid
            ):
                with self.assertRaises(DiscoveryFailure):
                    verify_candidate_unchanged(candidate)

    def test_windows_hresult_bindings_use_raw_signed_long(self) -> None:
        import ctypes
        from ctypes import wintypes

        from limit_halo import codex_discovery as discovery
        from limit_halo import startup as startup_module

        self.assertIs(discovery._HRESULT, wintypes.LONG)
        self.assertIs(startup_module._HRESULT, wintypes.LONG)
        self.assertIsNot(discovery._HRESULT, ctypes.HRESULT)
        self.assertEqual(ctypes.sizeof(discovery._HRESULT), 4)
        self.assertEqual(discovery._HRESULT(-1).value, -1)

        class FakeFunction:
            def __init__(self, result: int = 0) -> None:
                self.result = result
                self.argtypes = None
                self.restype = None

            def __call__(self, *_args: object) -> int:
                return self.result

        class FakeCombase:
            def __init__(self) -> None:
                self.RoInitialize = FakeFunction(-1)

        with mock.patch.object(discovery.ctypes, "WinDLL", return_value=FakeCombase()):
            self.assertEqual(discovery.enumerate_current_user_appx(), ())

        class FakeOle32:
            def __init__(self) -> None:
                self.CoInitializeEx = FakeFunction(0)
                self.CoCreateInstance = FakeFunction(-1)
                self.CoUninitialize = FakeFunction(0)

        with mock.patch.object(startup_module.ctypes, "WinDLL", return_value=FakeOle32()):
            with self.assertRaises(startup_module.StartupFailure):
                startup_module.ShellLink()

        for relative in ("src/limit_halo/codex_discovery.py", "src/limit_halo/startup.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("wintypes.HRESULT", source)
            self.assertIn("_HRESULT = wintypes.LONG", source)

    def test_discovery_rejects_ancestor_reparse_and_uses_windows_ordinal_unicode(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            trusted = Path(temporary)
            package = trusted / "npm" / "node_modules" / "@openai" / "codex"
            package.mkdir(parents=True)
            ancestor = trusted / "npm"
            with mock.patch("limit_halo.codex_discovery._is_reparse", side_effect=lambda path: path == ancestor):
                with self.assertRaises(DiscoveryFailure):
                    _reject_existing_reparse_chain(package, trusted_root=trusted)

        left = CodexCandidate("npm", Version.parse("1.0.0"), Path(r"C:\x\ß\codex.exe"), Path(r"C:\x\ß"))
        right = CodexCandidate("npm", Version.parse("1.0.0"), Path(r"C:\x\ss\codex.exe"), Path(r"C:\x\ss"))
        self.assertNotEqual(_compare_string_ordinal_ignore_case(str(left.executable), str(right.executable)), 0)
        forward = select_candidate((left, right))
        reverse = select_candidate((right, left))
        self.assertEqual(forward.executable, reverse.executable)
        self.assertEqual(forward.executable, right.executable)

    def test_config_and_startup_paths_reject_reparse_components(self) -> None:
        from limit_halo import app as app_module
        from limit_halo import config as config_module
        from limit_halo import startup as startup_module

        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            root = Path(temporary)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=False):
                self.assertEqual(app_module.state_root(), root / "AILimitsWidget")
            state = root / "state"
            state.mkdir()
            store = ConfigStore(state)
            with mock.patch("limit_halo.config._is_reparse", side_effect=lambda path: path == state):
                with self.assertRaises(OSError):
                    config_module.reject_reparse_components(store.path)
                with self.assertRaises(OSError):
                    store.save(AppConfig())

            executable = root / "LimitHalo.exe"
            executable.write_bytes(b"fixture")
            with mock.patch("limit_halo.startup.reject_reparse_components", side_effect=OSError("junction")), mock.patch(
                "limit_halo.startup.ShellLink"
            ) as shell_link:
                with self.assertRaises(OSError):
                    startup_module.set_start_with_windows(True, executable)
                shell_link.assert_not_called()

    def test_startup_external_truth_precedes_persistence_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            executable = Path(temporary) / "LimitHalo.exe"
            executable.write_bytes(b"fixture")
            actual = {"enabled": False}
            writes: list[bool] = []

            def reader(_executable: Path) -> bool:
                return actual["enabled"]

            def writer(enabled: bool, _executable: Path) -> None:
                writes.append(enabled)
                actual["enabled"] = enabled

            store = mock.Mock()
            old = AppConfig(startWithWindows=False)
            new = replace(old, startWithWindows=True)
            persist_configuration(
                store, old, new, executable=executable, portable=False,
                startup_reader=reader, startup_writer=writer,
            )
            self.assertTrue(actual["enabled"])
            self.assertEqual(writes, [True])
            self.assertTrue(store.save.call_args.args[0].startWithWindows)

            actual["enabled"] = False
            writes.clear()
            store.save.side_effect = OSError("disk full")
            with self.assertRaises(OSError):
                persist_configuration(
                    store, old, new, executable=executable, portable=False,
                    startup_reader=reader, startup_writer=writer,
                )
            self.assertFalse(actual["enabled"])
            self.assertEqual(writes, [True, False])

    def test_startup_checkmark_requires_every_ownership_field(self) -> None:
        from limit_halo import startup as startup_module

        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            root = Path(temporary)
            executable = root / "LimitHalo.exe"
            shortcut = root / "LimitHalo.lnk"
            executable.write_bytes(b"fixture")
            shortcut.write_bytes(b"fixture-link")
            fields = {
                "target": executable,
                "working": executable.parent,
                "arguments": "",
                "description": startup_module.STARTUP_DESCRIPTION,
            }

            class FakeLink:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def load(self, _path):
                    return None

                def target(self):
                    return fields["target"]

                def working_directory(self):
                    return fields["working"]

                def arguments(self):
                    return fields["arguments"]

                def description(self):
                    return fields["description"]

            with mock.patch.object(startup_module, "startup_shortcut_path", return_value=shortcut), mock.patch.object(
                startup_module, "ShellLink", side_effect=FakeLink
            ):
                for owned_arguments in ("", startup_module.STARTUP_ARGUMENTS):
                    fields["arguments"] = owned_arguments
                    self.assertEqual(startup_module.inspect_startup_registration(executable).state, "owned")
                fields["arguments"] = startup_module.STARTUP_ARGUMENTS
                for key, mutant in (
                    ("target", root / "Other.exe"),
                    ("working", root / "other"),
                    ("arguments", "--hidden"),
                    ("description", "other product"),
                ):
                    original = fields[key]
                    fields[key] = mutant
                    self.assertEqual(startup_module.inspect_startup_registration(executable).state, "foreign")
                    fields[key] = original

    def test_installer_startup_description_matches_runtime(self) -> None:
        from limit_halo.startup import STARTUP_ARGUMENTS, STARTUP_DESCRIPTION

        helper = (ROOT / "packaging" / "installer" / "Invoke-OwnedShortcut.ps1").read_text(encoding="utf-8")
        self.assertEqual(STARTUP_DESCRIPTION, "LimitHalo owned startup shortcut v1")
        self.assertEqual(helper.count(f"'{STARTUP_DESCRIPTION}'"), 1)
        self.assertEqual(helper.count("'LimitHalo subscription-limit HUD'"), 1)
        self.assertIn("elseif ($Kind -ceq 'Startup')", helper)
        self.assertEqual(STARTUP_ARGUMENTS, "--startup")
        self.assertIn("'--startup'", helper)

    def test_discovery_has_no_helper_process_or_hidden_probe(self) -> None:
        discovery = (ROOT / "src" / "limit_halo" / "codex_discovery.py").read_text(encoding="utf-8")
        tree = ast.parse(discovery)
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertNotIn("subprocess", imports)
        lowered = discovery.casefold()
        for helper in ("powershell", "pwsh", "winget", "npm.exe", "node.exe"):
            self.assertNotIn(helper, lowered)
        protocol = (ROOT / "src" / "limit_halo" / "codex_protocol.py").read_text(encoding="utf-8")
        self.assertEqual(protocol.count("subprocess.Popen("), 1)
        command = app_server_command(Path("codex.exe"))
        self.assertEqual(command[1:4], ["app-server", "--listen", "stdio://"])
        self.assertNotIn("thread", " ".join(command))
        self.assertNotIn("turn", " ".join(command))

    def test_consent_prevents_client_construction(self) -> None:
        store = MetricStore(now=2_000_000_000)
        calls = 0

        def factory() -> Any:
            nonlocal calls
            calls += 1
            raise AssertionError("factory reached")

        off = AppConfig(claudeConsent="granted", claudeMode="disabled")
        runtime = ClaudeRuntime(store, lambda: off, factory)
        self.assertFalse(runtime.start_if_consented())
        self.assertEqual(calls, 0)
        enabled = replace(off, claudeMode="enabled")
        runtime = ClaudeRuntime(store, lambda: enabled, factory)
        with self.assertRaises(AssertionError):
            runtime.start_if_consented()
        self.assertEqual(calls, 1)

    def test_config_v1_migration_and_fail_closed_variants(self) -> None:
        migrated = decode_config({"schemaVersion": 1, "x": 10, "y": 20, "topmost": False})
        self.assertEqual(migrated.schemaVersion, 3)
        self.assertEqual((migrated.x, migrated.y, migrated.topmost), (10, 20, False))
        self.assertEqual(migrated.claudeMode, "auto")
        rejected_future = decode_config({"schemaVersion": 99, "claudeEnabled": True})
        self.assertEqual(rejected_future.claudeMode, "auto")
        self.assertFalse(rejected_future.claudeConsentCompleted)
        self.assertEqual(decode_config({"schemaVersion": 1, "x": 10, "token": "secret"}), AppConfig())
        self.assertEqual(decode_config({"schemaVersion": 1, "x": 10, "providerMetrics": {"used": 1}}), AppConfig())
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            store = ConfigStore(Path(temporary))
            (Path(temporary) / "config.json").write_text(json.dumps({"schemaVersion": 1, "x": 1}), encoding="utf-8")
            loaded = store.load()
            persisted = json.loads((Path(temporary) / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(loaded.schemaVersion, 3)
            self.assertEqual(persisted["schemaVersion"], 3)
            self.assertTrue(set(persisted).isdisjoint({"metrics", "token", "rawResponse", "history"}))

    def test_english_default_and_saved_language_choices_are_preserved(self) -> None:
        self.assertEqual(AppConfig().locale, "en-US")
        self.assertTrue(AppConfig().startWithWindows)
        self.assertEqual(safe_defaults().locale, "en-US")

        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            store = ConfigStore(Path(temporary))
            self.assertEqual(store.load().locale, "en-US")
            store.path.write_bytes(b"{invalid-json")
            self.assertEqual(store.load().locale, "en-US")
            with self.assertRaises(OSError):
                store.save(AppConfig())
            self.assertEqual(store.path.read_bytes(), b"{invalid-json")
            # Explicit synthetic repair, not an automatic defaults overwrite.
            store.path.write_text(json.dumps(AppConfig().to_json_object()), encoding="utf-8")
            self.assertEqual(store.load(), AppConfig())
            for locale in ("en-US", "uk-UA"):
                with self.subTest(saved_locale=locale):
                    store.save(replace(AppConfig(), locale=locale))
                    self.assertEqual(store.load().locale, locale)

        v2 = {
            "schemaVersion": 2,
            "language": "en",
            "claudeEnabled": False,
            "consentCompleted": False,
            "topmost": True,
            "startWithWindows": False,
            "displayPreset": "compact",
            "warningThresholds": [20, 10, 5],
            "x": None,
            "y": None,
        }
        for language, locale in (("en", "en-US"), ("uk", "uk-UA")):
            with self.subTest(migrated_language=language):
                v2["language"] = language
                self.assertEqual(decode_config(v2).locale, locale)

    def test_config_v3_exact_nested_schema_defaults_and_consent_boundary(self) -> None:
        config = AppConfig()
        value = config.to_json_object()
        self.assertEqual(set(value), {
            "schemaVersion", "locale", "providers", "claudeConsent", "autostart", "colors",
            "alerts", "topmost", "view", "x", "y",
        })
        self.assertEqual(value["providers"], {"claude": "auto", "codex": "auto"})
        self.assertEqual(value["claudeConsent"], "not-asked")
        self.assertTrue(value["autostart"])
        self.assertEqual(value["alerts"], {
            "enabled": False,
            "thresholdPercent": 20,
            "quietHours": {"enabled": False, "start": "22:00", "end": "08:00"},
        })
        self.assertEqual(decode_config(value), config)
        for mutate in (
            lambda item: item.__setitem__("token", "secret"),
            lambda item: item["providers"].__setitem__("credential", "secret"),
            lambda item: item.__setitem__("claudeConsent", "yes"),
            lambda item: item["alerts"].__setitem__("thresholdPercent", 15),
            lambda item: item["alerts"]["quietHours"].__setitem__("start", "25:00"),
        ):
            mutant = json.loads(json.dumps(value))
            mutate(mutant)
            self.assertEqual(decode_config(mutant), AppConfig())

        future = json.loads(json.dumps(value))
        future["futureAppearance"] = {
            "density": "compact",
            "steps": [1, 2, 3],
            "nested": {"weekends": True},
        }
        preserved = decode_config(future)
        self.assertEqual(preserved.to_json_object(), future)
        changed = replace(preserved, locale="uk-UA", x=-120, y=40)
        changed_json = changed.to_json_object()
        self.assertEqual(changed_json["futureAppearance"], future["futureAppearance"])

        unsafe_future = json.loads(json.dumps(value))
        unsafe_future["futureAppearance"] = {
            "nested": {"future-credential": "must-not-survive"}
        }
        self.assertEqual(decode_config(unsafe_future), AppConfig())
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            root = Path(temporary)
            store = ConfigStore(root)
            store.path.write_text(json.dumps(future), encoding="utf-8")
            loaded_future = store.load()
            store.save(replace(loaded_future, colorMode="status"))
            persisted_future = json.loads(store.path.read_text(encoding="ascii"))
            self.assertEqual(
                persisted_future["futureAppearance"], future["futureAppearance"]
            )
            store.path.write_bytes(b'{"schemaVersion":3,"schemaVersion":3}')
            self.assertEqual(store.load(), AppConfig())
            store.path.write_bytes(b'{"oversized":' + b"9" * 4_301 + b"}")
            self.assertEqual(store.load(), AppConfig())
            store.path.write_bytes(b"{" + b" " * 40_000)
            self.assertEqual(store.load(), AppConfig())

    def test_frozen_provider_visibility_table(self) -> None:
        self.assertEqual(resolve_provider_visibility("disabled", trusted_client_present=True, ever_succeeded=True).lane, LaneVisibility.HIDDEN)
        self.assertEqual(resolve_provider_visibility("auto", trusted_client_present=False, ever_succeeded=False).collector, "not-started")
        retained = resolve_provider_visibility("auto", trusted_client_present=False, ever_succeeded=True, runtime_error="offline")
        self.assertEqual((retained.lane, retained.collector, retained.config_mutation), (LaneVisibility.VISIBLE, "backoff", "none"))
        enabled_absent = resolve_provider_visibility("enabled", trusted_client_present=False, ever_succeeded=False)
        self.assertEqual((enabled_absent.lane, enabled_absent.collector), (LaneVisibility.VISIBLE, "not-started"))

    def test_auth_launch_is_exact_visible_executable_without_shell_or_url(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            executable = Path(temporary) / "codex.exe"
            payload = b"fixture"
            executable.write_bytes(payload)
            candidate = CodexCandidate(
                "npm",
                Version.parse("1.0.0"),
                executable.resolve(),
                Path(temporary).resolve(),
                executable_sha256=executable_sha256(executable),
            )
            client = TrustedAuthClient("codex", executable.resolve(), ("login",), "fixture-trusted", candidate)
            process = mock.Mock()
            spawn = mock.Mock(return_value=process)
            verifier = mock.Mock(return_value=executable.resolve())
            self.assertIs(launch_trusted_client(client, spawn=spawn, codex_verifier=verifier), process)
            verifier.assert_called_once_with(candidate)
            args, kwargs = spawn.call_args
            self.assertEqual(args[0], [str(executable.resolve()), "login"])
            self.assertIs(kwargs["shell"], False)
            self.assertIsNone(kwargs["stdin"])
            self.assertIsNone(kwargs["stdout"])
            expected_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE
                if os.name == "nt" else 0
            )
            self.assertEqual(kwargs["creationflags"], expected_flags)
            self.assertNotIn("http", " ".join(args[0]).casefold())
            bad = replace(client, fixed_args=("login", "--token", "secret"))
            with self.assertRaises(AuthLaunchFailure):
                launch_trusted_client(bad, spawn=spawn)
            spawn.reset_mock()
            executable.write_bytes(b"X" + payload[1:])
            with mock.patch("limit_halo.codex_discovery.read_pe_file_version", return_value=None), mock.patch(
                "limit_halo.codex_discovery.verify_authenticode_cache_only", return_value=valid_trust()
            ):
                with self.assertRaises(AuthLaunchFailure):
                    launch_trusted_client(client, spawn=spawn)
            spawn.assert_not_called()
        with self.assertRaises(AuthLaunchFailure):
            launch_provider_sign_in("other")

    def test_claude_auth_discovers_only_native_cache_trusted_anthropic_executable(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            executable = Path(temporary) / "claude.exe"
            executable.write_bytes(b"fixture")
            self.assertTrue(claude_client_installed((executable.resolve(),)))
            self.assertFalse(claude_client_installed((Path(temporary) / "missing" / "claude.exe",)))
            trusted = TrustRecord(
                True,
                True,
                {
                    "2.5.4.3": "Anthropic, PBC",
                    "2.5.4.10": "Anthropic, PBC",
                    "2.5.4.7": "San Francisco",
                    "2.5.4.8": "California",
                    "2.5.4.6": "US",
                },
                frozenset({CODE_SIGNING_EKU}),
            )
            client = trusted_claude_client((executable.resolve(),), trust_verifier=lambda _path: trusted)
            self.assertEqual(
                (client.provider, client.executable, client.fixed_args),
                ("claude", executable.resolve(), ("auth", "login")),
            )
            process = mock.Mock()
            spawn = mock.Mock(return_value=process)
            self.assertIs(launch_trusted_client(client, spawn=spawn), process)
            self.assertEqual(spawn.call_args.args[0], [str(executable.resolve()), "auth", "login"])
            untrusted = replace(trusted, status_valid=False)
            with self.assertRaises(AuthLaunchFailure):
                trusted_claude_client((executable.resolve(),), trust_verifier=lambda _path: untrusted)
            shim = executable.with_suffix(".cmd")
            shim.write_text("echo unsafe", encoding="ascii")
            with self.assertRaises(AuthLaunchFailure):
                trusted_claude_client((shim.resolve(),), trust_verifier=lambda _path: trusted)

    def test_default_claude_candidates_include_the_native_winget_package(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as temporary:
            root = Path(temporary)
            local = root / "local"
            profile = root / "profile"
            with mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(local), "USERPROFILE": str(profile)},
                clear=False,
            ):
                candidates = _default_claude_candidates()
        self.assertIn(
            local
            / "Microsoft"
            / "WinGet"
            / "Packages"
            / "Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "claude.exe",
            candidates,
        )

    def test_last_good_cadence_coalescing_and_restart_state(self) -> None:
        self.assertEqual((CODEX_POLL_SECONDS, CODEX_STALE_SECONDS, CODEX_RESTART_BACKOFF_SECONDS), (60.0, 180.0, (5.0, 15.0, 60.0)))
        self.assertEqual((CLAUDE_POLL_SECONDS, CLAUDE_STALE_SECONDS, CLAUDE_FAILURE_BACKOFF_SECONDS), (300.0, 600.0, (15.0, 60.0, 300.0)))
        coalescer = RefreshCoalescer()
        self.assertTrue(coalescer.request("codex"))
        self.assertFalse(coalescer.request("codex"))
        self.assertFalse(coalescer.request("codex"))
        self.assertTrue(coalescer.complete("codex"))
        self.assertFalse(coalescer.complete("codex"))
        now = 2_000_000_000.0
        store = MetricStore(now=now)
        initial_codex, initial_claude = store.snapshot(now=now)
        self.assertEqual(initial_codex, ())
        self.assertTrue(all(metric.used_percent is None for metric in initial_claude))
        ready = (
            Metric("claude_300", "claude", 300, 25, int(now) + 10_000, ProviderState.READY, now),
            Metric("claude_10080", "claude", 10_080, 50, int(now) + 500_000, ProviderState.READY, now),
            Metric("claude_fable_10080", "claude", 10_080, 75, int(now) + 500_000, ProviderState.READY, now),
        )
        store.update_claude(ready)
        store.mark_provider("claude", ProviderState.OFFLINE, now=now + 1, error_code="transport_timeout")
        _codex, claude = store.snapshot(now=now + 2)
        self.assertEqual([metric.used_percent for metric in claude], [25, 50, 75])
        self.assertTrue(all(metric.state is ProviderState.OFFLINE for metric in claude))

    def test_manual_retry_interrupts_backoff(self) -> None:
        class ObservedEvent:
            def __init__(self) -> None:
                self._event = threading.Event()
                self.wait_entered = threading.Event()

            def is_set(self) -> bool:
                return self._event.is_set()

            def set(self) -> None:
                self._event.set()

            def clear(self) -> None:
                self._event.clear()

            def wait(self, timeout: float | None = None) -> bool:
                self.wait_entered.set()
                return self._event.wait(timeout)

        store = mock.Mock()
        first_error = threading.Event()
        second_attempt = threading.Event()
        attempts = 0

        def discovery() -> CodexCandidate:
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                second_attempt.set()
            raise DiscoveryFailure("synthetic unavailable provider")

        collector = CodexCollector(store, discovery=discovery)
        observed = ObservedEvent()
        collector._wake_event = observed

        def mark_provider(_provider: str, state: ProviderState, **_kwargs: object) -> None:
            if state is ProviderState.PROVIDER_ERROR:
                if attempts == 1:
                    first_error.set()
                elif attempts == 2:
                    collector.request_stop()

        store.mark_provider.side_effect = mark_provider
        with mock.patch("limit_halo.codex_protocol.CODEX_RESTART_BACKOFF_SECONDS", (60.0,)):
            worker = threading.Thread(target=collector._run, daemon=True)
            worker.start()
            self.assertTrue(first_error.wait(1.0))
            self.assertTrue(observed.wait_entered.wait(1.0))
            started = time.monotonic()
            collector.request_refresh()
            self.assertTrue(second_attempt.wait(1.0))
            elapsed = time.monotonic() - started
            worker.join(2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(attempts, 2)
        self.assertLess(elapsed, 1.0)

        during_mark_store = mock.Mock()
        during_mark_second_attempt = threading.Event()
        during_mark_attempts = 0

        def during_mark_discovery() -> CodexCandidate:
            nonlocal during_mark_attempts
            during_mark_attempts += 1
            if during_mark_attempts == 2:
                during_mark_second_attempt.set()
            raise DiscoveryFailure("synthetic unavailable provider")

        during_mark = CodexCollector(during_mark_store, discovery=during_mark_discovery)

        def request_inside_mark(_provider: str, state: ProviderState, **_kwargs: object) -> None:
            if state is not ProviderState.PROVIDER_ERROR:
                return
            if during_mark_attempts == 1:
                during_mark.request_refresh()
            elif during_mark_attempts == 2:
                during_mark.request_stop()

        during_mark_store.mark_provider.side_effect = request_inside_mark
        with mock.patch("limit_halo.codex_protocol.CODEX_RESTART_BACKOFF_SECONDS", (60.0,)):
            during_mark_worker = threading.Thread(target=during_mark._run, daemon=True)
            during_mark_worker.start()
            self.assertTrue(during_mark_second_attempt.wait(1.0))
            during_mark_worker.join(2.0)
        self.assertFalse(during_mark_worker.is_alive())
        self.assertEqual(during_mark_attempts, 2)

        restart = CodexCollector(mock.Mock(), discovery=discovery)
        restart.request_refresh()
        restart.request_stop()
        with mock.patch("limit_halo.codex_protocol.threading.Thread") as thread_type:
            restart.start()
        self.assertFalse(restart._manual_event.is_set())
        self.assertFalse(restart._wake_event.is_set())
        self.assertTrue(restart._manual.request("codex"))
        thread_type.return_value.start.assert_called_once_with()

    def test_safe_diagnostics_exact_whitelist_and_raw_mutant(self) -> None:
        now = 2_000_000_000.0
        config = AppConfig(claudeConsent="granted", claudeMode="disabled")
        codex = (Metric("codex_300", "codex", 300, 10, int(now) + 10_000, ProviderState.READY, now),)
        result = safe_diagnostics(config, codex, (), dpi=144, now=now)
        self.assertEqual(set(result), SAFE_FIELDS)
        encoded = diagnostics_json(result).casefold()
        for forbidden in ("username", "c:\\users", "http://", "https://", "authorization", "request body", "response body"):
            self.assertNotIn(forbidden, encoded)
        mutant = dict(result)
        mutant["rawResponse"] = "secret"
        with self.assertRaises(ValueError):
            diagnostics_json(mutant)


if __name__ == "__main__":
    unittest.main()
