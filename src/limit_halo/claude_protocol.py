from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .build_identity import BROKER_PROTOCOL, BROKER_RELATIVE_PATH, BROKER_SHA256
from .config import AppConfig
from .model import (
    CLAUDE_FAILURE_BACKOFF_SECONDS,
    CLAUDE_KEYS,
    CLAUDE_POLL_SECONDS,
    Metric,
    MetricStore,
    ProviderState,
    RefreshCoalescer,
)
from .process_guard import WindowsJob, child_creation_flags, safe_child_environment, system_directory


MAX_LINE_BYTES = 8192
ERROR_STATUSES = frozenset(
    {
        "BAD_REQUEST",
        "SECURITY_POLICY",
        "CREDENTIAL_MISSING",
        "CREDENTIAL_UNSUPPORTED",
        "CREDENTIAL_MALFORMED",
        "CREDENTIAL_CHANGED",
        "LOCK_CONTENDED",
        "AUTH_LOCKED",
        "TRANSIENT",
        "SCHEMA_MISMATCH",
        "DEADLINE",
        "INTERNAL",
    }
)
TRANSPORT_ENDPOINTS = frozenset({"refresh", "usage"})
TRANSPORT_PHASES = frozenset({"session_options", "connect", "send", "receive", "headers", "body"})
TRANSPORT_CLASSES = frozenset(
    {"timeout", "name_resolution", "cannot_connect", "secure_failure", "option_failure", "http_429", "http_5xx", "other"}
)
BROKER_METRIC_KEYS = {
    "claude_session": "claude_300",
    "claude_weekly_all": "claude_10080",
    "claude_fable_weekly": "claude_fable_10080",
}


class BrokerFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerResult:
    sequence: int
    status: str
    observed_at: int
    metrics: tuple[Metric, ...]
    provider_state: ProviderState
    redacted_error_code: str | None


def _exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise BrokerFailure("broker schema mismatch")


