from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Iterable


CODEX_POLL_SECONDS = 60.0
CODEX_STALE_SECONDS = 180.0
CODEX_RESTART_BACKOFF_SECONDS = (5.0, 15.0, 60.0)
CLAUDE_POLL_SECONDS = 300.0
CLAUDE_STALE_SECONDS = 600.0
# Recover promptly from a completed sign-in or a short network outage without
# hammering the provider during a sustained failure.
CLAUDE_FAILURE_BACKOFF_SECONDS = (15.0, 60.0, 300.0)
CLAUDE_KEYS = ("claude_300", "claude_10080", "claude_fable_10080")


class ProviderState(str, Enum):
    READY = "ready"
    REFRESHING = "refreshing"
    STALE = "stale"
    OFFLINE = "offline"
    AUTHORIZATION_REQUIRED = "authorization_required"
    PROVIDER_ERROR = "provider_error"
    DISABLED = "claude_disabled"
    UNAVAILABLE = "unavailable"


class LaneVisibility(str, Enum):
    HIDDEN = "hidden"
    VISIBLE = "visible"


@dataclass(frozen=True)
class ProviderResolution:
    lane: LaneVisibility
    collector: str
    config_mutation: str = "none"


def resolve_provider_visibility(
    selection: str,
    *,
    trusted_client_present: bool,
    ever_succeeded: bool,
    runtime_error: str | None = None,
) -> ProviderResolution:
    """Implement the frozen provider visibility table without mutating choice."""

    if selection not in {"auto", "enabled", "disabled"}:
        raise ValueError("invalid provider selection")
    if selection == "disabled":
        return ProviderResolution(LaneVisibility.HIDDEN, "stopped")
    if selection == "auto" and not trusted_client_present and not ever_succeeded:
        return ProviderResolution(LaneVisibility.HIDDEN, "not-started")
    if selection == "auto" and not trusted_client_present:
        return ProviderResolution(LaneVisibility.VISIBLE, "backoff")
    if selection == "enabled" and not trusted_client_present:
        return ProviderResolution(LaneVisibility.VISIBLE, "not-started")
    if runtime_error:
        return ProviderResolution(LaneVisibility.VISIBLE, "backoff")
    return ProviderResolution(LaneVisibility.VISIBLE, "running")


@dataclass(frozen=True)
class Metric:
    key: str
    provider: str
    window_duration_mins: int
    used_percent: int | None
    resets_at: int | None
    state: ProviderState
    observed_at: float
    retry_after_seconds: int | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in {"codex", "claude"}:
            raise ValueError("invalid provider")
        if not isinstance(self.state, ProviderState):
            raise ValueError("invalid provider state")
        if isinstance(self.window_duration_mins, bool) or not isinstance(self.window_duration_mins, int):
            raise ValueError("invalid duration")
        if self.window_duration_mins <= 0 or self.window_duration_mins > 525_600:
            raise ValueError("invalid duration")
        if self.used_percent is not None:
            if isinstance(self.used_percent, bool) or not isinstance(self.used_percent, int):
                raise ValueError("invalid percent")
            if not 0 <= self.used_percent <= 100:
                raise ValueError("invalid percent")
        if self.resets_at is not None and (isinstance(self.resets_at, bool) or not isinstance(self.resets_at, int)):
            raise ValueError("invalid reset")
        if not math.isfinite(self.observed_at):
            raise ValueError("invalid observation")

    def stale_if_needed(self, now: float) -> "Metric":
        stale_after = CODEX_STALE_SECONDS if self.provider == "codex" else CLAUDE_STALE_SECONDS
        if self.state is ProviderState.READY and now - self.observed_at >= stale_after:
            return replace(self, state=ProviderState.STALE)
        return self


def unavailable_claude(now: float, state: ProviderState = ProviderState.UNAVAILABLE) -> tuple[Metric, ...]:
    return (
        Metric("claude_300", "claude", 300, None, None, state, now),
        Metric("claude_10080", "claude", 10_080, None, None, state, now),
        Metric("claude_fable_10080", "claude", 10_080, None, None, state, now),
    )


