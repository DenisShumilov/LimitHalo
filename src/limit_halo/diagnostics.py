from __future__ import annotations

import json
import os
import platform
import sys
import time
from typing import Iterable

from . import APP_NAME, __version__
from .config import AppConfig, SCHEMA_VERSION
from .model import Metric, ProviderState


SAFE_FIELDS = frozenset(
    {
        "application",
        "windows",
        "effectiveDpiBucket",
        "settingsSchemaVersion",
        "providerState",
        "freshnessAgeBucket",
        "retryAfterBucket",
        "redactedErrorCategoryCode",
    }
)
ALLOWED_ERROR_CODES = frozenset(
    {
        "none",
        "codex_unavailable",
        "broker_failure",
        "auth_locked",
        "credential_missing",
        "credential_unsupported",
        "schema_mismatch",
        "security_policy",
        "deadline",
        "internal",
        "lock_contended",
        "credential_changed",
        "credential_malformed",
        "bad_request",
        "transport_timeout",
        "transport_name_resolution",
        "transport_cannot_connect",
        "transport_secure_failure",
        "transport_option_failure",
        "transport_http_429",
        "transport_http_5xx",
        "transport_other",
    }
)


def dpi_bucket(dpi: int) -> int:
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        return 96
    return min((96, 120, 144, 192), key=lambda candidate: abs(candidate - dpi))


def _age_bucket(metrics: Iterable[Metric], now: float) -> str:
    values = tuple(metrics)
    if not values:
        return "none"
    age = max(0.0, now - max(metric.observed_at for metric in values))
    if age < 60:
        return "under_1m"
    if age < 300:
        return "1_to_5m"
    if age < 900:
        return "5_to_15m"
    if age < 3600:
        return "15_to_60m"
    return "over_1h"


def _retry_bucket(metrics: Iterable[Metric]) -> str:
    retries = [metric.retry_after_seconds for metric in metrics if metric.retry_after_seconds is not None]
    if not retries:
        return "none"
    retry = max(retries)
    if retry <= 60:
        return "under_1m"
    if retry <= 300:
        return "1_to_5m"
    if retry <= 900:
        return "5_to_15m"
    return "over_15m"


def _provider_state(metrics: tuple[Metric, ...], provider: str, config: AppConfig) -> str:
    if provider == "claude" and not config.claudeEnabled:
        return ProviderState.DISABLED.value
    if not metrics:
        return ProviderState.UNAVAILABLE.value
    order = (
        ProviderState.AUTHORIZATION_REQUIRED,
        ProviderState.PROVIDER_ERROR,
        ProviderState.OFFLINE,
        ProviderState.STALE,
        ProviderState.REFRESHING,
        ProviderState.UNAVAILABLE,
        ProviderState.DISABLED,
        ProviderState.READY,
    )
    states = {metric.state for metric in metrics}
    return next(state.value for state in order if state in states)


def safe_diagnostics(
    config: AppConfig,
    codex: tuple[Metric, ...],
    claude: tuple[Metric, ...],
    *,
    dpi: int,
    now: float | None = None,
) -> dict[str, object]:
    current = time.time() if now is None else now
    errors = {
        metric.error_code
        for metric in (*codex, *claude)
        if metric.error_code in ALLOWED_ERROR_CODES
    }
    result: dict[str, object] = {
        "application": {"name": APP_NAME, "semanticVersion": __version__},
        "windows": {
            "family": "Windows" if os.name == "nt" else "non-Windows fixture",
            "architecture": platform.machine() or ("64-bit" if sys.maxsize > 2**32 else "32-bit"),
        },
        "effectiveDpiBucket": dpi_bucket(dpi),
        "settingsSchemaVersion": SCHEMA_VERSION,
        "providerState": {
            "codex": _provider_state(codex, "codex", config),
            "claude": _provider_state(claude, "claude", config),
        },
        "freshnessAgeBucket": {
            "codex": _age_bucket(codex, current),
            "claude": _age_bucket(claude, current),
        },
        "retryAfterBucket": {
            "codex": _retry_bucket(codex),
            "claude": _retry_bucket(claude),
        },
        "redactedErrorCategoryCode": sorted(errors) if errors else ["none"],
    }
    if set(result) != SAFE_FIELDS:
        raise RuntimeError("unsafe diagnostics schema")
    return result


def diagnostics_json(value: dict[str, object]) -> str:
    if set(value) != SAFE_FIELDS:
        raise ValueError("unsafe diagnostics")
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
