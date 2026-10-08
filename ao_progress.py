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


def _redact(text):
    """Absolute paths never leave the view; a slash inside a word keeps relative paths intact."""
    return re.sub(r"(?<![^\s'\"(])/[^\s'\"]+", "<path>", str(text))


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


def _step_label(step):
    label = step.get("activeForm") if step.get("status") == "in_progress" and step.get("activeForm") \
        else step.get("subject")
    return _redact(label)[:MAX_LABEL_CHARS] if label is not None else ""


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


def _fold_plan(rows, uses, results, launches=None, now=None, launches_known=False):
    steps, step_updated = {}, {}
    source, updated_at = "none", None
    for row in rows:
        stamp = row.get("timestamp")
        for block in _blocks(row):
            if not isinstance(block, dict):
                continue
            kind = None
            if block.get("type") == "tool_use" and block.get("name") == "TodoWrite":
                todos = (block.get("input") or {}).get("todos") if isinstance(block.get("input"), dict) else None
                if isinstance(todos, list):
                    steps, step_updated = {}, {}
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
                            kind = "task_tools"
                    elif key in steps:
                        for field in ("subject", "activeForm", "status"):
                            if given.get(field) is not None:
                                steps[key][field] = given[field]
                        step_updated[key] = stamp
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
                        kind = "task_tools"
            if kind is not None:
                source, updated_at = kind, stamp
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
    output, shown = [], {status: 0 for status in _STEP_STATUSES}
    for step in chosen:
        status = step.get("status")
        if status in shown:
            shown[status] += 1
        stale = False
        code = _leading_code(step.get("subject"))
        if (launches_known and status == "in_progress" and code is not None
                and code not in launch_codes and now is not None):
            # Unknown is never stale: a step without a tracked update stamp keeps False.
            step_stamp = step_updated.get(step["id"])
            try:
                updated_epoch = (ao_native_outcome.timestamp(step_stamp)
                                 if step_stamp is not None else None)
            except RoomError:
                updated_epoch = None
            stale = updated_epoch is not None and now - updated_epoch > STALE_IN_PROGRESS_SECONDS
        output.append({"label": _step_label(step),
                       "status": status if status is not None else "pending",
                       "stale_in_progress": stale})
    return {"source": source, "updated_at": updated_at, "steps": output, "counts": counts,
            "total": total, "shown": len(output),
            "omitted": {status: counts[status] - shown[status] for status in _STEP_STATUSES},
            "groups": _plan_groups(steps.values())}


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
    start = (ao_native_outcome.timestamp(started) if isinstance(started, str)
             else request.get("created_at"))
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
    view["plan"] = _fold_plan(rows, uses, results, launches=view["launches"], now=plan_now,
                              launches_known=bool(view["turn"].get("anchor_found")))
    if max_text_chars:
        # With an anchor the text belongs to the request's own turn; a turn that has
        # produced only tool calls has no assistant text yet. Without an anchor the
        # whole-window tail remains the only view available.
        text = _last_text(turn_rows if anchor_row is not None else rows)
        view["last_text"] = _redact(text)[:max_text_chars] if text is not None else None
    return view
