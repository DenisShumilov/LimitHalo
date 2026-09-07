from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from limit_halo import config as config_module
from limit_halo.config import (
    AppConfig,
    ConfigStore,
    FORBIDDEN_PERSISTED_FRAGMENTS,
    MAX_CONFIG_BYTES,
    decode_config,
)


class ConfigStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        # The run controller supplies an isolated, authorized output directory.
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ["LIMIT_HALO_TEST_ROOT"])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ConfigStore(self.root)

    @staticmethod
    def nondefault() -> AppConfig:
        return AppConfig(
            locale="uk-UA", claudeMode="enabled", codexMode="disabled",
            claudeConsent="granted", topmost=False, startWithWindows=False,
            displayPreset="wide", colorMode="status", alertsEnabled=True,
            alertThresholdPercent=5, quietHoursEnabled=True,
            quietStartMinutes=23 * 60 + 17, quietEndMinutes=6 * 60 + 43,
            x=-1750, y=236,
        )

    def put(self, value: object) -> bytes:
        payload = json.dumps(value, ensure_ascii=True).encode("ascii")
        self.store.path.write_bytes(payload)
        return payload

    def assert_preserved_and_blocked(self, payload: bytes, *, load: bool = True) -> None:
        self.store.path.write_bytes(payload)
        if load:
            self.assertEqual(self.store.load(), AppConfig())
        with self.assertRaises(OSError):
            self.store.save(replace(AppConfig(), x=4, y=1308))
        self.assertTrue(self.store.write_blocked)
        self.assertEqual(self.store.path.read_bytes(), payload)
        self.assertEqual(list(self.root.glob("config-*.tmp")), [])

    def test_nested_extensions_and_all_preferences_survive_restart_and_edits(self) -> None:
        value = self.nondefault().to_json_object()
        value["futureUi"] = {"scale": [1, 1.25], "appearance": {"small": True}}
        value["providers"]["futureProvider"] = {"display": "auto", "order": [1, 2]}
        value["alerts"]["sound"] = {"name": "soft", "volume": 0.25}
        value["alerts"]["quietHours"]["weekdays"] = [1, 2, {"day": 3}]
        self.put(value)
        loaded = self.store.load()
        self.assertEqual(loaded.to_json_object(), value)
        self.assertFalse(self.store.write_blocked)
        changed = replace(loaded, x=-1550, y=240, locale="en-US", colorMode="monochrome")
        self.store.save(changed)
        new_store = ConfigStore(self.root)
        restarted = new_store.load()
        self.assertEqual(restarted, changed)
        expected = copy.deepcopy(value)
        expected.update(x=-1550, y=240, locale="en-US", colors="monochrome")
        self.assertEqual(restarted.to_json_object(), expected)
        self.assertFalse(new_store.write_blocked)

    def test_missing_first_run_is_writable_with_and_without_load(self) -> None:
        self.assertEqual(self.store.load(), AppConfig())
        self.assertFalse(self.store.write_blocked)
        self.store.save(self.nondefault())
        self.assertEqual(ConfigStore(self.root).load(), self.nondefault())
        other = ConfigStore(self.root / "first-save")
        other.save(AppConfig(x=82, y=94))
        self.assertEqual(other.load(), AppConfig(x=82, y=94))

    def test_invalid_existing_current_and_old_files_are_never_clobbered(self) -> None:
        invalid = (
            b"{broken-json", b"", b"\xff\xfe",
            b'{"schemaVersion":3,"schemaVersion":3}',
            b'{"schemaVersion":3,"x":NaN}',
            b'{"schemaVersion":1,"x":"bad"}',
            b'{"schemaVersion":2,"language":"uk"}',
            b'{"schemaVersion":99,"x":17}',
            b'{"schemaVersion":1,"token":"synthetic-never-persist"}',
            b'{"x":"bad"}',
            b"{" + b" " * MAX_CONFIG_BYTES,
        )
        for payload in invalid:
            with self.subTest(payload_case=invalid.index(payload)):
                self.store = ConfigStore(self.root)
                self.assert_preserved_and_blocked(payload)

    def test_save_without_prior_load_cannot_overwrite_invalid_existing_file(self) -> None:
        self.assert_preserved_and_blocked(b"{invalid-before-first-load", load=False)

    def test_read_failure_preserves_existing_bytes_and_latches_until_valid_reload(self) -> None:
        original = self.put(self.nondefault().to_json_object())
        original_reader = Path.read_bytes

        def deny_target(path: Path) -> bytes:
            if path == self.store.path:
                raise PermissionError("synthetic read denied")
            return original_reader(path)

        with mock.patch.object(Path, "read_bytes", deny_target):
            self.assertEqual(self.store.load(), AppConfig())
        self.assertTrue(self.store.write_blocked)
        with self.assertRaises(OSError):
            self.store.save(AppConfig(x=4, y=1308))
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.load(), self.nondefault())
        self.assertFalse(self.store.write_blocked)
        self.store.save(replace(self.nondefault(), x=42))
        self.assertEqual(ConfigStore(self.root).load().x, 42)

    def test_invalid_file_explicitly_replaced_with_valid_file_can_recover(self) -> None:
        self.assert_preserved_and_blocked(b"{broken")
        self.put(self.nondefault().to_json_object())
        with self.assertRaises(OSError):
            self.store.save(AppConfig())
        recovered = self.store.load()
        self.assertEqual(recovered, self.nondefault())
        self.assertFalse(self.store.write_blocked)
        self.store.save(replace(recovered, x=15))
        self.assertEqual(ConfigStore(self.root).load().x, 15)

    def test_removing_rejected_file_does_not_silently_clear_same_instance_guard(self) -> None:
        self.assert_preserved_and_blocked(b"{broken")
        self.store.path.unlink()
        self.assertEqual(self.store.load(), AppConfig())
        self.assertTrue(self.store.write_blocked)
        with self.assertRaises(OSError):
            self.store.save(AppConfig(x=4, y=1308))
        self.assertFalse(self.store.path.exists())
        fresh = ConfigStore(self.root)
        fresh.save(AppConfig(x=83, y=91))
        self.assertEqual(fresh.load(), AppConfig(x=83, y=91))

    def test_valid_legacy_v1_v2_migrations_retain_preferences(self) -> None:
        cases = (
            ({"x": 17, "y": 28, "topmost": False}, AppConfig(x=17, y=28, topmost=False)),
            ({"schemaVersion": 1, "x": -420, "y": 37, "topmost": False},
             AppConfig(x=-420, y=37, topmost=False)),
            ({"schemaVersion": 2, "language": "uk", "claudeEnabled": True,
              "consentCompleted": True, "topmost": False, "startWithWindows": False,
              "displayPreset": "wide", "warningThresholds": [20, 10, 5], "x": 512, "y": 84},
             AppConfig(locale="uk-UA", claudeMode="enabled", claudeConsent="granted",
                       topmost=False, startWithWindows=False, displayPreset="wide", x=512, y=84)),
        )
        for old, expected in cases:
            with self.subTest(version=old.get("schemaVersion")):
                self.put(old)
                store = ConfigStore(self.root)
                self.assertEqual(store.load(), expected)
                self.assertFalse(store.write_blocked)
                self.assertEqual(json.loads(store.path.read_bytes()), expected.to_json_object())
                self.assertEqual(ConfigStore(self.root).load(), expected)

    def test_failed_migration_and_failed_atomic_writes_preserve_original(self) -> None:
        old = {"schemaVersion": 1, "x": 47, "y": 62, "topmost": False}
        original = self.put(old)
        with mock.patch("limit_halo.config.os.replace", side_effect=PermissionError("synthetic")):
            self.assertEqual(self.store.load(), AppConfig(x=47, y=62, topmost=False))
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("config-*.tmp")), [])
        self.store.save(self.nondefault())
        original = self.store.path.read_bytes()
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                with mock.patch("limit_halo.config.os." + operation, side_effect=OSError("synthetic")):
                    with self.assertRaises(OSError):
                        self.store.save(replace(self.nondefault(), x=22))
                self.assertEqual(self.store.path.read_bytes(), original)
                self.assertEqual(list(self.root.glob("config-*.tmp")), [])
        self.store.save(replace(self.nondefault(), x=25))
        self.assertEqual(ConfigStore(self.root).load().x, 25)

    def test_external_changes_do_not_get_replaced_by_stale_process_preferences(self) -> None:
        self.store.save(self.nondefault())
        loaded = self.store.load()
        external = replace(self.nondefault(), locale="en-US", x=9876)
        original = self.put(external.to_json_object())
        with self.assertRaises(OSError):
            self.store.save(replace(loaded, y=288))
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertTrue(self.store.write_blocked)
        self.assertEqual(self.store.load(), external)
        self.store.save(replace(external, y=299))
        self.assertEqual(ConfigStore(self.root).load().y, 299)

    def test_file_changed_during_atomic_save_is_preserved(self) -> None:
        self.store.save(self.nondefault())
        external = replace(self.nondefault(), x=9234, locale="en-US")
        external_payload = json.dumps(external.to_json_object()).encode("ascii")
        original_fsync = config_module.os.fsync

        def change_after_flush(descriptor: int) -> None:
            original_fsync(descriptor)
            self.store.path.write_bytes(external_payload)

        with mock.patch("limit_halo.config.os.fsync", side_effect=change_after_flush):
            with self.assertRaises(OSError):
                self.store.save(replace(self.nondefault(), x=42))
        self.assertTrue(self.store.write_blocked)
        self.assertEqual(self.store.path.read_bytes(), external_payload)
        self.assertEqual(list(self.root.glob("config-*.tmp")), [])

    def test_file_changed_during_read_is_not_accepted_or_overwritten(self) -> None:
        self.put(self.nondefault().to_json_object())
        replacement = json.dumps(replace(self.nondefault(), x=9991).to_json_object()).encode("ascii")
        original_read = Path.read_bytes

        def change_during_read(path: Path) -> bytes:
            payload = original_read(path)
            if path == self.store.path:
                path.write_bytes(replacement)
            return payload

        with mock.patch.object(Path, "read_bytes", change_during_read):
            self.assertEqual(self.store.load(), AppConfig())
        self.assertTrue(self.store.write_blocked)
        with self.assertRaises(OSError):
            self.store.save(AppConfig())
        self.assertEqual(self.store.path.read_bytes(), replacement)

    def test_forbidden_keys_are_rejected_at_every_extension_position(self) -> None:
        for fragment in FORBIDDEN_PERSISTED_FRAGMENTS:
            for path in ((), ("providers",), ("alerts",), ("alerts", "quietHours")):
                with self.subTest(fragment=fragment, path=path):
                    value = self.nondefault().to_json_object()
                    target = value
                    for key in path:
                        target = target[key]
                    target["future"] = [{".".join(fragment.upper()): "synthetic"}]
                    self.assertEqual(decode_config(value), AppConfig())
                    self.store = ConfigStore(self.root)
                    self.assert_preserved_and_blocked(json.dumps(value).encode("ascii"))

    def test_current_field_validation_and_overlay_collisions_stay_strict(self) -> None:
        mutations = (
            lambda item: item.__setitem__("schemaVersion", True),
            lambda item: item.__setitem__("locale", []),
            lambda item: item.__setitem__("autostart", "yes"),
            lambda item: item["providers"].__setitem__("codex", {}),
            lambda item: item["providers"].pop("claude"),
            lambda item: item["alerts"].__setitem__("thresholdPercent", 5.0),
            lambda item: item["alerts"]["quietHours"].__setitem__("start", "25:00"),
            lambda item: item["alerts"]["quietHours"].pop("end"),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                value = self.nondefault().to_json_object()
                mutation(value)
                self.assertEqual(decode_config(value), AppConfig())
        for overlay in (
            {"locale": "en-US"}, {"providers": {"claude": "disabled"}},
            {"alerts": {"quietHours": {"start": "06:00"}}},
            {"providers": []}, {"LOCALE": "en-US"},
        ):
            with self.subTest(overlay=overlay):
                value = replace(self.nondefault(), _preserved_unknown_json=json.dumps(overlay))
                with self.assertRaises(ValueError):
                    self.store.save(value)
                self.assertFalse(self.store.path.exists())

        for path, key in (((), "LOCALE"), (("providers",), "ＣＯＤＥＸ"),
                          (("alerts",), "quiet-hours"), (("alerts", "quietHours"), "START")):
            with self.subTest(alias=key):
                value = self.nondefault().to_json_object()
                target = value
                for part in path:
                    target = target[part]
                target[key] = "ambiguous"
                self.assertEqual(decode_config(value), AppConfig())

    def test_extension_depth_node_size_and_nonfinite_limits_remain_enforced(self) -> None:
        too_deep: object = True
        for _ in range(10):
            too_deep = {"next": too_deep}
        for extension in (
            {"future": too_deep}, {"future": "x" * 4097},
            {"future": list(range(65))}, {"future": 2**53 + 1},
            {"future": float("nan")}, {"future": float("inf")},
            {"future": {str(index): list(range(64)) for index in range(5)}},
            {"future": ["x" * 4096] * 9},
        ):
            with self.subTest(extension_type=type(extension["future"]).__name__):
                value = self.nondefault().to_json_object()
                value.update(extension)
                self.assertEqual(decode_config(value), AppConfig())
        oversized = replace(self.nondefault(), _preserved_unknown_json=json.dumps({"future": ["x" * 4096] * 9}))
        with self.assertRaises(ValueError):
            self.store.save(oversized)
        self.assertFalse(self.store.path.exists())

    def test_reparse_paths_remain_read_only_and_never_receive_temp_writes(self) -> None:
        original = self.put(self.nondefault().to_json_object())
        actual = config_module._is_reparse
        with mock.patch("limit_halo.config._is_reparse", side_effect=lambda path: path == self.root or actual(path)):
            self.assertEqual(self.store.load(), AppConfig())
            with self.assertRaises(OSError):
                self.store.save(AppConfig(x=4, y=1308))
        self.assertTrue(self.store.write_blocked)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("config-*.tmp")), [])

    def test_nonregular_existing_target_is_not_replaced(self) -> None:
        self.store.path.mkdir()
        self.assertEqual(self.store.load(), AppConfig())
        self.assertTrue(self.store.write_blocked)
        with self.assertRaises(OSError):
            self.store.save(self.nondefault())
        self.assertTrue(self.store.path.is_dir())
        self.assertEqual(list(self.root.glob("config-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
