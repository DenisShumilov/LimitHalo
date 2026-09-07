from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .codex_discovery import (
    CodexCandidate,
    DiscoveryFailure,
    discover_public_codex,
    verify_candidate_unchanged,
)
from .model import (
    CODEX_POLL_SECONDS,
    CODEX_RESTART_BACKOFF_SECONDS,
    Metric,
    MetricStore,
    ProviderState,
    RefreshCoalescer,
)
from .process_guard import WindowsJob, child_creation_flags, safe_child_environment, system_directory


MAX_SERVER_LINE = 65_536
MAX_JSON_NESTING = 64
DISABLED_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "enable_mcp_apps",
    "hooks",
    "in_app_browser",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "workspace_dependencies",
)
ALLOWED_METHODS = frozenset({"initialize", "initialized", "account/read", "account/rateLimits/read"})


class ProtocolFailure(RuntimeError):
    pass


class AuthorizationRequired(ProtocolFailure):
    """The official app server explicitly reported missing required auth."""


def account_is_usable(result: Any) -> bool:
    """Classify only the two documented account/read fields; retain no account data."""

    if not isinstance(result, dict) or not {"account", "requiresOpenaiAuth"}.issubset(result):
        raise ProtocolFailure("account/read schema mismatch")
    requires_auth = result["requiresOpenaiAuth"]
    account = result["account"]
    if not isinstance(requires_auth, bool):
        raise ProtocolFailure("account/read requiresOpenaiAuth is not boolean")
    if account is not None and not isinstance(account, dict):
        raise ProtocolFailure("account/read account shape rejected")
    return not (account is None and requires_auth)


class Transport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...
    def receive(self, timeout: float) -> dict[str, Any] | None: ...
    def close(self) -> None: ...


