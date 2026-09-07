"""Offline updater boundaries. No helper, installer, or real user state is used."""

from dataclasses import replace
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.request import Request

from limit_halo import updates as u


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture(tag="v1.0.2-beta.1"):
    version = tag[1:].split("-")[0]
    files = {
        f"LimitHalo-{version}-Setup.exe": b"inert setup fixture, not an executable",
        f"LimitHalo-{version}-Windows-x64.zip": b"inert portable fixture, not a zip",
        "LimitHalo-Agent.ps1": b"inert helper fixture, never executed",
    }
    manifest = {
        "schemaVersion": "1.0.0", "kind": "release-bundle", "internalProductId": "AILimitsWidget",
        "displayName": "LimitHalo", "displayNameStatus": "temporary-working-name",
        "version": version, "architecture": "x64", "candidateId": "a" * 64,
        "sourceIdentity": "sha256:" + "b" * 64,
        "signature": {"status": "unsigned", "verifiedSigner": None, "statement": "Unsigned test."},
        "automaticUpdater": False, "hostedGitHubActionsObserved": False, "publicationPerformed": False,
        "payloadManifestSha256": "c" * 64, "dependencyLicenseManifestSha256": "d" * 64,
        "dependencyInventory": [], "buildInputs": [],
        "artifacts": [{"path": name, "size": len(data), "sha256": sha(data)} for name, data in files.items()],
    }
    files["release-manifest.json"] = json.dumps(manifest).encode()
    files["SHA256SUMS.txt"] = "".join(f"{sha(data)}  {name}\n" for name, data in files.items()).encode()
    release = {
        "id": 1, "url": f"https://api.github.com/repos/{u.REPOSITORY}/releases/1",
        "tag_name": tag, "draft": False, "prerelease": "-beta." in tag,
        "html_url": f"{u.REPOSITORY_URL}/releases/tag/{tag}",
        "assets": [{"id": index, "name": name, "size": len(data), "digest": "sha256:" + sha(data),
                    "state": "uploaded", "url": f"https://api.github.com/repos/{u.REPOSITORY}/releases/assets/{index}",
                    "browser_download_url": f"{u.REPOSITORY_URL}/releases/download/{tag}/{name}"}
                   for index, (name, data) in enumerate(files.items(), 1)],
    }
    return release, files


class FixtureTransport:
    def __init__(self, releases, files=None):
        self.releases = releases
        self.files = files or {}
        self.calls = []

    def get(self, url, **options):
        self.calls.append((url, options))
        if url == u.RELEASES_URL:
            return json.dumps(self.releases).encode()
        return self.files[url.rsplit("/", 1)[1]]


