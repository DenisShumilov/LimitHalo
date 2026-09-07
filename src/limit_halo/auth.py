from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .codex_discovery import (
    CODE_SIGNING_EKU,
    CodexCandidate,
    DiscoveryFailure,
    TrustRecord,
    discover_public_codex,
    verify_candidate_unchanged,
    verify_authenticode_cache_only,
)
from .config import reject_reparse_components
from .process_guard import safe_child_environment, system_directory


class AuthLaunchFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class TrustedAuthClient:
    provider: str
    executable: Path
    fixed_args: tuple[str, ...]
    trust_source: str
    codex_candidate: CodexCandidate | None = None


def trusted_codex_client(
    discovery: Callable[[], CodexCandidate] = discover_public_codex,
) -> TrustedAuthClient:
    try:
        candidate = discovery()
    except (OSError, DiscoveryFailure) as exc:
        raise AuthLaunchFailure("trusted Codex client unavailable") from exc
    executable = candidate.executable.resolve(strict=True)
    if executable != candidate.executable or executable.name.casefold() != "codex.exe":
        raise AuthLaunchFailure("trusted Codex client changed")
    return TrustedAuthClient("codex", executable, ("login",), candidate.source, candidate)


def _default_claude_candidates() -> tuple[Path, ...]:
    profile = os.environ.get("USERPROFILE")
    local = os.environ.get("LOCALAPPDATA")
    result: list[Path] = []
    if profile:
        result.append(Path(profile) / ".local" / "bin" / "claude.exe")
    if local:
        result.extend(
            (
                Path(local) / "Programs" / "Claude Code" / "claude.exe",
                Path(local) / "AnthropicClaude" / "claude.exe",
                Path(local)
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe"
                / "claude.exe",
            )
        )
    return tuple(result)


def _anthropic_trust(record: TrustRecord) -> bool:
    subject = dict(record.subject)
    common_name = subject.get("2.5.4.3", "").casefold().replace(",", " ")
    organization = subject.get("2.5.4.10", "").casefold().replace(",", " ")
    normalized_cn = " ".join(common_name.split())
    normalized_org = " ".join(organization.split())
    allowed = {"anthropic", "anthropic pbc"}
    return (
        record.status_valid
        and record.revocation_cache_available
        and CODE_SIGNING_EKU in record.enhanced_key_usages
        and normalized_cn in allowed
        and normalized_org in allowed
        and subject.get("2.5.4.6") == "US"
    )


def claude_client_installed(candidates: Iterable[Path] | None = None) -> bool:
    """Detect an allowlisted Claude install without executing or trusting it.

    Visibility must not disappear merely because Windows has no cached
    revocation result.  Execution still goes through ``trusted_claude_client``
    and its stricter Authenticode policy.
    """

    for candidate in _default_claude_candidates() if candidates is None else tuple(candidates):
        try:
            reject_reparse_components(candidate)
            resolved = candidate.resolve(strict=True)
            if (
                resolved == candidate.absolute()
                and resolved.name.casefold() == "claude.exe"
                and resolved.is_file()
            ):
                return True
        except (OSError, ValueError):
            continue
    return False


def trusted_claude_client(
    candidates: Iterable[Path] | None = None,
    *,
    trust_verifier: Callable[[Path], TrustRecord] = verify_authenticode_cache_only,
) -> TrustedAuthClient:
    """Select only an exact native claude.exe with cache-valid Anthropic signing."""

    for candidate in _default_claude_candidates() if candidates is None else tuple(candidates):
        try:
            reject_reparse_components(candidate)
            resolved = candidate.resolve(strict=True)
            if resolved != candidate.absolute() or resolved.name.casefold() != "claude.exe" or not resolved.is_file():
                continue
            if not _anthropic_trust(trust_verifier(resolved)):
                continue
            return TrustedAuthClient("claude", resolved, ("auth", "login"), "authenticode-cache-only")
        except (OSError, ValueError):
            continue
    raise AuthLaunchFailure("trusted official Claude client unavailable")


def launch_trusted_client(
    client: TrustedAuthClient,
    *,
    spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    codex_verifier: Callable[[CodexCandidate], Path] = verify_candidate_unchanged,
) -> subprocess.Popen[bytes]:
    if client.provider not in {"codex", "claude"}:
        raise AuthLaunchFailure("unsupported provider")
    expected = ("login",) if client.provider == "codex" else ("auth", "login")
    if client.fixed_args != expected:
        raise AuthLaunchFailure("authentication arguments rejected")
    executable = client.executable.resolve(strict=True)
    if executable != client.executable or executable.suffix.casefold() != ".exe":
        raise AuthLaunchFailure("authentication executable changed")
    if client.provider == "codex":
        if client.codex_candidate is None:
            raise AuthLaunchFailure("Codex authentication identity unavailable")
        try:
            verified = codex_verifier(client.codex_candidate)
        except (OSError, DiscoveryFailure) as exc:
            raise AuthLaunchFailure("Codex authentication identity changed") from exc
        if verified != executable:
            raise AuthLaunchFailure("Codex authentication identity changed")
    command = [str(executable), *client.fixed_args]
    # Intentionally visible and without a shell: the human owns authentication.
    # The packaged HUD is windowed, so a console client otherwise starts with no
    # usable surface and the click appears to do nothing.
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE
    return spawn(
        command,
        stdin=None,
        stdout=None,
        stderr=None,
        cwd=system_directory(),
        env=safe_child_environment(),
        shell=False,
        creationflags=creation_flags,
    )


def launch_provider_sign_in(
    provider: str,
    *,
    codex_resolver: Callable[[], TrustedAuthClient] = trusted_codex_client,
    claude_resolver: Callable[[], TrustedAuthClient] = trusted_claude_client,
    spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    if provider == "codex":
        return launch_trusted_client(codex_resolver(), spawn=spawn)
    if provider == "claude":
        return launch_trusted_client(claude_resolver(), spawn=spawn)
    raise AuthLaunchFailure("unsupported provider")
