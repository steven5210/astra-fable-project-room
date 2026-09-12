"""Synthetic subprocess and evidence-boundary tests; no account or network use."""

import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import ao_mcp_attachment as attachment


FAKE = r'''
import json, os, subprocess, sys
TOOLS = ["deepseek_ask", "deepseek_cancel", "deepseek_health", "deepseek_result", "deepseek_status", "deepseek_submit"]
MODE = __MODE__
for line in sys.stdin.buffer:
    value = json.loads(line)
    method = value.get("method")
    if method == "probe":
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        continue
    if method == "exit":
        break
    if method == "notifications/initialized":
        continue
    if method == "background":
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        result = {"worker_pid":worker.pid}
    elif method == "initialize":
        result = {"protocolVersion":"2025-03-26", "capabilities":{"tools":{}}, "serverInfo":{"name":"fake", "version":"1"}}
    elif method == "tools/list":
        names = TOOLS[:-1] if MODE == "missing_tools" else TOOLS
        if MODE == "duplicate_tool": names = TOOLS + [TOOLS[0]]
        result = {"tools":[{"name":name} for name in names]}
        if MODE == "large_tools": result["padding"] = "x" * 1_000_000
    else:
        result = {}
    out = {"jsonrpc":"2.0", "id": value.get("id"), "result":result}
    if MODE == "wrong_id": out["id"] = "unrelated"
    if MODE == "error_initialize" and method == "initialize" or MODE == "error_tools" and method == "tools/list":
        out.pop("result")
        out["error"] = {"code":-32000, "message":"ordinary synthetic error"}
    sys.stdout.buffer.write(json.dumps(out, separators=(", ", " : ")).encode() + b"\r\n")
    sys.stdout.buffer.flush()
'''


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.room = self.base / "room"
        self.room.mkdir(mode=0o700)
        self.worktree = self.base / "workspace"
        self.worktree.mkdir()
        self.home = self.base / "controller"
        self.home.mkdir(mode=0o700)
        (self.home / "deepseek").mkdir(mode=0o700)
        self.ledger = self.home / "deepseek" / "ledger.sqlite3"
        with sqlite3.connect(self.ledger) as db:
            db.executescript("CREATE TABLE jobs(id TEXT PRIMARY KEY,room_id TEXT,state TEXT); CREATE TABLE resolutions(job_id TEXT,room_id TEXT);")
        self.ledger.chmod(0o600)
        self.receipts = self.room / "attachment-receipts"
        self.receipts.mkdir(mode=0o700)
        self.wrapper = self.room / "wrapper.py"
        self.wrapper.write_bytes(Path(attachment.__file__).read_bytes())
        self.wrapper.chmod(0o600)
        self.child = self.room / "fake_server.py"
        self.key = self.base / "synthetic-key.txt"
        self.key.write_text("SYNTHETIC-SECRET-NEVER-READ")
        self.config = self.room / "deepseek.json"
        self.write(self.config, {"key_file": str(self.key), "backend": "official", "model": "deepseek-flash"})
        self.profile = self.room / "implementation-mcp.json"
        self.child_server = {"command": sys.executable, "args": [str(self.child), "--home", str(self.home), "--room", "ao-synthetic", "--config", str(self.config)],
                             "env": {"PROJECT_ROOM_WORKTREE": str(self.worktree)}}
        self.write(self.profile, {"mcpServers": {"deepseek": self.child_server}})
        self.prep_path = self.room / "preparation-v2.json"
        self.manifest_path = self.room / "manifest.json"
        self.processes = []
        self.configure()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, sort_keys=True))
        path.chmod(0o600)

    def configure(self, mode="normal"):
        self.child.write_text(FAKE.replace("__MODE__", repr(mode)))
        self.child.chmod(0o600)
        files = {str(p): attachment.digest(p.read_bytes()) for p in (self.config, self.profile, self.child)}
        self.state = {"room_id": "ao-synthetic", "workflow": "fable_engineering", "preparation": self.prep_path.name,
                      "preparation_status": "configured", "bindings": {"engineer": {"session_id": "session-synthetic"}},
                      "delegate": {"provider": "deepseek", "files": {str(self.profile): files[str(self.profile)]},
                                   "inventory": {"files": {str(p): files[str(p)] for p in (self.config, self.child)}}}}
        self.manifest = {"version": 1, "room_directory": str(self.room), "room_id": self.state["room_id"],
                         "session_id": "session-synthetic", "worktree": str(self.worktree), "attachment_id": "attachment-synthetic",
                         "provider_files": files, "profile_path": str(self.profile), "child_server": self.child_server,
                         "wrapper_path": str(self.wrapper), "wrapper_sha256": attachment.digest(self.wrapper.read_bytes()),
                         "expected_tools": list(attachment.TOOLS), "receipt_directory": str(self.receipts)}
        self.prepared = {"room_id": self.state["room_id"], "worktree": str(self.worktree), "provider": "deepseek",
                         "delegate_sha256": attachment.digest(self.state["delegate"]),
                         "server": {"command": sys.executable, "args": [str(self.wrapper), "--manifest", str(self.manifest_path)],
                                    "env": {"PROJECT_ROOM_WORKTREE": str(self.worktree)}},
                         "mcp_attachment": {"version": 1, "attachment_id": "attachment-synthetic",
                                            "manifest_path": str(self.manifest_path), "manifest_sha256": attachment.digest(self.manifest)}}
        self.save()

    def save(self):
        self.write(self.manifest_path, self.manifest)
        self.prepared["mcp_attachment"]["manifest_sha256"] = attachment.digest(self.manifest)
        self.write(self.prep_path, self.prepared)
        self.state["preparation_sha256"] = attachment.digest(self.prepared)
        self.write(self.room / "state.json", self.state)

    def start(self, session="session-synthetic", cwd=None):
        proc = subprocess.Popen([sys.executable, "-B", str(self.wrapper), "--manifest", str(self.manifest_path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=cwd or self.worktree,
                                env={**os.environ, "AO_SESSION_ID": session, "PROJECT_ROOM_WORKTREE": str(self.worktree)}, bufsize=0)
        self.processes.append(proc)
        return proc

    @staticmethod
    def send(proc, data):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode() + b"\n"
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[proc.stdin.write(remaining):]
        proc.stdin.flush()

    def receive(self, proc):
        result = bytearray()
        deadline = time.monotonic() + 5
        while not result.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "fake transport response deadline")
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            self.assertTrue(ready, "fake response missing")
            data = os.read(proc.stdout.fileno(), 65536)
            self.assertTrue(data, "fake transport closed")
            result.extend(data)
        return bytes(result)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.01)
        self.fail("synthetic evidence did not reach expected state")

    def locator(self):
        path = self.receipts / "connection.lock"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_bytes())
        except ValueError:
            return None

    def ready_receipt(self):
        locator = self.locator()
        if not locator:
            return None
        path = self.receipts / (locator["connection_id"] + ".json")
        return json.loads(path.read_bytes()) if path.exists() else None

    def invalid_receipt(self):
        locator = self.locator()
        return locator and (self.receipts / (locator["connection_id"] + ".invalid.json")).exists()

    def initialize(self, proc, identifier=1):
        self.send(proc, {"jsonrpc": "2.0", "id": identifier, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        return self.receive(proc)

    def enumerate(self, proc, identifier=2, initialized=True):
        if initialized:
            self.send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send(proc, {"jsonrpc": "2.0", "id": identifier, "method": "tools/list", "params": {}})
        return self.receive(proc)

    def handshake(self, proc):
        self.initialize(proc)
        self.enumerate(proc)
        self.wait_for(self.ready_receipt)
        return attachment.validate_attachment(self.room, self.state, self.prepared)

    def stop(self, proc):
        if proc.poll() is None:
            proc.stdin.close()
            proc.wait(timeout=5)

    def tearDown(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if not stream.closed:
                    stream.close()
        self.temp.cleanup()

    def test_exact_handshake_required_then_current_lease_validates_without_mutation(self):
        proc = self.start()
        self.wait_for(self.locator)
        with self.assertRaises(ValueError):
            attachment.validate_attachment(self.room, self.state, self.prepared)
        self.initialize(proc)
        self.assertIsNone(self.ready_receipt())
        self.enumerate(proc, initialized=False)
        self.assertIsNone(self.ready_receipt())
        self.send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        receipt = self.wait_for(self.ready_receipt)
        before = {p.name: p.read_bytes() for p in self.receipts.iterdir() if p.is_file()}
        self.assertEqual(attachment.validate_attachment(self.room, self.state, self.prepared), receipt)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.receipts.iterdir() if p.is_file()})
        self.assertEqual(receipt["basis"]["preparation_sha256"], self.state["preparation_sha256"])
        self.assertEqual(receipt["basis"]["provider_files"], self.manifest["provider_files"])
        self.assertIn("no inference proof", receipt["meaning"])

    def test_wire_bytes_newlines_unicode_and_large_tool_payload_are_unchanged(self):
        proc = self.start()
        receipt = self.handshake(proc)
        # The response is larger than pipe capacity; full duplex pumps must not
        # serialize input against output or deadlock on normal backpressure.
        payload = {"jsonrpc": "2.0", "id": "large", "method": "probe", "params": {"text": "line1\nline2 \u2603 " + "x" * 5_000_000}}
        wire = b"  " + json.dumps(payload, ensure_ascii=False, separators=(", ", " : ")).encode() + b"  \r\n"
        import threading
        writer = threading.Thread(target=self.send, args=(proc, wire), daemon=True)
        writer.start()
        self.assertEqual(self.receive(proc), wire)
        writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(attachment.validate_attachment(self.room, self.state, self.prepared), receipt)
        for path in self.receipts.iterdir():
            self.assertNotIn(b"line1", path.read_bytes())

    def test_initialized_connection_survives_state_growth_above_legacy_launch_bound(self):
        state_path = self.room / "state.json"
        self.assertLess(state_path.stat().st_size, 4_000_000)
        proc = self.start()
        receipt = self.handshake(proc)
        # Ordinary saved request history can grow while the original connection
        # stays open. Do not rerun or weaken the retained launcher's read gate.
        self.state["requests"] = {"synthetic-completed": {
            "state": "completed", "text": "x" * 4_100_000}}
        self.write(state_path, self.state)
        self.assertGreater(state_path.stat().st_size, 4_000_000)
        self.assertLess(state_path.stat().st_size, attachment.MAX_EVIDENCE)
        original = state_path.read_bytes()
        evidence = {p.name: p.read_bytes() for p in self.receipts.iterdir() if p.is_file()}
        self.assertEqual(attachment.validate_attachment(self.room, self.state, self.prepared), receipt)
        self.assertEqual(state_path.read_bytes(), original)
        self.assertEqual(evidence, {p.name: p.read_bytes() for p in self.receipts.iterdir() if p.is_file()})
        wire = b'{"jsonrpc":"2.0","id":"after-growth","method":"probe"}\n'
        self.send(proc, wire)
        self.assertEqual(self.receive(proc), wire)

    def test_evidence_bound_remains_finite_and_manifest_keeps_its_small_limit(self):
        self.assertEqual(attachment.MAX_EVIDENCE, 96_000_000)
        self.assertEqual(attachment.MAX_MANIFEST, 256_000)
        oversized = self.room / "oversized-evidence.json"
        with oversized.open("wb") as stream:
            stream.truncate(attachment.MAX_EVIDENCE + 1)
        oversized.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "oversized"):
            attachment._load(oversized)
        # Valid JSON with excess insignificant whitespace must still be refused
        # by the manifest's own bound, not admitted by the larger evidence cap.
        with self.manifest_path.open("ab") as stream:
            stream.write(b" " * attachment.MAX_MANIFEST)
        with self.assertRaisesRegex(ValueError, "oversized"):
            attachment._validate_manifest(self.manifest_path)

    def test_tools_response_must_finish_writing_to_client_before_ready(self):
        self.configure("large_tools")
        proc = self.start()
        self.initialize(proc)
        self.send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertTrue(select.select([proc.stdout], [], [], 3)[0])
        # The million-byte response exceeds the OS pipe capacity. Without a
        # reader it cannot have reached client stdout in full.
        self.assertIsNone(self.ready_receipt())
        with self.assertRaises(ValueError):
            attachment.validate_attachment(self.room, self.state, self.prepared)
        self.assertGreater(len(self.receive(proc)), 1_000_000)
        self.wait_for(self.ready_receipt)
        attachment.validate_attachment(self.room, self.state, self.prepared)

    def test_error_or_inexact_tools_never_certify_readiness_and_errors_are_relayed(self):
        for mode in ("error_initialize", "error_tools", "missing_tools", "duplicate_tool", "wrong_id"):
            with self.subTest(mode=mode):
                self.configure(mode)
                proc = self.start()
                initial = self.initialize(proc)
                tools = self.enumerate(proc)
                if mode.startswith("error"):
                    self.assertIn(b"ordinary synthetic error", initial + tools)
                self.assertIsNone(self.ready_receipt())
                with self.assertRaises(ValueError):
                    attachment.validate_attachment(self.room, self.state, self.prepared)
                self.stop(proc)

    def test_duplicate_initialize_disqualifies_even_after_ready_without_rewriting_it(self):
        proc = self.start()
        receipt = self.handshake(proc)
        path = self.receipts / (receipt["connection"]["connection_id"] + ".json")
        original = path.read_bytes()
        self.initialize(proc, identifier="again")
        self.wait_for(self.invalid_receipt)
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaises(ValueError):
            attachment.validate_attachment(self.room, self.state, self.prepared)

    def test_reused_request_id_after_initialize_is_not_a_second_initialize(self):
        proc = self.start()
        self.initialize(proc, identifier="same")
        self.enumerate(proc, identifier="same")
        self.wait_for(self.ready_receipt)
        attachment.validate_attachment(self.room, self.state, self.prepared)
        self.enumerate(proc, identifier="same", initialized=False)
        attachment.validate_attachment(self.room, self.state, self.prepared)

    def test_native_session_and_cwd_mismatches_refuse_before_child_launch(self):
        for kwargs in ({"session": "different"}, {"cwd": self.base}):
            with self.subTest(kwargs=kwargs):
                proc = self.start(**kwargs)
                self.assertEqual(proc.wait(timeout=5), 1)
                self.assertFalse((self.receipts / "connection.lock").exists())
                self.assertNotIn(b"SYNTHETIC", proc.stderr.read())

    def test_provenance_tamper_and_unpinned_files_refuse_before_spawn(self):
        mutations = (
            lambda: self.manifest.update(session_id="wrong"),
            lambda: self.manifest.update(child_server={**self.child_server, "args": ["-c", "raise SystemExit(99)"]}),
            lambda: self.manifest["provider_files"].pop(str(self.config)),
            lambda: self.prepared["server"].update(args=[str(self.wrapper), "--manifest", str(self.room / "different.json")]),
            lambda: self.manifest.update(expected_tools=list(attachment.TOOLS[:-1])),
            lambda: self.manifest.update(receipt_directory=str(self.base)),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.configure()
                mutate()
                self.save()
                proc = self.start()
                self.assertEqual(proc.wait(timeout=5), 1)
                self.assertFalse((self.receipts / "connection.lock").exists())

    def test_manifest_file_digest_and_saved_preparation_tamper_refuse(self):
        self.manifest["attachment_id"] = "changed"
        self.write(self.manifest_path, self.manifest)
        proc = self.start()
        self.assertEqual(proc.wait(timeout=5), 1)
        self.configure()
        self.child.write_text(self.child.read_text() + "\n# changed\n")
        proc = self.start()
        self.assertEqual(proc.wait(timeout=5), 1)

    def test_protocol_metadata_duplicate_keys_and_request_id_collision_fail_closed(self):
        initial = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        for invalid in (
            b'{"jsonrpc":"2.0", "id":1, "id":2, "method":"initialize"}',
            b'{"jsonrpc":"2.0", "id":NaN, "method":"initialize"}',
            json.dumps({**initial, "id": True}).encode(),
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        ):
            with self.subTest(invalid=invalid):
                ready, bad = [], []
                witness = attachment.Witness(lambda: ready.append(True), lambda: bad.append(True))
                if b'"tools/list"' in invalid:
                    witness.observe(json.dumps(initial), True)
                witness.observe(invalid, True)
                self.assertEqual(bad, [True])
                self.assertEqual(ready, [])
        ready, bad = [], []
        witness = attachment.Witness(lambda: ready.append(True), lambda: bad.append(True))
        witness.observe(json.dumps(initial), True)
        witness.observe(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": "invented", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}}), False)
        self.assertEqual(bad, [True])
        self.assertEqual(ready, [])

    def test_replacing_locked_file_cannot_reuse_an_old_readiness_receipt(self):
        proc = self.start()
        self.handshake(proc)
        lease_path = self.receipts / "connection.lock"
        original = lease_path.read_bytes()
        lease_path.rename(self.receipts / "old.lock")
        lease_path.write_bytes(original)
        lease_path.chmod(0o600)
        fd = attachment._lease_open(self.receipts)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                attachment.validate_attachment(self.room, self.state, self.prepared)
        finally:
            os.close(fd)

    def test_validation_reads_no_key_and_constructs_no_adapter(self):
        original = attachment._read
        seen = []
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(Path(path), self.key)
            seen.append(Path(path))
            return original(path, *args, **kwargs)
        with mock.patch.object(attachment, "_read", side_effect=guarded):
            attachment._validate_manifest(self.manifest_path)
        self.assertIn(self.config, seen)
        self.assertNotIn(self.key, seen)
        self.assertNotIn("deepseek_adapter", attachment.__dict__)

    def test_missing_or_incomplete_ledger_refuses_without_recreation_or_schema_repair(self):
        saved = self.ledger.read_bytes()
        self.ledger.unlink()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=5), 1)
        self.assertFalse(self.ledger.exists())
        self.assertFalse((self.receipts / "connection.lock").exists())
        self.ledger.write_bytes(saved)
        self.ledger.chmod(0o600)
        with sqlite3.connect(self.ledger) as db:
            db.execute("DROP TABLE resolutions")
        before = self.ledger.read_bytes()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=5), 1)
        self.assertEqual(before, self.ledger.read_bytes())

    def test_active_unknown_abandoned_and_unrecognized_rows_refuse_before_child(self):
        for outcome in ("queued", "submitting", "streaming", "unknown_delivery", "failed_after_send", "abandoned_local_deadline", "abandoned_cancelled", "invented"):
            with self.subTest(outcome=outcome):
                with sqlite3.connect(self.ledger) as db:
                    db.execute("DELETE FROM jobs")
                    db.execute("INSERT INTO jobs VALUES(?,?,?)", ("a" * 32, "ao-synthetic", outcome))
                before = self.ledger.read_bytes()
                proc = self.start()
                self.assertEqual(proc.wait(timeout=5), 1)
                self.assertEqual(before, self.ledger.read_bytes())
                self.assertFalse((self.receipts / "connection.lock").exists())

    def test_retained_user_resolution_is_observed_and_other_rooms_do_not_block(self):
        with sqlite3.connect(self.ledger) as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?)", ("a" * 32, "ao-synthetic", "unknown_delivery"))
            db.execute("INSERT INTO resolutions VALUES(?,?)", ("a" * 32, "ao-synthetic"))
            db.execute("INSERT INTO jobs VALUES(?,?,?)", ("b" * 32, "different-room", "streaming"))
            db.execute("INSERT INTO jobs VALUES(?,?,?)", ("c" * 32, "ao-synthetic", "rejected_before_generation"))
        before = self.ledger.read_bytes()
        proc = self.start()
        self.handshake(proc)
        self.assertEqual(before, self.ledger.read_bytes())

    def test_orphan_job_metadata_or_reported_missing_row_blocks_constructor(self):
        jobs = self.home / "deepseek" / "jobs"
        jobs.mkdir(mode=0o700)
        orphan = jobs / ("a" * 32)
        orphan.mkdir(mode=0o700)
        path = orphan / "request.json"
        for value in ({"job_id": "a" * 32, "room_id": "ao-synthetic"}, {"job_id": "wrong", "room_id": "other"}):
            with self.subTest(value=value):
                self.write(path, value)
                proc = self.start()
                self.assertEqual(proc.wait(timeout=5), 1)
                self.assertFalse((self.receipts / "connection.lock").exists())
        self.write(path, {"job_id": "a" * 32, "room_id": "other"})
        proc = self.start()
        self.handshake(proc)
        self.stop(proc)
        self.state["requests"] = {"synthetic": {"reported_delegate_job_ids": ["c" * 32]}}
        self.save()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=5), 1)

    def test_receipt_tamper_and_released_lease_never_validate(self):
        proc = self.start()
        receipt = self.handshake(proc)
        path = self.receipts / (receipt["connection"]["connection_id"] + ".json")
        self.write(path, {**receipt, "meaning": "inference approved"})
        with self.assertRaises(ValueError):
            attachment.validate_attachment(self.room, self.state, self.prepared)
        self.write(path, receipt)
        self.stop(proc)
        with self.assertRaises(ValueError):
            attachment.validate_attachment(self.room, self.state, self.prepared)
        self.assertTrue((self.receipts / (receipt["connection"]["connection_id"] + ".closed.json")).exists())

    def test_normal_restart_preserves_old_receipt_and_requires_a_new_handshake(self):
        first = self.start()
        old = self.handshake(first)
        self.stop(first)
        second = self.start()
        self.wait_for(lambda: self.locator() and self.locator()["connection_id"] != old["connection"]["connection_id"])
        self.assertIsNone(self.ready_receipt())
        with self.assertRaises(ValueError):
            attachment.validate_attachment(self.room, self.state, self.prepared)
        new = self.handshake(second)
        self.assertNotEqual(old["connection"]["connection_id"], new["connection"]["connection_id"])
        self.assertEqual(json.loads((self.receipts / (old["connection"]["connection_id"] + ".json")).read_bytes()), old)

    def test_prior_crashed_connection_is_explicitly_superseded_not_borrowed(self):
        first = self.start()
        old = self.handshake(first)
        first.kill()
        first.wait(timeout=5)
        # SIGKILL cannot run wrapper cleanup. The test cleans only its synthetic
        # orphan server; a real provider job remains outside witness authority.
        try:
            os.kill(old["connection"]["child_pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass  # the fake server may already have observed its pipe's EOF
        second = self.start()
        path = self.receipts / (old["connection"]["connection_id"] + ".closed.json")
        self.wait_for(path.exists)
        self.assertEqual(json.loads(path.read_bytes())["reason"], "prior_local_lease_observed_released")
        with self.assertRaises(ValueError):
            attachment.validate_attachment(self.room, self.state, self.prepared)
        self.wait_for(lambda: self.locator() and self.locator()["connection_id"] != old["connection"]["connection_id"])
        self.assertIsNone(self.ready_receipt())
        new = self.handshake(second)
        self.assertNotEqual(new["connection"], old["connection"])

    def test_slow_restart_cannot_make_crashed_connections_receipt_live_again(self):
        first = self.start()
        old = self.handshake(first)
        first.kill()
        first.wait(timeout=5)
        try:
            os.kill(old["connection"]["child_pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        db = sqlite3.connect(self.ledger)
        owner = attachment._lease_open(self.receipts, filename="owner.lock")
        try:
            # A real read gate blocked on a synthetic writer makes the restart
            # interval observable. The new wrapper has lifecycle ownership, but
            # must not hold the previous ready connection's lease inode.
            db.execute("BEGIN EXCLUSIVE")
            second = self.start()
            self.wait_for(lambda: attachment._held(owner))
            self.assertEqual(self.locator(), old["connection"])
            with self.assertRaises(ValueError):
                attachment.validate_attachment(self.room, self.state, self.prepared)
        finally:
            db.rollback()
            db.close()
            os.close(owner)
        self.wait_for(lambda: self.locator() and self.locator()["connection_id"] != old["connection"]["connection_id"])
        self.handshake(second)

    def test_second_concurrent_wrapper_cannot_claim_another_connections_lease(self):
        first = self.start()
        receipt = self.handshake(first)
        second = self.start()
        self.assertEqual(second.wait(timeout=5), 1)
        self.assertEqual(attachment.validate_attachment(self.room, self.state, self.prepared), receipt)

    def test_parent_eof_cleans_only_owned_child(self):
        proc = self.start()
        receipt = self.handshake(proc)
        self.send(proc, {"jsonrpc": "2.0", "id": 3, "method": "background"})
        worker_pid = json.loads(self.receive(proc))["result"]["worker_pid"]
        try:
            self.stop(proc)
            with self.assertRaises(ProcessLookupError):
                os.kill(receipt["connection"]["child_pid"], 0)
            os.kill(worker_pid, 0)  # the owned server's detached worker survives
        finally:
            os.kill(worker_pid, signal.SIGKILL)

    def test_state_drift_at_handshake_and_during_read_refuses(self):
        proc = self.start()
        self.initialize(proc)
        self.state["bindings"]["engineer"]["session_id"] = "changed"
        self.write(self.room / "state.json", self.state)
        self.enumerate(proc)
        self.wait_for(self.invalid_receipt)
        self.assertIsNone(self.ready_receipt())

    def test_symlink_manifest_and_duplicate_json_keys_refuse(self):
        link = self.room / "manifest-link.json"
        link.symlink_to(self.manifest_path)
        with self.assertRaises(ValueError):
            attachment._validate_manifest(link)
        text = self.manifest_path.read_text()
        self.manifest_path.write_text('{"version": 1, ' + text[1:])
        proc = self.start()
        self.assertEqual(proc.wait(timeout=5), 1)


if __name__ == "__main__":
    unittest.main()