class DiscoveryTests(unittest.TestCase):
    def discover(self, release, current="v1.0.1-beta.1"):
        return u.discover(current, transport=FixtureTransport([release]))

    def test_newer_beta_is_offered_without_downloading_any_asset(self):
        data, files = fixture()
        client = FixtureTransport([data], files)
        release = u.discover("v1.0.1-beta.1", transport=client)
        self.assertEqual(release.tag, "v1.0.2-beta.1")
        self.assertEqual(release.version, "1.0.2")
        self.assertEqual(len(release.assets), 5)
        self.assertEqual([call[0] for call in client.calls], [u.RELEASES_URL])

    def test_numeric_order_beta_order_and_stable_promotion(self):
        tags = ["v1.0.10-beta.1", "v1.0.9", "v1.0.10-beta.9", "v1.0.10-beta.10", "v1.0.10"]
        releases = [fixture(tag)[0] for tag in tags]
        self.assertEqual(u.discover("v1.0.1-beta.1", transport=FixtureTransport(releases)).tag, "v1.0.10")
        self.assertEqual(self.discover(fixture("v1.0.1")[0]).tag, "v1.0.1")
        self.assertIsNone(self.discover(fixture("v1.0.1-beta.8")[0], current="v1.0.1"))
        self.assertEqual(self.discover(fixture("v1.0.1-beta.10")[0], current="v1.0.1-beta.9").tag, "v1.0.1-beta.10")

    def test_same_older_draft_and_other_channel_are_not_offered(self):
        for tag in ("v1.0.1-beta.1", "v1.0.0", "v0.9.9"):
            with self.subTest(tag=tag):
                self.assertIsNone(self.discover(fixture(tag)[0]))
        data, _ = fixture()
        data["draft"] = True
        self.assertIsNone(self.discover(data))
        for tag in ("v99.0.0-rc.1", "v99.0.0-alpha.1", "v99.0.0+build", "v01.0.0", "v9999999.0.0"):
            data, _ = fixture()
            data["tag_name"] = tag
            self.assertIsNone(self.discover(data))

    def test_invalid_current_tag_fails_without_network(self):
        client = FixtureTransport([])
        for tag in (None, "v1.0", "v1.0.0-beta.0", "v1.0.0-beta.01", "v1.0.0-rc.1", "x" * 100):
            with self.subTest(tag=tag), self.assertRaises(u.UpdateError):
                u.discover(tag, transport=client)
        self.assertEqual(client.calls, [])

    def test_repository_tag_and_channel_mismatch_fail(self):
        mutations = [
            lambda d: d.update(url=d["url"].replace(u.REPOSITORY, "attacker/LimitHalo")),
            lambda d: d.update(html_url=d["html_url"].replace(u.REPOSITORY, "attacker/LimitHalo")),
            lambda d: d.update(tag_name="1.0.2-beta.1"),
            lambda d: d.update(prerelease=False),
            lambda d: d.update(prerelease=1),
            lambda d: d.update(id=True),
            lambda d: d.update(draft="false"),
        ]
        for mutation in mutations:
            data, _ = fixture()
            mutation(data)
            with self.subTest(data=data), self.assertRaises(u.UpdateError):
                self.discover(data)

    def test_incomplete_duplicate_extra_and_bad_asset_identity_fail(self):
        mutations = [
            lambda a: a.pop(), lambda a: a.append(copy.deepcopy(a[0])),
            lambda a: a.__setitem__(1, copy.deepcopy(a[0])),
            lambda a: a[0].update(name="../evil.exe"),
            lambda a: a[0].update(name="LimitHalo-1.0.9-Setup.exe"),
            lambda a: a[0].update(url=a[0]["url"].replace(u.REPOSITORY, "attacker/LimitHalo")),
            lambda a: a[0].update(browser_download_url=a[0]["browser_download_url"].replace(u.REPOSITORY, "attacker/LimitHalo")),
            lambda a: a[0].update(digest=None), lambda a: a[0].update(digest="sha256:" + "z" * 64),
            lambda a: a[0].update(size=True), lambda a: a[0].update(size=0),
            lambda a: a[0].update(size=u.MAX_SETUP_BYTES + 1),
            lambda a: a[0].update(state="new"), lambda a: a[0].update(id=-1),
            lambda a: a[0].update(id=a[1]["id"]),
        ]
        for mutation in mutations:
            data, _ = fixture()
            mutation(data["assets"])
            with self.subTest(mutation=mutation), self.assertRaises(u.UpdateError):
                self.discover(data)

    def test_duplicate_tags_and_invalid_topology_fail_closed(self):
        data, _ = fixture()
        for items in ([data, data], [None], {"message": "rate limited"}, [data] * 101):
            with self.subTest(items=type(items)), self.assertRaises(u.UpdateError):
                u.discover("v1.0.1-beta.1", transport=FixtureTransport(items))

    def test_json_duplicate_keys_oversize_bad_utf8_and_nonfinite_fail(self):
        for raw in (b'{"draft":false,"draft":true}', b'[{"draft":NaN}]', b"\xff\xff", b"[" * 3000, b" " * (u.MAX_API_BYTES + 1)):
            client = mock.Mock()
            client.get.return_value = raw
            with self.subTest(size=len(raw)), self.assertRaises(u.UpdateError):
                u.discover("v1.0.1-beta.1", transport=client)