def _strict_int(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BrokerFailure("invalid broker integer")
    return value


def parse_broker_line(
    line: bytes,
    *,
    request_id: str,
    acquisition_sequence: int,
    received_at: float | None = None,
) -> BrokerResult:
    if not re_full_hex(request_id) or not line or len(line) > MAX_LINE_BYTES:
        raise BrokerFailure("broker framing violation")
    if line.endswith(b"\r\n"):
        body = line[:-2]
    elif line.endswith(b"\n"):
        body = line[:-1]
    else:
        body = line
    if not body or b"\n" in body or b"\r" in body:
        raise BrokerFailure("broker framing violation")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise BrokerFailure("duplicate broker key")
            value[key] = item
        return value

    try:
        payload = json.loads(body.decode("utf-8", "strict"), object_pairs_hook=no_duplicates)
    except (UnicodeError, json.JSONDecodeError, ValueError, OverflowError, RecursionError) as exc:
        raise BrokerFailure("invalid broker JSON") from exc
    if not isinstance(payload, dict):
        raise BrokerFailure("broker result is not an object")
    _exact_keys(
        payload,
        {
            "protocol",
            "requestId",
            "acquisitionSequence",
            "status",
            "acquiredAtUnixSeconds",
            "durationMs",
            "credentialVersion",
            "transportDiagnostic",
            "metrics",
        },
    )
    if payload["protocol"] != BROKER_PROTOCOL or payload["requestId"] != request_id:
        raise BrokerFailure("broker correlation mismatch")
    sequence = _strict_int(payload["acquisitionSequence"], 1, 9_007_199_254_740_991)
    if sequence != acquisition_sequence:
        raise BrokerFailure("broker sequence mismatch")
    observed = _strict_int(payload["acquiredAtUnixSeconds"], 1, 9_007_199_254_740_991)
    _strict_int(payload["durationMs"], 0, 55_000)
    current = time.time() if received_at is None else received_at
    if observed > int(current) + 300:
        raise BrokerFailure("broker time is in the future")
    credential_version = payload["credentialVersion"]
    if credential_version is not None:
        if not isinstance(credential_version, dict):
            raise BrokerFailure("credential version malformed")
        _exact_keys(credential_version, {"credentialLastWrittenFileTime", "credentialBlobSize"})
        filetime = credential_version["credentialLastWrittenFileTime"]
        if not isinstance(filetime, str) or not filetime.isascii() or not filetime.isdecimal() or not 1 <= len(filetime) <= 20:
            raise BrokerFailure("credential version malformed")
        if (len(filetime) > 1 and filetime.startswith("0")) or int(filetime) > 18_446_744_073_709_551_615:
            raise BrokerFailure("credential version malformed")
        _strict_int(credential_version["credentialBlobSize"], 0, 65_536)
    status = payload["status"]
    if status != "OK" and status not in ERROR_STATUSES:
        raise BrokerFailure("unknown broker status")
    diagnostic = payload["transportDiagnostic"]
    diagnostic_code: str | None = None
    if status == "TRANSIENT":
        if not isinstance(diagnostic, dict):
            raise BrokerFailure("missing transport diagnostic")
        _exact_keys(diagnostic, {"endpoint", "phase", "class"})
        if diagnostic["endpoint"] not in TRANSPORT_ENDPOINTS or diagnostic["phase"] not in TRANSPORT_PHASES or diagnostic["class"] not in TRANSPORT_CLASSES:
            raise BrokerFailure("invalid transport diagnostic")
        diagnostic_code = f"transport_{diagnostic['class']}"
    elif diagnostic is not None:
        raise BrokerFailure("unexpected transport diagnostic")
    values = payload["metrics"]
    if not isinstance(values, dict):
        raise BrokerFailure("metrics malformed")
    _exact_keys(values, set(BROKER_METRIC_KEYS))
    if status != "OK":
        if any(value is not None for value in values.values()):
            raise BrokerFailure("failure envelope carries values")
        state = {
            "AUTH_LOCKED": ProviderState.AUTHORIZATION_REQUIRED,
            "CREDENTIAL_MISSING": ProviderState.AUTHORIZATION_REQUIRED,
            "CREDENTIAL_UNSUPPORTED": ProviderState.AUTHORIZATION_REQUIRED,
            "CREDENTIAL_MALFORMED": ProviderState.AUTHORIZATION_REQUIRED,
            "TRANSIENT": ProviderState.OFFLINE,
            "CREDENTIAL_CHANGED": ProviderState.OFFLINE,
            "LOCK_CONTENDED": ProviderState.OFFLINE,
            "DEADLINE": ProviderState.OFFLINE,
        }.get(status, ProviderState.PROVIDER_ERROR)
        return BrokerResult(sequence, status, observed, (), state, diagnostic_code or status.casefold())
    # A successful acquisition must contain the canonical short-window
    # baseline.  Treating an all-null or baseline-null envelope as a real
    # quota snapshot would overwrite the last good values and falsely turn a
    # provider schema change into an "unavailable quota" UI state.  Optional
    # weekly/scoped windows may still be explicitly absent.
    if values["claude_session"] is None:
        raise BrokerFailure("broker baseline metric unavailable")
    parsed: list[Metric] = []
    for wire_key, key in BROKER_METRIC_KEYS.items():
        value = values[wire_key]
        if value is None:
            parsed.append(
                Metric(key, "claude", 300 if key == "claude_300" else 10_080, None, None, ProviderState.UNAVAILABLE, observed)
            )
            continue
        if not isinstance(value, dict):
            raise BrokerFailure("metric malformed")
        _exact_keys(value, {"remainingMicros", "resetUnixSeconds"})
        remaining_micros = _strict_int(value["remainingMicros"], 0, 100_000_000)
        duration = 300 if key == "claude_300" else 10_080
        reset_value = value["resetUnixSeconds"]
        if reset_value is None:
            if key != "claude_300":
                raise BrokerFailure("metric reset unavailable")
            reset = None
        else:
            reset = _strict_int(reset_value, 1, 9_007_199_254_740_991)
            if not observed < reset <= observed + duration * 60 + 86_400:
                raise BrokerFailure("metric reset rejected")
        used = int(math.floor((100.0 - remaining_micros / 1_000_000.0) + 0.5))
        parsed.append(Metric(key, "claude", duration, used, reset, ProviderState.READY, observed))
    return BrokerResult(sequence, status, observed, tuple(parsed), ProviderState.READY, None)


def re_full_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(character in "0123456789abcdef" for character in value)


class BrokerClient:
    """The only Claude broker spawn point. Construct only after stored consent."""

    def __init__(self, executable: Path, expected_sha256: str) -> None:
        try:
            resolved = executable.resolve(strict=True)
        except OSError as exc:
            raise BrokerFailure("broker unavailable") from exc
        if resolved.name.casefold() != BROKER_RELATIVE_PATH.casefold():
            raise BrokerFailure("broker path rejected")
        if expected_sha256 == "0" * 64 or len(expected_sha256) != 64:
            raise BrokerFailure("broker build identity is not frozen")
        try:
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError as exc:
            raise BrokerFailure("broker identity unavailable") from exc
        if digest != expected_sha256.casefold():
            raise BrokerFailure("broker identity mismatch")
        self.executable = resolved
        self._sequence = 0
        self._lock = threading.Lock()

    def acquire(self) -> BrokerResult:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        request_id = secrets.token_hex(16)
        request = {"protocol": BROKER_PROTOCOL, "requestId": request_id, "acquisitionSequence": sequence}
        encoded = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
        process = subprocess.Popen(
            [str(self.executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=system_directory(),
            env=safe_child_environment(),
            creationflags=child_creation_flags(),
        )
        try:
            job = WindowsJob(process)
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            raise
        try:
            try:
                stdout, _ = process.communicate(encoded, timeout=55.0)
            except subprocess.TimeoutExpired as exc:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    job.close()
                raise BrokerFailure("broker deadline exceeded") from exc
            if process.returncode not in (0, 2):
                raise BrokerFailure("broker process failed")
            if len(stdout) > MAX_LINE_BYTES or stdout.count(b"\n") > 1:
                raise BrokerFailure("broker output framing violation")
            return parse_broker_line(
                stdout,
                request_id=request_id,
                acquisition_sequence=sequence,
            )
        finally:
            job.close()
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass


class ClaudeCollector:
    def __init__(self, acquire: Callable[[], BrokerResult], store: MetricStore) -> None:
        self._acquire = acquire
        self._store = store
        self._stop = threading.Event()
        self._manual_event = threading.Event()
        self._manual = RefreshCoalescer()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._store.mark_provider("claude", ProviderState.REFRESHING)
        self._thread = threading.Thread(target=self._run, name="limithalo-claude", daemon=True)
        self._thread.start()

    def request_refresh(self) -> None:
        if self._manual.request("claude"):
            self._manual_event.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._manual_event.set()

    def join(self, timeout: float | None = 5.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _finish_manual(self) -> None:
        if self._manual.complete("claude"):
            self._manual_event.set()

    def _run(self) -> None:
        failure_index = 0
        next_automatic = time.monotonic()
        while not self._stop.is_set():
            manual = self._manual_event.is_set()
            now = time.monotonic()
            if not manual and now < next_automatic:
                self._stop.wait(min(1.0, next_automatic - now))
                continue
            if manual:
                self._manual_event.clear()
                self._store.mark_provider("claude", ProviderState.REFRESHING)
            try:
                result = self._acquire()
                if result.status == "OK":
                    accepted = self._store.update_claude(
                        result.metrics,
                        sequence=result.sequence,
                    )
                    if not accepted:
                        self._store.mark_provider(
                            "claude",
                            ProviderState.PROVIDER_ERROR,
                            error_code="stale_snapshot",
                        )
                    failure_index = 0
                    if not manual:
                        next_automatic = time.monotonic() + CLAUDE_POLL_SECONDS
                else:
                    self._store.mark_provider(
                        "claude",
                        result.provider_state,
                        error_code=result.redacted_error_code,
                    )
                    if not manual:
                        delay = CLAUDE_FAILURE_BACKOFF_SECONDS[
                            min(failure_index, len(CLAUDE_FAILURE_BACKOFF_SECONDS) - 1)
                        ]
                        failure_index = min(failure_index + 1, len(CLAUDE_FAILURE_BACKOFF_SECONDS) - 1)
                        next_automatic = time.monotonic() + delay
            except (OSError, BrokerFailure):
                self._store.mark_provider("claude", ProviderState.PROVIDER_ERROR, error_code="broker_failure")
                if not manual:
                    delay = CLAUDE_FAILURE_BACKOFF_SECONDS[
                        min(failure_index, len(CLAUDE_FAILURE_BACKOFF_SECONDS) - 1)
                    ]
                    failure_index = min(failure_index + 1, len(CLAUDE_FAILURE_BACKOFF_SECONDS) - 1)
                    next_automatic = time.monotonic() + delay
            finally:
                if manual:
                    self._finish_manual()


class ClaudeRuntime:
    """Consent gate that prevents even BrokerClient construction while off."""

    def __init__(
        self,
        store: MetricStore,
        config_provider: Callable[[], AppConfig],
        client_factory: Callable[[], BrokerClient],
    ) -> None:
        self._store = store
        self._config_provider = config_provider
        self._client_factory = client_factory
        self._collector: ClaudeCollector | None = None

    def start_if_consented(self) -> bool:
        config = self._config_provider()
        if not (config.consentCompleted and config.claudeEnabled):
            self._store.set_claude_disabled()
            return False
        if self._collector is None:
            client = self._client_factory()
            self._collector = ClaudeCollector(client.acquire, self._store)
        self._collector.start()
        return True

    def disable(self) -> None:
        collector = self._collector
        self._collector = None
        if collector is not None:
            collector.request_stop()
            collector.join(5.0)
        self._store.set_claude_disabled()

    def request_refresh(self) -> None:
        if self._collector is not None:
            self._collector.request_refresh()

    def shutdown(self) -> None:
        collector = self._collector
        self._collector = None
        if collector is not None:
            collector.request_stop()
            collector.join(5.0)


def production_client(package_root: Path) -> BrokerClient:
    root = package_root.resolve(strict=True)
    executable = (root / BROKER_RELATIVE_PATH).resolve(strict=True)
    if executable.parent != root:
        raise BrokerFailure("broker escaped package root")
    return BrokerClient(executable, BROKER_SHA256)