def _load_object(data: bytes) -> dict[str, Any]:
    if not data or len(data) > MAX_SERVER_LINE:
        raise ProtocolFailure("invalid app-server frame size")

    # Python's JSON decoder no longer consistently raises RecursionError for
    # deeply nested input on every supported runtime.  Bound nesting before
    # decoding so an app-server frame cannot turn into an unbounded structure.
    depth = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise ProtocolFailure("app-server JSON nesting limit exceeded")
        elif byte in (0x7D, 0x5D):  # } ]
            depth -= 1

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ProtocolFailure("duplicate app-server JSON key")
            value[key] = item
        return value

    try:
        parsed = json.loads(data.decode("utf-8", "strict"), object_pairs_hook=no_duplicates)
    except (UnicodeError, json.JSONDecodeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProtocolFailure("malformed app-server JSON") from exc
    if not isinstance(parsed, dict):
        raise ProtocolFailure("app-server frame is not an object")
    return parsed


def _strict_window(window: Any, observed_at: float) -> Metric:
    if not isinstance(window, dict) or not {"windowDurationMins", "usedPercent", "resetsAt"}.issubset(window):
        raise ProtocolFailure("rate-limit window schema mismatch")
    duration = window["windowDurationMins"]
    used = window["usedPercent"]
    reset = window["resetsAt"]
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 525_600:
        raise ProtocolFailure("invalid window duration")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise ProtocolFailure("invalid usedPercent")
    if isinstance(used, int):
        if not 0 <= used <= 100:
            raise ProtocolFailure("invalid usedPercent")
        used_number = used
    else:
        if not math.isfinite(used) or not 0 <= used <= 100 or not used.is_integer():
            raise ProtocolFailure("invalid usedPercent")
        try:
            used_number = int(used)
        except (ValueError, OverflowError) as exc:
            raise ProtocolFailure("invalid usedPercent") from exc
    future_bound = max(86_400, duration * 60 + 86_400)
    if isinstance(reset, bool) or not isinstance(reset, int) or not observed_at < reset <= observed_at + future_bound:
        raise ProtocolFailure("invalid resetsAt")
    try:
        return Metric(
            f"codex_{duration}",
            "codex",
            duration,
            used_number,
            reset,
            ProviderState.READY,
            observed_at,
        )
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProtocolFailure("rate-limit window rejected") from exc


def _normalize_snapshot(snapshot: Any, observed_at: float) -> tuple[Metric, ...]:
    if not isinstance(snapshot, dict) or not {"limitId", "primary"}.issubset(snapshot):
        raise ProtocolFailure("Codex snapshot malformed")
    if snapshot.get("limitId") != "codex":
        raise ProtocolFailure("exact codex bucket unavailable")
    metrics: list[Metric] = [_strict_window(snapshot["primary"], observed_at)]
    secondary = snapshot.get("secondary")
    if secondary is not None:
        metrics.append(_strict_window(secondary, observed_at))
    if not 1 <= len(metrics) <= 2:
        raise ProtocolFailure("Codex returned zero or too many windows")
    durations = {metric.window_duration_mins for metric in metrics}
    if len(durations) != len(metrics):
        raise ProtocolFailure("duplicate Codex window duration")
    return tuple(sorted(metrics, key=lambda metric: metric.window_duration_mins))


def _metric_values(metrics: tuple[Metric, ...]) -> tuple[tuple[int, int | None, int | None], ...]:
    return tuple((item.window_duration_mins, item.used_percent, item.resets_at) for item in metrics)


def normalize_rate_limits(result: Any, *, observed_at: float | None = None) -> tuple[Metric, ...]:
    if not isinstance(result, dict):
        raise ProtocolFailure("rate-limit result schema mismatch")
    if "rateLimits" not in result:
        raise ProtocolFailure("rateLimits snapshot missing")
    now = time.time() if observed_at is None else observed_at
    top_snapshot = result["rateLimits"]
    top_metrics = (
        _normalize_snapshot(top_snapshot, now)
        if isinstance(top_snapshot, dict) and top_snapshot.get("limitId") == "codex"
        else None
    )
    if "rateLimitsByLimitId" not in result:
        if top_metrics is None:
            raise ProtocolFailure("exact codex bucket unavailable")
        return top_metrics
    by_id = result["rateLimitsByLimitId"]
    if not isinstance(by_id, dict) or "codex" not in by_id:
        raise ProtocolFailure("exact codex bucket unavailable")
    selected = _normalize_snapshot(by_id["codex"], now)
    if top_metrics is not None and _metric_values(selected) != _metric_values(top_metrics):
        raise ProtocolFailure("conflicting Codex snapshots")
    return selected


def app_server_command(executable: Path) -> list[str]:
    command = [
        str(executable),
        "app-server",
        "--listen",
        "stdio://",
        "-c",
        "mcp_servers={}",
        "-c",
        "analytics.enabled=false",
    ]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    return command


class SubprocessTransport:
    """The only Codex spawn point, reached after public metadata selection."""

    def __init__(self, candidate: CodexCandidate) -> None:
        try:
            executable = verify_candidate_unchanged(candidate)
        except DiscoveryFailure as exc:
            raise ProtocolFailure("selected Codex identity changed") from exc
        self._process = subprocess.Popen(
            app_server_command(executable),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=system_directory(),
            env=safe_child_environment(),
            creationflags=child_creation_flags(),
        )
        try:
            self._job = WindowsJob(self._process)
        except BaseException:
            self._process.terminate()
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2.0)
            raise
        self._queue: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue(maxsize=128)
        self._reader = threading.Thread(target=self._read_loop, name="limithalo-codex-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._process.stdout is not None
        try:
            while True:
                line = self._process.stdout.readline(MAX_SERVER_LINE + 1)
                if not line:
                    self._queue.put(None)
                    return
                self._queue.put(_load_object(line.rstrip(b"\r\n")), timeout=1.0)
        except BaseException as exc:
            try:
                self._queue.put(exc, timeout=1.0)
            except queue.Full:
                pass

    def send(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str) or method not in ALLOWED_METHODS or method.startswith(("thread/", "turn/")):
            raise ProtocolFailure("method outside fixed collector protocol")
        assert self._process.stdin is not None
        encoded = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
        self._process.stdin.write(encoded)
        self._process.stdin.flush()

    def receive(self, timeout: float) -> dict[str, Any] | None:
        try:
            item = self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if isinstance(item, BaseException):
            raise ProtocolFailure("app-server reader failed") from item
        if item is None:
            raise ProtocolFailure("app-server exited")
        return item

    def close(self) -> None:
        if self._process.stdin:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        try:
            self._process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._job.close()
                try:
                    self._process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
        finally:
            self._job.close()


class CodexSession:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._next_id = 1
        self._pending_update = False

    @staticmethod
    def _notification(message: dict[str, Any]) -> str:
        if not isinstance(message.get("method"), str) or not isinstance(message.get("params"), dict):
            raise ProtocolFailure("unexpected app-server notification or request")
        method = message["method"]
        if method == "remoteControl/status/changed":
            emitted = message.get("emittedAtMs")
            if (
                set(message) != {"method", "params", "emittedAtMs"}
                or isinstance(emitted, bool)
                or not isinstance(emitted, int)
                or emitted < 0
            ):
                raise ProtocolFailure("malformed remote-control status notification")
            return "ignored"
        if set(message) != {"method", "params"}:
            raise ProtocolFailure("unexpected app-server notification or request")
        if method != "account/rateLimits/updated":
            raise ProtocolFailure("unexpected app-server notification")
        params = message["params"]
        if "rateLimits" not in params:
            raise ProtocolFailure("malformed rate-limit update")
        # Apply the same complete snapshot/window validation as a read result.
        # The notification is only a wake-up signal; values are still acquired
        # by the fixed account/rateLimits/read request.
        normalize_rate_limits({"rateLimits": params["rateLimits"]})
        return "update"

    def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        if method not in {"initialize", "account/read", "account/rateLimits/read"}:
            raise ProtocolFailure("request outside fixed protocol")
        request_id = self._next_id
        self._next_id += 1
        self.transport.send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.transport.receive(min(0.25, max(0.0, deadline - time.monotonic())))
            if message is None:
                continue
            if "method" in message:
                if self._notification(message) == "update":
                    self._pending_update = True
                continue
            if set(message) != {"id", "result"} or message.get("id") != request_id or not isinstance(message.get("result"), dict):
                raise ProtocolFailure("app-server response rejected")
            return message["result"]
        raise ProtocolFailure("app-server request deadline")

    def initialize_and_read(self) -> tuple[Metric, ...]:
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "limit_halo", "title": "LimitHalo", "version": "1.0.0"},
                "capabilities": {
                    "optOutNotificationMethods": ["account/updated", "remoteControl/status/changed"]
                },
            },
        )
        self.transport.send({"method": "initialized", "params": {}})
        if not account_is_usable(self._request("account/read", {"refreshToken": False})):
            raise AuthorizationRequired("Codex authorization required")
        return self.read()

    def read(self) -> tuple[Metric, ...]:
        return normalize_rate_limits(self._request("account/rateLimits/read"))

    def wait_for_update(self, timeout: float) -> bool:
        if self._pending_update:
            self._pending_update = False
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            message = self.transport.receive(max(0.0, deadline - time.monotonic()))
            if message is None:
                return False
            if self._notification(message) == "update":
                return True
            if time.monotonic() >= deadline:
                return False