class Response:
    def __init__(self, data=b"[]", *, headers=None, status=200, url=u.RELEASES_URL):
        self.stream = io.BytesIO(data)
        self.headers = headers or {}
        self.status = status
        self.url = url

    def geturl(self):
        return self.url

    def read(self, size):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class NetworkTests(unittest.TestCase):
    def test_transport_is_finite_and_has_no_authorization_header(self):
        opener = mock.Mock()
        opener.open.return_value = Response(headers={"Content-Length": "2"})
        with mock.patch.object(u, "build_opener", return_value=opener):
            self.assertEqual(u.HttpsTransport().get(u.RELEASES_URL, max_bytes=100), b"[]")
        request = opener.open.call_args.args[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], u.NETWORK_TIMEOUT)
        self.assertEqual(opener.open.call_count, 1)

    def test_wrong_size_overflow_truncation_encoding_status_and_final_host_fail(self):
        responses = [Response(b"abc", headers={"Content-Length": "2"}),
                     Response(b"[]", headers={"Content-Length": "3"}),
                     Response(b"[]", headers={"Content-Length": "1001"}),
                     Response(b"x" * 1001), Response(headers={"Content-Length": "-1"}),
                     Response(headers={"Content-Encoding": "gzip"}), Response(status=206),
                     Response(url="https://attacker.example/asset")]
        for response in responses:
            opener = mock.Mock()
            opener.open.return_value = response
            with self.subTest(response=response), mock.patch.object(u, "build_opener", return_value=opener), self.assertRaises(u.UpdateError):
                u.HttpsTransport().get(u.RELEASES_URL, max_bytes=1000)
        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(u, "build_opener", return_value=opener), self.assertRaises(u.UpdateError):
            u.HttpsTransport().get(u.RELEASES_URL, max_bytes=1000, expected_size=4)

    def test_failure_and_expired_deadline_do_not_retry(self):
        opener = mock.Mock()
        opener.open.side_effect = OSError("offline")
        with mock.patch.object(u, "build_opener", return_value=opener), self.assertRaises(u.UpdateError):
            u.HttpsTransport().get(u.RELEASES_URL, max_bytes=100)
        self.assertEqual(opener.open.call_count, 1)
        with self.assertRaises(u.UpdateError):
            u.HttpsTransport().get(u.RELEASES_URL, max_bytes=100, deadline=0)

    def test_safe_redirect_hosts_and_unsafe_url_partitions(self):
        urls = [u.RELEASES_URL, "https://release-assets.githubusercontent.com/github-production-release-asset/1?a=b",
                "https://objects.githubusercontent.com/github-production-release-asset/1"]
        for url in urls:
            self.assertEqual(u._safe_url(url), url)
        bad = ["http://api.github.com/repos/" + u.REPOSITORY + "/releases",
               "https://api.github.com:443/repos/" + u.REPOSITORY + "/releases",
               "https://u:p@api.github.com/repos/" + u.REPOSITORY + "/releases",
               "https://github.com/attacker/LimitHalo/releases/download/v1/file",
               "https://github.com/DenisShumilov/LimitHalo/issues",
               "https://api.github.com/repos/attacker/LimitHalo/releases",
               "https://release-assets.githubusercontent.com.attacker.example/x",
               "https://release-assets.githubusercontent.com/x#fragment",
               "https://release-assets.githubusercontent.com/%2e%2e/other",
               "https://release-assets.githubusercontent.com/a\\b",
               "https://release-assets.githubusercontent.com/a\nb", "file:///C:/foo"]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(u.UpdateError):
                u._safe_url(url)

    def test_redirect_count_does_not_reset_when_url_changes(self):
        request = Request(u.RELEASES_URL)
        handler = u._SafeRedirects()
        for count in range(u.MAX_REDIRECTS):
            request = handler.redirect_request(request, None, 302, "Found", {},
                                               f"https://release-assets.githubusercontent.com/{count}")
        with self.assertRaises(u.UpdateError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://objects.githubusercontent.com/final")
        with self.assertRaises(u.UpdateError):
            handler.redirect_request(Request(u.RELEASES_URL), None, 302, "Found", {}, "https://attacker.example/x")


class StagingTests(unittest.TestCase):
    def setUp(self):
        # The runner must explicitly place all scratch state inside its run.
        self.scratch = tempfile.TemporaryDirectory(dir=os.environ["LIMIT_HALO_TEST_ROOT"])
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name)
        self.data, self.files = fixture()
        self.client = FixtureTransport([self.data], self.files)
        self.release = u.discover("v1.0.1-beta.1", transport=self.client)

    def stage(self):
        return u.stage_release(self.release, self.folder / "updates", transport=self.client)

    def replace_metadata(self, mutate):
        manifest = json.loads(self.files["release-manifest.json"])
        mutate(manifest)
        self.files["release-manifest.json"] = json.dumps(manifest).encode()
        self.files["SHA256SUMS.txt"] = "".join(f"{sha(data)}  {name}\n" for name, data in self.files.items()
                                                       if name != "SHA256SUMS.txt").encode()
        self.refresh_asset_digests()

    def refresh_asset_digests(self):
        self.release = replace(self.release, assets=tuple(replace(asset, size=len(self.files[asset.name]),
                                                                sha256=sha(self.files[asset.name]))
                                                         for asset in self.release.assets))

    def test_complete_inert_bundle_and_exact_consent_are_reverified(self):
        staged = self.stage()
        self.assertEqual(staged.directory.parent, self.folder / "updates")
        expected = ":".join(["a" * 64, sha(self.files["LimitHalo-1.0.2-Setup.exe"]), sha(self.files["LimitHalo-Agent.ps1"])])
        self.assertEqual(staged.acceptance, expected)
        self.assertEqual(u.verify_staged(staged), expected)
        self.assertEqual(set(path.name for path in staged.directory.iterdir()), set(self.files))
        self.assertNotEqual(self.stage().directory, staged.directory)

    def test_all_five_network_corruptions_fail_before_execution(self):
        for name in self.files:
            old = self.files[name]
            self.files[name] = b"X" * len(old)
            with self.subTest(name=name), self.assertRaises(u.UpdateError):
                self.stage()
            self.files[name] = old

    def test_all_five_post_stage_mutations_are_detected(self):
        for name in self.files:
            staged = self.stage()
            path = staged.directory / name
            data = path.read_bytes()
            path.write_bytes(b"X" * len(data))
            with self.subTest(name=name), self.assertRaises(u.UpdateError):
                u.verify_staged(staged)

    def test_manifest_identity_schema_hash_flags_signature_and_artifacts_fail(self):
        mutations = [lambda m: m.update(version="1.0.9"), lambda m: m.update(architecture="arm64"),
                     lambda m: m.update(internalProductId="OtherApp"), lambda m: m.update(candidateId="z" * 64),
                     lambda m: m.update(automaticUpdater=True), lambda m: m.update(publicationPerformed=True),
                     lambda m: m.update(automaticUpdater=0), lambda m: m.update(sourceIdentity="bad"),
                     lambda m: m.update(extra="unexpected"), lambda m: m["signature"].update(status="signed"),
                     lambda m: m["artifacts"].pop(),
                     lambda m: m["artifacts"].__setitem__(1, copy.deepcopy(m["artifacts"][0])),
                     lambda m: m["artifacts"][0].update(path="../evil.exe"),
                     lambda m: m["artifacts"][0].update(size=True),
                     lambda m: m["artifacts"][0].update(sha256="0" * 64)]
        for mutation in mutations:
            self.data, self.files = fixture()
            self.client = FixtureTransport([self.data], self.files)
            self.release = u.discover("v1.0.1-beta.1", transport=self.client)
            self.replace_metadata(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(u.UpdateError):
                self.stage()

    def test_checksum_mismatch_duplicate_missing_extra_and_nonascii_fail(self):
        original = self.files["SHA256SUMS.txt"]
        variants = [original.replace(original[:64], b"0" * 64, 1),
                    original + original.splitlines(keepends=True)[0],
                    b"\n".join(original.splitlines()[:3]) + b"\n",
                    original.replace(b"release-manifest.json", b"../../evil"), b"\xff\xff"]
        for content in variants:
            self.files["SHA256SUMS.txt"] = content
            self.refresh_asset_digests()
            with self.subTest(content=content[:10]), self.assertRaises(u.UpdateError):
                self.stage()

    def test_unexpected_file_wrong_root_or_changed_acceptance_are_rejected(self):
        staged = self.stage()
        for changed in (replace(staged, acceptance="0" * 194), replace(staged, update_root=self.folder)):
            with self.assertRaises(u.UpdateError):
                u.verify_staged(changed)
        (staged.directory / "unexpected.txt").write_bytes(b"inert")
        with self.assertRaises(u.UpdateError):
            u.verify_staged(staged)

    def test_relative_missing_parent_and_file_roots_are_rejected(self):
        file_root = self.folder / "plain-file"
        file_root.write_bytes(b"inert")
        for root in (Path("relative-update-folder"), self.folder / "missing" / "root", file_root):
            with self.subTest(root=root), self.assertRaises(u.UpdateError):
                u.stage_release(self.release, root, transport=self.client)

    def test_hardlinked_staged_file_is_rejected(self):
        staged = self.stage()
        os.link(staged.directory / "LimitHalo-Agent.ps1", self.folder / "hardlink.ps1")
        with self.assertRaises(u.UpdateError):
            u.verify_staged(staged)

    def test_reparse_component_is_rejected_without_following_it(self):
        real_stat = Path.lstat

        def reparse(path):
            actual = real_stat(path)
            if path == self.folder:
                fake = mock.Mock(wraps=actual)
                fake.st_mode = actual.st_mode
                fake.st_file_attributes = 0x400
                return fake
            return actual

        with mock.patch.object(Path, "lstat", reparse), self.assertRaises(u.UpdateError):
            self.stage()


if __name__ == "__main__":
    unittest.main()
