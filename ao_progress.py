"""Read-only per-request progress view and its one-time delivery instruction.

The progress plan part asks the retained engineer to keep the native task list
current; the same rows fold into AO's session plan panel and into this view.
The view exposes only numbers, labels, timestamps, bounded engineer-authored
strings and identifiers the saved status already prints — never transcript or
private filesystem paths. It performs no state mutation, hold release, receipt,
audit or report replacement.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

import ao_native_outcome
import ao_rate_limits
from room import RoomError

VERSION = 1
PART = "progress_plan_v1"
INSTRUCTION_SHA256 = "983fbab2fe00e5c0f432567d5a18d05a5581b81a93f321442944f07f320823ae"
INSTRUCTION = (
    "Progress plan (reporting default; it grants no scope, execution permission, recovery or review allowance): "
    "keep the native task list current with TaskCreate and TaskUpdate (TodoWrite is equivalent) so the session host "
    "and the operator can follow the work without reading the transcript. When an implementation or correction turn "
    "starts, call TaskList once and create one task per bounded work unit of the current request with a short stable "
    "subject (reuse the same subject across turns for the same unit; give an activeForm for the in-progress wording). "
    "Mark a task in_progress when its work starts and completed only when its evidence exists: the applied change, "
    "the worker's delivered handback or the gate output. A blocker, an unresolved quota failure or a decision the "
    "operator must make is its own task left pending with the reason in its subject; a unit whose worker ended "
    "without a handback stays in_progress or returns to pending, never completed. Update at unit boundaries, not per "
    "tool call, and keep the list to the current request's units rather than a history. The list is a progress view "
    "only: the final JSON report keeps its existing format and remains the engineering verdict, and nothing in the "
    "list replaces delegate evidence or review."
)
PART_V2 = "progress_plan_v2"
INSTRUCTION_V2_SHA256 = "3ee80001834443cbdcdf06c15c42692db42e14e5906c46ad2a42249420b78ee0"
INSTRUCTION_V2 = (
    "Progress plan v2 (reporting default; supersedes the list-keeping rules of progress plan v1; it grants no "
    "scope, execution permission, recovery or review allowance): a task is one bounded work unit — the unit one "
    "worker launch or one Fable step completes — never a phase, a spec section or an umbrella. When a unit's "
    "review finds residuals, create a new task for the next round (EXIT-5 after EXIT-4) instead of reopening or "
    "renaming the finished one; a completed task never returns to in_progress. in_progress means a worker is "
    "running on that unit now or Fable is working on it now; every other unit stays pending, so the in_progress "
    "rows are the current work and the completed count is a true completion count. Call TaskList once when a "
    "turn starts and do not re-mark an unchanged status; update only at unit boundaries. When launching two "
    "workers at once, pair units of similar expected size so neither slot idles while the other finishes. The "
    "list remains a progress view only: the final JSON report keeps its existing format and remains the "
    "engineering verdict, and nothing in the list replaces delegate evidence or review."
)
PART_V3 = "progress_plan_v3"
INSTRUCTION_V3_SHA256 = "9e559c9b963f8a883a979f1f97e3936aba0d2df5c7f7b5c682634da3380b665b"
INSTRUCTION_V3 = (
    "Progress plan v3 (reporting default; consolidates and supersedes progress plan v1 and progress plan v2, whose frozen "
    "text is never re-sent; it grants no scope, execution permission, recovery or review allowance and changes no model, "
    "guard, budget or authority): keep the native task list current with TaskCreate and TaskUpdate (TodoWrite is "
    "equivalent) so the session host and the operator can follow the work without reading the transcript. When an "
    "implementation or correction turn starts, call TaskList once and create one task per bounded work unit of the "
    "current request — the unit one worker launch or one Fable step completes, never a phase, a spec section or an "
    "umbrella — with a short stable subject reused across turns for the same unit and an activeForm for the in-progress "
    "wording. in_progress means a worker is running on that unit now or Fable is working on it now; every other unit "
    "stays pending, so the in_progress rows are the current work and the completed count is a true completion count. A "
    "unit waiting for an operator, a decision, a quota hold or another unit is waiting, not implementing: leave it "
    "pending, and keep a blocker, an unresolved quota failure or an operator decision as its own pending task with the "
    "reason in its subject. When control returns from a worker, a delegate job or the operator with its result, update "
    "that unit before launching further work or writing the final handback, and say in its wording which evidence exists: "
    "result received, result inspected, changes applied, worker-reported tests, formal candidate verification or "
    "independent acceptance. A received result or an exited process never shows a feature complete; mark a unit completed "
    "only when its evidence exists — the applied change, the inspected handback or the gate output — and leave a failed, "
    "missing or partial handback unfinished with its reason: in_progress only while Fable or a worker is working on it now, "
    "otherwise back to pending, and never completed. Residual "
    "work found by a unit's review is a new task carrying its lineage (EXIT-5 after EXIT-4), never a reopened or renamed "
    "one; a completed task never returns to in_progress, and a superseded row keeps its lineage and is never newly "
    "completed. Lineage keys, dispositions and closure counts follow the lifecycle execution and closure default; this "
    "plan adds no key. Update at unit boundaries, not per tool call, do not re-mark an unchanged status, and keep the "
    "list to the current request's units rather than a history. When two workers run at once, pairing units of similar "
    "expected size is a slot-utilisation preference only: never predict sizes or serialize work to keep rows aligned. "
    "Only the engineer writes this list, and only during its own turns: no instruction, operator or controller can "
    "refresh it while the engineer is blocked, idle or waiting, so a row left stale after a turn ends is a disclosed "
    "reporting limit, not evidence of activity, and the controller never wakes the session, edits the list or a "
    "transcript in the background, interrupts a worker or patches the host's plan panel to repair it. The list remains a "
    "progress view only: the final JSON report keeps its existing format and remains the engineering verdict, and nothing "
    "in the list replaces delegate evidence or review."
)
# Superseded one-time progress-plan parts: their frozen text, digests and carried records stay valid for
# historical sessions, but no packet ever carries them again -- progress_plan_v3 consolidates and supersedes both.
SUPERSEDED_PARTS = (PART, PART_V2)
MAX_WINDOW_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 2000
MAX_STEPS = 64
MAX_LAUNCHES = 64
MAX_LABEL_CHARS = 200
MAX_DESCRIPTION_CHARS = 120
MAX_KEY_CHARS = 64
MAX_HISTOGRAM_KEYS = 32
MAX_GROUP_ACTIVE = 8
STALE_IN_PROGRESS_SECONDS = 45 * 60
GUARD_REFUSAL = "Project Room routing guard"
EARLY_EXIT = "Agent terminated early"
_CREATED = re.compile(r"Task #(\S+) created successfully")
_LIST_LINE = re.compile(r"#(\S+)\s+\[([^\]]+)\]\s*(.*)")
_GROUP_TOKEN = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*(?:-[0-9]+[a-z]?)?(?=[:\s]|$)")
_GROUP_ROUND = re.compile(r"-[0-9]+[a-z]?$")
_STEP_STATUSES = ("pending", "in_progress", "completed")

# Deliverables projection (additive, read-only): lineage bounds and the description-token grammar.
MAX_DECLARED_LABELS = 32
MAX_REQUIREMENT_LABELS = 32
MAX_UNIT_LIST = 16
MAX_ID_LIST = 16
# The limits.notes budget: at most 1 anchor note + 6 list/label notes + 1 character-clip note = 8, so
# no note is ever dropped. Anyone adding a note must raise this bound.
MAX_LIMIT_NOTES = 8
MAX_REQ_CHARS = 16
MAX_FROM_CHARS = 64
MAX_BLOCKER_CHARS = 120
# Every id-valued deliverables field is bounded to this many characters after redaction; identity
# (supersession, lookups) always compares the raw ids. MAX_SUPERSEDED_CHARS is the historical alias.
MAX_ID_CHARS = 64
MAX_SUPERSEDED_CHARS = MAX_ID_CHARS
MAX_REASON_CHARS = 200
MAX_KIND_RAW_CHARS = 32
DELIVERABLE_KINDS = ("planned", "defect", "proof_gap", "dependency", "enhancement")
# The deliverables status vocabulary: any other observed task status is reported as "unknown",
# counted open and never completed. The native plan keeps its own three-status counts.
_DELIVERABLE_STATUSES = _STEP_STATUSES + ("unknown",)
_OPEN_STATUSES = ("pending", "in_progress", "unknown")
# The per-key field names the character-clip note counts, in its fixed order, and the subset that
# _resolve_unit reports for one row (in the same relative order).
_CLIP_FIELDS = ("id", "label", "req", "kind_raw", "from", "blocker", "reason", "superseded_by")
_UNIT_CLIP_FIELDS = ("req", "kind_raw", "from", "blocker", "reason")
_DESC_TOKEN = re.compile(r"(?:^|\s)(req|kind|from|blocker|superseded_by)=(\S*)")
# reason= consumes the rest of its line as free text, so it must be the last token on that
# line; every other key (including blocker) is a single whitespace-delimited key=value token.
_DESC_LINE_REST = re.compile(r"(?:^|\s)reason=(.*)")
_DECLARED_LABEL = re.compile(r"^([A-Z]{1,4}[0-9]{1,3})\s+[—–:-]\s", re.MULTILINE)
# A path or URL run ends at whitespace, an ASCII or typographic quote, a backtick or a bracket-like
# delimiter; every other character (including ')', ',', ':', ';', '=' and '*') stays inside the run.
_RUN_END = "\\s'\"`<>\\[\\]{}|\u201c\u201d\u2018\u2019"
_REDACTION = re.compile(
    # A file:// URL names a local path: always redacted whole, whatever its host part.
    "(?i:file)://[^" + _RUN_END + "]*"
    # Any other scheme://authority is kept; the rest of that URL is scanned like any other text, so a
    # path segment inside its query ("?next=/x") is still redacted while "/a/b" after the host is not.
    # The scheme is tried only where a scheme-character run starts, which keeps the scan linear.
    "|(?P<url>(?<![A-Za-z0-9+.\\-])[A-Za-z][A-Za-z0-9+.\\-]*://[^/?#" + _RUN_END + "]+)"
    # An absolute path run: a '/' that no word character, '.' or '~' precedes, plus one or more
    # run characters (so "//x" is one run and a lone " / " is not a path). A '-' does not protect the
    # '/' after it, so a pasted diff line's "-/x" loses its path just as "+/x" does.
    "|(?<![\\w.~])/[^" + _RUN_END + "]+")


def _redact(text):
    """Absolute paths and file:// URLs never leave the view; relative paths and other URLs stay intact.

    A path run starts at the string start, after whitespace or after any non-path character (quote,
    backtick, bracket, '@', '|', '*', '=', ':', ',', ';', ...), including runs that begin with "//".
    A '/' after a word character, '.' or '~' is not a path start, so "src/x.py", "3/4", "and/or"
    and "R1/R2" stay intact, as do "https://host/a/b", "ssh://git@host/r.git" and "git@host:org/r.git";
    a '/' after '-' does start one, so a diff line's "-/x" becomes "-<path>" just as "+/x" becomes "+<path>".
    """
    return _REDACTION.sub(lambda match: match.group("url") or "<path>", str(text))


def _clip(value, limit):
    """Redacted, then bounded; the flag says whether bounding removed characters (redaction never counts).

    This is the one bounding primitive for plan labels and every deliverables string. The flag is
    measured on the redacted text, so a path rewritten to "<path>" is never a shortened value.
    """
    redacted = _redact(value)
    return redacted[:limit], len(redacted) > limit


def _window(path, native_session_id):
    """The bounded tail window of root-session transcript rows, with the outcome audit's ownership discipline."""
    path = Path(path)
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise RoomError("Use the exact owned native transcript without symlinks")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022):
            raise RoomError("Native transcript ownership or size is unsafe")
        offset = max(0, before.st_size - MAX_WINDOW_BYTES)
        if offset:
            stream.seek(offset)
        raw = stream.read(MAX_WINDOW_BYTES)
        after = os.fstat(stream.fileno())
    # A live transcript grows between the two stats; only a replaced or truncated file refuses.
    if (before.st_ino, before.st_dev) != (after.st_ino, after.st_dev) or after.st_size < before.st_size:
        raise RoomError("Native transcript changed while being observed")
    if offset:
        newline = raw.find(b"\n")
        raw = raw[newline + 1:] if newline >= 0 else b""  # The first partial line is outside the window.
    if raw and not raw.endswith(b"\n"):
        raw = raw[:raw.rfind(b"\n") + 1]  # An appended tail may still be mid-write; it is not malformed.
    rows, malformed = [], 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(row, dict):
            malformed += 1
            continue
        if (row.get("sessionId") != native_session_id or row.get("isSidechain") is True
                or row.get("agentId") is not None or row.get("agent_id") is not None):
            continue
        rows.append(row)
    return rows, malformed