class CodexCollector:
    def __init__(
        self,
        store: MetricStore,
        *,
        discovery: Callable[[], CodexCandidate] = discover_public_codex,
        transport_factory: Callable[[CodexCandidate], Transport] = SubprocessTransport,
    ) -> None:
        self.store = store
        self.discovery = discovery
        self.transport_factory = transport_factory
        self._stop = threading.Event()
        self._manual_event = threading.Event()
        self._wake_event = threading.Event()
        self._manual = RefreshCoalescer()
        self._thread: threading.Thread | None = None
        self._acquisition_sequence = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._manual_event.clear()
        self._wake_event.clear()
        self._manual = RefreshCoalescer()
        self._thread = threading.Thread(target=self._run, name="limithalo-codex", daemon=True)
        self._thread.start()

    def request_refresh(self) -> None:
        if self._manual.request("codex"):
            self._manual_event.set()
            self._wake_event.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake_event.set()

    def join(self, timeout: float | None = 5.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _finish_manual(self) -> None:
        if self._manual.complete("codex"):
            self._manual_event.set()
            self._wake_event.set()

    def _accept_snapshot(self, metrics: tuple[Metric, ...]) -> None:
        self._acquisition_sequence += 1
        if not self.store.update_codex(
            metrics, sequence=self._acquisition_sequence
        ):
            raise ProtocolFailure("non-monotonic Codex snapshot")

    def _run(self) -> None:
        backoff_index = 0
        while not self._stop.is_set():
            manual_attempt = self._manual_event.is_set()
            if manual_attempt:
                self._manual_event.clear()
                self._wake_event.clear()
            transport: Transport | None = None
            try:
                candidate = self.discovery()
                transport = self.transport_factory(candidate)
                session = CodexSession(transport)
                self._accept_snapshot(session.initialize_and_read())
                if manual_attempt:
                    self._finish_manual()
                    manual_attempt = False
                backoff_index = 0
                next_poll = time.monotonic() + CODEX_POLL_SECONDS
                while not self._stop.is_set():
                    manual = self._manual_event.is_set()
                    if manual:
                        self._manual_event.clear()
                        self._wake_event.clear()
                        self.store.mark_provider("codex", ProviderState.REFRESHING)
                        try:
                            self._accept_snapshot(session.read())
                        finally:
                            self._finish_manual()
                        continue
                    wait = max(0.0, min(1.0, next_poll - time.monotonic()))
                    update = session.wait_for_update(wait)
                    now = time.monotonic()
                    if update or now >= next_poll:
                        self._accept_snapshot(session.read())
                        if now >= next_poll:
                            next_poll = now + CODEX_POLL_SECONDS
            except AuthorizationRequired:
                self.store.mark_provider(
                    "codex", ProviderState.AUTHORIZATION_REQUIRED, error_code="authorization_required"
                )
            except (OSError, DiscoveryFailure, ProtocolFailure):
                self.store.mark_provider("codex", ProviderState.PROVIDER_ERROR, error_code="codex_unavailable")
            finally:
                if manual_attempt:
                    self._finish_manual()
                if transport is not None:
                    transport.close()
            if self._stop.is_set():
                break
            delay = CODEX_RESTART_BACKOFF_SECONDS[min(backoff_index, len(CODEX_RESTART_BACKOFF_SECONDS) - 1)]
            backoff_index = min(backoff_index + 1, len(CODEX_RESTART_BACKOFF_SECONDS) - 1)
            self._wake_event.wait(delay)