class MetricStore:
    """Thread-safe, memory-only provider state with last-good preservation."""

    def __init__(self, *, now: float | None = None, on_change: Callable[[], None] | None = None) -> None:
        current = time.time() if now is None else now
        self._lock = threading.RLock()
        self._codex: dict[int, Metric] = {}
        self._claude = {metric.key: metric for metric in unavailable_claude(current)}
        self._ever_succeeded = {"codex": False, "claude": False}
        self._accepted_freshness: dict[str, tuple[float, int] | None] = {
            "codex": None,
            "claude": None,
        }
        self._on_change = on_change

    @staticmethod
    def _snapshot_freshness(
        metrics: tuple[Metric, ...], sequence: int | None
    ) -> tuple[float, int]:
        observations = {metric.observed_at for metric in metrics}
        if len(observations) != 1:
            raise ValueError("snapshot observations differ")
        if sequence is None:
            sequence = 0
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("invalid acquisition sequence")
        return next(iter(observations)), sequence

    def update_codex(self, metrics: Iterable[Metric], *, sequence: int | None = None) -> bool:
        values = tuple(metrics)
        if not 1 <= len(values) <= 2:
            raise ValueError("Codex must expose one or two windows")
        durations: set[int] = set()
        clean: dict[int, Metric] = {}
        for metric in values:
            if metric.provider != "codex" or metric.state is not ProviderState.READY:
                raise ValueError("invalid Codex metric")
            if metric.window_duration_mins in durations:
                raise ValueError("duplicate Codex duration")
            durations.add(metric.window_duration_mins)
            clean[metric.window_duration_mins] = metric
        freshness = self._snapshot_freshness(values, sequence)
        with self._lock:
            previous = self._accepted_freshness["codex"]
            if previous is not None and freshness <= previous:
                return False
            self._codex = clean
            self._ever_succeeded["codex"] = True
            self._accepted_freshness["codex"] = freshness
        self._notify()
        return True

    def update_claude(self, metrics: Iterable[Metric], *, sequence: int | None = None) -> bool:
        items = tuple(metrics)
        if len(items) != len(CLAUDE_KEYS):
            raise ValueError("incomplete Claude metric set")
        values = {metric.key: metric for metric in items}
        if len(values) != len(items) or set(values) != set(CLAUDE_KEYS):
            raise ValueError("incomplete Claude metric set")
        if any(metric.provider != "claude" for metric in items):
            raise ValueError("invalid Claude provider")
        if values["claude_300"].state is not ProviderState.READY:
            raise ValueError("Claude session baseline is not ready")
        if any(
            metric.state not in {ProviderState.READY, ProviderState.UNAVAILABLE}
            for metric in items
        ):
            raise ValueError("invalid Claude snapshot state")
        freshness = self._snapshot_freshness(items, sequence)
        with self._lock:
            previous = self._accepted_freshness["claude"]
            if previous is not None and freshness <= previous:
                return False
            merged: dict[str, Metric] = {}
            for key, metric in values.items():
                prior = self._claude.get(key)
                if (
                    metric.state is ProviderState.UNAVAILABLE
                    and prior is not None
                    and prior.used_percent is not None
                ):
                    # Optional provider windows can disappear temporarily while
                    # the mandatory session window is still valid.  Keep the
                    # last measured value visible, but label it as old instead
                    # of turning a transient omission into a false empty lane.
                    merged[key] = replace(
                        prior,
                        state=ProviderState.STALE,
                        error_code="optional_temporarily_unavailable",
                    )
                else:
                    merged[key] = metric
            self._claude = merged
            self._ever_succeeded["claude"] = True
            self._accepted_freshness["claude"] = freshness
        self._notify()
        return True

    def mark_provider(self, provider: str, state: ProviderState, *, now: float | None = None, error_code: str | None = None) -> None:
        current = time.time() if now is None else now
        with self._lock:
            if provider == "codex":
                if self._codex:
                    self._codex = {
                        duration: replace(
                            metric,
                            state=state,
                            observed_at=(metric.observed_at if metric.used_percent is not None else current),
                            error_code=error_code,
                        )
                        for duration, metric in self._codex.items()
                    }
                else:
                    # A first-run typed state must be visible before any quota
                    # snapshot exists.  The duration is a neutral HUD slot, not
                    # a claimed daily window.
                    self._codex = {
                        10_080: Metric(
                            "codex_unavailable",
                            "codex",
                            10_080,
                            None,
                            None,
                            state,
                            current,
                            error_code=error_code,
                        )
                    }
            elif provider == "claude":
                self._claude = {
                    key: replace(
                        metric,
                        state=state,
                        observed_at=(metric.observed_at if metric.used_percent is not None else current),
                        error_code=error_code,
                    )
                    for key, metric in self._claude.items()
                }
            else:
                raise ValueError("invalid provider")
        self._notify()

    def set_claude_disabled(self, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        with self._lock:
            self._claude = {
                metric.key: metric
                for metric in unavailable_claude(current, ProviderState.DISABLED)
            }
            self._accepted_freshness["claude"] = None
        self._notify()

    def snapshot(self, *, now: float | None = None) -> tuple[tuple[Metric, ...], tuple[Metric, ...]]:
        current = time.time() if now is None else now
        with self._lock:
            codex = tuple(metric.stale_if_needed(current) for _, metric in sorted(self._codex.items()))
            claude = tuple(self._claude[key].stale_if_needed(current) for key in CLAUDE_KEYS)
        return codex, claude

    def ever_succeeded(self, provider: str) -> bool:
        if provider not in self._ever_succeeded:
            raise ValueError("invalid provider")
        with self._lock:
            return self._ever_succeeded[provider]

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()


class RefreshCoalescer:
    """At most one active and one coalesced pending manual refresh per provider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = {"codex": False, "claude": False}
        self._pending = {"codex": False, "claude": False}

    def request(self, provider: str) -> bool:
        if provider not in self._active:
            raise ValueError("invalid provider")
        with self._lock:
            if self._active[provider]:
                self._pending[provider] = True
                return False
            self._active[provider] = True
            return True

    def complete(self, provider: str) -> bool:
        if provider not in self._active:
            raise ValueError("invalid provider")
        with self._lock:
            if not self._active[provider]:
                raise RuntimeError("refresh is not active")
            if self._pending[provider]:
                self._pending[provider] = False
                return True
            self._active[provider] = False
            return False


def warning_color(used_percent: int | None, state: ProviderState, *, thresholds: tuple[int, int, int] = (20, 10, 5)) -> str:
    if state not in {ProviderState.READY, ProviderState.REFRESHING} or used_percent is None:
        return "#AEB6C2"
    warning, critical, urgent = thresholds
    if not (0 <= urgent < critical < warning <= 100):
        raise ValueError("invalid thresholds")
    remaining = 100 - used_percent
    if remaining <= urgent:
        return "#FF5C5C"
    if remaining <= critical:
        return "#FF9F43"
    if remaining <= warning:
        return "#F6C85F"
    return "#FFFFFF"


def duration_label(provider: str, minutes: int, language: str, *, fable: bool = False) -> str:
    if provider not in {"codex", "claude"} or language not in {"uk", "en"}:
        raise ValueError("invalid label input")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        raise ValueError("invalid duration")
    prefix = "Fable" if fable else ("Codex" if provider == "codex" else "Claude")
    if minutes % 10_080 == 0:
        count = minutes // 10_080 * 7
        unit = "днів" if language == "uk" else ("day" if count == 1 else "days")
    elif minutes % 1_440 == 0:
        count = minutes // 1_440
        unit = "днів" if language == "uk" else ("day" if count == 1 else "days")
    elif minutes % 60 == 0:
        count = minutes // 60
        unit = "год" if language == "uk" else ("hour" if count == 1 else "hours")
    else:
        count = minutes
        unit = "хв" if language == "uk" else ("minute" if count == 1 else "minutes")
    return f"{prefix} · {count} {unit}"


def select_hud_metrics(preset: str, codex: Iterable[Metric], claude: Iterable[Metric]) -> tuple[Metric, ...]:
    codex_values = tuple(sorted(codex, key=lambda metric: metric.window_duration_mins))
    claude_by_key = {metric.key: metric for metric in claude}
    if set(claude_by_key) != set(CLAUDE_KEYS):
        raise ValueError("incomplete Claude state")
    if preset == "wide":
        selected_codex = codex_values[:2]
    elif preset == "compact":
        weekly = tuple(metric for metric in codex_values if metric.window_duration_mins == 10_080)
        selected_codex = weekly[:1] if weekly else codex_values[-1:]
    else:
        raise ValueError("invalid preset")
    return selected_codex + tuple(claude_by_key[key] for key in CLAUDE_KEYS)


def select_visible_hud_metrics(
    preset: str,
    codex: Iterable[Metric],
    claude: Iterable[Metric],
    *,
    codex_visible: bool,
    claude_visible: bool,
) -> tuple[Metric, ...]:
    codex_values = tuple(sorted(codex, key=lambda metric: metric.window_duration_mins))
    claude_values = tuple(claude)
    selected: list[Metric] = []
    if codex_visible and codex_values:
        if preset == "wide":
            selected.extend(codex_values[:2])
        elif preset == "compact":
            selected.append(codex_values[-1])
        else:
            raise ValueError("invalid preset")
    elif preset not in {"compact", "wide"}:
        raise ValueError("invalid preset")
    if claude_visible:
        by_key = {metric.key: metric for metric in claude_values}
        if set(by_key) != set(CLAUDE_KEYS):
            raise ValueError("incomplete Claude state")
        selected.extend(by_key[key] for key in CLAUDE_KEYS)
    return tuple(selected)
