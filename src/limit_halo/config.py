from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 3
LOCALES = frozenset({"uk-UA", "en-US"})
LANGUAGES = frozenset({"uk", "en"})
PROVIDER_MODES = frozenset({"auto", "enabled", "disabled"})
DISPLAY_PRESETS = frozenset({"compact", "wide"})
COLOR_MODES = frozenset({"monochrome", "status"})
ALERT_THRESHOLDS = frozenset({20, 10, 5})
MAX_CONFIG_BYTES = 32_768
V3_KEYS = frozenset(
    {"schemaVersion", "locale", "providers", "claudeConsent", "autostart", "colors", "alerts", "topmost", "view", "x", "y"}
)
PROVIDER_KEYS = frozenset({"claude", "codex"})
ALERT_KEYS = frozenset({"enabled", "thresholdPercent", "quietHours"})
QUIET_HOUR_KEYS = frozenset({"enabled", "start", "end"})
V2_KEYS = frozenset(
    {
        "schemaVersion",
        "language",
        "claudeEnabled",
        "consentCompleted",
        "topmost",
        "startWithWindows",
        "displayPreset",
        "warningThresholds",
        "x",
        "y",
    }
)
V1_KEYS = frozenset({"schemaVersion", "x", "y", "topmost"})
LEGACY_KEYS = frozenset({"x", "y", "topmost"})
FORBIDDEN_PERSISTED_FRAGMENTS = (
    "token",
    "credential",
    "response",
    "request",
    "providerbody",
    "chat",
    "browser",
    "log",
    "metric",
    "quota",
    "history",
    "account",
    "resetat",
    "usedpercent",
    "remainingpercent",
)


