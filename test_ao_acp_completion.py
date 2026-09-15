"""Offline synthetic SDK streams; never import SDKs or inspect host accounts.

The enclosing consumer keeps the reviewed settlement/dispatch anchor shapes;
the patch itself supplies all completion-barrier behavior exercised below.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ao_acp_patch as acp


SYNTHETIC_MODULE = b'''const AUTONOMOUS_RESULT_ORIGINS = new Set(["task-notification", "peer"]);
const isHeldOpen = (turn) => !!turn && !turn.settled && turn.deferredSettle !== undefined;
const sessionUsage = (session) => session.usage;
const errorKindData = (kind) => ({ kind });
const RequestError = { internalError: (data, detail) => Object.assign(new Error(detail), { data }) };
export class ClaudeAcpAgent {
    sessions = {};
    async syncFastModeState() {}
    async prompt(params) {
        const session = this.sessions[params.sessionId];
        if (Array.from(session.taskState.values()).some((task) => task.status !== "completed")) {
            throw new Error("fixture has no plans");
        }
        session.submissions++;
        const turn = { settled: false, spawnedTaskIds: new Set(), promptUuid: params.uuid };
        session.turnQueue.push(turn);
        return new Promise((resolve, reject) => { turn.resolve = resolve; turn.reject = reject; });
    }
    async runConsumer(session, params) {
        let stopReason = "end_turn";
        const settleActive = (result) => {
            const turn = session.activeTurn;
            if (!turn || turn.settled) return;
            turn.settled = true;
            turn.resolve(result);
            session.turnQueue = session.turnQueue.filter((entry) => entry !== turn);
            session.activeTurn = null;
        };
        const failActiveWithSessionFailure = async (kind, error, title) => {
            const turn = session.activeTurn;
            if (!turn || turn.settled) return;
            turn.settled = true;
            turn.reject(error);
            session.activeTurn = null;
        };
        const turnAwaitingSubagents = (turn) => {
            return [...(turn?.spawnedTaskIds ?? [])].some((id) => session.liveBackgroundTasks.has(id));
        };
        const settleDeferredIfDrained = () => {
            const turn = session.activeTurn;
            if (isHeldOpen(turn) && !turnAwaitingSubagents(turn)) settleActive(turn.deferredSettle);
        };
        const settleOrDefer = (outcome) => {
            if (session.activeTurn && turnAwaitingSubagents(session.activeTurn)) session.activeTurn.deferredSettle = outcome;
            else settleActive(outcome);
        };
        const recordResultForOrphanCommands = () => {};
        const ensureActiveTurn = () => {
            if (session.activeTurn) {
                if (!isHeldOpen(session.activeTurn)) {
                    return;
                }
                settleActive(session.activeTurn.deferredSettle);
            }
            session.activeTurn = session.turnQueue.find((turn) => !turn.settled) ?? null;
            session.usage = 0;
        };
        try {
            while (true) {
                const { value: message, done } = await session.query.next();
                if (done) {
                    const inFlight = session.activeTurn;
                    settleActive(session.cancelled
                        ? { stopReason: "cancelled", usage: sessionUsage(session) }
                        : (inFlight?.deferredSettle ?? { stopReason, usage: sessionUsage(session) }));
                    return;
                }
                if (message.type === "command_lifecycle") {
                    continue;
                }
                switch (message.type) {
                    case "system":
                        if (message.subtype === "task_started") {
                            session.liveBackgroundTasks.set(message.task_id, message);
                            session.activeTurn?.spawnedTaskIds.add(message.task_id);
                        } else if (["task_notification", "task_updated"].includes(message.subtype)) {
                            session.liveBackgroundTasks.delete(message.task_id);
                        } else if (message.subtype === "background_tasks_changed") {
                            session.liveBackgroundTasks.clear();
                        } else if (message.subtype === "session_state_changed") {
                            if (session.cancelled) settleActive({ stopReason: "cancelled" });
                            else settleDeferredIfDrained();
                        }
                        break;
                    case "assistant":
                        session.text.push(message.text);
                        break;
                    case "user": {
                        const queued = session.turnQueue.find((turn) => turn.promptUuid === message.uuid && !turn.settled);
                        if (queued) {
                                if (session.activeTurn !== queued) {
                                    if (session.activeTurn) settleActive(session.activeTurn.deferredSettle);
                                    session.activeTurn = queued;
                                    session.usage = 0;
                                }
                        }
                        break;
                    }
                    case "result": {
                        const isAutonomousResult = message.origin != null && AUTONOMOUS_RESULT_ORIGINS.has(message.origin.kind);
                        if (!isAutonomousResult) {
                                await this.syncFastModeState();
                                recordResultForOrphanCommands();
                                ensureActiveTurn();
                        }
                        session.usage += message.usage ?? 1;
                        if (isAutonomousResult) { settleDeferredIfDrained(); break; }
                        if (message.stop_reason === "refusal") {
                            settleOrDefer({ stopReason: "refusal", usage: sessionUsage(session) });
                            break;
                        }
                        switch (message.subtype) {
                            case "success":
                                if (message.result?.includes("Please run /login")) {
                                    await failActiveWithSessionFailure("auth_required", new Error(message.result));
                                    break;
                                }
                                if (message.stop_reason === "max_tokens") {
                                    stopReason = "max_tokens"; break;
                                }
                                if (message.is_error) {
                                    await failActiveWithSessionFailure("provider_error", new Error(message.result));
                                }
                                break;
                            case "error_during_execution":
                                if (message.stop_reason === "max_tokens") {
                                    stopReason = "max_tokens"; break;
                                }
                                if (message.is_error) {
                                    await failActiveWithSessionFailure("provider_error", new Error(message.errors.join(", ")));
                                }
                                break;
                            case "error_max_turns":
                            case "error_max_budget_usd":
                            case "error_max_structured_output_retries":
                                stopReason = "max_turn_requests";
                                break;
                        }
                        if (!session.cancelled) settleOrDefer({ stopReason, usage: sessionUsage(session) });
                        break;
                    }
                }
            }
        } catch (error) {
            session.queryClosed = true;
            for (const turn of session.turnQueue) {
              if (!turn.settled) {
                const wasHeld = isHeldOpen(turn);
                turn.settled = true;
                    if (wasHeld) {
                        // A held turn's answer already streamed and its outcome is
                        turn.resolve(turn.deferredSettle);
                    } else { turn.reject(error); }
              }
            }
        }
    }
}
'''


HARNESS = r'''
function fixture() {
    const settled = [];
    const messages = [];
    let waiting;
    let drained;
    const session = {
        submissions: 0, taskState: new Map(), toolUseCache: {}, liveBackgroundTasks: new Map(),
        cancelled: false, usage: 0, text: [],
        query: { next: () => {
            if (drained) { drained(); drained = undefined; }
            return new Promise((resolve, reject) => { waiting = { resolve, reject }; });
        } },
    };
    const turn = {
        settled: false, spawnedTaskIds: new Set(), promptUuid: "primary",
        resolve: (result) => settled.push({ result, text: [...session.text] }),
        reject: (error) => settled.push({ error: error.message, data: error.data }),
    };
    session.activeTurn = turn; session.turnQueue = [turn];
    const agent = new ClaudeAcpAgent(); agent.sessions.test = session;
    const running = agent.runConsumer(session, { sessionId: "test" });
    const send = async (message) => {
        if (!waiting) throw new Error("fixture has no pending read");
        await Promise.race([new Promise((resolve) => {
            drained = resolve;
            const waiter = waiting; waiting = undefined;
            waiter.resolve({ value: message, done: false });
        }), running]);
    };
    const end = async (crash = false) => {
        if (session.queryClosed) return running;
        if (crash) waiting.reject(new Error("synthetic transport crash"));
        else waiting.resolve({ done: true });
        await running;
    };
    const launch = async (task, tool = "Agent", parent = null) => {
        const id = "tool-" + task;
        session.toolUseCache[id] = { name: tool, id };
        await send({ type: "system", subtype: "task_started", task_id: task,
            subagent_type: ["Agent", "Task"].includes(tool) ? "worker" : undefined, tool_use_id: id });
        await send({ type: "user", parent_tool_use_id: parent,
            message: { content: [{ type: "tool_result", tool_use_id: id }] },
            tool_use_result: { status: "async_launched", agentId: task } });
    };
    const stopped = async (task) => send({ type: "system", subtype: "task_notification", task_id: task });
    const notice = async (task, uuid, extra = {}) => send({ type: "user", parent_tool_use_id: null,
        origin: { kind: "task-notification" }, uuid,
        message: { content: "<task-notification>\n<task-id>" + task + "</task-id>\n<tool-use-id>tool-" + task + "</tool-use-id>\n<result>synthetic</result>\n</task-notification>" }, ...extra });
    const result = async (uuid, extra = {}) => send({ type: "result", subtype: "success", is_error: false,
        user_message_uuid: uuid, origin: { kind: uuid === "primary" ? "human" : "task-notification" },
        stop_reason: "end_turn", usage: 1, result: "synthetic", ...extra });
    const idle = async () => send({ type: "system", subtype: "session_state_changed", state: "idle" });
    return { session, turn, settled, send, end, launch, stopped, notice, result, idle, agent };
}
const assert = (value, detail) => { if (!value) throw new Error(detail); };
'''


class CompletionPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source_hash = hashlib.sha256(SYNTHETIC_MODULE).hexdigest()
        self.precedence_hash = hashlib.sha256(SYNTHETIC_MODULE.replace(acp.OLD, acp.NEW)).hexdigest()
        self.pins = patch.multiple(acp, SOURCE_SHA256=self.source_hash, PRECEDENCE_SHA256=self.precedence_hash)
        self.pins.start(); self.addCleanup(self.pins.stop)

    def execute(self, body, patched=True):
        node = shutil.which('node')
        if not node:
            self.skipTest('Offline JavaScript consumer probes require Node; no SDK or network is used')
        module = acp.patched(SYNTHETIC_MODULE, True) if patched else SYNTHETIC_MODULE
        run = subprocess.run([node, '--input-type=module', '-'],
                             input=module.decode() + HARNESS + '\n' + body + '\nconsole.log("passed");\n',
                             text=True, capture_output=True, timeout=15, env={})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), 'passed')

    def test_baseline_reproduces_last_worker_edge_before_queued_followup(self):
        self.execute('''const f = fixture(); await f.launch("a"); await f.launch("b");
            await f.stopped("a"); await f.result("primary");
            await f.notice("a", "notice-a"); await f.stopped("b");
            await f.send({ type: "assistant", text: "interim wait" });
            await f.result("notice-a");
            assert(f.settled.length === 1 && f.settled[0].text[0] === "interim wait", "baseline no longer reproduces");
            await f.end();''', patched=False)

    def test_parallel_completion_chain_waits_for_last_notification_result(self):
        self.execute('''const f = fixture(); await f.launch("a"); await f.launch("b");
            await f.stopped("a"); await f.result("primary");
            await f.notice("a", "notice-a"); await f.stopped("b");
            await f.send({ type: "assistant", text: "interim wait" });
            await f.result("notice-a"); await f.idle();
            assert(f.settled.length === 0, "worker termination settled pending parent notification");
            await f.notice("b", "notice-b"); await f.launch("c");
            await f.result("notice-b"); await f.stopped("c"); await f.idle();
            assert(f.settled.length === 0, "chained worker escaped original turn");
            await f.notice("c", "notice-c"); await f.send({ type: "assistant", text: "final report" });
            await f.result("notice-c"); await f.idle();
            assert(f.settled.length === 1 && f.settled[0].text.at(-1) === "final report", "final missing or settled twice");
            assert(f.settled[0].result.usage === 4, "usage not retained across followups"); await f.end();''')

    def test_replayed_next_notice_cannot_be_consumed_by_previous_result_or_idle(self):
        self.execute('''const f = fixture(); await f.launch("a"); await f.launch("b");
            await f.stopped("a"); await f.stopped("b"); await f.result("primary");
            await f.notice("a", "notice-a"); await f.notice("b", "notice-b");
            await f.result("notice-a"); await f.idle();
            await f.result("peer", { origin: { kind: "peer" } });
            assert(f.settled.length === 0, "unrelated result consumed queued notification");
            await f.result("notice-b"); assert(f.settled.length === 1, "exact last result did not settle"); await f.end();''')

    def test_missing_identity_and_level_snapshots_hold_then_eof_fails(self):
        self.execute('''const f = fixture(); await f.launch("a");
            await f.send({ type: "system", subtype: "background_tasks_changed", tasks: [] });
            await f.result("primary"); await f.notice("a", "notice-a");
            await f.result(undefined); await f.idle(); await f.idle();
            assert(f.settled.length === 0, "missing identity or idle converted to success");
            await f.end(); assert(f.settled.length === 1 && f.settled[0].error.includes("completion is unverified"), "EOF accepted interim text");''')

    def test_transport_crash_preserves_failure_in_held_turn(self):
        self.execute('''const f = fixture(); await f.launch("a"); await f.result("primary");
            await f.end(true); assert(f.settled.length === 1 && f.settled[0].error === "synthetic transport crash", "crash became held success");''')

    def test_last_correlated_result_handler_failure_cannot_reuse_interim_success(self):
        self.execute('''const f = fixture(); await f.launch("a"); await f.result("primary");
            await f.stopped("a"); await f.notice("a", "notice-a");
            f.agent.syncFastModeState = async () => { throw new Error("synthetic result delivery failure"); };
            await f.result("notice-a");
            assert(f.settled.length === 1 && f.settled[0].error === "synthetic result delivery failure", "final handler failure became older success"); await f.end();''')

    def test_successor_rejected_before_dispatch_while_owned_debt_is_pending(self):
        self.execute('''const f = fixture(); await f.launch("a"); await f.result("primary");
            const attempts = await Promise.allSettled([f.agent.prompt({ sessionId: "test" }), f.agent.prompt({ sessionId: "test" })]);
            assert(attempts.every((x) => x.status === "rejected") && f.session.submissions === 0, "successor was queued");
            f.session.cancelled = true; await f.idle();
            assert(f.settled[0].result.stopReason === "cancelled", "cancel was hidden"); await f.end();''')

    def test_prequeued_successor_cannot_steal_owned_followup_result_or_usage(self):
        self.execute('''const f = fixture(); let successor;
            const submitted = f.agent.prompt({ sessionId: "test", uuid: "successor" }).then((value) => { successor = value; });
            await f.launch("a"); await f.result("primary"); await f.stopped("a");
            await f.notice("a", "notice-a"); await f.send({ type: "assistant", text: "A final" });
            await f.result("notice-a");
            assert(f.settled.length === 1 && f.settled[0].result.usage === 2, "parent lost followup result or usage");
            assert(successor === undefined && f.session.turnQueue.length === 1, "queued successor received A result");
            await f.send({ type: "user", parent_tool_use_id: null, uuid: "successor", message: { content: "B" } });
            await f.result("successor", { origin: { kind: "human" }, usage: 7 }); await submitted;
            assert(successor.usage === 7, "successor inherited parent usage"); await f.end();''')

    def test_ambiguous_prequeued_handoff_fails_both_submitted_requests(self):
        for echo in (True, False):
            with self.subTest(echo=echo):
                self.execute('''const f = fixture(); let successor;
                    const submitted = f.agent.prompt({ sessionId: "test", uuid: "successor" }).then(
                        (result) => { successor = { result }; }, (error) => { successor = { error }; });
                    await f.launch("a"); await f.result("primary");
                    if (ECHO) await f.send({ type: "user", parent_tool_use_id: null, uuid: "successor", message: { content: "B" } });
                    else await f.result("successor", { origin: { kind: "human" } });
                    await submitted;
                    assert(f.settled.length === 1 && f.settled[0].data.kind === "async_completion_unverified", "parent handed off with interim success");
                    assert(successor.error?.data?.kind === "async_completion_unverified", "submitted successor got a false result");
                    assert(f.session.submissions === 1 && f.session.queryClosed, "uncertain dispatch was relabeled unsent"); await f.end();'''
                             .replace('ECHO', json.dumps(echo)))

    def test_ordinary_non_async_turn_needs_no_result_uuid(self):
        self.execute('''const f = fixture(); await f.result(undefined, { origin: undefined });
            assert(f.settled.length === 1 && f.settled[0].result.stopReason === "end_turn", "ordinary turn stranded"); await f.end();''')

    def test_only_confirmed_root_agent_launches_create_debt(self):
        for tool, parent in [('Bash', None), ('Agent', 'parent-tool')]:
            with self.subTest(tool=tool, parent=parent):
                self.execute('''const f = fixture(); await f.launch("a", TOOL, PARENT); await f.stopped("a");
                    await f.result(undefined, { origin: undefined });
                    assert(f.settled.length === 1, "non-root/non-Agent work acquired root debt"); await f.end();'''
                             .replace('TOOL', json.dumps(tool)).replace('PARENT', json.dumps(parent)))
        self.execute('''const f = fixture(); await f.launch("a", "Task"); await f.stopped("a"); await f.result("primary");
            assert(f.settled.length === 0, "historical Task async work bypassed debt");
            await f.notice("a", "notice-a"); await f.result("notice-a");
            assert(f.settled.length === 1, "historical Task result did not settle"); await f.end();''')

    def test_ambiguous_launch_and_unknown_stop_reason_cannot_drain_debt(self):
        self.execute('''const f = fixture();
            f.session.toolUseCache["tool-a"] = { name: "Agent" };
            f.session.toolUseCache["tool-b"] = { name: "Agent" };
            await f.send({ type: "user", parent_tool_use_id: null,
                message: { content: [{ type: "tool_result", tool_use_id: "tool-a" }, { type: "tool_result", tool_use_id: "tool-b" }] },
                tool_use_result: { status: "async_launched", agentId: "a" } });
            await f.result("primary"); await f.notice("a", "notice-a"); await f.result("notice-a");
            assert(f.settled.length === 0, "ambiguous structured output invented tool correlation"); await f.end();''')
        self.execute('''const f = fixture(); await f.launch("a"); await f.stopped("a"); await f.result("primary");
            await f.notice("a", "notice-a"); await f.result("notice-a", { stop_reason: "tool_use" }); await f.idle();
            assert(f.settled.length === 0, "unknown stop reason established completion"); await f.end();''')

    def test_typed_start_pairs_cold_cache_or_grouped_launch_without_guessing_ids(self):
        for grouped in (True, False):
            with self.subTest(grouped=grouped):
                self.execute('''const f = fixture();
                    await f.send({ type: "system", subtype: "task_started", task_id: "a", subagent_type: "worker", tool_use_id: "tool-a" });
                    const blocks = [{ type: "tool_result", tool_use_id: "tool-a" }];
                    if (GROUPED) {
                        f.session.toolUseCache["tool-b"] = { name: "Agent" };
                        blocks.push({ type: "tool_result", tool_use_id: "tool-b" });
                    }
                    await f.stopped("a");
                    await f.send({ type: "user", parent_tool_use_id: null, message: { content: blocks },
                        tool_use_result: { status: "async_launched", agentId: "a" } });
                    await f.result("primary"); assert(f.settled.length === 0, "typed native launch lost on missing cache");
                    await f.notice("a", "notice-a"); await f.result("notice-a");
                    assert(f.settled.length === 1 && f.settled[0].result.usage === 2, "exact typed task pairing did not drain"); await f.end();'''
                             .replace('GROUPED', json.dumps(grouped)))
        self.execute('''const f = fixture();
            await f.send({ type: "system", subtype: "task_started", task_id: "a", subagent_type: "worker", tool_use_id: "other-tool" });
            f.session.toolUseCache["tool-a"] = { name: "Agent" };
            await f.send({ type: "user", parent_tool_use_id: null,
                message: { content: [{ type: "tool_result", tool_use_id: "tool-a" }] },
                tool_use_result: { status: "async_launched", agentId: "a" } });
            await f.stopped("a"); await f.result("primary"); await f.notice("a", "notice-a"); await f.result("notice-a");
            assert(f.settled.length === 0, "conflicting typed tool identity guessed completion"); await f.end();''')

    def test_sdk_mapped_null_terminal_stops_drain_but_deferred_work_stays_held(self):
        for subtype in ('success', 'error_during_execution', 'error_max_turns',
                        'error_max_budget_usd', 'error_max_structured_output_retries'):
            with self.subTest(subtype=subtype):
                expected = 'max_turn_requests' if subtype.startswith('error_max_') else 'end_turn'
                self.execute('''const f = fixture(); await f.launch("a"); await f.result("primary");
                    await f.stopped("a"); await f.notice("a", "notice-a");
                    await f.result("notice-a", { subtype: SUBTYPE, stop_reason: null, errors: [] });
                    assert(f.settled.length === 1 && f.settled[0].result.stopReason === EXPECTED, "SDK terminal mapping stranded turn"); await f.end();'''
                             .replace('SUBTYPE', json.dumps(subtype)).replace('EXPECTED', json.dumps(expected)))
        for extra in ({'stop_reason': None, 'terminal_reason': 'tool_deferred'},
                      {'stop_reason': None, 'terminal_reason': 'max_turns'},
                      {'stop_reason': None, 'deferred_tool_use': {'id': 'pending-tool'}}):
            with self.subTest(extra=extra):
                self.execute('''const f = fixture(); await f.launch("a"); await f.result("primary");
                    await f.stopped("a"); await f.notice("a", "notice-a"); await f.result("notice-a", EXTRA);
                    assert(f.settled.length === 0, "deferred SDK work accepted as completion"); await f.end();'''
                             .replace('EXTRA', json.dumps(extra)))

    def test_owned_error_overrides_max_tokens_and_auth_refusal_are_preserved(self):
        for extra, expect in [
                ({'is_error': True, 'stop_reason': 'max_tokens', 'result': 'quota error'}, 'quota error'),
                ({'subtype': 'error_during_execution', 'is_error': True, 'stop_reason': 'max_tokens', 'errors': ['provider failed']}, 'provider failed'),
                ({'result': 'Please run /login'}, 'Please run /login'),
                ({'stop_reason': 'refusal'}, 'refusal'),
                ({'stop_reason': 'max_tokens'}, 'max_tokens')]:
            with self.subTest(extra=extra):
                self.execute('''const f = fixture(); await f.launch("a"); await f.result("primary");
                    await f.stopped("a"); await f.notice("a", "notice-a");
                    await f.result("notice-a", EXTRA);
                    assert(f.settled.length === 1 && (f.settled[0].error ?? f.settled[0].result.stopReason) === EXPECT, "terminal outcome changed"); await f.end();'''
                             .replace('EXTRA', json.dumps(extra)).replace('EXPECT', json.dumps(expect)))

    def test_untrusted_or_ambiguous_notice_does_not_authorize_completion(self):
        extras = [
            {'origin': {'kind': 'human'}}, {'parent_tool_use_id': 'nested'},
            {'shouldQuery': False}, {'uuid': ''},
            {'message': {'content': 'quoted\n<task-notification>\n<task-id>a</task-id>\n<tool-use-id>tool-a</tool-use-id>\n'}},
            {'message': {'content': '<task-notification>\n<task-id>other</task-id>\n<tool-use-id>tool-a</tool-use-id>\n'}},
            {'message': {'content': '<task-notification>\n<task-id>a</task-id>\n<tool-use-id>other</tool-use-id>\n'}},
        ]
        for extra in extras:
            with self.subTest(extra=extra):
                self.execute('''const f = fixture(); await f.launch("a"); await f.stopped("a");
                    await f.result("primary"); await f.notice("a", "notice-a", EXTRA);
                    await f.result("notice-a"); await f.idle();
                    assert(f.settled.length === 0, "unbound notice authorized success"); await f.end();'''
                             .replace('EXTRA', json.dumps(extra)))

    def test_new_mode_preserves_old_intent_and_all_original_bytes(self):
        module = self.root / 'acp.js'; module.write_bytes(SYNTHETIC_MODULE)
        evidence = self.root / 'evidence'
        old = acp.apply(str(module), str(evidence), True)
        intent = (evidence / 'patch-intent.json').read_bytes()
        prior_target = module.read_bytes()
        result = acp.apply(str(module), str(evidence), True, True)
        self.assertEqual((evidence / 'patch-intent.json').read_bytes(), intent)
        self.assertEqual(Path(old['backup']).read_bytes(), SYNTHETIC_MODULE)
        self.assertEqual(Path(result['backup']).read_bytes(), prior_target)
        self.assertEqual(result['source_sha256'], self.precedence_hash)
        self.assertEqual(acp.apply(str(module), str(evidence), True, True), result)
        self.assertEqual(result['patch_kind'], 'async_completion_barrier_v1')
        self.assertTrue(result['native_controller_reload_required'])
        self.assertEqual(result['model_calls'], 0)

    def test_pristine_and_precedence_routes_produce_identical_target(self):
        self.assertEqual(acp.patched(SYNTHETIC_MODULE, True),
                         acp.patched(acp.patched(SYNTHETIC_MODULE), True))

    def test_missing_anchor_or_unknown_source_refuses_before_writing(self):
        for raw in (SYNTHETIC_MODULE + b'// new upstream\n',
                    SYNTHETIC_MODULE.replace(b'const ensureActiveTurn = () => {', b'const changed = () => {')):
            module = self.root / 'acp.js'; module.write_bytes(raw)
            with self.assertRaises(ValueError):
                acp.apply(str(module), str(self.root / 'evidence'), True, True)
            self.assertEqual(module.read_bytes(), raw)
            self.assertFalse((self.root / 'evidence').exists())
        altered = SYNTHETIC_MODULE.replace(b'const ensureActiveTurn = () => {', b'const changed = () => {')
        module.write_bytes(altered)
        with patch.object(acp, 'SOURCE_SHA256', hashlib.sha256(altered).hexdigest()):
            with self.assertRaisesRegex(ValueError, 'completion anchor'):
                acp.apply(str(module), str(self.root / 'evidence'), True, True)
        self.assertEqual(module.read_bytes(), altered)
        self.assertFalse((self.root / 'evidence').exists())