def _blocks(row):
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block["text"] for block in content
                       if isinstance(block, dict) and block.get("type") == "text"
                       and isinstance(block.get("text"), str))
    if isinstance(content, dict):
        value = content.get("text")
        return value if isinstance(value, str) else None
    return None


def _tools(rows):
    uses, results = {}, {}
    for row in rows:
        stamp = row.get("timestamp")
        for block in _blocks(row):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                uses[block["id"]] = {"name": block.get("name"), "input": block.get("input"), "timestamp": stamp}
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                results[block["tool_use_id"]] = {"text": _result_text(block.get("content")),
                                                 "is_error": block.get("is_error") is True, "timestamp": stamp}
    return uses, results


def _task_created_id(text):
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        task = value.get("task")
        if isinstance(task, dict) and task.get("id") is not None:
            return str(task["id"])
    match = _CREATED.match(text)
    return match.group(1) if match else None


def _task_succeeded(result):
    """A task tool call advances the plan only through a present, non-error, non-false result."""
    if not isinstance(result, dict) or result.get("is_error"):
        return False
    text = result.get("text")
    if isinstance(text, str):
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if isinstance(value, dict) and "success" in value and value["success"] is not True:
            return False
    return True


def _task_list(text):
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    items = None
    if isinstance(value, dict) and isinstance(value.get("tasks"), list):
        items = [{"id": str(task.get("id")), "subject": task.get("subject") or task.get("content"),
                  "activeForm": task.get("activeForm"), "status": task.get("status") or "pending"}
                 for task in value["tasks"] if isinstance(task, dict) and task.get("id") is not None]
    else:
        if text.strip() == "No tasks found":
            return []
        items = []
        for line in text.splitlines():
            match = _LIST_LINE.match(line.strip())
            if match:
                items.append({"id": match.group(1), "subject": match.group(3) or None,
                              "activeForm": None, "status": match.group(2)})
        if not items:
            items = None
    return items


def _group_key(subject):
    """A task's area: its leading unit code minus one trailing round suffix.

    The code is an uppercase token like EXIT-12, UI-PROOF-2, BARS-1c or P3 at the
    start of the subject, closed by a colon, whitespace or the end; one trailing
    -<digits>[a-z]? round suffix folds the unit's own rounds into its area.
    """
    if not isinstance(subject, str):
        return "other"
    match = _GROUP_TOKEN.match(subject)
    if match is None:
        return "other"
    return _GROUP_ROUND.sub("", match.group(0))


def _leading_code(text):
    """The raw leading unit code (round suffix kept), or None when the text has none."""
    if not isinstance(text, str):
        return None
    match = _GROUP_TOKEN.match(text)
    return None if match is None else match.group(0)


def _display_key(key):
    """The bounded group key; a long code keeps a digest suffix so distinct keys stay distinct."""
    if len(key) <= MAX_KEY_CHARS:
        return key
    return key[:MAX_KEY_CHARS - 12] + "…" + hashlib.sha256(key.encode()).hexdigest()[:11]


def _new_group(key):
    return {"key": key, "counts": {status: 0 for status in _STEP_STATUSES}, "total": 0, "active": []}


def _label_text(step):
    """The raw label choice: activeForm while in progress (when given), else subject; may be None."""
    return step.get("activeForm") if step.get("status") == "in_progress" and step.get("activeForm") \
        else step.get("subject")


def _step_label(step):
    label = _label_text(step)
    return _clip(label, MAX_LABEL_CHARS)[0] if label is not None else ""


def _plan_groups(steps):
    """Every step folded by area code; groups sort by key and the tail folds into other."""
    grouped = {}
    for step in steps:
        key = _group_key(step.get("subject"))
        entry = grouped.setdefault(key, _new_group(key))
        status = step.get("status")
        if status in entry["counts"]:
            entry["counts"][status] += 1
        entry["total"] += 1
        if status == "in_progress" and len(entry["active"]) < MAX_GROUP_ACTIVE:
            entry["active"].append(_step_label(step))
    named = sorted(key for key in grouped if key != "other")
    for key in named[MAX_HISTOGRAM_KEYS - 1:]:
        overflow = grouped.pop(key)
        folded = grouped.setdefault("other", _new_group("other"))
        for status in _STEP_STATUSES:
            folded["counts"][status] += overflow["counts"][status]
        folded["total"] += overflow["total"]
        room = MAX_GROUP_ACTIVE - len(folded["active"])
        if room > 0:
            folded["active"].extend(overflow["active"][:room])
    return [{"key": _display_key(entry["key"]), "counts": entry["counts"],
             "completed_of_total": str(entry["counts"]["completed"]) + "/" + str(entry["total"]),
             "active": entry["active"]}
            for entry in sorted(grouped.values(), key=lambda item: item["key"])]


def _merge_metadata(target, given_metadata):
    """TaskCreate/TaskUpdate metadata merged key by key; a None value deletes that key.

    Only str/bool/int/float values are kept (converted to str); any other value type
    contributes nothing to the merge (neither set nor deleted).
    """
    if not isinstance(given_metadata, dict):
        return
    for key, value in given_metadata.items():
        if not isinstance(key, str):
            continue
        if value is None:
            target.pop(key, None)
        elif isinstance(value, (str, bool, int, float)):
            target[key] = str(value)


