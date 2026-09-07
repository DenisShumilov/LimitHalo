"""Keep hosted fixture validation distinct from the frozen production builder."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def validation_errors(text: str) -> list[str]:
    errors = []
    if re.findall(r"python-version:\s*'([^']+)'", text) != ["3.12.10"]:
        errors.append("exact available validation interpreter")
    for required in (
        "architecture: x64", "runs-on: windows-2025", "persist-credentials: false",
        "Assert-PythonBuildLock.ps1", "Test-BuildLocks.ps1", "--require-hashes",
        "--no-index", "scripts/run_offline_tests.ps1", "Test-ReleaseFragment.ps1",
        "source_manifest.py", "validation modified source", "Confirm repository remains fixture-only",
    ):
        if required not in text:
            errors.append(required)
    if re.search(r"(?im)^\s*(?:continue-on-error|if):", text):
        errors.append("validation must not hide or skip failures")
    return errors


class ValidationContractTests(unittest.TestCase):
    def test_validation_is_pinned_and_keeps_all_coverage_boundaries(self):
        text = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        self.assertEqual(validation_errors(text), [])
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("python-version: '3.12.13'", release)
        self.assertIn("github.ref_protected", release)

    def test_unavailable_pin_and_removed_or_hidden_checks_are_detected(self):
        text = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        changes = (
            text.replace("python-version: '3.12.10'", "python-version: '3.12.13'"),
            text.replace("Test-BuildLocks.ps1", "Omitted-LockCheck.ps1"),
            text.replace("scripts/run_offline_tests.ps1", "scripts/omitted_tests.ps1"),
            text.replace("Test-ReleaseFragment.ps1", "Omitted-ReleaseTests.ps1"),
            text.replace("--require-hashes", "--omitted-hashes"),
            text + "\n    continue-on-error: true\n",
            text + "\n    if: false\n",
        )
        for changed in changes:
            with self.subTest(change=changed[-90:]):
                self.assertTrue(validation_errors(changed))


if __name__ == "__main__":
    unittest.main()
