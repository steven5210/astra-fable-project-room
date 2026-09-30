import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import ao_model_qualification
import ao_project_room
import ao_release_check
import project_room
import project_room_mcp


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.read_size = None

    def read(self, size):
        self.read_size = size
        return self.body


class FakeHealthResponse(FakeResponse):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.args = None
        self.request_args = None
        self.closed = False
        self.request_count = 0

    def request(self, method, path, headers):
        self.request_count += 1
        self.request_args = (method, path, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "ao"
        self.root.mkdir()
        self.app = self.base / "Agent Orchestrator.app"
        self.executable = self.app / "Contents" / "Resources" / "daemon" / "ao"
        self.info = self.app / "Contents" / "Info.plist"
        self.process_start = int(time.time()) + 3600
        self.checked_time = self.process_start + 100
        self.ao_url = "http://127.0.0.1:1234"
        self.health = {
            "service": "agent-orchestrator-daemon",
            "status": "ok",
            "pid": 4242,
            "executablePath": str(self.executable),
        }
        self.latest = {
            "draft": False,
            "prerelease": False,
            "tag_name": "v0.13.2",
            "html_url": "https://github.com/Untrivial-ai/agent-orchestrator/releases/tag/v0.13.2",
            "published_at": "2023-11-14T00:00:00Z",
        }
        self.write_bundle("0.13.2", self.process_start - 10)
        self.fetch_latest = Mock(return_value=self.latest)
        self.fetch_health = Mock(return_value=self.health)
        self.run = Mock(return_value=self.process_result(self.process_start))
        # The claude_code and qualification_sources sections: hermetic defaults so every existing
        # test stays offline — no controller claude_bin, no qualification pointer, an empty home,
        # and a failing guard on the real fetch defaults in case a test forgets to inject.
        self.claude_home = self.base / "claude-home"
        self.claude_home.mkdir()
        self.claude_latest = {
            "draft": False, "prerelease": False, "tag_name": "v2.1.285",
            "html_url": "https://github.com/anthropics/claude-code/releases/tag/v2.1.285",
            "published_at": "2026-09-20T00:00:00Z",
        }
        self.fetch_claude_latest = Mock(return_value=self.claude_latest)
        self.fetch_source = Mock(return_value=b"")
        for name, guard in (("_fetch_claude_latest", AssertionError("tests must inject fetch_claude_latest")),
                            ("_fetch_source", AssertionError("tests must inject fetch_source"))):
            patcher = patch.object(ao_release_check, name, side_effect=guard)
            patcher.start()
            self.addCleanup(patcher.stop)
        which_patch = patch.object(ao_release_check.shutil, "which", return_value=None)
        which_patch.start()
        self.addCleanup(which_patch.stop)

    def process_result(self, start, returncode=0, stdout=None):
        output = time.strftime(ao_release_check.PS_TIME_FORMAT, time.localtime(start)) if stdout is None else stdout
        return subprocess.CompletedProcess(
            ["ps"], returncode, stdout=output.encode("ascii") if isinstance(output, str) else output, stderr=b""
        )

    def write_bundle(self, version, mtime):
        self.executable.parent.mkdir(parents=True, exist_ok=True)
        self.executable.write_bytes(b"synthetic AO executable")
        self.info.parent.mkdir(parents=True, exist_ok=True)
        with self.info.open("wb") as stream:
            plistlib.dump({"CFBundleShortVersionString": version}, stream)
        for path in (self.executable, self.info):
            os.utime(path, (mtime, mtime))

    def check(self, root=None, **overrides):
        arguments = {
            "fetch_latest": self.fetch_latest,
            "fetch_health": self.fetch_health,
            "run": self.run,
            "now": lambda: self.checked_time,
            "fetch_claude_latest": self.fetch_claude_latest,
            "fetch_source": self.fetch_source,
            "home": self.claude_home,
        }
        arguments.update(overrides)
        return ao_release_check.check(self.root if root is None else root, self.ao_url, **arguments)

    def write_qualification(self, evidence=b"Synthetic source capture naming claude-opus-5-5.\n",
                            extra_sources=()):
        """A digest-pinned synthetic family_qualification artifact plus private evidence files.

        extra_sources is a list of (source_id, uri, evidence_bytes) triples appended after the
        default "docs-src" descriptor."""
        source_ids = ["docs-src"]

        def descriptor(source_id, uri, evidence, evidence_name):
            evidence_file = (self.base / evidence_name).resolve()
            evidence_file.write_bytes(evidence)
            evidence_file.chmod(0o600)
            return {"id": source_id, "uri": uri, "captured_at": "2026-06-01T00:00:00Z",
                    "sha256": hashlib.sha256(evidence).hexdigest(),
                    "evidence_file": str(evidence_file)}

        sources = [descriptor("docs-src", "https://docs.invalid.example/claude",
                              evidence, "evidence.bin")]
        for index, (source_id, uri, extra_evidence) in enumerate(extra_sources):
            source_ids.append(source_id)
            sources.append(descriptor(source_id, uri, extra_evidence, f"evidence-{index}.bin"))
        artifact = {
            "format": "ao-model-qualification/v1", "revision": 1,
            "families": {
                "fable": {"expected_model": "claude-fable-5-1", "source_ids": source_ids},
                "opus": {"expected_model": "claude-opus-5-5", "source_ids": source_ids,
                         "minimum_claude_code_version": "2.1.280"},
                "sonnet": {"expected_model": "claude-sonnet-5-5", "source_ids": source_ids,
                           "minimum_claude_code_version": "2.1.197"},
            },
            "sources": sources,
        }
        artifact_path = (self.base / "qualification.json").resolve()
        artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True))
        artifact_path.chmod(0o600)
        (self.root / "config.json").write_text(json.dumps(
            {"family_qualification": {"path": str(artifact_path),
                                      "sha256": ao_model_qualification.digest(artifact)}}))
        return artifact

    def claude_layout(self, configured="2.1.268", installed="2.1.285"):
        """A controller claude_bin plus a native-installer versions entry; returns a run dispatcher."""
        self.claude_bin = self.base / "configured-claude"
        self.claude_bin.write_text("#!/bin/sh\nexit 0\n")
        self.claude_bin.chmod(0o700)
        (self.base / "config.json").write_text(json.dumps({"claude_bin": str(self.claude_bin)}))
        versions = {str(self.claude_bin): configured}
        if installed is not None:
            entry = self.claude_home / ".local" / "share" / "claude" / "versions" / installed
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text("#!/bin/sh\nexit 0\n")
            entry.chmod(0o700)
            versions[str(entry)] = installed

        def run(argv, **kwargs):
            if argv[0] == "ps":
                return self.process_result(self.process_start)
            if argv[1:] == ["--version"] and argv[0] in versions:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=versions[argv[0]] + " (Claude Code)\n", stderr="")
            raise AssertionError("unexpected run argv " + repr(argv))

        return run

    def test_matching_latest_and_bundle_is_up_to_date_and_saves_success(self):
        result = self.check()
        record = json.loads((self.root / "version-check.json").read_bytes())
        self.assertEqual(result["outcome"], "up_to_date")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["latest_version"], "v0.13.2")
        self.assertEqual(result["installed_version"], "0.13.2")
        self.assertEqual(result["running_version"], "0.13.2")
        self.assertEqual(record["schema"], "ao-release-check/v1")
        self.assertEqual(record["last_successful"], {key: value for key, value in record.items()
                                                      if key != "last_successful"})
        self.assertEqual(result["last_successful_checked_at"], result["checked_at"])
        self.run.assert_called_once_with(
            ["ps", "-o", "lstart=", "-p", "4242"],
            capture_output=True,
            timeout=5,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"},
        )

    def test_older_bundle_is_update_available(self):
        self.write_bundle("0.13.1", self.process_start - 10)
        result = self.check()
        self.assertEqual(result["outcome"], "update_available")
        self.assertEqual(result["installed_version"], "0.13.1")
        self.assertEqual(result["running_version"], "0.13.1")

    def test_bundle_changed_after_process_start_is_mismatch(self):
        os.utime(self.executable, (self.process_start + 2, self.process_start + 2))
        result = self.check()
        self.assertEqual(result["outcome"], "mismatch")
        self.assertIn("bundle_replaced_since_daemon_start", result["reasons"])
        self.assertIsNone(result["running_version"])

    def test_failed_latest_with_replaced_bundle_keeps_previous_last_successful(self):
        self.check()
        previous = json.loads((self.root / "version-check.json").read_bytes())["last_successful"]

        def fail_fetch():
            raise OSError("synthetic release fetch failure")

        os.utime(self.executable, (self.process_start + 2, self.process_start + 2))
        result = self.check(fetch_latest=fail_fetch)
        record = json.loads((self.root / "version-check.json").read_bytes())
        self.assertEqual(result["outcome"], "mismatch")
        self.assertIn("latest_fetch_failed", result["reasons"])
        self.assertIn("bundle_replaced_since_daemon_start", result["reasons"])
        self.assertIsNone(record["latest"])
        self.assertEqual(record["last_successful"], previous)

        fresh_root = self.base / "fresh"
        result = self.check(root=fresh_root, fetch_latest=fail_fetch)
        fresh_record = json.loads((fresh_root / "version-check.json").read_bytes())
        self.assertEqual(result["outcome"], "mismatch")
        self.assertIsNone(fresh_record["last_successful"])

    def test_bundle_ctime_after_process_start_is_mismatch_even_when_mtimes_are_old(self):
        timestamps = (
            self.process_start - 10,
            self.process_start + 2,
            self.process_start - 10,
            self.process_start - 10,
        )
        with patch.object(ao_release_check, "_bundle_timestamps", return_value=timestamps):
            result = self.check()
        self.assertEqual(result["outcome"], "mismatch")
        self.assertIn("bundle_replaced_since_daemon_start", result["reasons"])
        self.assertIsNone(result["running_version"])

    def test_development_bundle_version_is_unknown_without_running_version_probe(self):
        self.write_bundle("dev", self.process_start - 10)
        result = self.check()
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("bundle_version_dev", result["reasons"])
        self.assertIsNone(result["installed_version"])
        self.run.assert_not_called()

    def test_unreadable_bundle_metadata_is_unknown(self):
        self.info.unlink()
        result = self.check()
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("bundle_version_unreadable", result["reasons"])
        self.run.assert_not_called()

    def test_fetch_failure_and_invalid_latest_releases_preserve_last_success(self):
        self.check()
        previous = json.loads((self.root / "version-check.json").read_bytes())["last_successful"]

        def fail_fetch():
            raise OSError("synthetic fetch failure")

        cases = [
            ("fetch-failed", fail_fetch, "latest_fetch_failed"),
            ("prerelease", lambda: {**self.latest, "prerelease": True}, "latest_invalid"),
            ("bad-tag", lambda: {**self.latest, "tag_name": "nightly"}, "latest_invalid"),
        ]
        for name, fetch, reason in cases:
            with self.subTest(name=name):
                root = self.base / name
                root.mkdir()
                (root / "version-check.json").write_text(
                    json.dumps({"schema": ao_release_check.SCHEMA, "last_successful": previous}), encoding="utf-8"
                )
                result = self.check(root=root, fetch_latest=fetch)
                record = json.loads((root / "version-check.json").read_bytes())
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn(reason, result["reasons"])
                self.assertEqual(record["last_successful"], previous)

    def test_non_bundle_executable_is_unknown(self):
        paths = (
            "/usr/local/bin/ao",
            str(self.app / "Contents" / "Resources" / "daemon" / "other"),
        )
        for executable_path in paths:
            with self.subTest(executable_path=executable_path):
                self.run.reset_mock()
                health = {**self.health, "executablePath": executable_path}
                result = self.check(fetch_health=lambda _, value=health: value)
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn("executable_not_app_bundle", result["reasons"])
                self.run.assert_not_called()

    def test_invalid_and_unreachable_health_and_unconfigured_url(self):
        cases = [
            ("service", {**self.health, "service": "other"}, "daemon_identity_invalid"),
            ("bool-pid", {**self.health, "pid": True}, "daemon_identity_invalid"),
            ("relative-path", {**self.health, "executablePath": "Applications/AO.app/ao"},
             "daemon_identity_invalid"),
            ("fetch-error", RuntimeError("synthetic health failure"), "daemon_unreachable"),
        ]
        for name, health, reason in cases:
            with self.subTest(name=name):
                root = self.base / ("health-" + name)
                root.mkdir()

                def fetch_health(_):
                    if isinstance(health, Exception):
                        raise health
                    return health

                result = ao_release_check.check(
                    root, self.ao_url, fetch_latest=self.fetch_latest, fetch_health=fetch_health,
                    run=self.run, now=lambda: self.checked_time
                )
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn(reason, result["reasons"])
        self.run.reset_mock()
        fetch_health = Mock(side_effect=AssertionError("unconfigured URL must not fetch health"))
        run = Mock(side_effect=AssertionError("unconfigured URL must not inspect processes"))
        result = ao_release_check.check(
            self.base / "unconfigured", None, fetch_latest=self.fetch_latest, fetch_health=fetch_health,
            run=run, now=lambda: self.checked_time
        )
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("daemon_url_unconfigured", result["reasons"])
        fetch_health.assert_not_called()
        run.assert_not_called()

    def test_process_start_failure_and_bad_output_are_unknown(self):
        for run in (
            Mock(return_value=self.process_result(self.process_start, returncode=1)),
            Mock(return_value=self.process_result(self.process_start, stdout="not a ps timestamp")),
        ):
            with self.subTest(run=run):
                result = self.check(run=run)
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn("process_start_unavailable", result["reasons"])
                self.assertEqual(result["installed_version"], "0.13.2")
                self.assertIsNone(result["running_version"])

    def test_running_newer_than_latest_stable_is_unknown(self):
        self.write_bundle("0.14.0", self.process_start - 10)
        result = self.check()
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("running_newer_than_latest_stable", result["reasons"])
        self.assertEqual(result["running_version"], "0.14.0")

    def test_legacy_file_is_moved_once_without_overwriting_legacy_copy(self):
        path = self.root / "version-check.json"
        old_bytes = b'{"checked":"manual"}\n'
        path.write_bytes(old_bytes)
        self.check()
        legacy = self.root / "version-check.legacy.json"
        self.assertEqual(legacy.read_bytes(), old_bytes)
        self.assertEqual(json.loads(path.read_bytes())["schema"], ao_release_check.SCHEMA)

        sentinel = b"preserve existing legacy evidence\n"
        legacy.write_bytes(sentinel)
        hand_written = b"Astra hand-written release record"
        path.write_bytes(hand_written)
        self.check()
        self.assertEqual(legacy.read_bytes(), sentinel)
        self.assertEqual((self.root / "version-check.legacy-2.json").read_bytes(), hand_written)
        self.assertEqual(json.loads(path.read_bytes())["schema"], ao_release_check.SCHEMA)

        malformed_root = self.base / "malformed"
        malformed_root.mkdir()
        malformed = ("[" * 1100 + "0" + "]" * 1100).encode()
        (malformed_root / "version-check.json").write_bytes(malformed)
        self.check(root=malformed_root)
        self.assertEqual((malformed_root / "version-check.legacy.json").read_bytes(), malformed)

    def test_legacy_migration_replace_failure_does_not_abort_the_check(self):
        path = self.root / "version-check.json"
        old_bytes = b"invalid legacy record"
        path.write_bytes(old_bytes)
        with patch.object(ao_release_check.os, "link", side_effect=OSError("synthetic legacy migration failure")):
            with patch.object(ao_release_check.ao_project_room, "atomic") as atomic:
                result = self.check()
        self.assertEqual(result["outcome"], "up_to_date")
        self.assertIn("evidence_not_saved", result["reasons"])
        self.assertEqual(path.read_bytes(), old_bytes)
        self.assertFalse((self.root / "version-check.legacy.json").exists())
        atomic.assert_not_called()

    def test_legacy_migration_uses_unique_free_name_when_default_exists(self):
        path = self.root / "version-check.json"
        old_bytes = b"old-format release evidence"
        path.write_bytes(old_bytes)
        legacy = self.root / "version-check.legacy.json"
        occupied_bytes = b"existing legacy evidence"
        legacy.write_bytes(occupied_bytes)
        result = self.check()
        self.assertEqual(result["outcome"], "up_to_date")
        self.assertEqual(legacy.read_bytes(), occupied_bytes)
        self.assertEqual((self.root / "version-check.legacy-2.json").read_bytes(), old_bytes)
        self.assertEqual(json.loads(path.read_bytes())["schema"], ao_release_check.SCHEMA)

    def test_previous_record_is_read_while_version_check_lock_is_held(self):
        lock_path = self.root / ".version-check.lock"
        existing_record = ao_release_check._existing_record

        def assert_locked(path):
            fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            return existing_record(path)

        with patch.object(ao_release_check, "_existing_record", side_effect=assert_locked):
            result = self.check()
        self.assertEqual(result["outcome"], "up_to_date")
        self.assertTrue(lock_path.exists())

    def test_public_result_has_no_executable_path_pid_or_root(self):
        result = self.check()
        encoded = json.dumps(result)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(str(self.health["pid"]), encoded)
        self.assertNotIn("executable_path", encoded)
        self.assertNotIn('"pid"', encoded)
        self.assertEqual(
            set(result),
            {"outcome", "reasons", "latest_version", "installed_version", "running_version",
             "release_url", "checked_at", "last_successful_checked_at",
             "claude_code", "qualification_sources"},
        )

    def test_release_metadata_is_sanitized(self):
        raw = {
            **self.latest,
            "html_url": "https://example.com/releases/v0.13.2",
            "published_at": "x" * 65,
        }
        result = self.check(fetch_latest=lambda: raw)
        record = json.loads((self.root / "version-check.json").read_bytes())
        self.assertEqual(result["outcome"], "up_to_date")
        self.assertIsNone(result["release_url"])
        self.assertEqual(record["latest"]["tag_name"], "v0.13.2")
        self.assertIsNone(record["latest"]["html_url"])
        self.assertIsNone(record["latest"]["published_at"])

    def test_latest_transport_is_bounded_and_does_not_follow_redirects(self):
        body = json.dumps(self.latest).encode()
        response = FakeResponse(200, body)
        connection = FakeConnection(response)
        with patch.object(ao_release_check.http.client, "HTTPSConnection", return_value=connection) as https:
            result = ao_release_check._fetch_latest()
        self.assertEqual(result, self.latest)
        self.assertEqual(https.call_args.args[:2], ("api.github.com", 443))
        self.assertEqual(https.call_args.kwargs["timeout"], 10)
        self.assertIsNotNone(https.call_args.kwargs["context"])
        self.assertEqual(connection.request_args, (
            "GET",
            "/repos/Untrivial-ai/agent-orchestrator/releases/latest",
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "astra-fable-project-room",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ))
        self.assertEqual(response.read_size, ao_release_check.MAX_RESPONSE_BYTES + 1)
        self.assertTrue(connection.closed)

        too_large = FakeConnection(FakeResponse(200, b"x" * (ao_release_check.MAX_RESPONSE_BYTES + 1)))
        with patch.object(ao_release_check.http.client, "HTTPSConnection", return_value=too_large):
            with self.assertRaises(ao_release_check._FetchFailure) as raised:
                ao_release_check._fetch_latest()
        self.assertEqual(raised.exception.reason, "latest_fetch_failed")

        redirect = FakeConnection(FakeResponse(301, b""))
        with patch.object(ao_release_check.http.client, "HTTPSConnection", return_value=redirect):
            with self.assertRaises(ao_release_check._FetchFailure) as raised:
                ao_release_check._fetch_latest()
        self.assertEqual(raised.exception.reason, "latest_fetch_failed")
        self.assertEqual(redirect.request_count, 1)

    def test_rate_limited_transport_has_the_specific_reason(self):
        for status in (403, 429):
            with self.subTest(status=status):
                connection = FakeConnection(FakeResponse(status, b""))
                with patch.object(ao_release_check.http.client, "HTTPSConnection", return_value=connection):
                    result = self.check(fetch_latest=None)
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn("latest_rate_limited", result["reasons"])

    def test_real_health_fetch_refuses_non_loopback_before_building_an_opener(self):
        with patch.object(ao_project_room.urllib.request, "build_opener") as build_opener:
            result = ao_release_check.check(
                self.root, "http://example.com:1234",
                fetch_latest=self.fetch_latest, run=self.run, now=lambda: self.checked_time,
                fetch_claude_latest=self.fetch_claude_latest, fetch_source=self.fetch_source,
                home=self.claude_home,
            )
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("daemon_url_invalid", result["reasons"])
        build_opener.assert_not_called()

    def test_configured_below_family_floor_reports_below_floor_with_action(self):
        run = self.claude_layout(configured="2.1.268", installed="2.1.285")
        self.write_qualification()
        result = self.check(run=run)
        section = result["claude_code"]
        self.assertEqual(section["configured"]["version"], "2.1.268")
        self.assertEqual(section["highest_family_floor"], "2.1.280")
        self.assertIs(section["floor_satisfied"], False)
        self.assertEqual(section["newest_installed"]["version"], "2.1.285")
        self.assertEqual(section["latest_published"]["version"], "2.1.285")
        self.assertEqual(section["outcome"], "below_floor")
        expected_path = str(self.claude_home / ".local" / "share" / "claude" / "versions" / "2.1.285")
        self.assertEqual(section["action"], "python3 project_room.py setup --claude-bin " + expected_path)

    def test_configured_installed_and_published_equal_is_up_to_date(self):
        run = self.claude_layout(configured="2.1.285", installed="2.1.285")
        result = self.check(run=run)
        section = result["claude_code"]
        self.assertEqual(section["outcome"], "up_to_date")
        self.assertIsNone(section["action"])
        self.assertIs(section["floor_satisfied"], True)
        self.assertEqual(result["outcome"], "up_to_date")

    def test_newer_installed_than_configured_is_update_available(self):
        run = self.claude_layout(configured="2.1.280", installed="2.1.285")
        result = self.check(run=run)
        section = result["claude_code"]
        self.assertEqual(section["outcome"], "update_available")
        self.assertTrue(section["action"].endswith(
            str(Path(".local") / "share" / "claude" / "versions" / "2.1.285")))

    def test_claude_latest_fetch_failure_reports_reason_and_never_up_to_date(self):
        run = self.claude_layout(configured="2.1.285", installed="2.1.285")
        result = self.check(run=run,
                            fetch_claude_latest=Mock(side_effect=OSError("synthetic fetch failure")))
        section = result["claude_code"]
        self.assertIn("claude_latest_fetch_failed", section["reasons"])
        self.assertIsNone(section["latest_published"])
        self.assertNotEqual(section["outcome"], "up_to_date")

    def test_missing_claude_bin_key_is_unconfigured_and_unknown(self):
        (self.base / "config.json").write_text("{}")
        result = self.check()
        section = result["claude_code"]
        self.assertEqual(section["configured"]["reasons"], ["claude_bin_unconfigured"])
        self.assertIsNone(section["configured"]["version"])
        self.assertEqual(section["outcome"], "unknown")

    def test_qualification_source_unchanged_and_identifiers_differ(self):
        evidence = b"Synthetic source capture naming claude-opus-5-5.\n"
        self.write_qualification(evidence=evidence)
        result = self.check(fetch_source=Mock(return_value=evidence))
        section = result["qualification_sources"]
        self.assertTrue(section["artifact_present"])
        self.assertEqual(section["revision"], 1)
        self.assertEqual(section["outcome"], "unchanged")
        self.assertEqual(section["sources"],
                         [{"id": "docs-src", "outcome": "unchanged", "bytes_match": True,
                           "identifiers_not_in_capture": {}, "identifiers_only_in_capture": {}}])

        drifted = b"Synthetic source now announcing claude-opus-6 beside claude-opus-5-5.\n"
        result = self.check(fetch_source=Mock(return_value=drifted))
        section = result["qualification_sources"]
        self.assertEqual(section["outcome"], "identifiers_differ")
        self.assertEqual(section["sources"][0]["outcome"], "identifiers_differ")
        self.assertFalse(section["sources"][0]["bytes_match"])
        self.assertEqual(section["sources"][0]["identifiers_not_in_capture"],
                         {"opus": ["claude-opus-6"]})
        self.assertEqual(section["sources"][0]["identifiers_only_in_capture"], {})

        result = self.check(fetch_source=Mock(side_effect=OSError("synthetic source failure")))
        section = result["qualification_sources"]
        self.assertEqual(section["sources"][0]["outcome"], "unreachable")
        self.assertIsNone(section["sources"][0]["bytes_match"])
        self.assertEqual(section["sources"][0]["identifiers_not_in_capture"], {})
        self.assertEqual(section["outcome"], "unknown")

    def test_qualification_source_bytes_differ_without_identifier_drift(self):
        evidence = b"Synthetic source capture naming claude-opus-5-5.\n"
        self.write_qualification(evidence=evidence)
        result = self.check(fetch_source=Mock(return_value=evidence + b"\n"))
        section = result["qualification_sources"]
        self.assertEqual(section["outcome"], "bytes_differ")
        self.assertEqual(section["sources"][0]["outcome"], "bytes_differ")
        self.assertFalse(section["sources"][0]["bytes_match"])
        self.assertEqual(section["sources"][0]["identifiers_not_in_capture"], {})
        self.assertEqual(section["sources"][0]["identifiers_only_in_capture"], {})

    def test_qualification_source_identifier_dropped_from_fresh_bytes(self):
        evidence = b"Synthetic source capture naming claude-opus-5-5 and claude-opus-9.\n"
        self.write_qualification(evidence=evidence)
        fresh = b"Synthetic source now naming only claude-opus-5-5.\n"
        result = self.check(fetch_source=Mock(return_value=fresh))
        section = result["qualification_sources"]
        self.assertEqual(section["outcome"], "identifiers_differ")
        self.assertEqual(section["sources"][0]["identifiers_not_in_capture"], {})
        self.assertEqual(section["sources"][0]["identifiers_only_in_capture"],
                         {"opus": ["claude-opus-9"]})

    def test_qualification_sources_outcome_precedence(self):
        captured = b"Captured page naming claude-sonnet-5-5.\n"
        self.write_qualification(
            evidence=captured,
            extra_sources=[("second-src", "https://docs.invalid.example/second",
                            b"Second capture.\n")])
        # One bytes-only drift plus one unreachable source -> bytes_differ overall.
        def fetch(uri):
            if uri == "https://docs.invalid.example/claude":
                return captured + b"trailing whitespace\n"
            raise OSError("synthetic source failure")

        section = self.check(fetch_source=fetch)["qualification_sources"]
        self.assertEqual(section["sources"][0]["outcome"], "bytes_differ")
        self.assertEqual(section["sources"][1]["outcome"], "unreachable")
        self.assertEqual(section["outcome"], "bytes_differ")
        self.assertIn("source_fetch_failed", section["reasons"])

        # A single unreachable source with no differing source -> unknown.
        section = self.check(
            fetch_source=Mock(side_effect=OSError("synthetic source failure"))
        )["qualification_sources"]
        self.assertEqual(section["sources"][0]["outcome"], "unreachable")
        self.assertEqual(section["sources"][1]["outcome"], "unreachable")
        self.assertEqual(section["outcome"], "unknown")

    def test_no_qualification_pointer_reports_no_qualification(self):
        result = self.check()
        self.assertEqual(result["qualification_sources"],
                         {"artifact_present": False, "revision": None, "outcome": "no_qualification",
                          "reasons": [], "sources": []})
        self.fetch_source.assert_not_called()

    def test_saved_record_carries_both_sections_and_old_records_still_read(self):
        run = self.claude_layout()
        self.write_qualification()
        self.check(run=run)
        record = json.loads((self.root / "version-check.json").read_bytes())
        self.assertIn("claude_code", record)
        self.assertIn("qualification_sources", record)
        self.assertEqual(record["claude_code"]["outcome"], "below_floor")
        self.assertEqual(record["last_successful"]["claude_code"], record["claude_code"])

        old_shape = self.base / "old-shape"
        old_shape.mkdir()
        previous = {"schema": ao_release_check.SCHEMA, "outcome": "up_to_date",
                    "checked_at": "2026-01-01T00:00:00Z", "reasons": [],
                    "latest": None, "installed_version": "0.13.2", "running_version": "0.13.2",
                    "daemon": None, "last_successful": None}
        (old_shape / "version-check.json").write_text(json.dumps(previous))
        self.assertEqual(ao_release_check._existing_record(old_shape / "version-check.json"), previous)

    def test_health_fetch_uses_healthz_and_reads_only_one_megabyte_plus_one_byte(self):
        body = json.dumps(self.health).encode()
        response = FakeHealthResponse(200, body)
        opener = Mock()
        opener.open.return_value = response
        client = Mock(base=self.ao_url, opener=opener)
        with patch.object(ao_project_room, "Client", return_value=client):
            result = ao_release_check._fetch_health(self.ao_url)
        self.assertEqual(result, self.health)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.ao_url + "/healthz")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        self.assertEqual(response.read_size, ao_release_check.MAX_RESPONSE_BYTES + 1)

        response = FakeHealthResponse(200, b"x" * (ao_release_check.MAX_RESPONSE_BYTES + 1))
        opener.open.return_value = response
        with patch.object(ao_project_room, "Client", return_value=client):
            with self.assertRaises(ValueError):
                ao_release_check._fetch_health(self.ao_url)

    def test_service_mcp_dispatch_schema_url_resolution_and_lock_independence(self):
        home = self.base / "controller"
        service = project_room.Service(home)
        listing = project_room_mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service
        )
        tool = next(entry for entry in listing["result"]["tools"] if entry["name"] == "ao_release_check")
        self.assertEqual(tool["inputSchema"], project_room.TOOL_SCHEMAS["ao_release_check"][1])
        self.assertEqual(tool["inputSchema"], {
            "type": "object",
            "properties": {"ao_url": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        })
        self.assertTrue(tool["annotations"]["openWorldHint"])
        self.assertFalse(tool["annotations"]["readOnlyHint"])

        arguments = {"ao_url": self.ao_url}
        synthetic = {"outcome": "unknown", "reasons": ["daemon_url_unconfigured"]}
        with patch("ao_release_check.check", return_value=synthetic) as check:
            response = project_room_mcp.handle(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "ao_release_check", "arguments": arguments}},
                service,
            )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"], synthetic)
        check.assert_called_once_with(home.resolve() / "ao", self.ao_url)

        adapter = ao_project_room.Service(home)
        config_path = adapter.root / "config.json"
        config_path.write_text(json.dumps({"ao_url": "http://127.0.0.1:5678"}), encoding="utf-8")
        with patch("ao_release_check.check", return_value=synthetic) as check:
            adapter.ao_release_check(self.ao_url)
            check.assert_called_with(adapter.root, self.ao_url)
            with patch.dict(os.environ, {"PROJECT_ROOM_AO_URL": "http://127.0.0.1:8765"}):
                adapter.ao_release_check()
            check.assert_called_with(adapter.root, "http://127.0.0.1:8765")
            with patch.dict(os.environ, {"PROJECT_ROOM_AO_URL": ""}):
                adapter.ao_release_check()
            check.assert_called_with(adapter.root, "http://127.0.0.1:5678")

        completed = threading.Event()
        result = {}
        errors = []

        def call_while_locked():
            try:
                result["value"] = adapter.ao_release_check(self.ao_url)
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        with patch("ao_release_check.check", return_value=synthetic):
            with (adapter.root / ".lock").open("a+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                worker = threading.Thread(target=call_while_locked)
                worker.start()
                completed_during_lock = completed.wait(2)
                fcntl.flock(stream, fcntl.LOCK_UN)
            worker.join(2)
        self.assertTrue(completed_during_lock)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["value"], synthetic)


if __name__ == "__main__":
    unittest.main()