def _description_tokens(description):
    """Whitespace-separated key=value tokens from a task description.

    req/kind/from/blocker/superseded_by are each a single non-whitespace token; reason=
    consumes the rest of its line as its own value, so it must be the last token on that line.
    """
    tokens = {}
    if not isinstance(description, str):
        return tokens
    for line in description.splitlines():
        rest_match = _DESC_LINE_REST.search(line)
        scanned = line[:rest_match.start()] if rest_match else line
        for match in _DESC_TOKEN.finditer(scanned):
            tokens[match.group(1)] = match.group(2)
        if rest_match:
            tokens["reason"] = rest_match.group(1)
    return tokens


def _bounded_text(value, limit):
    """_clip's (text, clipped) pair for a lineage string; a non-string or empty value is (None, False)."""
    return _clip(value, limit) if isinstance(value, str) and value else (None, False)


def _display_id(raw):
    """An emitted id-valued field and its clip flag: redacted, then bounded. Identity never uses this form."""
    return _clip(raw, MAX_ID_CHARS)


def _resolve_unit(entry):
    """One task's raw requirement/successor identities and bounded, redacted lineage fields.

    Description tokens supply the base; metadata overrides them key by key for the same name.
    A missing entry (never created or updated inside the observed window) resolves unmapped.
    A nonblank requirement stays raw for grouping; only emitted labels are redacted and clipped.
    The successor stays raw (surrounding whitespace stripped, exactly one leading '#' removed) so
    identity compares it with raw task ids; it is absent for ""/"false" (any case), which also
    covers metadata False after its str conversion, and for an empty description token. The
    successor is never bounded here. "clipped" names, in _UNIT_CLIP_FIELDS order, each of
    req/kind_raw/from/blocker/reason whose value its character bound shortened.
    """
    metadata = (entry or {}).get("metadata") or {}
    merged = _description_tokens((entry or {}).get("description"))
    merged.update(metadata)
    req = merged.get("req")
    if not isinstance(req, str) or not req.strip():
        req = None
    _, req_clipped = _bounded_text(req, MAX_REQ_CHARS)
    kind_value = merged.get("kind")
    kind_raw_clipped = False
    if not kind_value:
        kind, kind_raw = "planned", None
    elif kind_value in DELIVERABLE_KINDS:
        kind, kind_raw = kind_value, None
    else:
        kind = "invalid"
        kind_raw, kind_raw_clipped = _clip(kind_value, MAX_KIND_RAW_CHARS)
    blocker_value = merged.get("blocker")
    blocker, blocker_clipped = (_clip(blocker_value, MAX_BLOCKER_CHARS)
                                if blocker_value and blocker_value.strip().lower() != "false" else (None, False))
    superseded_value = merged.get("superseded_by")
    successor = superseded_value.strip() if isinstance(superseded_value, str) else ""
    if successor.lower() == "false":
        successor = ""
    if successor.startswith("#"):
        successor = successor[1:]
    from_text, from_clipped = _bounded_text(merged.get("from"), MAX_FROM_CHARS)
    reason, reason_clipped = _bounded_text(merged.get("reason"), MAX_REASON_CHARS)
    flags = (req_clipped, kind_raw_clipped, from_clipped, blocker_clipped, reason_clipped)
    return {"req": req, "kind": kind, "kind_raw": kind_raw, "from": from_text,
            "blocker": blocker, "successor": successor or None, "reason": reason,
            "clipped": tuple(field for field, clipped in zip(_UNIT_CLIP_FIELDS, flags) if clipped)}


def _supersession(steps, lineage):
    """The one supersession verdict per tracked id, shared by closure.window and every deliverables row.

    successor is the raw named id (None when absent); observed means it names a currently tracked
    raw id; cycle means following the observed-successor chain from the row returns to the row (a
    self-reference is a cycle of length one); superseded is observed and not cycle. A chain that runs
    into a cycle elsewhere still supersedes the rows outside that cycle (1->2->3->2 supersedes 1 only).
    """
    successors = {task_id: _resolve_unit(lineage.get(task_id))["successor"] for task_id in steps}
    edges = {task_id: successor for task_id, successor in successors.items()
             if successor is not None and successor in steps}
    on_cycle, finished = set(), set()
    for start in edges:
        path, position, node = [], {}, start
        while node in edges and node not in finished and node not in position:
            position[node] = len(path)
            path.append(node)
            node = edges[node]
        if node in position:  # this walk closed a loop: exactly the nodes from there on form the cycle
            on_cycle.update(path[position[node]:])
        finished.update(path)
    return {task_id: {"successor": successors[task_id], "observed": task_id in edges,
                      "cycle": task_id in on_cycle, "superseded": task_id in edges and task_id not in on_cycle}
            for task_id in steps}


def _stale(step, step_updated, launch_codes, now, launches_known):
    """in_progress, its leading code unmatched by a launch, last update older than the stale bound."""
    status = step.get("status")
    code = _leading_code(step.get("subject"))
    if not (launches_known and status == "in_progress" and code is not None
            and code not in launch_codes and now is not None):
        return False
    step_stamp = step_updated.get(step["id"])
    try:
        updated_epoch = ao_native_outcome.timestamp(step_stamp) if step_stamp is not None else None
    except RoomError:
        updated_epoch = None
    return updated_epoch is not None and now - updated_epoch > STALE_IN_PROGRESS_SECONDS


def _fold_plan(rows, uses, results, launches=None, now=None, launches_known=False):
    steps, step_updated = {}, {}
    lineage, window = {}, {"created": 0, "completed": 0, "superseded": 0}
    completed_seen = set()
    source, updated_at = "none", None
    # The row of the last plan change: provenance scopes the plan to the selected turn only when
    # this exact row lies inside that turn. It is internal and never emitted.
    updated_row = None
    for row in rows:
        stamp = row.get("timestamp")
        for block in _blocks(row):
            if not isinstance(block, dict):
                continue
            kind = None
            if block.get("type") == "tool_use" and block.get("name") == "TodoWrite":
                todos = (block.get("input") or {}).get("todos") if isinstance(block.get("input"), dict) else None
                if isinstance(todos, list):
                    steps, step_updated, lineage = {}, {}, {}
                    for index, todo in enumerate(todos):
                        if isinstance(todo, dict):
                            key = "todo-" + str(index)
                            steps[key] = {"id": key, "subject": todo.get("content"),
                                          "activeForm": todo.get("activeForm"),
                                          "status": todo.get("status") or "pending"}
                    kind = "todo_write"
            elif block.get("type") == "tool_use" and block.get("name") == "TaskCreate":
                given = block.get("input") if isinstance(block.get("input"), dict) else {}
                result = results.get(block.get("id"))
                task_id = _task_created_id((result or {}).get("text")) if _task_succeeded(result) else None
                if task_id is not None:
                    steps[task_id] = {"id": task_id, "subject": given.get("subject"),
                                      "activeForm": given.get("activeForm"), "status": "pending"}
                    step_updated[task_id] = stamp
                    window["created"] += 1
                    entry = {"metadata": {}, "description": None}
                    _merge_metadata(entry["metadata"], given.get("metadata"))
                    if isinstance(given.get("description"), str):
                        entry["description"] = given["description"]
                    lineage[task_id] = entry
                    kind = "task_tools"
            elif block.get("type") == "tool_use" and block.get("name") == "TaskUpdate":
                given = block.get("input") if isinstance(block.get("input"), dict) else {}
                task_id = given.get("taskId", given.get("id"))
                if task_id is not None and _task_succeeded(results.get(block.get("id"))):
                    key = str(task_id)
                    if given.get("status") == "deleted":
                        if key in steps:
                            del steps[key]
                            step_updated.pop(key, None)
                            lineage.pop(key, None)
                            kind = "task_tools"
                    elif key in steps:
                        for field in ("subject", "activeForm", "status"):
                            if given.get(field) is not None:
                                steps[key][field] = given[field]
                        step_updated[key] = stamp
                        if given.get("status") == "completed":
                            completed_seen.add(key)
                        entry = lineage.setdefault(key, {"metadata": {}, "description": None})
                        _merge_metadata(entry["metadata"], given.get("metadata"))
                        if isinstance(given.get("description"), str):
                            entry["description"] = given["description"]
                        kind = "task_tools"
            elif block.get("type") == "tool_result":
                use = uses.get(block.get("tool_use_id"))
                if use is not None and use.get("name") == "TaskList":
                    items = _task_list(_result_text(block.get("content")))
                    if items is not None:
                        previous, previous_updated = steps, step_updated
                        steps = {item["id"]: item for item in items}
                        step_updated = {}
                        for item in items:
                            key = item["id"]
                            before = previous.get(key)
                            if before is None:
                                continue  # First seen in the list: its update time is unknown.
                            if before.get("status") != item.get("status"):
                                step_updated[key] = stamp
                            elif key in previous_updated:
                                step_updated[key] = previous_updated[key]
                        lineage = {key: value for key, value in lineage.items() if key in steps}
                        kind = "task_tools"
            if kind is not None:
                source, updated_at, updated_row = kind, stamp, row
    counts = {status: 0 for status in _STEP_STATUSES}
    for step in steps.values():
        status = step.get("status")
        if status in counts:
            counts[status] += 1
    total = len(steps)
    ordered = list(steps.values())
    if total <= MAX_STEPS:
        chosen = ordered
    else:
        # The bound keeps the units the operator acts on: every in-progress unit,
        # then the newest pending and newest completed in fold order.
        active = [step for step in ordered if step.get("status") == "in_progress"]
        pending = [step for step in ordered if step.get("status") not in ("in_progress", "completed")]
        completed = [step for step in ordered if step.get("status") == "completed"]
        chosen = (active + pending[::-1] + completed[::-1])[:MAX_STEPS]
    launch_codes = {_leading_code(launch.get("description")) for launch in launches or ()}
    launch_codes.discard(None)
    # Computed once for every tracked step (not just the shown ones) so the deliverables
    # projection can reuse the exact same staleness the operator sees in the shown steps.
    stale_by_id = {step["id"]: _stale(step, step_updated, launch_codes, now, launches_known)
                   for step in steps.values()}
    output, shown = [], {status: 0 for status in _STEP_STATUSES}
    for step in chosen:
        status = step.get("status")
        if status in shown:
            shown[status] += 1
        output.append({"label": _step_label(step),
                       "status": status if status is not None else "pending",
                       "stale_in_progress": stale_by_id[step["id"]]})
    window["completed"] = len(completed_seen)
    # closure.window.created/completed count transitions seen anywhere in the bounded transcript
    # tail; closure.window.superseded is the final evaluation over the currently tracked ids. Lineage
    # is only observable (pruned by TaskList/TodoWrite/deletion) inside this window, so a dangling
    # successor or a cycle contributes nothing to it (F2). The verdicts are computed once here and
    # handed to the deliverables rows, so both counts read the same per-row verdict.
    supersession = _supersession(steps, lineage)
    window["superseded"] = sum(1 for verdict in supersession.values() if verdict["superseded"])
    plan = {"source": source, "updated_at": updated_at, "steps": output, "counts": counts,
            "total": total, "shown": len(output),
            "omitted": {status: counts[status] - shown[status] for status in _STEP_STATUSES},
            "groups": _plan_groups(steps.values())}
    return plan, {"steps": steps, "lineage": lineage, "stale": stale_by_id, "window": window,
                  "supersession": supersession, "updated_row": updated_row}