@dataclass(frozen=True)
class AppConfig:
    schemaVersion: int = SCHEMA_VERSION
    locale: str = "en-US"
    claudeMode: str = "auto"
    codexMode: str = "auto"
    claudeConsent: str = "not-asked"
    topmost: bool = True
    startWithWindows: bool = True
    displayPreset: str = "compact"
    colorMode: str = "monochrome"
    alertsEnabled: bool = False
    alertThresholdPercent: int = 20
    quietHoursEnabled: bool = False
    quietStartMinutes: int = 22 * 60
    quietEndMinutes: int = 8 * 60
    x: int | None = None
    y: int | None = None
    # Canonical JSON overlay for future, non-sensitive settings fields.  The
    # application never interprets these values; it only round-trips them so a
    # newer version's settings are not destroyed by an older compatible build.
    _preserved_unknown_json: str = field(default="{}", repr=False)

    @property
    def language(self) -> str:
        return "uk" if self.locale == "uk-UA" else "en"

    @property
    def claudeEnabled(self) -> bool:
        return self.claudeMode != "disabled"

    @property
    def consentCompleted(self) -> bool:
        return self.claudeConsent == "granted"

    @property
    def claudeConsentCompleted(self) -> bool:
        return self.claudeConsent == "granted"

    @property
    def warningThresholds(self) -> tuple[int, int, int]:
        return (20, 10, 5)

    def to_json_object(self) -> dict[str, Any]:
        result = {
            "schemaVersion": SCHEMA_VERSION,
            "locale": self.locale,
            "providers": {"claude": self.claudeMode, "codex": self.codexMode},
            "claudeConsent": self.claudeConsent,
            "autostart": self.startWithWindows,
            "colors": self.colorMode,
            "alerts": {
                "enabled": self.alertsEnabled,
                "thresholdPercent": self.alertThresholdPercent,
                "quietHours": {
                    "enabled": self.quietHoursEnabled,
                    "start": _format_minutes(self.quietStartMinutes),
                    "end": _format_minutes(self.quietEndMinutes),
                },
            },
            "topmost": self.topmost,
            "view": self.displayPreset,
            "x": self.x,
            "y": self.y,
        }
        try:
            if not isinstance(self._preserved_unknown_json, str) or len(self._preserved_unknown_json) > MAX_CONFIG_BYTES:
                raise ValueError("preserved configuration fields are too large")
            overlay = json.loads(
                self._preserved_unknown_json,
                object_pairs_hook=_no_duplicate_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite extension number")
                ),
            )
        except (TypeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise ValueError("invalid preserved configuration fields") from exc
        if not isinstance(overlay, dict) or not _safe_unknown_value(overlay):
            raise ValueError("invalid preserved configuration fields")
        _merge_unknown_overlay(result, overlay)
        return result


def safe_defaults() -> AppConfig:
    return AppConfig()


def _is_reparse(path: Path) -> bool:
    try:
        information = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(information, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _existing_chain(path: Path) -> tuple[Path, ...]:
    absolute = Path(os.path.abspath(path))
    chain: list[Path] = []
    cursor = absolute
    while True:
        if cursor.exists() or cursor.is_symlink():
            chain.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    chain.reverse()
    return tuple(chain)


def reject_reparse_components(path: Path) -> None:
    for component in _existing_chain(path):
        if _is_reparse(component):
            raise OSError("reparse component rejected")


def _valid_coordinate(value: Any) -> bool:
    return value is None or (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -100_000 <= value <= 100_000
    )


def _plain_int(value: Any, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _format_minutes(value: int) -> str:
    if not _plain_int(value, 0, 1_439):
        raise ValueError("invalid local time")
    return f"{value // 60:02d}:{value % 60:02d}"


def _parse_minutes(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return None
    hours, minutes = value[:2], value[3:]
    if not hours.isascii() or not minutes.isascii() or not hours.isdecimal() or not minutes.isdecimal():
        return None
    result = int(hours) * 60 + int(minutes)
    if int(hours) > 23 or int(minutes) > 59:
        return None
    return result


def _has_forbidden_key(keys: set[str]) -> bool:
    normalized = {_normalized_key(key) for key in keys}
    fragments = {
        "".join(character for character in fragment if character.isalnum())
        for fragment in FORBIDDEN_PERSISTED_FRAGMENTS
    }
    return any(fragment in key for key in normalized for fragment in fragments)


def _normalized_key(key: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", key).casefold()
        if character.isalnum()
    )


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = item
    return result


def _safe_unknown_value(
    value: Any, *, depth: int = 0, budget: list[int] | None = None
) -> bool:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 8 or budget[0] > 256:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= 4_096
    if isinstance(value, int) and not isinstance(value, bool):
        return -(2**53) <= value <= 2**53
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return len(value) <= 64 and all(
            _safe_unknown_value(item, depth=depth + 1, budget=budget)
            for item in value
        )
    if isinstance(value, Mapping):
        keys = set(value)
        return (
            len(keys) <= 64
            and all(isinstance(key, str) and 0 < len(key) <= 128 for key in keys)
            and not _has_forbidden_key(keys)
            and all(
                _safe_unknown_value(item, depth=depth + 1, budget=budget)
                for item in value.values()
            )
        )
    return False


def _merge_unknown_overlay(target: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    known_normalized = {_normalized_key(key) for key in target}
    for key, value in overlay.items():
        if key not in target:
            if _normalized_key(key) in known_normalized:
                raise ValueError("preserved configuration field aliases current schema")
            target[key] = value
            continue
        existing = target[key]
        if not isinstance(existing, dict) or not isinstance(value, Mapping):
            raise ValueError("preserved configuration field collides with current schema")
        _merge_unknown_overlay(existing, value)


def _unknown_overlay(value: Mapping[str, Any]) -> dict[str, Any] | None:
    overlay = {key: item for key, item in value.items() if key not in V3_KEYS}
    providers = value["providers"]
    alerts = value["alerts"]
    quiet = alerts["quietHours"]
    provider_overlay = {key: item for key, item in providers.items() if key not in PROVIDER_KEYS}
    alert_overlay = {key: item for key, item in alerts.items() if key not in ALERT_KEYS}
    quiet_overlay = {key: item for key, item in quiet.items() if key not in QUIET_HOUR_KEYS}
    if provider_overlay:
        overlay["providers"] = provider_overlay
    if quiet_overlay:
        alert_overlay["quietHours"] = quiet_overlay
    if alert_overlay:
        overlay["alerts"] = alert_overlay
    if not _safe_unknown_value(overlay):
        return None
    try:
        # Reject disguised aliases of known fields as well as exact collisions;
        # only the schema's explicit container branches may be merged.
        _merge_unknown_overlay(safe_defaults().to_json_object(), overlay)
        encoded = json.dumps(
            overlay,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return overlay if len(encoded) <= MAX_CONFIG_BYTES else None


def _decode_v3(value: Mapping[str, Any]) -> AppConfig | None:
    if not V3_KEYS.issubset(value):
        return None
    if not isinstance(value.get("topmost"), bool) or not isinstance(value.get("autostart"), bool):
        return None
    if value.get("locale") not in LOCALES:
        return None
    if value.get("claudeConsent") not in {"not-asked", "granted", "denied"}:
        return None
    providers = value.get("providers")
    if not isinstance(providers, Mapping) or not PROVIDER_KEYS.issubset(providers):
        return None
    if providers.get("claude") not in PROVIDER_MODES or providers.get("codex") not in PROVIDER_MODES:
        return None
    if value.get("view") not in DISPLAY_PRESETS or value.get("colors") not in COLOR_MODES:
        return None
    alerts = value.get("alerts")
    if not isinstance(alerts, Mapping) or not ALERT_KEYS.issubset(alerts):
        return None
    if (
        not isinstance(alerts.get("enabled"), bool)
        or not _plain_int(alerts.get("thresholdPercent"), 0, 100)
        or alerts.get("thresholdPercent") not in ALERT_THRESHOLDS
    ):
        return None
    quiet = alerts.get("quietHours")
    if not isinstance(quiet, Mapping) or not QUIET_HOUR_KEYS.issubset(quiet) or not isinstance(quiet.get("enabled"), bool):
        return None
    start, end = _parse_minutes(quiet.get("start")), _parse_minutes(quiet.get("end"))
    if start is None or end is None:
        return None
    if not _valid_coordinate(value.get("x")) or not _valid_coordinate(value.get("y")):
        return None
    overlay = _unknown_overlay(value)
    if overlay is None:
        return None
    try:
        return AppConfig(
            locale=value["locale"],
            claudeMode=providers["claude"],
            codexMode=providers["codex"],
            claudeConsent=value["claudeConsent"],
            topmost=value["topmost"],
            startWithWindows=value["autostart"],
            displayPreset=value["view"],
            colorMode=value["colors"],
            alertsEnabled=alerts["enabled"],
            alertThresholdPercent=alerts["thresholdPercent"],
            quietHoursEnabled=quiet["enabled"],
            quietStartMinutes=start,
            quietEndMinutes=end,
            x=value["x"],
            y=value["y"],
            _preserved_unknown_json=json.dumps(
                overlay,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _decode_config(value: Any) -> AppConfig | None:
    """Strict decoder: invalid input must not become saveable default state."""

    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        return None
    keys = set(value)
    if _has_forbidden_key(keys):
        return None
    version = value.get("schemaVersion")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
        return None
    if version == SCHEMA_VERSION:
        return _decode_v3(value)
    if version == 2:
        if keys != set(V2_KEYS):
            return None
        bool_keys = ("claudeEnabled", "consentCompleted", "topmost", "startWithWindows")
        if any(not isinstance(value.get(key), bool) for key in bool_keys):
            return None
        if value.get("language") not in LANGUAGES or value.get("displayPreset") not in DISPLAY_PRESETS:
            return None
        old_thresholds = value.get("warningThresholds")
        if not (
            isinstance(old_thresholds, list)
            and len(old_thresholds) == 3
            and all(_plain_int(item, 0, 100) for item in old_thresholds)
            and old_thresholds[2] < old_thresholds[1] < old_thresholds[0]
        ):
            return None
        if not _valid_coordinate(value.get("x")) or not _valid_coordinate(value.get("y")):
            return None
        if value["claudeEnabled"] and not value["consentCompleted"]:
            return None
        return AppConfig(
            locale="uk-UA" if value["language"] == "uk" else "en-US",
            claudeMode="enabled" if value["claudeEnabled"] else "disabled",
            codexMode="auto",
            claudeConsent="granted" if value["consentCompleted"] else "denied",
            topmost=value["topmost"],
            startWithWindows=value["startWithWindows"],
            displayPreset="compact" if value["displayPreset"] not in DISPLAY_PRESETS else value["displayPreset"],
            colorMode="monochrome",
            alertsEnabled=False,
            x=value["x"],
            y=value["y"],
        )
    if version == 1:
        if not keys.issubset(V1_KEYS) or keys == {"schemaVersion"}:
            return None
    elif version is None:
        if not keys or not keys.issubset(LEGACY_KEYS):
            return None
    else:
        return None
    if "x" in value and not _valid_coordinate(value["x"]):
        return None
    if "y" in value and not _valid_coordinate(value["y"]):
        return None
    if "topmost" in value and not isinstance(value["topmost"], bool):
        return None
    return replace(
        safe_defaults(),
        x=value.get("x"),
        y=value.get("y"),
        topmost=value.get("topmost", True),
    )


def _try_decode_config(value: Any) -> AppConfig | None:
    try:
        return _decode_config(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def decode_config(value: Any) -> AppConfig:
    """Return safe display defaults for invalid input; never authorize a write."""
    return _try_decode_config(value) or safe_defaults()


class ConfigStore:
    def __init__(self, state_root: Path) -> None:
        root = Path(os.path.abspath(state_root))
        self.path = root / "config.json"
        self._write_blocked_reason: str | None = None
        self._observed_disk = False
        self._disk_revision: tuple[int, ...] | None = None

    @property
    def write_blocked(self) -> bool:
        """Existing data must be recovered and explicitly reloaded before writes."""
        return self._write_blocked_reason is not None

    @property
    def write_blocked_reason(self) -> str | None:
        # Static codes only: never expose the original data or an exception body.
        return self._write_blocked_reason

    def _revision(self) -> tuple[int, ...] | None:
        reject_reparse_components(self.path)
        try:
            information = self.path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(information.st_mode):
            raise OSError("configuration target is not a regular file")
        return (
            information.st_dev, information.st_ino, information.st_size,
            information.st_mtime_ns, information.st_ctime_ns,
        )

    def _read_current(self) -> tuple[AppConfig, bool, tuple[int, ...] | None]:
        revision = self._revision()
        if revision is None:
            return safe_defaults(), False, None
        if revision[2] > MAX_CONFIG_BYTES:
            raise ValueError("configuration is too large")
        data = self.path.read_bytes()
        if not data or len(data) > MAX_CONFIG_BYTES:
            raise ValueError("configuration size is invalid")
        raw = json.loads(
            data.decode("utf-8", "strict"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite configuration number")
            ),
        )
        decoded = _try_decode_config(raw)
        if decoded is None:
            raise ValueError("configuration schema is invalid")
        if self._revision() != revision:
            raise OSError("configuration changed during read")
        migrate = raw.get("schemaVersion") in (None, 1, 2)
        return decoded, migrate, revision

    def load(self) -> AppConfig:
        try:
            decoded, migrate, revision = self._read_current()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, OverflowError, RecursionError):
            self._write_blocked_reason = "existing-config-unreadable-or-invalid"
            return safe_defaults()
        # Removing a previously rejected file is not an implicit reset.  A valid
        # replacement plus explicit reload recovers this instance; a fresh
        # process with no file remains an ordinary first run.
        if revision is not None or not self.write_blocked:
            self._write_blocked_reason = None
        self._observed_disk = True
        self._disk_revision = revision
        if migrate:
            try:
                self.save(decoded)
            except OSError:
                pass
        return decoded

    def _check_write_target(self) -> None:
        if self.write_blocked:
            raise OSError("existing configuration preserved; valid reload required")
        try:
            if not self._observed_disk:
                _, _, revision = self._read_current()
                self._disk_revision = revision
                self._observed_disk = True
            if self._revision() != self._disk_revision:
                raise OSError("configuration changed since last load or save")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, OverflowError, RecursionError) as exc:
            self._write_blocked_reason = "existing-config-changed-or-unreadable"
            raise OSError("existing configuration preserved; valid reload required") from exc

    def save(self, config: AppConfig) -> None:
        if not isinstance(config, AppConfig):
            raise TypeError("AppConfig required")
        value = config.to_json_object()
        if _try_decode_config(value) != config:
            raise ValueError("unsafe configuration")
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        if len(payload.encode("ascii")) > MAX_CONFIG_BYTES:
            raise ValueError("configuration is too large")
        self._check_write_target()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        reject_reparse_components(self.path)
        descriptor, temporary = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            reject_reparse_components(Path(temporary))
            self._check_write_target()
            os.replace(temporary, self.path)
            self._disk_revision = self._revision()
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
