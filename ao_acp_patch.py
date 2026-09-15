"""Explicit, hash-pinned workarounds for the reviewed AO 0.13 Claude ACP module.

Never run automatically during plugin installation or model dispatch. The caller
supplies a verified idle runtime module and a private backup/receipt directory.
Unknown/new upstream bytes refuse; updates require a fresh compatibility review.
The optional completion barrier requires exact async launch/notification/result
correlation; missing evidence holds the turn until cancellation or an audited
failure. It neither imports a late report nor repairs an old completion receipt.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

SOURCE_SHA256 = 'fdc6ca40b0316dc77b93013f4530fbe004806de15adb7bace56654fbde956d78'
OLD = b'if (message.stop_reason === "max_tokens") {'
NEW = b'if (message.stop_reason === "max_tokens" && !message.is_error) {'
PRECEDENCE_SHA256 = '7ee854a6bbcdfa07556aa023eb4c691e7406e26ffb29b49383ded8c316fe4b95'

# This helper is inserted into the verified vendor module, without another
# runtime dependency. A task's terminal edge / a background-set snapshot proves
# the worker stopped, not that its queued notification has been processed.
# Only root Agent/Task structured async results establish debt. The SDK's typed
# task-notification origin and exact leading envelope bind its task/tool ids;
# a result's user_message_uuid must then match that notification's replay uuid.
# Never search arbitrary report text for an id, count idle events, or use time.
COMPLETION_HELPER = r'''function projectRoomAsyncCompletionBarrier(session) {
    const tasks = (turn) => turn?._projectRoomAsyncTasks;
    const outstanding = (turn) => (tasks(turn)?.size ?? 0) > 0;
    const pending = (turn) => !turn?.settled && outstanding(turn);
    const contentBlocks = (message) => typeof message.message?.content === "string"
        ? [{ type: "text", text: message.message.content }]
        : (Array.isArray(message.message?.content) ? message.message.content : []);
    const observe = (message) => {
        const turn = session.activeTurn;
        if (!turn || turn.settled || message.type !== "user" ||
            message.parent_tool_use_id !== null) return;
        const blocks = contentBlocks(message);
        const output = message.tool_use_result;
        if (output?.status === "async_launched" &&
            typeof output.agentId === "string" && output.agentId.length > 0) {
            const calls = blocks.filter((block) => {
                const tool = session.toolUseCache[block.tool_use_id];
                return block.type === "tool_result" &&
                    (tool?.name === "Agent" || tool?.name === "Task");
            });
            if (calls.length > 0) {
                const debts = turn._projectRoomAsyncTasks ??= new Map();
                // Ambiguous structured output is still confirmed async work,
                // but cannot authorize completion using an invented tool id.
                const toolId = calls.length === 1 ? calls[0].tool_use_id : null;
                const key = toolId ?? ("unbound:" + output.agentId);
                if (!debts.has(key)) debts.set(key, {
                    taskId: output.agentId, toolId, notificationUuid: null,
                });
            }
        }
        if (!pending(turn) || message.origin?.kind !== "task-notification" ||
            message.shouldQuery === false || typeof message.uuid !== "string" ||
            message.uuid.length === 0) return;
        for (const block of blocks) {
            if (block.type !== "text" || typeof block.text !== "string") continue;
            const match = /^<task-notification>\r?\n<task-id>([^<>\r\n]+)<\/task-id>\r?\n<tool-use-id>([^<>\r\n]+)<\/tool-use-id>(?:\r?\n|$)/.exec(block.text);
            if (!match) continue;
            const task = tasks(turn).get(match[2]);
            if (task?.taskId === match[1] && task.toolId === match[2] &&
                task.notificationUuid === null) task.notificationUuid = message.uuid;
        }
    };
    const finish = (message) => {
        const turn = session.activeTurn;
        if (!pending(turn) || typeof message.user_message_uuid !== "string" ||
            message.user_message_uuid.length === 0) return false;
        const matched = [...tasks(turn)].filter(([, task]) =>
            task.notificationUuid === message.user_message_uuid);
        if (!matched.length) return false;
        // Owned followups use the normal result handler, including its typed
        // errors, auth, refusal and truncation. Unknown success is never proof.
        const knownSubtype = new Set(["success", "error_during_execution",
            "error_max_turns", "error_max_budget_usd",
            "error_max_structured_output_retries"]);
        if (knownSubtype.has(message.subtype) && (message.is_error === true ||
            ["end_turn", "max_tokens", "refusal"].includes(message.stop_reason))) {
            for (const [key] of matched) tasks(turn).delete(key);
        }
        return true;
    };
    return { observe, finish, pending, outstanding };
}
'''.encode()


def _completion_edits():
    """Exact anchors in the reviewed module; every replacement is single-site."""
    return (
        (b'export class ClaudeAcpAgent {', COMPLETION_HELPER + b'export class ClaudeAcpAgent {'),
        (b'    async runConsumer(session, params) {\n',
         b'    async runConsumer(session, params) {\n'
         b'        const completionBarrier = projectRoomAsyncCompletionBarrier(session);\n'),
        (b'                if (message.type === "command_lifecycle") {',
         b'                completionBarrier.observe(message);\n'
         b'                if (message.type === "command_lifecycle") {'),
        (b'        const turnAwaitingSubagents = (turn) => {\n',
         b'        const turnAwaitingSubagents = (turn) => {\n'
         b'            if (completionBarrier.pending(turn)) return true;\n'),
        (b'        const ensureActiveTurn = () => {\n'
         b'            if (session.activeTurn) {\n'
         b'                if (!isHeldOpen(session.activeTurn)) {\n',
         b'        const ensureActiveTurn = () => {\n'
         b'            if (session.activeTurn) {\n'
         b'                if (!isHeldOpen(session.activeTurn) ||\n'
         b'                    !(session.turnQueue ?? []).some((queued) =>\n'
         b'                        queued !== session.activeTurn && !queued.settled)) {\n'),
        (b'                        const isAutonomousResult = message.origin != null && AUTONOMOUS_RESULT_ORIGINS.has(message.origin.kind);',
         b'                        const ownedAsyncResult = completionBarrier.finish(message);\n'
         b'                        const isAutonomousResult = !ownedAsyncResult && message.origin != null && AUTONOMOUS_RESULT_ORIGINS.has(message.origin.kind);'),
        (b'                    const inFlight = session.activeTurn;\n'
         b'                    settleActive(session.cancelled\n'
         b'                        ? { stopReason: "cancelled", usage: sessionUsage(session) }\n'
         b'                        : (inFlight?.deferredSettle ?? { stopReason, usage: sessionUsage(session) }));',
         b'                    const inFlight = session.activeTurn;\n'
         b'                    if (!session.cancelled && completionBarrier.pending(inFlight)) {\n'
         b'                        const detail = "The SDK stream ended before owned async notifications had correlated terminal results; completion is unverified. Do not replay the prompt.";\n'
         b'                        await failActiveWithSessionFailure("internal_error", RequestError.internalError(errorKindData("async_completion_unverified"), detail), detail);\n'
         b'                    } else {\n'
         b'                        settleActive(session.cancelled\n'
         b'                            ? { stopReason: "cancelled", usage: sessionUsage(session) }\n'
         b'                            : (inFlight?.deferredSettle ?? { stopReason, usage: sessionUsage(session) }));\n'
         b'                    }'),
        (b'        if (Array.from(session.taskState.values()).some((task) => task.status !== "completed")) {',
         b'        if (!session.activeTurn?.settled && session.activeTurn?._projectRoomAsyncTasks?.size) {\n'
         b'            throw RequestError.internalError(undefined, "Owned async work is still awaiting correlated completion; a successor prompt cannot be queued. Cancel or reconcile the existing turn first.");\n'
         b'        }\n'
         b'        if (Array.from(session.taskState.values()).some((task) => task.status !== "completed")) {'),
        (b'                    if (wasHeld) {\n'
         b"                        // A held turn's answer already streamed and its outcome is\n",
         b'                    if (wasHeld && !completionBarrier.outstanding(turn)) {\n'
         b"                        // A held turn's answer already streamed and its outcome is\n"),
    )


def patched(raw, completion_barrier=False):
    digest = hashlib.sha256(raw).hexdigest()
    if completion_barrier and digest == PRECEDENCE_SHA256:
        original = raw.replace(NEW, OLD)
        if raw.count(NEW) != 2 or hashlib.sha256(original).hexdigest() != SOURCE_SHA256:
            raise ValueError('ACP precedence patch no longer matches the reviewed upstream module')
    elif digest != SOURCE_SHA256 or raw.count(OLD) != 2:
        raise ValueError('ACP source differs from the reviewed AO 0.13 module; do not patch an unknown update')
    updated = raw.replace(OLD, NEW)
    if completion_barrier:
        for old, new in _completion_edits():
            if updated.count(old) != 1:
                raise ValueError('ACP completion anchor differs from the reviewed module')
            updated = updated.replace(old, new)
    return updated


def apply(module_path, evidence_directory, confirmed_idle, completion_barrier=False):
    if confirmed_idle is not True:
        raise ValueError('The operator must establish that the target runtime is idle before patching')
    path = Path(module_path)
    evidence = Path(evidence_directory)
    if (not path.is_absolute() or not evidence.is_absolute()
            or any(x.is_symlink() for x in (path, *path.parents, evidence, *evidence.parents))):
        raise ValueError('Use explicit absolute module/evidence paths without module symlinks')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('ACP source ownership is unsafe')
    raw = path.read_bytes()
    receipt_path = evidence / ('completion-patch-intent.json' if completion_barrier else 'patch-intent.json')
    if receipt_path.exists():
        prior = json.loads(receipt_path.read_text())
        if prior.get('module') == str(path) and hashlib.sha256(raw).hexdigest() == prior.get('target_sha256'):
            allowed_sources = {SOURCE_SHA256, PRECEDENCE_SHA256} if completion_barrier else {SOURCE_SHA256}
            source = prior.get('source_sha256')
            if (source not in allowed_sources or prior.get('backup') != str(evidence / (source + '.js'))
                    or Path(prior['backup']).is_symlink()):
                raise ValueError('ACP backup has no exact owned path')
            original = Path(prior['backup']).read_bytes()
            if hashlib.sha256(original).hexdigest() != source or patched(original, completion_barrier) != raw:
                raise ValueError('Patched ACP source/backup differs from its immutable intent')
            return prior
    updated = patched(raw, completion_barrier)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_hash = hashlib.sha256(raw).hexdigest()
    target_hash = hashlib.sha256(updated).hexdigest()
    backup = evidence / (source_hash + '.js')
    if backup.exists():
        if backup.read_bytes() != raw:
            raise ValueError('Existing ACP backup differs')
    else:
        with backup.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    receipt = {'source_sha256': source_hash, 'target_sha256': target_hash, 'module': str(path),
               'backup': str(backup), 'changes': 2, 'confirmed_idle': True, 'model_calls': 0,
               'native_controller_reload_required': True}
    if completion_barrier:
        receipt.update(patch_kind='async_completion_barrier_v1',
                       upstream_sha256=SOURCE_SHA256,
                       changes=len(_completion_edits()) + (2 if source_hash == SOURCE_SHA256 else 0),
                       missing_correlation='hold_without_success',
                       ordinary_turns_require_async_correlation=False)
    if receipt_path.exists():
        if json.loads(receipt_path.read_text()) != receipt:
            raise ValueError('A different ACP patch intent exists')
    else:
        with receipt_path.open('x') as stream:
            json.dump(receipt, stream, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise ValueError('ACP module changed after backup')
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.acp-patch-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(updated); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', required=True)
    parser.add_argument('--evidence-directory', required=True)
    parser.add_argument('--confirmed-idle', action='store_true')
    parser.add_argument('--completion-barrier', action='store_true',
                        help='Also prevent completion before owned async notification results; use a new immutable intent')
    args = parser.parse_args()
    print(json.dumps(apply(args.module, args.evidence_directory, args.confirmed_idle,
                           args.completion_barrier), indent=2))