def _turn_window(rows, request, start, end):
    """The request's anchor row and the turn rows after it, up to the next human row.

    The anchor is the earliest root human row carrying the request's text inside the
    request's own time bounds: on or after ``start - 5`` seconds (a small grace for
    native clock skew) and before the next same-session request's creation. A request
    that was never delivered has no row inside its bounds, and a bare echo of the text
    outside them cannot displace the delivered caller.
    """
    wanted = request.get("text_sha256")
    anchor = None
    if isinstance(wanted, str) and isinstance(start, (int, float)):
        for index, row in enumerate(rows):
            text = ao_native_outcome.human_text(row)
            if not text or hashlib.sha256(text.encode()).hexdigest() != wanted:
                continue
            try:
                stamp = ao_native_outcome.timestamp(row.get("timestamp"))
            except RoomError:
                continue  # A row without a readable timestamp is never a candidate.
            if stamp < start - 5.0 or (end is not None and stamp >= end):
                continue
            anchor = index
            break
    if anchor is None:
        return None, None
    window = []
    for row in rows[anchor + 1:]:
        if ao_native_outcome.human_text(row):
            break
        window.append(row)
    return rows[anchor], window


def _launches(uses, results):
    rows = []
    for identity, use in uses.items():
        if use.get("name") != "Agent":
            continue
        given = use.get("input") if isinstance(use.get("input"), dict) else {}
        result = results.get(identity)
        started, ended, duration = use.get("timestamp"), None, None
        status = "running"
        if result is not None:
            ended = result.get("timestamp")
            text = result.get("text")
            status = "error" if result.get("is_error") or (isinstance(text, str) and text.startswith(EARLY_EXIT)) \
                else "completed"
            try:
                duration = ao_native_outcome.timestamp(ended) - ao_native_outcome.timestamp(started)
            except RoomError:
                duration = None
        description = given.get("description")
        rows.append({"subagent_type": given.get("subagent_type"),
                     "description": (_redact(description)[:MAX_DESCRIPTION_CHARS]
                                     if description is not None else None),
                     "started_at": started, "ended_at": ended, "status": status,
                     "duration_seconds": duration})
    return rows


def _bump(histogram, key):
    """One bounded histogram entry; named keys stop at MAX_HISTOGRAM_KEYS - 1, the rest fold into other."""
    if not isinstance(key, str):
        return
    key = key[:MAX_KEY_CHARS]
    if key not in histogram and len(histogram) - ("other" in histogram) >= MAX_HISTOGRAM_KEYS - 1:
        key = "other"
    histogram[key] = histogram.get(key, 0) + 1


def _turn(anchor_row, window):
    assistant = [row for row in window if row.get("type") == "assistant"]
    served_models, stop_reasons, tool_calls = {}, {}, {}
    for row in assistant:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        _bump(served_models, message.get("model"))
        _bump(stop_reasons, message.get("stop_reason"))
        for block in _blocks(row):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                _bump(tool_calls, block.get("name"))
    api_errors = sum(1 for row in window if row.get("isApiErrorMessage") is True)
    compactions = sum(1 for row in window if row.get("type") == "user"
                      and row.get("isCompactSummary") is True and row.get("isVisibleInTranscriptOnly") is True)
    guard_refusals = sum(1 for row in window for block in _blocks(row)
                         if isinstance(block, dict) and block.get("type") == "tool_result"
                         and isinstance(_result_text(block.get("content")), str)
                         and _result_text(block.get("content")).startswith(GUARD_REFUSAL))
    started_at = anchor_row.get("timestamp")
    last_activity_at = started_at
    for row in reversed(window):
        stamp = row.get("timestamp")
        try:
            ao_native_outcome.timestamp(stamp)
        except RoomError:
            continue
        last_activity_at = stamp
        break
    try:
        elapsed = ao_native_outcome.timestamp(last_activity_at) - ao_native_outcome.timestamp(started_at)
    except RoomError:
        elapsed = None
    return {"anchor_found": True, "started_at": started_at, "last_activity_at": last_activity_at,
            "elapsed_seconds": elapsed, "assistant_rows": len(assistant), "served_models": served_models,
            "stop_reasons": stop_reasons, "tool_calls": tool_calls, "api_errors": api_errors,
            "compactions": compactions, "guard_refusals": guard_refusals}


