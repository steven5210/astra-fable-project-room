"""Package lifetime regressions: synthetic state and local fake workers only."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import project_room_mcp
import project_room_runtime as runtime
from test_project_room import ProjectFixture


ROOT = Path(__file__).resolve().parent
READ_ONLY_BOOTSTRAP = r'''
import os, runpy, sys
actual_home = sys.argv[2]
def offline_only(event, args):
    if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'socket.__new__', 'socket.connect'):
        raise RuntimeError('Read-only fixture denied ' + event)
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = os.path.abspath(os.fsdecode(args[0]))
        flags = args[2]
        # The audit walks from a no-follow root directory descriptor, even
        # when an unmapped container user's home is '/'. Never allow a file
        # or mutating open through this directory-only traversal exception.
        root_traversal = (actual_home == path == os.sep
                          and flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
                          and flags & os.O_ACCMODE == os.O_RDONLY
                          and not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_EXCL))
        if (path == actual_home or path.startswith(actual_home + os.sep)) and not root_traversal:
            raise RuntimeError('Read-only fixture denied real-home access')
sys.addaudithook(offline_only)
runpy.run_path(sys.argv[1], run_name='__main__')
'''


def copy_package(target):
    target.mkdir()
    for name in runtime.RUNTIME_FILES:
        shutil.copyfile(ROOT / name, target / name)
    (target / ".codex-plugin").mkdir()
    shutil.copyfile(ROOT / ".codex-plugin/plugin.json", target / ".codex-plugin/plugin.json")
    return target


class ReadOnlyBootstrapTests(unittest.TestCase):
    def run_guard(self, protected_home, body):
        bootstrap = READ_ONLY_BOOTSTRAP.replace(
            "runpy.run_path(sys.argv[1], run_name='__main__')", body)
        result = subprocess.run(
            [sys.executable, "-E", "-s", "-B", "-c", bootstrap, "unused", str(protected_home)],
            cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_root_home_allows_only_readonly_nofollow_directory_anchor(self):
        self.run_guard(os.sep, r'''
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
fd = os.open(os.sep, flags)
try:
    assert os.fstat(fd).st_mode & 0o170000 == 0o040000
finally:
    os.close(fd)
for denied in (os.O_RDONLY, os.O_RDONLY | os.O_DIRECTORY,
               os.O_RDONLY | os.O_NOFOLLOW,
               flags | os.O_WRONLY, flags | os.O_RDWR,
               flags | os.O_CREAT, flags | os.O_TRUNC,
               flags | os.O_APPEND, flags | os.O_EXCL):
    # Emit the real open event shape without risking a mutating syscall.
    try:
        sys.audit('open', os.sep, None, denied)
    except RuntimeError as exc:
        assert str(exc) == 'Read-only fixture denied real-home access'
    else:
        raise AssertionError('Root file or write access was not denied')
''')

    def test_guard_still_denies_protected_home_file_reads_and_writes(self):
        with tempfile.TemporaryDirectory(prefix="protected-home-fixture-") as directory:
            protected_home = Path(directory).resolve()
            sentinel = protected_home / "private.fixture"
            sentinel.write_bytes(b"synthetic private fixture")
            self.run_guard(protected_home, r'''
for mode in ('rb', 'wb', 'ab', 'r+b'):
    try:
        with open(os.path.join(actual_home, 'private.fixture'), mode):
            pass
    except RuntimeError as exc:
        assert str(exc) == 'Read-only fixture denied real-home access'
    else:
        raise AssertionError('Protected-home file access was not denied')
try:
    sys.audit('open', actual_home, None, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
except RuntimeError as exc:
    assert str(exc) == 'Read-only fixture denied real-home access'
else:
    raise AssertionError('Protected-home directory access was not denied')
''')
            self.assertEqual(sentinel.read_bytes(), b"synthetic private fixture")

    def test_guard_still_denies_network_and_process_events(self):
        self.run_guard(os.sep, r'''
for event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'socket.__new__', 'socket.connect'):
    try:
        sys.audit(event)
    except RuntimeError as exc:
        assert str(exc) == 'Read-only fixture denied ' + event
    else:
        raise AssertionError('Network or process event was not denied')
''')


class StdioFixture:
    def start_server(self, source=None, *, readonly=True, state_home=None):
        source = source or self.source
        argv = [sys.executable, str(source / "project_room_mcp.py")]
        if readonly:
            argv = [sys.executable, "-c", READ_ONLY_BOOTSTRAP, str(source / "project_room_mcp.py"), str(Path.home())]
        server = subprocess.Popen(argv, cwd=source, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env={**os.environ, "PROJECT_ROOM_HOME": str(state_home or self.home),
                                       "PYTHONDONTWRITEBYTECODE": "1"})
        self.servers.append(server)
        return server

    def request(self, server, method, params=None):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        server.stdin.write(json.dumps(message).encode() + b"\n")
        server.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(server.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(10), "MCP response timed out")
        line = server.stdout.readline()
        self.assertTrue(line, "MCP exited without replying")
        response = json.loads(line)
        self.assertNotIn("error", response)
        return response["result"]

    def tool(self, server, name, arguments=None):
        response = self.request(server, "tools/call", {"name": name, "arguments": arguments or {}})
        self.assertFalse(response["isError"], response)
        return response["structuredContent"]

    def initialize(self, server):
        result = self.request(server, "initialize", {"protocolVersion": "2025-06-18"})
        pinned = result["_meta"]["project-room/runtime"]
        self.assertEqual(result["serverInfo"]["version"], pinned["version"])
        self.assertEqual(Path(pinned["path"]).name, pinned["sha256"])
        return pinned

    def close_servers(self):
        for server in self.servers:
            if not server.stdin.closed:
                server.stdin.close()
            if server.poll() is None:
                server.wait(timeout=8)
            stderr = server.stderr.read().decode(errors="replace")
            server.stdout.close()
            server.stderr.close()
            self.assertEqual(server.returncode, 0, stderr)
            self.assertEqual(stderr, "")


class RuntimeRetentionTests(StdioFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = copy_package(self.base / "installed")
        self.home = self.base / "state"
        self.servers = []
        self.addCleanup(self.close_servers)

    def synthetic_room(self):
        directory = self.home / "ao/rooms/ao-runtime-fixture"
        directory.mkdir(parents=True)
        state = {"room_id": "ao-runtime-fixture", "workflow": "fable_engineering", "requests": {},
                 "project_path": str(self.base / "project"), "feature": "runtime fixture", "created_at": 0,
                 "ao_url": "http://127.0.0.1:1", "delegate": {"provider": "none"}, "bindings": {},
                 "verifications": [], "acceptances": []}
        target = directory / "state.json"
        target.write_text(json.dumps(state))
        return target

    def test_runtime_roster_covers_all_distributable_python_and_no_tests(self):
        expected = {path.name for path in ROOT.glob("*.py") if not path.name.startswith("test_")}
        self.assertEqual(set(runtime.RUNTIME_FILES), expected)

    def test_evicted_installation_still_serves_cold_status_and_retained_reconnect(self):
        state = self.synthetic_room()
        original = state.read_bytes()
        server = self.start_server()
        pinned = self.initialize(server)
        tools = self.request(server, "tools/list")["tools"]
        self.assertTrue(next(tool for tool in tools if tool["name"] == "ao_room_status")["annotations"]["readOnlyHint"])
        # No status warm-up: its recovery/provider modules must load for the
        # first time after installation eviction.
        shutil.rmtree(self.source)
        status = self.tool(server, "ao_room_status", {"room_id": "ao-runtime-fixture"})
        self.assertEqual(status["room_id"], "ao-runtime-fixture")
        self.assertEqual(status["delegate"]["provider"], "none")
        self.assertIsNone(status["acceptance_review_extension"])
        self.assertEqual(status["acceptance_review_attempts"], 0)
        self.assertEqual(status["latest_prompt"]["coverage"], "unavailable")
        self.assertEqual(status["usage"]["native_worker_usage"],
                         {"coverage": "unavailable", "reason": "native_worker_usage_not_attributed"})
        self.assertEqual(self.tool(server, "ao_room_list")["count"], 1)
        self.assertEqual(self.request(server, "ping"), {})
        self.assertEqual(state.read_bytes(), original)
        server.stdin.close()
        self.assertEqual(server.wait(timeout=8), 0)
        reopened = self.start_server(Path(pinned["path"]))
        self.assertEqual(self.initialize(reopened), pinned)
        self.assertEqual(self.tool(reopened, "ao_room_status", {"room_id": "ao-runtime-fixture"})["room_id"], "ao-runtime-fixture")

    def test_changeset_cli_imports_after_installation_eviction(self):
        retained = runtime.retain(self.source, self.home)
        directory = Path(retained["path"])
        expected = {name for name in runtime.RUNTIME_FILES if name.startswith("changeset_")}
        self.assertEqual(len(expected), 8)
        self.assertTrue(all((directory / name).is_file() for name in expected))
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        shutil.rmtree(self.source)
        # A cold process must resolve every changeset dependency from the
        # retained package, with no source checkout or inherited Python path.
        result = subprocess.run([sys.executable, "-E", "-s", "-B",
                                 str(directory / "changeset_tool.py"), "--help"],
                                cwd=self.base, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("{plan,apply,audit,resume}", result.stdout)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)

    def test_evidence_audit_cli_survives_eviction_without_creating_state(self):
        retained = runtime.retain(self.source, self.home)
        directory = Path(retained["path"])
        required = ("ao_prompt_metrics.py", "ao_evidence_audit.py", "ao_evidence_audit_io.py",
                    "ao_evidence_audit_native.py")
        self.assertTrue(all((directory / name).is_file() for name in required))
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        shutil.rmtree(self.source)
        absent = self.base / "audit-home-must-stay-absent"
        bootstrap = READ_ONLY_BOOTSTRAP.replace(
            "runpy.run_path(sys.argv[1], run_name='__main__')",
            "entry = sys.argv[1]\nsys.path.insert(0, os.path.dirname(entry))\n"
            "sys.argv = [entry] + sys.argv[3:]\nrunpy.run_path(entry, run_name='__main__')")
        # Numeric container users without a passwd home can receive HOME='/'.
        for protected_home in dict.fromkeys((str(Path.home()), os.sep)):
            with self.subTest(protected_home=protected_home):
                result = subprocess.run(
                    [sys.executable, "-E", "-s", "-B", "-c", bootstrap,
                     str(directory / "project_room.py"), protected_home, "ao-evidence-read-audit",
                     "--home", str(absent), "--room", "fixture-room", "--request", "fixture-request",
                     "--ao-database", str(self.base / "absent.db"), "--evidence-root", str(self.base / "evidence")],
                    cwd=self.base, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, "")
                report = json.loads(result.stdout)
                self.assertEqual(report["coverage"], "unavailable")
                self.assertEqual(report["reasons"], ["request_unbound"])
                self.assertFalse(absent.exists())
                self.assertFalse((self.base / "absent.db").exists())
                self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)

    def test_new_release_preserves_old_connection_and_identifies_both_exact_copies(self):
        self.synthetic_room()
        old_server = self.start_server()
        old = self.initialize(old_server)
        original = (Path(old["path"]) / "ao_reviewer_recovery.py").read_bytes()
        package = json.loads((self.source / ".codex-plugin/plugin.json").read_text())
        package["version"] = "0.3.0+fixture.next"
        (self.source / ".codex-plugin/plugin.json").write_text(json.dumps(package))
        with (self.source / "ao_reviewer_recovery.py").open("a") as stream:
            stream.write("\nFIXTURE_RELEASE = 'second'\n")
        new_server = self.start_server()
        new = self.initialize(new_server)
        self.assertNotEqual(old["sha256"], new["sha256"])
        self.assertEqual(new["version"], "0.3.0+fixture.next")
        self.assertEqual((Path(old["path"]) / "ao_reviewer_recovery.py").read_bytes(), original)
        self.assertNotEqual((Path(new["path"]) / "ao_reviewer_recovery.py").read_bytes(), original)
        shutil.rmtree(self.source)
        for server in (old_server, new_server):
            self.assertEqual(self.tool(server, "ao_room_status", {"room_id": "ao-runtime-fixture"})["room_id"], "ao-runtime-fixture")

    def test_copy_excludes_private_configuration_credentials_transcripts_and_fixtures(self):
        secret = b"SYNTHETIC_PRIVATE_INPUT_MUST_NOT_BE_COPIED"
        private = ("config.json", "auth.json", ".env", "transcript.jsonl", "test_secret.py", "testdata/private.txt", ".claude/settings.json")
        for name in private:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(secret)
        with mock.patch.object(runtime, "_read_regular", wraps=runtime._read_regular) as reading:
            result = runtime.retain(self.source, self.home)
        retained = Path(result["path"])
        self.assertEqual({path.name for path in retained.iterdir()}, {*runtime.RUNTIME_FILES, runtime.MARKER})
        self.assertTrue(all(secret not in path.read_bytes() for path in retained.iterdir()))
        self.assertFalse(any(call.args[0].is_relative_to(self.source) and str(call.args[0].relative_to(self.source)) in private
                             for call in reading.call_args_list))

    def test_missing_or_linked_source_is_refused_before_any_runtime_is_published(self):
        for kind in ("missing", "symlink", "fifo"):
            with self.subTest(kind=kind):
                source = copy_package(self.base / kind)
                target = source / "ao_reviewer_recovery.py"
                target.unlink()
                if kind == "symlink":
                    target.symlink_to(self.source / "ao_reviewer_recovery.py")
                elif kind == "fifo":
                    os.mkfifo(target)
                home = self.base / (kind + "-state")
                with self.assertRaises(runtime.RuntimeRetentionError):
                    runtime.retain(source, home)
                self.assertFalse(home.exists())

    def test_damaged_retained_release_is_refused_without_repair_or_fallback(self):
        for kind in ("missing", "changed", "symlink", "manifest", "extra"):
            with self.subTest(kind=kind):
                home = self.base / (kind + "-state")
                result = runtime.retain(self.source, home)
                directory = Path(result["path"])
                directory.chmod(0o700)
                target = directory / "ao_reviewer_recovery.py"
                if kind in ("missing", "symlink"):
                    target.unlink()
                    if kind == "symlink":
                        target.symlink_to(self.source / target.name)
                elif kind in ("changed", "manifest"):
                    if kind == "manifest":
                        target = directory / runtime.MARKER
                    target.chmod(0o600)
                    target.write_text("damaged fixture\n")
                else:
                    (directory / "unexpected.py").write_text("extra = True\n")
                with self.assertRaises(runtime.RuntimeRetentionError):
                    runtime.retain(self.source, home)
                self.assertEqual([path.name for path in (home / "runtimes").iterdir() if path.is_dir()], [result["sha256"]])

    def test_concurrent_retention_uses_one_complete_release_and_no_pending_copy(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: runtime.retain(self.source, self.home), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual({path.name for path in (self.home / "runtimes").iterdir()}, {".lock", results[0]["sha256"]})

    def test_interrupted_publication_leaves_no_usable_partial_release(self):
        original_write = runtime._write_once

        def interrupted(path, data):
            original_write(path, data)
            if path.name == "ao_reviewer_recovery.py":
                raise OSError("fixture disk write interrupted")

        with mock.patch.object(runtime, "_write_once", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "fixture disk write interrupted"):
                runtime.retain(self.source, self.home)
        self.assertEqual({path.name for path in (self.home / "runtimes").iterdir()}, {".lock"})
        # A separately started publication may now build the same exact source;
        # no partially published digest is repaired or mistaken for completion.
        result = runtime.retain(self.source, self.home)
        self.assertEqual({path.name for path in Path(result["path"]).iterdir()}, {*runtime.RUNTIME_FILES, runtime.MARKER})

    def test_relative_state_home_keeps_its_original_meaning_after_cwd_changes(self):
        self.home = self.base / "relative-state"
        state = self.synthetic_room()
        original = state.read_bytes()
        server = self.start_server(state_home="../relative-state")
        pinned = self.initialize(server)
        self.assertEqual(Path(pinned["path"]).parent, self.home / "runtimes")
        status = self.tool(server, "ao_room_status", {"room_id": "ao-runtime-fixture"})
        self.assertEqual(Path(status["room_path"]), state.parent)
        self.assertEqual(state.read_bytes(), original)

    def test_claude_config_selection_and_inherited_environment_survive_runtime_chdir(self):
        settings = self.base / "claude-settings"
        settings.mkdir()
        (settings / "settings.json").write_text('{"synthetic_marker":"same-settings"}')
        script = r'''
import json, os, sys, sysconfig
from pathlib import Path
from types import SimpleNamespace
actual_home = Path.home()
home_roots = (actual_home, actual_home.resolve())
fixture_root = Path(sys.argv[1]).resolve().parent
stdlib_root = Path(sysconfig.get_path('stdlib')).resolve()
root_home = actual_home.resolve() == Path(actual_home.anchor)
write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
def within(path, root):
    return path == root or root in path.parents
def offline_only(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn',
                                                'os.exec', 'os.fork', 'os.forkpty', 'pty.spawn'):
        raise RuntimeError('Fixture denied ' + event)
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.path.abspath(os.fsdecode(args[0])))
        resolved = path.resolve()
        if any(within(path, root) or within(resolved, root) for root in home_roots):
            # UID-only CI containers can have HOME=/. Permit only this fixture
            # and read-only stdlib imports there, preserving the account fence.
            if root_home and (within(resolved, fixture_root)
                              or (not args[2] & write_flags and within(resolved, stdlib_root))):
                return
            raise RuntimeError('Fixture denied actual-home access')
sys.addaudithook(offline_only)
for event, args in (
    ('open', (str(actual_home / '.claude' / 'synthetic-never-read'), 'r', os.O_RDONLY)),
    ('open', (str(actual_home / '.project-room' / 'synthetic-never-read'), 'r', os.O_RDONLY)),
    ('subprocess.Popen', ('synthetic-never-run', [], None, None)),
    ('socket.__new__', (None, 0, 0, 0)),
):
    try:
        sys.audit(event, *args)  # Check the fence without touching an account or launching anything.
    except RuntimeError:
        pass
    else:
        raise AssertionError('Fixture fence did not deny ' + event)
sys.path.insert(0, sys.argv[1])
from project_room_runtime import activate
runtime = activate(Path(sys.argv[1]) / 'project_room_mcp.py')
from ao_routing import context
home = Path(os.environ['PROJECT_ROOM_HOME'])
observed = context(SimpleNamespace(root=home / 'ao'), home / 'synthetic-room', {})
selected = Path(observed['claude_config_dir'])
print(json.dumps({'selected': str(selected), 'inherited': os.environ['CLAUDE_CONFIG_DIR'],
                  'settings': json.loads((selected / 'settings.json').read_text()), 'cwd': str(Path.cwd()),
                  'runtime': runtime['path'], 'guard_home': str(actual_home)}))
'''
        for probe_home in dict.fromkeys((str(Path.home()), os.path.sep)):
            for configured in ("../claude-settings", str(settings)):
                with self.subTest(home=probe_home, configured=configured):
                    result = subprocess.run([sys.executable, "-B", "-c", script, str(self.source)], cwd=self.source,
                        env={**os.environ, "HOME": probe_home, "PROJECT_ROOM_HOME": str(self.home),
                             "CLAUDE_CONFIG_DIR": configured}, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    observed = json.loads(result.stdout)
                    self.assertEqual(observed['guard_home'], probe_home)
                    self.assertEqual(observed['selected'], str(settings))
                    self.assertEqual(observed['inherited'], str(settings))
                    self.assertEqual(observed['settings'], {'synthetic_marker': 'same-settings'})
                    self.assertEqual(observed['cwd'], observed['runtime'])
                    self.assertNotEqual(observed['cwd'], str(self.source))

    def test_overlap_and_linked_store_are_refused(self):
        with self.assertRaises(runtime.RuntimeRetentionError):
            runtime.retain(self.source, self.source / "state")
        self.home.mkdir()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.home / "runtimes").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(runtime.RuntimeRetentionError):
            runtime.retain(self.source, self.home)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_api_import_keeps_source_semantics_without_provisioning_a_runtime(self):
        self.assertIsNone(project_room_mcp.RUNTIME)
        self.assertEqual(project_room_mcp.project_room.ROOT, ROOT)
        with mock.patch.dict(os.environ, {"PROJECT_ROOM_HOME": str(self.home)}):
            result = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, None)
        self.assertEqual(result["result"]["serverInfo"]["version"], "0.3.0")
        self.assertNotIn("_meta", result["result"])
        self.assertFalse(self.home.exists())

    def test_relative_registry_filters_keep_startup_directory_after_cache_eviction(self):
        project = self.base / "project"
        project.mkdir()
        controller = project_room_mcp.project_room.Service(self.home)
        with controller.db() as database:
            database.execute("INSERT INTO rooms VALUES(?,?,?,?,?)", ("fixture", str(project), "fixture", str(self.home / "room"), "0"))
        server = self.start_server()
        self.initialize(server)
        absolute = self.tool(server, "room_list", {"project_path": str(project)})
        self.assertEqual(len(absolute["rooms"]), 1)
        self.assertEqual(self.tool(server, "room_list", {"project_path": "../project"}), absolute)
        shutil.rmtree(self.source)
        self.assertEqual(self.tool(server, "room_list", {"project_path": "../project"}), absolute)

    def test_only_supported_relative_path_fields_are_normalized_at_mcp_boundary(self):
        paths = {name: field for name, (_, schema) in project_room_mcp.project_room.TOOL_SCHEMAS.items()
                 for field in schema["properties"] if field in ("project_path", "worktree_path", "candidate_path")}
        self.assertEqual(project_room_mcp.RELATIVE_PATH_ARGUMENTS, paths)
        with mock.patch.object(project_room_mcp, "INPUT_CWD", self.source):
            for name, field in paths.items():
                arguments = {field: "../project", "message": "../unchanged-message",
                             "gates": [["./check", "../argument"]], "profile": {"file": "../unchanged"}}
                capture = mock.Mock()
                capture.call.return_value = {}
                response = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}}, capture)
                self.assertFalse(response["result"]["isError"])
                passed = capture.call.call_args.args[1]
                self.assertEqual(passed, {**arguments, field: str(self.source / "../project")})
                self.assertEqual(arguments[field], "../project")
                for unchanged in (None, 5, "", str(self.base / "absolute"), "~/home-path"):
                    value = {field: unchanged}
                    self.assertIs(project_room_mcp.input_arguments(name, value), value)
                if name != "room_list":
                    value = {field: "   "}
                    self.assertIs(project_room_mcp.input_arguments(name, value), value)
            for name, value in (
                ("ao_room_outcome_audit", {"ao_database_path": "../db", "native_transcript_path": "../transcript"}),
                ("ao_room_spec_review_extension_audit", {"native_owner_database": "../db"}),
                ("ao_room_provider_transition_audit", {"target_profile": {"api_key_file": "../key"}}),
                ("ao_room_send", {"message": "../instructions", "request_id": "../not-a-path"}),
                ("ao_room_spec_put", {"content": "../spec", "gates": [["./script", "../file"]]}),
            ):
                self.assertIs(project_room_mcp.input_arguments(name, value), value)
        unchanged = {"project_path": "../project"}
        self.assertIs(project_room_mcp.input_arguments("room_open", unchanged), unchanged)


class RuntimeWorkerLifetimeTests(StdioFixture, ProjectFixture):
    def setUp(self):
        super().setUp()
        self.servers = []
        self.addCleanup(self.close_servers)
        self.source = copy_package(self.base / "installed")

    def test_room_open_dot_preserves_original_startup_directory_without_model_dispatch(self):
        server = self.start_server()
        pinned = self.initialize(server)
        result = self.tool(server, "room_open", {"project_path": ".", "feature": "original startup directory"})
        self.assertEqual(result["project_path"], str(self.source))
        self.assertNotEqual(result["project_path"], pinned["path"])
        self.assertEqual(self.calls(), [])

    def test_actual_fake_review_worker_reads_runtime_after_mcp_exit_and_install_eviction(self):
        # This fixture-only hook runs in the real controller worker after its
        # fake backend finishes. It proves a late import and sibling-byte read,
        # not merely that already-imported Python functions survived.
        target = self.source / "project_room.py"
        content = target.read_text()
        anchor = "            service.worker(args.job_id, args.lease_fd)\n"
        self.assertEqual(content.count(anchor), 1)
        hook = ("            import ao_reviewer_recovery\n"
                "            proof = {'root': str(ROOT), 'module': ao_reviewer_recovery.__file__,\n"
                "                     'sha256': room.sha(Path(ao_reviewer_recovery.__file__).read_bytes())}\n"
                "            (service.home / 'worker-runtime-proof.json').write_text(json.dumps(proof))\n")
        target.write_text(content.replace(anchor, anchor + hook))
        self.control(wait=True)
        server = self.start_server(readonly=False)
        pinned = self.initialize(server)
        job = self.tool(server, "room_review_submit", {"room_id": self.room_id, "revision": 1,
                        "message": "Review the offline fixture", "request_id": "retained-runtime-worker"})
        self.wait_started()
        server.stdin.close()
        self.assertEqual(server.wait(timeout=8), 0)
        shutil.rmtree(self.source)
        (self.base / "release").touch()
        result = self.service.room_job_status(job["id"], 15)
        self.assertEqual(result["status"], "succeeded", result)
        proof_path = self.home / "worker-runtime-proof.json"
        deadline = time.monotonic() + 8
        while not proof_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        proof = json.loads(proof_path.read_text())
        self.assertEqual(proof["root"], pinned["path"])
        self.assertEqual(proof["module"], str(Path(pinned["path"]) / "ao_reviewer_recovery.py"))
        self.assertEqual(proof["sha256"], hashlib.sha256((ROOT / "ao_reviewer_recovery.py").read_bytes()).hexdigest())
        self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
