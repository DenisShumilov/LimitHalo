from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .config import AppConfig, V3_KEYS
from .localization import STRINGS
from .model import LaneVisibility, resolve_provider_visibility
from .ui import (
    FOREGROUND_ALPHA_BYTE,
    MODE_SPECS,
    MUTED,
    PROVIDER_ICON_NAMES,
    REAR_ALPHA_BYTE,
    RESET_FONT_FAMILY,
    TEXT,
    TRANSPARENT_KEY,
    VALUE_FONT_FAMILY,
    compute_layout,
    hit_contour_contains,
)


KEYBOARD_ACTIONS = ["Tab", "Shift+Tab", "Enter", "Space", "Escape", "Ctrl+R", "Shift+F10", "Menu"]
MENU_ACTIONS = ["refresh", "colors:toggle", "check-updates", "startup", "topmost", "configure", "exit"]
G04_MUTANTS = (
    "GLYPH-CHANGED",
    "TYPOGRAPHY-CHANGED",
    "COMPACT-LAYOUT-CHANGED",
    "MONOCHROME-CHANGED",
    "BLANK-DRAG-REMOVED",
    "ENGLISH-DEFAULT-REMOVED",
    "UKRAINIAN-REMOVED",
    "PROVIDER-DISCOVERY-REMOVED",
    "UNUSED-LANE-SHOWN",
    "STARTUP-PREFERENCE-DROPPED",
    "CONFIG-FIELD-DROPPED",
    "AI-ENTRY-REMOVED",
)


def _relative_luminance(hex_color: str) -> float:
    if len(hex_color) != 7 or not hex_color.startswith("#"):
        raise ValueError("invalid color")
    channels = [int(hex_color[index:index + 2], 16) / 255.0 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(left: str, right: str) -> float:
    first, second = _relative_luminance(left), _relative_luminance(right)
    bright, dark = max(first, second), min(first, second)
    return (bright + 0.05) / (dark + 0.05)


def production_ui_contract() -> dict[str, Any]:
    return {
        "sizes": {name: list(spec.size) for name, spec in MODE_SPECS.items()},
        "anchors": {
            name: {
                "icons": [[x, y] for _provider, x, y in spec.icon_anchors],
                "textX": list(spec.text_anchors),
                "hitRects": [list(rect) for rect in spec.hit_rects],
            }
            for name, spec in MODE_SPECS.items()
        },
        "dpis": [96, 120, 144, 192],
        "native": {
            "windowCount": 2,
            "rearAlphaByte": 1,
            "foregroundAlphaByte": 255,
            "foregroundColorKey": "#010203",
            "samePidAligned": True,
            "foregroundDirectlyAboveRear": True,
            "nativeContextMenu": True,
            "menuOwner": "foreground-hwnd",
        },
        "interaction": {
            "ordinaryClick": "none",
            "popup": "none",
            "dragThresholdDip": 5.0,
            "dragThresholdExclusive": True,
            "wholeRoundedContour": True,
            "outsidePassThrough": True,
            "actionPrecedence": "action-unless-motion-greater-than-threshold",
        },
        "defaults": {"view": "compact", "colors": "monochrome", "alerts": False, "alertThreshold": 20},
        "keyboardActions": list(KEYBOARD_ACTIONS),
        "menuActions": list(MENU_ACTIONS),
        "translations": deepcopy(STRINGS),
        "noInventedCodexDaily": True,
    }


def validate_ui_contract(value: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    expected_sizes = {name: list(spec.size) for name, spec in MODE_SPECS.items()}
    if value.get("sizes") != expected_sizes:
        errors.append("geometry")
    anchors = value.get("anchors")
    if not isinstance(anchors, Mapping):
        errors.append("anchor")
    else:
        for name, spec in MODE_SPECS.items():
            expected = {
                "icons": [[x, y] for _provider, x, y in spec.icon_anchors],
                "textX": list(spec.text_anchors),
                "hitRects": [list(rect) for rect in spec.hit_rects],
            }
            if anchors.get(name) != expected:
                errors.append("anchor")
                break
    if value.get("dpis") != [96, 120, 144, 192]:
        errors.append("dpi")
    native = value.get("native")
    expected_native = {
        "windowCount": 2, "rearAlphaByte": 1, "foregroundAlphaByte": 255,
        "foregroundColorKey": "#010203", "samePidAligned": True,
        "foregroundDirectlyAboveRear": True, "nativeContextMenu": True,
        "menuOwner": "foreground-hwnd",
    }
    if native != expected_native:
        errors.append("native-window")
    interaction = value.get("interaction")
    if not isinstance(interaction, Mapping) or interaction.get("ordinaryClick") != "none" or interaction.get("popup") != "none":
        errors.append("click-popup")
    if not isinstance(interaction, Mapping) or interaction.get("dragThresholdDip") != 5.0 or interaction.get("dragThresholdExclusive") is not True:
        errors.append("drag-threshold")
    if not isinstance(interaction, Mapping) or interaction.get("wholeRoundedContour") is not True or interaction.get("outsidePassThrough") is not True:
        errors.append("drag-contour")
    if value.get("defaults") != {"view": "compact", "colors": "monochrome", "alerts": False, "alertThreshold": 20}:
        errors.append("defaults")
    if value.get("keyboardActions") != KEYBOARD_ACTIONS:
        errors.append("keyboard")
    if value.get("menuActions") != MENU_ACTIONS:
        errors.append("menu")
    translations = value.get("translations")
    if not isinstance(translations, Mapping) or set(translations) != {"uk", "en"}:
        errors.append("translation")
    else:
        uk, en = translations.get("uk"), translations.get("en")
        if not isinstance(uk, Mapping) or not isinstance(en, Mapping) or set(uk) != set(en):
            errors.append("translation")
        elif any(not isinstance(item, str) or not item.strip() for catalog in (uk, en) for item in catalog.values()):
            errors.append("translation")
        else:
            combined = " ".join(str(item).casefold() for catalog in (uk, en) for item in catalog.values())
            if "codex · daily" in combined or "codex · денний" in combined:
                errors.append("daily-label")
    if value.get("noInventedCodexDaily") is not True:
        errors.append("daily-label")
    return tuple(dict.fromkeys(errors))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65_536), b""):
            digest.update(block)
    return digest.hexdigest()