def _last_text(rows):
    for row in reversed(rows):
        if row.get("type") != "assistant":
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        texts = [block["text"] for block in (content if isinstance(content, list) else [])
                 if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
        if texts:
            return texts[-1]
    return None


def _request_order(request):
    return (request.get("created_order") if isinstance(request.get("created_order"), (int, float)) else 0,
            request.get("created_at") if isinstance(request.get("created_at"), (int, float)) else 0)


def _request_view(request):
    return {"request_id": request.get("request_id"), "state": request.get("state"),
            "purpose": request.get("purpose"), "created_at": request.get("created_at"),
            "turn_id": request.get("turn_id"), "configured_model": request.get("model"),
            "semantic_status": request.get("semantic_status")}


def _declared_labels(content):
    """Unique req-style labels the spec body declares, in order of appearance, bounded."""
    labels = []
    for match in _DECLARED_LABEL.finditer(content if isinstance(content, str) else ""):
        label = match.group(1)
        if label not in labels:
            labels.append(label)
    return labels[:MAX_DECLARED_LABELS], len(labels) > MAX_DECLARED_LABELS


def _spec_anchor(directory, state):
    """The room's immutable spec record identity and declared labels; independent of the transcript."""
    notes = []
    record_sha = state.get("spec_record_sha256")
    anchor = {"spec_record_sha256": record_sha if isinstance(record_sha, str) else None,
              "spec_revision": None, "spec_sha256": None, "declared_labels": []}
    truncated = False
    if isinstance(record_sha, str):
        import ao_workflow
        try:
            spec = ao_workflow.spec_record_file(directory, record_sha)
            if not isinstance(spec, dict):
                # A malformed on-disk record (e.g. a JSON array or scalar) whose digest still
                # matches is unavailable exactly like a missing or unreadable record, never a
                # crash from calling .get on a non-mapping.
                notes.append("spec record unavailable")
            else:
                anchor["spec_revision"] = spec.get("revision")
                anchor["spec_sha256"] = spec.get("sha256")
                anchor["declared_labels"], truncated = _declared_labels(spec.get("content"))
                if truncated:
                    notes.append("declared labels bounded to " + str(MAX_DECLARED_LABELS))
        except (RoomError, OSError, ValueError, KeyError, TypeError):
            notes.append("spec record unavailable")
    else:
        notes.append("spec record unavailable")
    return anchor, notes, truncated


def _empty_unit_bucket():
    return {"in_progress": [], "pending": [], "completed": []}


def _deliverable_status(value):
    """The deliverables status: a missing status is pending, any value outside the vocabulary unknown."""
    if not value:
        return "pending"
    return value if value in _STEP_STATUSES else "unknown"


def _new_requirement_accumulator():
    # units_by_status holds (unit record, shown clipped field names) pairs; req_clipped is set when
    # this raw requirement's display label was shortened. Neither private form reaches the view.
    return {"counts": {status: 0 for status in _DELIVERABLE_STATUSES}, "superseded": 0,
            "required_open": 0, "enhancement_open": 0, "blockers": 0,
            "kinds": {kind: 0 for kind in DELIVERABLE_KINDS + ("invalid",)},
            "units_by_status": _empty_unit_bucket(), "req_clipped": False}


def _new_clip_counts():
    return {field: 0 for field in _CLIP_FIELDS}


def _among(row_clipped, shown_fields):
    """The clipped field names one record shows: shown_fields (in order) that the row's set holds."""
    return tuple(field for field in shown_fields if field in row_clipped)


def _shown(pairs, bound, clips):
    """The first bound (value, clipped field names) pairs as plain values, tallying only those kept.

    A value beyond the bound is not shown, so its clipped fields never count; the bound's own
    list note already discloses it.
    """
    kept = pairs[:bound]
    for _, fields in kept:
        for field in fields:
            clips[field] += 1
    return [value for value, _ in kept]


def _finalize_requirement(label, declared, accum):
    """One requirement entry plus the clip counts of the values it shows: its kept units and its label."""
    by_status = accum["units_by_status"]
    ordered = by_status["in_progress"][::-1] + by_status["pending"][::-1] + by_status["completed"][::-1]
    clips = _new_clip_counts()
    units = _shown(ordered, MAX_UNIT_LIST, clips)
    # The requirement label appears once, regardless of how many units belong to it.
    if accum["req_clipped"]:
        clips["req"] += 1
    return ({"label": _bounded_text(label, MAX_REQ_CHARS)[0],
             "declared": declared, "folded": False, "counts": accum["counts"],
             "superseded": accum["superseded"], "required_open": accum["required_open"],
             "enhancement_open": accum["enhancement_open"], "blockers": accum["blockers"],
             "kinds": accum["kinds"], "units": units, "units_total": len(ordered)}, clips)


def _fold_requirement_labels(buckets, declared_labels):
    """Sorted requirement entries bounded to MAX_REQUIREMENT_LABELS; overflow folds into "other".

    Keys are raw identities: requirements whose display labels collide remain separate entries.
    The folded overflow entry is distinguished by "folded": True rather than by its "other"
    label, so a genuine requirement literally named "other" keeps its own entry (folded False).
    The third value is the clip counts of the kept entries; the folded entry shows no units and a
    fixed label, so the requirements folded into it contribute nothing.
    """
    finalized = {label: _finalize_requirement(label, label in declared_labels, accum)
                 for label, accum in buckets.items()}
    labels = sorted(finalized)
    kept, overflow = labels[:MAX_REQUIREMENT_LABELS], labels[MAX_REQUIREMENT_LABELS:]
    entries = [finalized[label][0] for label in kept]
    clips = _new_clip_counts()
    for label in kept:
        for field, count in finalized[label][1].items():
            clips[field] += count
    if overflow:
        folded = {"label": "other", "declared": False, "folded": True,
                  "counts": {status: 0 for status in _DELIVERABLE_STATUSES},
                  "superseded": 0, "required_open": 0, "enhancement_open": 0, "blockers": 0,
                  "kinds": {kind: 0 for kind in DELIVERABLE_KINDS + ("invalid",)},
                  "units": [], "units_total": 0}
        for label in overflow:
            source = finalized[label][0]
            for status in _DELIVERABLE_STATUSES:
                folded["counts"][status] += source["counts"][status]
            for key in ("superseded", "required_open", "enhancement_open", "blockers", "units_total"):
                folded[key] += source[key]
            for kind_key in folded["kinds"]:
                folded["kinds"][kind_key] += source["kinds"][kind_key]
        entries.append(folded)
    return entries, bool(overflow), clips


def _deliverables(available, extra, anchor, anchor_notes, anchor_truncated, unavailable_reason=None):
    """Additive, read-only lineage/closure projection folded from the same transcript rows as plan."""
    notes, truncated = list(anchor_notes), anchor_truncated
    result = {"version": 1, "available": available, "anchor": anchor, "requirements": [],
              "unmapped": {"count": 0, "metadata_unavailable": 0, "ids": [], "superseded": 0, "required_open": 0,
                           "completed": 0, "blockers": 0},
              "superseded": [], "superseded_total": 0, "blockers": [], "blockers_total": 0,
              "conflicts": [], "conflicts_total": 0,
              "closure": {"completed": 0, "enhancement_completed": 0, "required_open": 0, "enhancement_open": 0,
                          "superseded": 0, "blockers": 0, "conflicts": 0, "stale_in_progress": 0,
                          "window": {"created": 0, "completed": 0, "superseded": 0}},
              "limits": {"truncated": truncated, "notes": notes}}
    if not available:
        # closure, closure.window and every list are forced to their zero/empty state regardless
        # of what extra carries: unavailable is a hard contract, not an incidental side effect of
        # an empty transcript (F6).
        if unavailable_reason:
            notes.append(unavailable_reason)
        elif not notes:
            notes.append("no task tool activity observed")
        result["limits"] = {"truncated": truncated, "notes": notes[:MAX_LIMIT_NOTES]}
        return result
    window = extra.get("window") or {"created": 0, "completed": 0, "superseded": 0}
    result["closure"]["window"] = {"created": window.get("created", 0), "completed": window.get("completed", 0),
                                   "superseded": window.get("superseded", 0)}
    steps, lineage, stale = extra["steps"], extra["lineage"], extra["stale"]
    # The verdicts _fold_plan already computed for closure.window (one shared helper either way).
    verdicts = extra.get("supersession")
    if verdicts is None:
        verdicts = _supersession(steps, lineage)
    closure = result["closure"]
    buckets, declared_labels = {}, set(anchor.get("declared_labels") or ())
    # Each of these holds (record or id, clipped field names it shows) pairs until _shown applies
    # its bound, so only values the view shows count toward the character-clip note (C8).
    superseded_full, blockers_full, conflicts_full, unmapped_ids = [], [], [], []
    unmapped_count = unmapped_unavailable = 0
    unmapped_totals = {"superseded": 0, "required_open": 0, "completed": 0, "blockers": 0}
    for task_id, step in steps.items():
        entry = lineage.get(task_id)
        resolved = _resolve_unit(entry)
        displayed_req = _bounded_text(resolved["req"], MAX_REQ_CHARS)[0]
        verdict = verdicts[task_id]
        status = _deliverable_status(step.get("status"))
        is_open = status in _OPEN_STATUSES
        # superseded(u) depends only on whether its named successor is itself a currently tracked
        # raw task id and on u not lying on a successor cycle, never on u's own open/closed status
        # (B2); a dangling reference or a cycle is a conflict, not a disposition, and such a row
        # keeps its own open/completed counting.
        is_superseded = verdict["superseded"]
        successor, successor_clipped = ((None, False) if verdict["successor"] is None
                                        else _display_id(verdict["successor"]))
        # Unmapped work (no usable req) is always required, regardless of a parsed kind=
        # enhancement token: unknown work is never optional.
        is_enhancement = resolved["req"] is not None and resolved["kind"] == "enhancement"
        is_blocker = is_open and not is_superseded and not is_enhancement and resolved["blocker"] is not None
        is_stale = bool(stale.get(task_id, False))
        if is_stale:
            closure["stale_in_progress"] += 1
        if status == "completed" and not is_superseded:
            if is_enhancement:
                closure["enhancement_completed"] += 1
            else:
                closure["completed"] += 1
        if is_open and not is_superseded and not is_enhancement:
            closure["required_open"] += 1
        if is_open and not is_superseded and is_enhancement:
            closure["enhancement_open"] += 1
        if is_superseded:
            closure["superseded"] += 1
        if is_blocker:
            closure["blockers"] += 1
        # Every id the view emits is redacted and bounded (F1, C4); task_id itself keeps its raw
        # value for the internal steps/lineage lookups and the supersession identity above.
        display_id, id_clipped = _display_id(task_id)
        label_text = _label_text(step)
        label, label_clipped = _clip(label_text, MAX_LABEL_CHARS) if label_text is not None else ("", False)
        # Every per-key field of this row that its bound shortened; each record below counts only
        # the fields it shows, once per shown value (C8).
        row_clipped = set(resolved["clipped"])
        for field, clipped in (("id", id_clipped), ("label", label_clipped), ("superseded_by", successor_clipped)):
            if clipped:
                row_clipped.add(field)
        unit_record = {"id": display_id, "label": label, "status": status,
                       "kind": resolved["kind"], "kind_raw": resolved["kind_raw"], "from": resolved["from"],
                       "superseded_by": successor, "blocker": resolved["blocker"],
                       "stale_in_progress": is_stale}
        if is_superseded:
            superseded_full.append(({"id": display_id, "successor": successor,
                                     "reason": resolved["reason"], "status": status},
                                    _among(row_clipped, ("id", "superseded_by", "reason"))))
        # At most one conflict per row, in this precedence.
        conflict = None
        if verdict["successor"] is not None and not verdict["observed"]:
            conflict = "successor_unobserved"
        elif verdict["cycle"]:
            conflict = "superseded_cycle"
        elif is_superseded and status == "completed":
            conflict = "completed_and_superseded"
        if conflict is not None:
            conflicts_full.append(({"id": display_id, "req": displayed_req, "reason": conflict,
                                    "successor": successor},
                                   _among(row_clipped, ("id", "req", "superseded_by"))))
        if is_blocker:
            # A bare "true" marker carries no explanation of its own; the blocker's own reason
            # text (when given) stands in for it, otherwise the list shows no reason text. The
            # shown text counts under the field it came from.
            carries_blocker_text = resolved["blocker"].strip().lower() != "true"
            blocker_reason = resolved["blocker"] if carries_blocker_text else resolved["reason"]
            blockers_full.append(({"id": display_id, "req": displayed_req, "label": unit_record["label"],
                                   "reason": blocker_reason},
                                  _among(row_clipped, ("id", "req", "label",
                                                       "blocker" if carries_blocker_text else "reason"))))
        if resolved["req"] is None:
            unmapped_count += 1
            unmapped_unavailable += 1 if entry is None else 0
            # An unmapped row has no unit record: only its id (and the list entries above) can show.
            unmapped_ids.append((display_id, _among(row_clipped, ("id",))))
            # The same per-row predicates as closure (an unmapped row is never an enhancement).
            if is_superseded:
                unmapped_totals["superseded"] += 1
            elif is_open:
                unmapped_totals["required_open"] += 1
            elif status == "completed":
                unmapped_totals["completed"] += 1
            if is_blocker:
                unmapped_totals["blockers"] += 1
            continue
        accum = buckets.setdefault(resolved["req"], _new_requirement_accumulator())
        accum["req_clipped"] = accum["req_clipped"] or "req" in row_clipped
        # A superseded row is never counted under its status (never as completed): counts hold the
        # non-superseded rows, superseded the rest, so units_total == sum(counts) + superseded.
        if is_superseded:
            accum["superseded"] += 1
        else:
            accum["counts"][status] += 1
        if is_open and not is_superseded and not is_enhancement:
            accum["required_open"] += 1
        if is_open and not is_superseded and is_enhancement:
            accum["enhancement_open"] += 1
        if is_blocker:
            accum["blockers"] += 1
        if resolved["kind"] in accum["kinds"]:
            accum["kinds"][resolved["kind"]] += 1
        bucket_status = status if status in accum["units_by_status"] else "pending"
        accum["units_by_status"][bucket_status].append(
            (unit_record, _among(row_clipped, ("id", "label", "kind_raw", "from", "blocker", "superseded_by"))))
    clips = _new_clip_counts()
    requirements, labels_truncated, requirement_clips = _fold_requirement_labels(buckets, declared_labels)
    for field, count in requirement_clips.items():
        clips[field] += count
    result["requirements"] = requirements
    if any(entry["units_total"] > MAX_UNIT_LIST for entry in requirements if not entry["folded"]):
        truncated = True
        notes.append("requirement units bounded to " + str(MAX_UNIT_LIST))
    if labels_truncated:
        truncated = True
        notes.append("requirement labels bounded to " + str(MAX_REQUIREMENT_LABELS))
    result["unmapped"] = {"count": unmapped_count, "metadata_unavailable": unmapped_unavailable,
                          "ids": _shown(unmapped_ids, MAX_ID_LIST, clips), **unmapped_totals}
    if len(unmapped_ids) > MAX_ID_LIST:
        truncated = True
        notes.append("unmapped ids bounded to " + str(MAX_ID_LIST))
    result["superseded"] = _shown(superseded_full, MAX_UNIT_LIST, clips)
    result["superseded_total"] = len(superseded_full)
    if len(superseded_full) > MAX_UNIT_LIST:
        truncated = True
        notes.append("superseded list bounded to " + str(MAX_UNIT_LIST))
    result["blockers"], result["blockers_total"] = _shown(blockers_full, MAX_UNIT_LIST, clips), len(blockers_full)
    if len(blockers_full) > MAX_UNIT_LIST:
        truncated = True
        notes.append("blockers list bounded to " + str(MAX_UNIT_LIST))
    result["conflicts"] = _shown(conflicts_full, MAX_UNIT_LIST, clips)
    result["conflicts_total"] = len(conflicts_full)
    closure["conflicts"] = len(conflicts_full)
    if len(conflicts_full) > MAX_UNIT_LIST:
        truncated = True
        notes.append("conflicts list bounded to " + str(MAX_UNIT_LIST))
    # A character bound that shortened a value the view shows is disclosed like any other bound: one
    # note, appended last, counting each shown shortened value under its per-key field (C8).
    if any(clips.values()):
        truncated = True
        notes.append("character bounds clipped: " + ", ".join(
            field + " " + str(clips[field]) for field in _CLIP_FIELDS if clips[field]))
    result["limits"] = {"truncated": truncated, "notes": notes[:MAX_LIMIT_NOTES]}
    return result


# Provenance and stage evidence (additive, read-only). Both objects are computed from controller state and
# the room's own saved records before the transcript is opened, so a source-unavailable view still carries
# them; only provenance's transcript-derived fields are filled in after a successful read. Neither names a
# path, a session id, transcript content or a credential, and a stage "error" status carries no detail; the
# only free text in either object is provenance.latest_engineer_request.semantic_status.reason, the
# controller's saved semantic-status reason for that request (the saved error summary for an unclassified
# failure), redacted and bounded like the rest of the view. Nothing in them is inferred, completed or
# rewritten.
_ENGINEERING_PURPOSES = ("implementation", "correction")
_LIVE_REQUEST_STATES = ("submitted", "running")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# Saved engineering records and verification checkpoints embed a full candidate snapshot. A record above
# this bound cannot be confirmed and reports its stage as an error; it is never read without a bound.
MAX_STAGE_RECORD_BYTES = 64 * 1024 * 1024
# Any failure reading or checking saved stage evidence maps to the stage's "error" status, never its text.
_STAGE_ERRORS = (RoomError, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError)
PROVENANCE_MEANING = (
    "The plan and last_text are authored only by the engineer during its own turns in its registered native "
    "session, so when the selected request is not that engineer's turn they describe the engineer's work rather "
    "than the selected request, and the controller never refreshes, infers or rewrites them.")
STAGES_MEANING = (
    "Each stage reports the controller's saved records checked only against their recorded digests, and "
    "identity_match compares saved identities without reading the live worktree, so no stage is inferred, "
    "completed or rewritten.")
CURRENTNESS = {"checked": False, "worktree": "unknown", "basis": "saved_identities_only"}


def _sha256_or_none(value):
    """A recorded digest in its exact lowercase-hex form; any other value is never shown as an identity."""
    return value if isinstance(value, str) and _SHA256_HEX.fullmatch(value) else None


def _finite_or_none(value):
    """A recorded number such as created_at; a bool, NaN, an infinity or a non-number is None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _revision_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _identity_match(saved, current):
    """matches or mismatched for two known saved identities; unknown when either is unknown."""
    if saved is None or current is None:
        return "unknown"
    return "matches" if saved == current else "mismatched"


def _stage_identity_match(state, anchor, engineering_candidate, candidate, record_sha256, spec_sha256):
    """The three-way rule shared by the verification and acceptance stages.

    The stage's candidate is compared with the latest captured engineering candidate, its recorded spec record
    digest with the room's current spec record digest, and its recorded spec digest with the current spec digest:
    mismatched when any known pair differs, matches only when all three are known and equal, otherwise unknown.
    """
    checks = (_identity_match(candidate, engineering_candidate),
              _identity_match(record_sha256, _sha256_or_none(state.get("spec_record_sha256"))),
              _identity_match(spec_sha256, _sha256_or_none(anchor.get("spec_sha256"))))
    if "mismatched" in checks:
        return "mismatched"
    return "matches" if all(check == "matches" for check in checks) else "unknown"


def _binding_role(state, request):
    """The single binding role whose bound session sent the request; the session id is never emitted."""
    session = request.get("session_id")
    bindings = state.get("bindings") if isinstance(state.get("bindings"), dict) else {}
    roles = [role for role in ("engineer", "reviewer")
             if isinstance(session, str) and session and isinstance(bindings.get(role), dict)
             and bindings[role].get("session_id") == session]
    return roles[0] if len(roles) == 1 else "unknown"


def _semantic_status_view(value):
    """The saved semantic status as bounded, redacted {kind, hold, reason} and whether a bound clipped it.

    A legacy bare status string is shown as its kind; any other non-mapping is None.
    """
    if isinstance(value, str):
        value = {"kind": value}
    if not isinstance(value, dict):
        return None, False
    kind, kind_clipped = _bounded_text(value.get("kind"), MAX_KEY_CHARS)
    reason, reason_clipped = _bounded_text(value.get("reason"), MAX_REASON_CHARS)
    hold = value.get("hold") if isinstance(value.get("hold"), bool) else None
    return {"kind": kind, "hold": hold, "reason": reason}, kind_clipped or reason_clipped


def _provenance(state, requests, request, explicit):
    """Who authored the plan and last_text and how they relate to the selected request, before any read.

    The transcript-derived fields start unavailable; _observe_provenance fills them after a read.
    """
    role = _binding_role(state, request)
    engineer_turn = role == "engineer"
    engineer = [item for item in requests.values()
                if isinstance(item, dict) and _binding_role(state, item) == "engineer"]
    latest = max(engineer, key=_request_order) if engineer else None
    latest_view, truncated = None, False
    if latest is not None:
        latest_id, id_clipped = _bounded_text(latest.get("request_id"), MAX_ID_CHARS)
        purpose, purpose_clipped = _bounded_text(latest.get("purpose"), MAX_KEY_CHARS)
        recorded, state_clipped = _bounded_text(latest.get("state"), MAX_KEY_CHARS)
        semantic, semantic_clipped = _semantic_status_view(latest.get("semantic_status"))
        latest_view = {"request_id": latest_id, "purpose": purpose, "state": recorded,
                       "created_at": _finite_or_none(latest.get("created_at")), "semantic_status": semantic}
        truncated = id_clipped or purpose_clipped or state_clipped or semantic_clipped
    return {"request_selection": "explicit" if explicit else "latest",
            "selected_request_role": role,
            "selected_request_is_engineer_turn": engineer_turn,
            "selected_request_created_at": _finite_or_none(request.get("created_at")),
            "latest_engineer_request": latest_view,
            "selected_is_latest_engineer_request": latest is not None and latest is request,
            "plan": {"author": "engineer", "source": "unavailable", "updated_at": None, "scope": None,
                     "refreshable_by_selected_request": engineer_turn and request.get("state") in _LIVE_REQUEST_STATES,
                     "activity": "unknown"},
            "turn_anchor_reason": "source_unavailable" if engineer_turn else "selected_request_not_engineer_turn",
            "last_text": {"author": "engineer", "scope": None},
            "meaning": PROVENANCE_MEANING,
            "truncated": truncated}


def _observe_provenance(provenance, plan, updated_row, anchor_row, turn_rows, last_text):
    """Fill provenance's transcript-derived fields after the registered engineer source was read.

    The plan scope is "turn" only when the row of its last change lies inside the selected request's own
    turn; no ordering between plan.updated_at and any request timestamp is computed.
    """
    observed = plan.get("source") != "none"
    updated_at, clipped = _bounded_text(plan.get("updated_at"), MAX_KEY_CHARS) if observed else (None, False)
    in_turn = anchor_row is not None and any(row is updated_row for row in turn_rows or ())
    provenance["plan"].update(source="registered_engineer_native_source", updated_at=updated_at,
                              scope=("turn" if in_turn else "window") if observed else None,
                              activity="observed" if observed else "unknown")
    if anchor_row is not None:
        provenance["turn_anchor_reason"] = None
    elif provenance["selected_request_is_engineer_turn"]:
        provenance["turn_anchor_reason"] = "anchor_not_found"
    provenance["last_text"]["scope"] = None if last_text is None else "turn" if anchor_row is not None else "window"
    provenance["truncated"] = provenance["truncated"] or clipped


def _saved_record(directory, relative):
    """One room-owned JSON record named by a relative path inside the room, read with the owned-file discipline."""
    from ao_delegate_launcher import owned_bytes
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RoomError("Saved stage record is unavailable")
    return json.loads(owned_bytes(Path(directory) / relative, MAX_STAGE_RECORD_BYTES))


def _record_digest(value):
    from ao_project_room import digest
    return digest(value)


def _engineering_record(directory, request):
    """(record, digest_valid) for a request's captured engineering record.

    digest_valid is None when the request names no record, and False when the named record is missing,
    unreadable, oversized or differs from its recorded digest; the record is then withheld.
    """
    if request is None or (request.get("engineering_record") is None
                           and request.get("engineering_record_sha256") is None):
        return None, None
    try:
        record = _saved_record(directory, request.get("engineering_record"))
        valid = _record_digest(record) == request.get("engineering_record_sha256")
    except _STAGE_ERRORS:
        return None, False
    return (record if valid else None), valid


def _engineering_stage(directory, requests, request):
    """The latest implementation or correction request's captured record, plus its confirmed candidate."""
    engineering = [item for item in requests.values()
                   if isinstance(item, dict) and item.get("purpose") in _ENGINEERING_PURPOSES]
    latest = max(engineering, key=_request_order) if engineering else None
    stage = {"status": "missing", "request_id": None, "candidate_sha256": None, "report_sha256": None,
             "selected_request_has_record": False, "digest_valid": None}
    clipped = False
    if latest is not None:
        stage["request_id"], clipped = _bounded_text(latest.get("request_id"), MAX_ID_CHARS)
        record, stage["digest_valid"] = _engineering_record(directory, latest)
        if stage["digest_valid"] is not None:
            candidate = _sha256_or_none(latest.get("result_candidate_sha256"))
            saved = record.get("candidate") if isinstance(record, dict) else None
            report = _sha256_or_none(record.get("report_sha256")) if isinstance(record, dict) else None
            if candidate is not None and report is not None and isinstance(saved, dict) \
                    and saved.get("sha256") == candidate:
                stage.update(status="captured", candidate_sha256=candidate, report_sha256=report)
            else:
                stage["status"] = "error"
        elif latest.get("engineering_error") or latest.get("normalization_capture_error"):
            stage["status"] = "error"  # A recorded capture failure; its text is never shown.
    if request is latest:
        stage["selected_request_has_record"] = stage["digest_valid"] is True
    else:
        stage["selected_request_has_record"] = _engineering_record(directory, request)[1] is True
    return stage, clipped, stage["candidate_sha256"]


def _verification_stage(directory, state, engineering_candidate, anchor):
    """The latest recorded verification attempt and its saved checkpoint.

    A drifted spec keeps the recorded status and identities and reports identity_match as mismatched; a pending
    attempt has no checkpoint yet, so its spec identities are unknown and it never matches.
    """
    stage = {"status": "missing", "recorded_status": None, "request_id": None, "candidate_sha256": None,
             "spec_sha256": None, "spec_record_sha256": None, "gates": None, "identity_match": "unknown",
             "digest_valid": None}
    entries = state.get("verifications")
    if not isinstance(entries, list) or not entries:
        return stage, False
    entry = entries[-1] if isinstance(entries[-1], dict) else {}
    recorded = entry.get("state")
    stage["recorded_status"], clipped = _bounded_text(recorded, MAX_KEY_CHARS)
    candidate = _sha256_or_none(entry.get("candidate_sha256"))
    stage["status"] = "error"
    if recorded == "running" and candidate is not None:
        stage.update(status="pending", candidate_sha256=candidate)
    elif recorded in ("passed", "failed"):
        passed = recorded == "passed"
        try:
            checkpoint = _saved_record(directory, entry.get("path"))
            # Only a passed checkpoint has a recorded digest; a failed one is read but has none to confirm.
            valid = _record_digest(checkpoint) == state.get("checkpoint_sha256") if passed else None
        except _STAGE_ERRORS:
            checkpoint, valid = None, (False if passed else None)
        stage["digest_valid"] = valid
        gates = checkpoint.get("gates") if isinstance(checkpoint, dict) else None
        if (valid is not False and isinstance(gates, list) and checkpoint.get("passed") is passed
                and checkpoint.get("id") == entry.get("id") and candidate is not None
                and checkpoint.get("candidate_sha256") == candidate
                and (not passed or state.get("checkpoint") in (None, entry.get("path")))):
            stage.update(status=recorded, candidate_sha256=candidate, gates=len(gates),
                         spec_sha256=_sha256_or_none(checkpoint.get("spec_sha256")),
                         spec_record_sha256=_sha256_or_none(checkpoint.get("spec_record_sha256")))
    if stage["status"] != "error":
        stage["identity_match"] = _stage_identity_match(state, anchor, engineering_candidate, candidate,
                                                        stage["spec_record_sha256"], stage["spec_sha256"])
    return stage, clipped


def _spec_identity(directory, record_sha256, anchor):
    """(revision, spec_sha256) of the saved spec record with this exact digest; (None, None) if unavailable."""
    if record_sha256 is None:
        return None, None
    if record_sha256 == anchor.get("spec_record_sha256"):
        return _revision_or_none(anchor.get("spec_revision")), _sha256_or_none(anchor.get("spec_sha256"))
    import ao_workflow
    try:
        spec = ao_workflow.spec_record_file(directory, record_sha256)
    except _STAGE_ERRORS:
        return None, None
    if not isinstance(spec, dict):
        return None, None
    return _revision_or_none(spec.get("revision")), _sha256_or_none(spec.get("sha256"))


def _review_status(directory, review):
    """An acceptance review without a recorded acceptance, from its recorded state and saved verdict."""
    recorded = review.get("state")
    if recorded == "uncertain":
        return "uncertain"
    if recorded in _LIVE_REQUEST_STATES:
        return "pending"
    if recorded != "completed":
        return "error"
    import ao_workflow
    try:
        # The controller's own receipt-digest, native-identity and verdict-format checks; read-only.
        verdict = ao_workflow.final_json(directory, review)
    except _STAGE_ERRORS:
        return "error"
    decision = verdict.get("decision") if isinstance(verdict, dict) else None
    # An approving verdict still awaits the recorded acceptance, so the stage stays pending.
    return "rejected" if decision == "rejected" else "pending" if decision == "approved" else "error"


def _acceptance_stage(directory, state, requests, engineering_candidate, anchor):
    """The latest recorded acceptance, or the newer acceptance review that has none yet."""
    stage = {"status": "missing", "recorded_status": None, "request_id": None, "candidate_sha256": None,
             "spec_revision": None, "spec_sha256": None, "identity_match": "unknown"}
    accepted = state.get("acceptances")
    accepted = accepted[-1] if isinstance(accepted, list) and accepted else None
    reviews = [item for item in requests.values() if isinstance(item, dict) and item.get("role") == "reviewer"
               and item.get("purpose") in (None, "acceptance_review")]
    review = max(reviews, key=_request_order) if reviews else None
    if accepted is None and review is None:
        return stage, False
    if accepted is not None and (review is None or (isinstance(accepted, dict)
                                                    and accepted.get("request_id") == review.get("request_id"))):
        if not isinstance(accepted, dict):
            stage["status"] = "error"
            return stage, False
        status, identities, request_id = "approved", accepted, accepted.get("request_id")
    else:
        status = _review_status(directory, review)
        identities = review.get("review") if isinstance(review.get("review"), dict) else {}
        request_id = review.get("request_id")
    stage["status"] = status
    stage["request_id"], id_clipped = _bounded_text(request_id, MAX_ID_CHARS)
    stage["recorded_status"], state_clipped = (_bounded_text(review.get("state"), MAX_KEY_CHARS)
                                               if review is not None else (None, False))
    if status != "error":
        candidate = _sha256_or_none(identities.get("candidate_sha256"))
        spec_sha256 = _sha256_or_none(identities.get("spec_sha256"))
        record_sha256 = _sha256_or_none(review.get("spec_record_sha256")) if review is not None else None
        revision, record_spec_sha256 = _spec_identity(directory, record_sha256, anchor)
        # A revision is attributed only when the reviewed spec record names the same spec bytes.
        stage.update(candidate_sha256=candidate, spec_sha256=spec_sha256,
                     spec_revision=revision if record_spec_sha256 is not None and record_spec_sha256 == spec_sha256
                     else None)
        stage["identity_match"] = _stage_identity_match(state, anchor, engineering_candidate, candidate,
                                                        record_sha256, spec_sha256)
    return stage, id_clipped or state_clipped


def _stages(directory, state, requests, request, anchor):
    """Saved engineering, verification and acceptance evidence, each checked only against recorded digests."""
    engineering, engineering_clipped, candidate = _engineering_stage(directory, requests, request)
    verification, verification_clipped = _verification_stage(directory, state, candidate, anchor)
    acceptance, acceptance_clipped = _acceptance_stage(directory, state, requests, candidate, anchor)
    return {"engineering": engineering, "verification": verification, "acceptance": acceptance,
            "currentness": dict(CURRENTNESS), "meaning": STAGES_MEANING,
            "truncated": engineering_clipped or verification_clipped or acceptance_clipped}


def inspect(directory, state, request_id=None, max_text_chars=400):
    """One request's bounded progress view; a validation failure yields controller facts only."""
    if (not isinstance(max_text_chars, int) or isinstance(max_text_chars, bool)
            or not 0 <= max_text_chars <= MAX_TEXT_CHARS):
        raise RoomError("max_text_chars must be an integer between 0 and " + str(MAX_TEXT_CHARS))
    requests = state.get("requests") or {}
    if request_id is None:
        eligible = [request for request in requests.values() if isinstance(request, dict)]
        if not eligible:
            raise RoomError("The room has no requests")
        request = max(eligible, key=_request_order)
    else:
        request = requests.get(request_id)
        if not isinstance(request, dict):
            raise RoomError("Unknown request_id " + str(request_id))
    view = {"version": VERSION, "room_id": state.get("room_id"), "request": _request_view(request),
            "source": {"available": False}, "turn": {"anchor_found": False},
            "launches": [], "plan": {"source": "none", "updated_at": None, "steps": [],
                                     "counts": {status: 0 for status in _STEP_STATUSES},
                                     "total": 0, "shown": 0,
                                     "omitted": {status: 0 for status in _STEP_STATUSES},
                                     "groups": []},
            "malformed_rows": 0}
    outcome_source = state.get("native_outcome_source") or {}
    view["rate_limits"] = ao_rate_limits.latest(outcome_source.get("database"), request.get("session_id"))
    anchor, anchor_notes, anchor_truncated = _spec_anchor(directory, state)
    empty_extra = {"steps": {}, "lineage": {}, "stale": {}, "window": {"created": 0, "completed": 0, "superseded": 0}}
    view["deliverables"] = _deliverables(False, empty_extra, anchor, anchor_notes, anchor_truncated,
                                         unavailable_reason="transcript unavailable")
    # Controller facts only, computed before the transcript is opened so an unavailable source keeps them.
    view["provenance"] = _provenance(state, requests, request, explicit=request_id is not None)
    view["stages"] = _stages(directory, state, requests, request, anchor)
    try:
        # The bounded tail read in _window needs no file size cap; the dispatch default stays.
        validated = ao_native_outcome.validate_registered_source(directory, state, transcript_size_limit=None)
        rows, malformed = _window(validated["source"]["transcript"], validated["source"]["native_session_id"])
    except (RoomError, OSError) as exc:
        view["source"]["reason"] = _redact(str(exc))[:200]
        return view
    # The anchor is time-bounded: from the request's observed start (or its creation, with
    # a small grace) until the next same-session request's creation. Identical request
    # texts then correlate by when they were delivered rather than by matching order.
    observed = request.get("observed_turn")
    started = observed.get("startedAt") if isinstance(observed, dict) else None
    # A malformed anchor degrades to the request's own created_at (or None), like every other
    # timestamp read in this module, rather than raising.
    start = None
    if isinstance(started, str):
        try:
            start = ao_native_outcome.timestamp(started)
        except RoomError:
            start = None
    if start is None:
        start = request.get("created_at")
    order = _request_order(request)
    later = [other["created_at"] for other in requests.values()
             if isinstance(other, dict) and other.get("session_id") == request.get("session_id")
             and _request_order(other) > order
             and isinstance(other.get("created_at"), (int, float))]
    end = min(later) if later else None
    uses, results = _tools(rows)
    view["source"] = {"available": True}
    view["malformed_rows"] = malformed
    anchor_row, turn_rows = _turn_window(rows, request, start, end)
    view["turn"] = {"anchor_found": False} if anchor_row is None else _turn(anchor_row, turn_rows)
    if anchor_row is not None:
        # Launches belong to the request's own turn; without an anchor nothing is attributable.
        launch_uses, launch_results = _tools(turn_rows)
        view["launches"] = _launches(launch_uses, launch_results)[-MAX_LAUNCHES:]
    # Staleness measures against the transcript's own clock — the turn's last activity —
    # the same clock the view uses for elapsed_seconds.
    plan_now = None
    try:
        plan_now = ao_native_outcome.timestamp(view["turn"].get("last_activity_at"))
    except RoomError:
        plan_now = None
    view["plan"], fold_extra = _fold_plan(rows, uses, results, launches=view["launches"], now=plan_now,
                                          launches_known=bool(view["turn"].get("anchor_found")))
    available = bool(view["source"].get("available")) and view["plan"]["source"] != "none"
    reason = None if available else "no task tool activity observed"
    view["deliverables"] = _deliverables(available, fold_extra, anchor, anchor_notes, anchor_truncated,
                                         unavailable_reason=reason)
    if max_text_chars:
        # With an anchor the text belongs to the request's own turn; a turn that has
        # produced only tool calls has no assistant text yet. Without an anchor the
        # whole-window tail remains the only view available.
        text = _last_text(turn_rows if anchor_row is not None else rows)
        view["last_text"] = _redact(text)[:max_text_chars] if text is not None else None
    _observe_provenance(view["provenance"], view["plan"], fold_extra.get("updated_row"), anchor_row, turn_rows,
                        view.get("last_text"))
    return view
