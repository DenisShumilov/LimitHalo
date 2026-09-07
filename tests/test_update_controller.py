from __future__ import annotations
import os
import base64
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

from limit_halo.update_controller import UpdateController, launch_update
from limit_halo.updates import Release, StagedUpdate, UpdateError


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(os.environ["LIMIT_HALO_TEST_ROOT"])
        self.release = Release("1.0.2", "v1.0.2-beta.1", (), "https://github.com/DenisShumilov/LimitHalo/releases/tag/v1.0.2-beta.1")
        self.staged = StagedUpdate(self.parent / "stage", ":".join(["a" * 64]*3), self.release, self.parent)
        self.clock = [0.0]
        self.ask = mock.Mock(return_value=True)
        self.inform = mock.Mock()
        self.launch = mock.Mock()
        self.c = UpdateController(update_root=self.parent, language=lambda:"en", portable=False,
            ask=self.ask, inform=self.inform, find_release=mock.Mock(return_value=self.release),
            stage=mock.Mock(return_value=self.staged), launch=self.launch, clock=lambda:self.clock[0])

    def pump(self, until):
        deadline=time.monotonic()+3
        while not until() and time.monotonic()<deadline:
            self.c.tick()
            time.sleep(.002)
        self.assertTrue(until())

    def test_background_only_detects_without_popup_download_or_launch(self):
        self.clock[0]=16
        self.pump(lambda:self.c.release is not None)
        self.ask.assert_not_called(); self.c.stage.assert_not_called(); self.launch.assert_not_called()
        self.assertIn("v1.0.2-beta.1", self.c.menu_state()[0])

    def test_manual_exact_consent_once(self):
        self.c.click()
        self.pump(lambda:self.c.launch_requested)
        self.assertEqual(self.ask.call_count,2)
        self.assertIn(self.staged.acceptance,self.ask.call_args.args[1])
        self.launch.assert_called_once_with(self.staged,self.staged.acceptance,"en")
        self.c.click(); self.c.tick()
        self.launch.assert_called_once()

    def test_no_download_consent_no_stage(self):
        self.ask.return_value=False
        self.c.click(); self.pump(lambda:self.c.release is not None)
        self.c.stage.assert_not_called(); self.launch.assert_not_called()

    def test_cancel_exact_consent_runs_no_downloaded_code(self):
        self.ask.side_effect=[True,False]
        self.c.click(); self.pump(lambda:self.c.staged is not None)
        self.launch.assert_not_called()

    def test_portable_never_installs(self):
        self.c.portable=True
        self.c.click(); self.pump(lambda:self.c.staged is not None)
        self.assertEqual(self.ask.call_count,1)
        self.launch.assert_not_called()

    def test_late_result_after_close_no_dialog(self):
        gate=threading.Event()
        self.c.find_release=lambda _tag: (gate.wait(1),self.release)[1]
        self.c.click(); self.c.close(); gate.set()
        time.sleep(.03); self.c.tick()
        self.ask.assert_not_called(); self.launch.assert_not_called()

    def test_network_worker_does_not_touch_ui_and_coalesces(self):
        main=threading.get_ident(); gate=threading.Event(); ids=[]
        def find(_tag):
            ids.append(threading.get_ident()); gate.wait(1); return self.release
        self.c.find_release=find; self.ask.return_value=False
        self.ask.side_effect=lambda *_: self.assertEqual(threading.get_ident(),main) or False
        started=time.monotonic(); self.c.click(); self.c.click()
        self.assertLess(time.monotonic()-started,.1)
        gate.set(); self.pump(lambda:self.c.release is not None)
        self.assertEqual(len(ids),1); self.assertNotEqual(ids[0],main)

    def test_background_error_silent_and_bounded(self):
        self.c.find_release=mock.Mock(side_effect=UpdateError("offline"))
        self.clock[0]=16; self.c.tick(); self.pump(lambda:self.c.busy is None)
        self.inform.assert_not_called(); self.c.tick()
        self.c.find_release.assert_called_once()

    def test_wrong_acceptance_rejected_before_process(self):
        with mock.patch("limit_halo.update_controller.subprocess.Popen") as proc:
            with self.assertRaises(UpdateError): launch_update(self.staged,"wrong","en")
            proc.assert_not_called()

    def test_replaced_stage_rejected_before_process(self):
        with mock.patch("limit_halo.update_controller.verify_staged", return_value="changed"), mock.patch("limit_halo.update_controller.subprocess.Popen") as proc:
            with self.assertRaises(UpdateError): launch_update(self.staged,self.staged.acceptance,"en")
            proc.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows launcher")
    def test_launcher_sanitizes_powershell7_module_path(self):
        with mock.patch.dict(os.environ, {"PSModulePath": "C:\\Program Files\\PowerShell\\7\\Modules"}), \
             mock.patch("limit_halo.update_controller.verify_staged", return_value=self.staged.acceptance), \
             mock.patch("limit_halo.update_controller.subprocess.Popen") as proc:
            launch_update(self.staged, self.staged.acceptance, "uk")
            proc.assert_called_once()
            environment = proc.call_args.kwargs["env"]
            keys = [key for key in environment if key.casefold() == "psmodulepath"]
            self.assertEqual(keys, ["PSModulePath"])
            self.assertEqual(environment[keys[0]], str(Path(os.environ["SystemRoot"]) /
                "System32/WindowsPowerShell/v1.0/Modules"))
            self.assertIn("uk", proc.call_args.args[0])

    @unittest.skipUnless(os.name == "nt", "Windows helper process")
    def test_wrapper_captures_real_console_stdout_with_tainted_parent_environment(self):
        wrapper=Path(__file__).resolve().parents[1]/"src/limit_halo/assets/update_install.ps1"
        powershell=Path(os.environ["SystemRoot"])/"System32/WindowsPowerShell/v1.0/powershell.exe"
        with tempfile.TemporaryDirectory(prefix="wrapper-", dir=self.parent) as temporary:
            profile=Path(temporary)/"profile with spaces"
            stage=profile/"AILimitsWidget/updates/release"; stage.mkdir(parents=True)
            helper=stage/"LimitHalo-Agent.ps1"
            helper.write_text("$a=[string]$args[2];$p=$a.Split(':')\n"
                "[IO.File]::AppendAllText((Join-Path $PSScriptRoot 'calls.txt'),'ONE')\n"
                "$r=@{schemaVersion='1.0.0';kind='LimitHaloAgentResult';action='Auto';"
                "status='OK';launched=$true;acceptance=$a;candidateId=$p[0];"
                "setupSha256=$p[1];helperSha256=$p[2]}\n"
                "[Console]::Out.WriteLine(($r|ConvertTo-Json -Compress));exit 0\n",encoding="ascii")
            acceptance=":".join(["a"*64,"b"*64,hashlib.sha256(helper.read_bytes()).hexdigest()])
            (stage/"release-manifest.json").write_text(json.dumps({"candidateId":"a"*64,
                "artifacts":[{"path":"LimitHalo-1.0.2-Setup.exe","sha256":"b"*64}]}),encoding="ascii")
            environment={k:v for k,v in os.environ.items() if k.casefold()!="psmodulepath"}
            environment.update(LOCALAPPDATA=str(profile),PSModulePath=r"C:\Program Files\PowerShell\7\Modules")
            result=subprocess.run([str(powershell),"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
                "-File",str(wrapper),"-Bundle",str(stage),"-Acceptance",acceptance],env=environment,
                capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((stage/"calls.txt").read_text(),"ONE")

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell parser")
    def test_update_wrapper_parses_in_windows_powershell_51(self):
        wrapper=Path(__file__).resolve().parents[1]/"src/limit_halo/assets/update_install.ps1"
        self.assertTrue(wrapper.read_bytes().isascii(), "PowerShell5.1 wrapper must be ASCII-safe")
        powershell=Path(os.environ["SystemRoot"])/"System32/WindowsPowerShell/v1.0/powershell.exe"
        quoted=str(wrapper).replace("'", "''")
        script=("$ErrorActionPreference='Stop';$t=$null;$e=$null;"
            "[System.Management.Automation.Language.Parser]::ParseFile('"+quoted+"',[ref]$t,[ref]$e)|Out-Null;"
            "if($e.Count -ne 0){exit 1};if($PSVersionTable.PSVersion.Major -ne 5){exit 2};exit 0")
        encoded=base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result=subprocess.run([str(powershell),"-NoProfile","-NonInteractive","-EncodedCommand",encoded],
            capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__ == "__main__":
    unittest.main()
