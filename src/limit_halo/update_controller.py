"""Quiet update state machine; workers never call Tk or execute remote code."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Callable

from . import RELEASE_TAG
from .localization import tr
from .updates import Release, StagedUpdate, UpdateError, discover, stage_release, verify_staged

CHECK_INTERVAL_SECONDS = 6 * 60 * 60


def launch_update(staged: StagedUpdate, acceptance: str, language: str) -> None:
    """Called only for the tuple explicitly confirmed by the person, once."""
    if acceptance != staged.acceptance or verify_staged(staged) != acceptance:
        raise UpdateError("Staged update changed after confirmation")
    if os.name != "nt":
        raise UpdateError("Updates require Windows")
    wrapper = Path(__file__).resolve().parent / "assets" / "update_install.ps1"
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not wrapper.is_file() or not powershell.is_file():
        raise UpdateError("Update launcher unavailable")
    # A Python/frozen child of PowerShell 7 otherwise passes its incompatible
    # module tree to Windows PowerShell 5.1. Remove every case variant first.
    environment = {key: value for key, value in os.environ.items()
                   if key.casefold() != "psmodulepath"}
    environment["PSModulePath"] = str(powershell.parent / "Modules")
    subprocess.Popen(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper), "-Bundle", str(staged.directory),
         "-Acceptance", acceptance, "-Language", "uk" if language == "uk" else "en"],
        cwd=str(staged.directory), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True, env=environment,
    )


class UpdateController:
    def __init__(
        self, *, update_root: Path, language: Callable[[], str], portable: bool,
        ask: Callable[[str, str], bool], inform: Callable[[str, str], None],
        find_release=discover, stage=stage_release, launch=launch_update,
        clock=time.monotonic,
    ) -> None:
        self.update_root, self.language, self.portable = update_root, language, portable
        self.ask, self.inform = ask, inform
        self.find_release, self.stage, self.launch, self.clock = find_release, stage, launch, clock
        self.release: Release | None = None
        self.staged: StagedUpdate | None = None
        self.busy: str | None = None
        self.closed = False
        self.manual = False
        self.launch_requested = False
        self.next_check = self.clock() + 15
        self.results: queue.Queue = queue.Queue()

    def text(self, key: str, **values) -> str:
        return tr(self.language(), key, **values)

    def menu_state(self) -> tuple[str, bool]:
        if self.launch_requested:
            return self.text("update_installing"), False
        if self.busy:
            key = {"stage": "update_downloading", "launch": "update_installing", "check": "update_checking"}[self.busy]
            return self.text(key), False
        if self.release:
            return self.text("update_available_menu", version=self.release.tag), True
        return self.text("check_updates"), True

    def _work(self, operation: str, call: Callable) -> None:
        if self.closed or self.busy or self.launch_requested:
            return
        self.busy = operation
        def worker() -> None:
            try:
                self.results.put((operation, call(), None))
            except Exception:
                # No paths, credentials, remote response bodies or tracebacks in UI.
                self.results.put((operation, None, "failed"))
        threading.Thread(target=worker, name="LimitHalo-update", daemon=True).start()

    def click(self) -> None:
        if self.closed or self.busy or self.launch_requested:
            return
        self.manual = True
        if self.release is not None:
            self._offer_download()
        else:
            self._work("check", lambda: self.find_release(RELEASE_TAG))

    def _offer_download(self) -> None:
        release = self.release
        if release is None or self.closed:
            return
        message = self.text("update_download_question", version=release.tag)
        if self.portable:
            message += "\n\n" + self.text("update_portable_notice")
        if self.ask(self.text("check_updates"), message):
            self._work("stage", lambda: self.stage(release, self.update_root))

    def tick(self) -> None:
        """Pump on the UI thread. Background checks never show a dialog."""
        if self.closed:
            return
        while True:
            try:
                operation, value, error = self.results.get_nowait()
            except queue.Empty:
                break
            self.busy = None
            if error:
                if self.manual or operation != "check":
                    self.inform(self.text("check_updates"), self.text("update_failed"))
                self.manual = False
                self.next_check = self.clock() + CHECK_INTERVAL_SECONDS
                continue
            if operation == "check":
                self.next_check = self.clock() + CHECK_INTERVAL_SECONDS
                self.release = value
                if self.manual:
                    if value is None:
                        self.inform(self.text("check_updates"), self.text("update_current", version=RELEASE_TAG))
                    else:
                        self._offer_download()
                self.manual = False
            elif operation == "stage":
                self.staged = value
                if self.portable:
                    self.inform(self.text("check_updates"), self.text("update_portable_ready", folder=str(value.directory)))
                    continue
                # The exact identity is computed locally; no downloaded helper
                # has run, including its otherwise harmless-looking Status mode.
                if self.ask(self.text("update_install_title"), self.text(
                    "update_exact_consent", version=value.release.tag, acceptance=value.acceptance
                )):
                    self._work("launch", lambda: self.launch(value, value.acceptance, self.language()))
            elif operation == "launch":
                self.launch_requested = True
        if not self.busy and not self.launch_requested and self.clock() >= self.next_check:
            self.next_check = self.clock() + CHECK_INTERVAL_SECONDS
            self._work("check", lambda: self.find_release(RELEASE_TAG))

    def close(self) -> None:
        self.closed = True