def production_g04_snapshot() -> dict[str, Any]:
    """Return a deterministic semantic and native-style preservation image."""

    asset_root = Path(__file__).resolve().parent / "assets"
    layouts: list[dict[str, Any]] = []
    for mode in MODE_SPECS:
        for dpi in (96, 120, 144, 192):
            layout = compute_layout(mode, dpi / 96)
            contour = bytearray()
            accepted = 0
            for y in range(layout.height):
                for x in range(layout.width):
                    hit = hit_contour_contains(layout, x, y)
                    contour.append(1 if hit else 0)
                    accepted += int(hit)
            layouts.append(
                {
                    "mode": mode,
                    "dpi": dpi,
                    "size": [layout.width, layout.height],
                    "iconSize": layout.icon_size,
                    "fontSizes": [layout.value_font_size, layout.reset_font_size],
                    "baselines": [layout.value_y, layout.reset_y],
                    "icons": [[provider, x, y] for provider, x, y in layout.icon_anchors],
                    "textX": list(layout.text_anchors),
                    "hitRects": [list(rect) for rect in layout.hit_rects],
                    "contourAcceptedCells": accepted,
                    "contourSha256": hashlib.sha256(contour).hexdigest(),
                }
            )

    defaults = AppConfig()
    hidden = resolve_provider_visibility(
        "auto", trusted_client_present=False, ever_succeeded=False
    )
    installed_later = resolve_provider_visibility(
        "auto", trusted_client_present=True, ever_succeeded=False
    )
    retained = resolve_provider_visibility(
        "auto",
        trusted_client_present=False,
        ever_succeeded=True,
        runtime_error="offline",
    )
    return {
        "schemaVersion": "g04-ui-native-v1",
        "assets": {
            name: _file_sha256(asset_root / filename)
            for name, filename in {
                "app": "app.ico",
                **PROVIDER_ICON_NAMES,
            }.items()
        },
        "typography": {
            "valueFamily": VALUE_FONT_FAMILY,
            "resetFamily": RESET_FONT_FAMILY,
        },
        "readyAppearance": {
            "defaultMode": defaults.colorMode,
            "value": TEXT,
            "reset": MUTED,
        },
        "native": {
            "windowCount": 2,
            "rearAlphaByte": REAR_ALPHA_BYTE,
            "foregroundAlphaByte": FOREGROUND_ALPHA_BYTE,
            "transparentKey": TRANSPARENT_KEY,
            "alignedPhysicalRectangles": True,
            "foregroundDirectlyAboveRear": True,
        },
        "interaction": {
            "wholeContourDrag": True,
            "outsidePassThrough": True,
            "actionPrecedenceAtFiveDip": True,
        },
        "layouts": layouts,
        "languages": {
            "freshDefault": defaults.locale,
            "ukrainianSelectable": "uk" in STRINGS,
        },
        "providers": {
            "defaults": [defaults.codexMode, defaults.claudeMode],
            "unusedHidden": hidden.lane is LaneVisibility.HIDDEN,
            "installedLaterVisible": installed_later.lane is LaneVisibility.VISIBLE,
            "lastGoodRetainedVisible": retained.lane is LaneVisibility.VISIBLE,
        },
        "startup": {
            "freshEnabled": defaults.startWithWindows,
            "userOffRoundTrips": True,
        },
        "config": {
            "knownFields": sorted(V3_KEYS),
            "safeUnknownTopLevelRoundTrips": True,
        },
        "aiEntry": {
            "file": "LimitHalo-Agent.ps1",
            "actions": ["Auto", "Status", "Install", "Repair"],
            "singleCommand": True,
        },
    }


def validate_g04_snapshot(value: Mapping[str, Any]) -> tuple[str, ...]:
    expected = production_g04_snapshot()
    checks = {
        "GLYPH-CHANGED": "assets",
        "TYPOGRAPHY-CHANGED": "typography",
        "COMPACT-LAYOUT-CHANGED": "layouts",
        "MONOCHROME-CHANGED": "readyAppearance",
        "BLANK-DRAG-REMOVED": "interaction",
        "ENGLISH-DEFAULT-REMOVED": "languages",
        "UKRAINIAN-REMOVED": "languages",
        "PROVIDER-DISCOVERY-REMOVED": "providers",
        "UNUSED-LANE-SHOWN": "providers",
        "STARTUP-PREFERENCE-DROPPED": "startup",
        "CONFIG-FIELD-DROPPED": "config",
        "AI-ENTRY-REMOVED": "aiEntry",
    }
    errors = [name for name, section in checks.items() if value.get(section) != expected[section]]
    # This validates the semantic snapshot only. Actual HWND measurements are
    # supplied by the independent native fixture, not invented by this function.
    if value.get("native") != expected["native"]:
        errors.append("NATIVE-STATE-CHANGED")
    if value.get("schemaVersion") != expected["schemaVersion"]:
        errors.extend(name for name in G04_MUTANTS if name not in errors)
    return tuple(errors)
