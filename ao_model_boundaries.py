"""Frozen worker expectations for one request boundary, their strict reader and boundary notices.

W2 request/delivery unit. Nothing here sends, syncs, writes room state, calls a model or claims a
served identity, provider availability, entitlement or effective effort.

``freeze`` runs at the actual new normal engineer request boundary in ``ao_room_send`` for a
delegation-capable implementation/correction request, after the same-key idempotent return and
before the existing outcome gate can persist observations, before any intent and before POST. It
reads the authenticated W1 routing helper (``ao_routing.effective_worker_selection``, which now also
applies the predecessor's fresh reported-version consistency check), never the current private
pointer or raw runtime files alone, and returns a versioned ``native_worker_expectations`` value
binding the exact worker role map, the recorded family intents, configured MAX effort, the effective
routing digest/version, the retained qualification digest/record and the immutable
preparation/routing-refresh authority. A room with no native routing record has no enabled workers
to freeze and keeps its existing path; an enabled but unqualified worker selection refuses with the
supported explicit-refresh readiness.

``read`` is the strict reusable read-only consumer for one retained request's frozen expectation. It
validates schema, digest, exact map, roles, family intents, effort and ancestry against that
request's OWN historical routing epoch and the room-retained source bytes, even after a later
refresh. It never reads today's mutable runtime files, the effective executable, the active private
selection or any new-readiness input. Before it returns a trusted frozen expectation OR an authentic
historical absence it authenticates the caller's copy, the retained request and that request's OWN
persisted receipt through the existing bounded selected-receipt reader: its content-addressed safe
pointer, an owned no-follow bounded receipt file, identity fields including the purpose and each
field's actual presence, the exact sent text, the receipt's own configured settings/turn/provider
identity, the unique owned user message, the conditional prompt-projection binding and, for an
already settled failure, its own settlement proof. A genuinely attributable completed or failed
native receipt -- including a persisted failed receipt whose request is still uncertain before
settlement -- establishes only the selected request, its frozen expectation and its input delivery;
it is never successful execution, final acceptance or release authority. A missing, torn, mismatched,
unsupported or unfinished delivery is never observation authority. It returns an explicit
legacy absence only for a genuinely absent key; an explicit null, empty or malformed value is an
error. Unknown or unqualified evidence never validates merely because a dictionary is empty.

``readiness`` is the pure new-dispatch phase ``ao_room_send`` runs for every normal engineer
new-request purpose before any intent or POST, including a read-only specification review. It reuses
the audited executable-binding lane and the predecessor's fresh reported-version consistency check to
validate this room's own effective executable against the root epoch's own retained and currently
bundled compatibility floor. A root never inherits worker qualification or the active private
default; worker qualification readiness stays in ``freeze`` and applies only to delegation-capable
implementation/correction work. A readiness refusal creates no request intent, POST, source, policy,
authority or counter change, while the enclosing room lock's existing diagnostic path may still
invalidate a retained history-reconciliation proof exactly as for any other refused locked operation.

Boundary notices disclose the CURRENT effective committed root boundary and the CURRENT effective
committed worker boundary, independently and once each. An earlier carried notice is authenticated
against its own committed authority, the owning request's verified receipt and that request's own
authenticated dispatch order -- a later observation resolution never rewrites a notice already
delivered -- before it counts as delivered, and only genuinely undelivered notices are carried.
Pending, abandoned and unclaimed transition or refresh records are no notice.
"""

import copy
import re

import ao_routing
from ao_prompt_metrics import digest
from room import RoomError

VERSION = 1
EFFORT = "max"
DELEGATING_PURPOSES = ("implementation", "correction")
NOTICE_PURPOSES = ("spec_review", "implementation", "correction")
ROOT_CHANGE = "root_model_change"
ROOT_RESET = "root_model_reset"
WORKER_ROUTING = "worker_routing"
NOTICE_KINDS = (ROOT_CHANGE, ROOT_RESET, WORKER_ROUTING)
NOTICE_VERSION = 1
TRANSITION_AUTHORITY = "engineering_model_transition"
AUTHORITY_KINDS = ("preparation", "routing_refresh")
EXPECTATION_FIELDS = frozenset((
    "version", "purpose", "session_id", "routing_version", "routing_sha256", "authority",
    "qualification_sha256", "qualification_record", "preparation", "preparation_sha256",
    "agents", "agent_selection", "worker_families", "effort", "basis"))
NOTICE_FIELDS = frozenset(("version", "kind", "authority", "fragment_sha256"))
ROOT_AUTHORITY_FIELDS = frozenset(("kind", "request_id", "record_sha256"))
WORKER_AUTHORITY_FIELDS = frozenset((
    "kind", "session_id", "preparation", "preparation_sha256", "routing_refresh", "routing_sha256",
    "routing_version", "qualification_sha256", "qualification_record"))
BASIS = ("The routing record's own authenticated selector map, family intents, configured MAX effort, effective "
         "routing digest/version, room-retained qualification and immutable preparation/refresh authority in force; "
         "configured intent only, never an attributed served worker identity, provider availability or effective effort")
MALFORMED = ("Frozen worker expectations are present but empty, malformed or unsupported; preserve the request and "
             "diagnose it, never read it as an absent historical field")
NOT_DELEGATING = ("Frozen worker expectations belong only to a delegation-capable implementation or correction "
                  "engineer request of this room")
NOT_RETAINED = ("A retained engineer request of this room is required to read frozen worker expectations; preserve "
                "and diagnose the copy instead of trusting it")
UNPROVEN = ("A frozen worker expectation or an authentic historical absence requires this request's own persisted "
            "native receipt read through the bounded selected-receipt reader, its owner/turn/provider/settings "
            "identity, the unique owned sent message and the matching prompt-projection binding; an unfinished, "
            "missing, changed, unsupported or mismatched delivery is not completed observation authority")
EXECUTABLE_REQUIRED = ("The current engineering root's compatibility floor requires concrete effective Claude "
                       "executable evidence, and this room retains no executable identity to check; use the audited "
                       "executable binding lane or a fresh preparation before dispatch")
NOTICE_MALFORMED = ("A carried boundary notice is malformed or unsupported; preserve and diagnose it before any "
                    "further disclosure")
NOTICE_UNCOMMITTED = ("A carried boundary notice does not name this room's own committed boundary; pending, "
                      "abandoned and unclaimed evidence is no notice")
NOTICE_MISMATCH = ("A carried boundary notice does not match its own committed boundary and exact fragment; "
                   "preserve and diagnose it")
NOTICE_CONTEXT = ("A carried boundary notice is authenticated against the exact request that carried it: the "
                  "room's own retained receipt-bound record, its authenticated dispatch order and its "
                  "receipt-bound carried entry; a stale or foreign request copy, a borrowed notice or an "
                  "unauthenticated order is never that context")
NOTICE_ORDER_REQUIRED = ("A root notice of this unqualified family epoch renders differently before and after its "
                         "observation resolution, so its fragment requires the carrying request's authenticated "
                         "dispatch order; supply the owning request instead of guessing from today's epoch or a "
                         "matching fragment digest")
HEX = re.compile(r"[0-9a-f]{64}")
MODEL_ID = re.compile(r"claude-[a-z]+(-[0-9]+)+")


def _hex(value):
    return isinstance(value, str) and bool(HEX.fullmatch(value))


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def enabled_worker_families():
    """The exact enabled worker role to family mapping of this routing implementation."""
    return {role: family for role, family in ao_routing.WORKER_FAMILIES.items()}


def _engineer_session(state):
    binding = (state.get("bindings") or {}).get("engineer") or {}
    return binding.get("session_id")


def validate_authority(value):
    """Pure check of one frozen immutable routing authority; returns it or raises."""
    if not isinstance(value, dict) or value.get("kind") not in AUTHORITY_KINDS:
        raise RoomError(MALFORMED)
    if value["kind"] == "preparation":
        if set(value) != {"kind"}:
            raise RoomError(MALFORMED)
        return value
    if (set(value) != {"kind", "path", "sha256"} or not _text(value.get("path"))
            or not _hex(value.get("sha256"))):
        raise RoomError(MALFORMED)
    return value


def validate_worker_expectation(value):
    """Pure shape validation of one versioned frozen worker expectation.

    The exact worker role map must name every enabled worker role with an exact Claude identifier,
    the recorded family intents and enabled family map must equal this routing implementation's own,
    effort must be the configured MAX and the qualification, preparation and routing identities must
    be present. An empty or partial dictionary never validates: unknown or unqualified evidence
    cannot bypass model gating by being empty.
    """
    if not isinstance(value, dict) or set(value) != EXPECTATION_FIELDS:
        raise RoomError(MALFORMED)
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise RoomError(MALFORMED)
    if value["purpose"] not in DELEGATING_PURPOSES:
        raise RoomError(NOT_DELEGATING)
    if not _text(value["session_id"]):
        raise RoomError(MALFORMED)
    if type(value["routing_version"]) is not int or value["routing_version"] not in (1, 2, 3):
        raise RoomError(MALFORMED)
    if not _hex(value["routing_sha256"]):
        raise RoomError(MALFORMED)
    if value["effort"] != EFFORT:
        raise RoomError(MALFORMED)
    families = enabled_worker_families()
    agents, selection = value["agents"], value["agent_selection"]
    if (not isinstance(agents, dict) or set(agents) != set(families)
            or any(not (_text(agents[role]) and MODEL_ID.fullmatch(agents[role])) for role in agents)):
        raise RoomError(MALFORMED)
    if (not isinstance(selection, dict) or set(selection) != set(families)
            or any(selection[role] != {"kind": "family", "family": families[role]} for role in selection)):
        raise RoomError(MALFORMED)
    if value["worker_families"] != families:
        raise RoomError(MALFORMED)
    if not _hex(value["qualification_sha256"]) or not _text(value["qualification_record"]):
        raise RoomError(MALFORMED)
    if not _text(value["preparation"]) or not _hex(value["preparation_sha256"]):
        raise RoomError(MALFORMED)
    if not _text(value["basis"]):
        raise RoomError(MALFORMED)
    validate_authority(value["authority"])
    return value


def authority_in_force(state):
    """The immutable routing authority in force: the exact committed refresh pointer, else the preparation."""
    pointer = state.get("routing_refresh")
    if pointer is None:
        return {"kind": "preparation"}
    if (not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}
            or not _text(pointer.get("path")) or not _hex(pointer.get("sha256"))):
        raise RoomError("The room's committed routing-refresh pointer is malformed; preserve and diagnose")
    return {"kind": "routing_refresh", "path": pointer["path"], "sha256": pointer["sha256"]}


def freeze(directory, state, purpose):
    """The versioned frozen worker expectation a new delegation-capable request records, or None.

    Returns ``None`` without writing anything when this is not a delegation-capable purpose, when the
    room is not a normal one, or when this room has no native routing record at all (no enabled
    workers exist to freeze, and the existing not-configured disclosure still applies). Raises
    RoomError before any intent or POST when an enabled worker selection is not source-qualified, and
    the W1 helper it reads re-applies the predecessor's fresh reported-version consistency check, so
    a pinned or bound executable that no longer reports its recorded version refuses here too.
    """
    import ao_delegates
    import ao_workflow
    if purpose not in DELEGATING_PURPOSES or not ao_workflow.normal(state):
        return None
    binding = (state.get("bindings") or {}).get("engineer")
    if not binding:
        raise RoomError("Bind the prepared native engineering orchestrator first")
    prepared = ao_delegates.validate_preparation(directory, state, binding.get("session_id"), check_routing=False)
    selection = ao_routing.effective_worker_selection(prepared, state, directory)
    if selection is None:
        return None
    if not selection.get("source_qualified"):
        raise RoomError(ao_routing.UNQUALIFIED_ROUTING)
    value = {"version": VERSION, "purpose": purpose, "session_id": binding["session_id"],
             "routing_version": selection["routing_version"], "routing_sha256": selection["routing_sha256"],
             "authority": authority_in_force(state),
             "qualification_sha256": selection["qualification_sha256"],
             "qualification_record": selection["qualification_record"],
             "preparation": selection["preparation"], "preparation_sha256": selection["preparation_sha256"],
             "agents": copy.deepcopy(selection["agents"]),
             "agent_selection": copy.deepcopy(selection["agent_selection"]),
             "worker_families": copy.deepcopy(selection["worker_families"]),
             "effort": selection["effort"], "basis": BASIS}
    validate_worker_expectation(value)
    return value


def _recorded_executable(value):
    """True when one recorded executable identity carries the concrete path, size and mtime to re-probe."""
    return (isinstance(value, dict) and isinstance(value.get("path"), str) and value["path"].startswith("/")
            and type(value.get("size")) is int and type(value.get("mtime_ns")) is int)


def readiness(directory, state, purpose):
    """Pure new-dispatch readiness for one normal engineer request purpose.

    Runs before any request intent or POST for every normal engineer purpose, including a read-only
    specification review. It validates the actual effective executable of this room's own engineering
    root against that root's own retained and currently bundled compatibility floor and against the
    identity recorded for it, reusing the audited executable-binding lane and the predecessor's fresh
    reported-version consistency check. Today's active private family qualification pointer and the
    effective worker selection are never read: worker qualification readiness stays in ``freeze`` and
    applies only to delegation-capable implementation/correction work, and a root never inherits
    worker qualification. A room whose root pins no floor and records no concrete executable identity
    keeps its existing path, so an unsupported or no-routing historic room is never required to gain
    native routing. The helper itself writes nothing; an exception raised here still passes through
    the room lock's existing diagnostic invalidation path, exactly like any other refused locked
    operation, so a retained history-reconciliation proof may be invalidated while no request intent,
    POST, source, policy, authority or counter changes.
    """
    import ao_delegates
    import ao_engineering_model
    import ao_executable_binding
    import ao_routing_refresh
    if purpose not in NOTICE_PURPOSES:
        return None
    if not (state.get("bindings") or {}).get("engineer"):
        return None  # the existing bind-first refusal still applies at this boundary
    prepared = ao_delegates.preparation(directory, state)
    epoch = ao_engineering_model.current(directory, state)
    selector = epoch.get("selector") or {}
    family = selector.get("family") if selector.get("kind") == "family" else None
    minimum = ao_engineering_model.required_minimum(family, epoch.get("qualification"))
    routing = prepared.get("routing")
    replacement = None
    if routing or state.get("executable_binding") is not None:
        replacement = ao_executable_binding.effective(directory, state, prepared)
    evidence = None
    if replacement is not None or _recorded_executable((routing or {}).get("claude")):
        evidence = ao_routing_refresh._current_executable(None, directory, state, routing or {}, replacement)
    if evidence is None:
        if minimum is None:
            return None
        raise RoomError(EXECUTABLE_REQUIRED)
    if minimum is not None:
        observed = ao_engineering_model.parse_version(evidence.get("version"))
        if observed is None or observed < ao_engineering_model.parse_version(minimum):
            raise RoomError((epoch.get("configured_model") or selector.get("model") or "The engineering root")
                            + " requires Claude Code " + minimum + " or newer; the effective executable reports "
                            + str(evidence.get("version") or "no version")
                            + ". Use the audited executable upgrade or repair lane first")
    return evidence


def exact_authority(authority):
    """The exact form ``ao_routing.historical_routing`` authenticates for one frozen expectation authority."""
    validate_authority(authority)
    if authority["kind"] == "preparation":
        return {"kind": "preparation"}
    return {"kind": "routing_refresh", "path": authority["path"], "sha256": authority["sha256"]}


def _authenticated_request(directory, retained):
    """Authenticate one retained request's own persisted receipt, owner/turn/text and projection.

    Reuses the actual bounded selected-receipt reader and the existing request/receipt identity and
    observed-delivery checks: the content-addressed safe pointer, an owned no-follow bounded receipt
    file, the receipt's own configured settings, turn, provider turn and reroute identity, the unique
    owned user message on this request's own turn, the conditional prompt/projection binding and, for
    an already settled failure, its own settlement proof. A genuinely attributable persisted native
    receipt in a completed or failed turn state -- including a failed receipt whose request is still
    uncertain before settlement -- establishes only the selected request/expectation and its input
    delivery, never successful execution, acceptance or release authority. A missing, torn,
    mismatched, unsupported or unfinished receipt, or a native receipt in any other turn state
    (cancelled, interrupted or recovered), is never observation authority, so it raises instead of
    letting a substituted or deleted field read as history. Nothing is reloaded, rewritten or
    repaired here: every check uses the current retained in-memory request and its actual persisted
    receipt.
    """
    import ao_prompt_metrics
    receipt = ao_prompt_metrics._read_selected_receipt(directory, retained.get("request_id"), retained)
    turn = receipt.get("turn") if isinstance(receipt, dict) else None
    if (retained.get("state") not in ("completed", "settled_failure", "uncertain")
            or not isinstance(receipt, dict) or not ao_prompt_metrics._receipt_identity(retained, receipt)
            or not isinstance(turn, dict) or turn.get("state") not in ("completed", "failed")
            or not ao_prompt_metrics._observed_delivery(retained, receipt)):
        raise RoomError(UNPROVEN)
    text = retained.get("text")
    if not isinstance(text, str) or retained.get("text_sha256") != digest(text.encode()):
        raise RoomError(UNPROVEN)
    if "prompt_projection" in retained:
        from ao_prompt_metrics import projection_binding, validate_projection
        projection = validate_projection(retained.get("prompt_projection"), text)
        if projection is None or retained.get("text_sha256") != projection["text_sha256"]:
            raise RoomError(UNPROVEN)
        try:
            binding = projection_binding(retained, projection)
        except RoomError as exc:
            raise RoomError(UNPROVEN) from exc
        if receipt.get("prompt_projection_sha256") != binding:
            raise RoomError(UNPROVEN)
    elif "prompt_projection_sha256" in receipt:
        raise RoomError(UNPROVEN)
    if retained.get("state") == "settled_failure":
        import ao_outcomes
        ao_outcomes.validate_settlement(directory, retained)
    return receipt


def read(directory, state, request):
    """Strict read-only consumer for one retained request's frozen worker expectation.

    Signature: ``read(directory, state, request) -> dict``.

    The caller's copy, the retained request and this request's OWN persisted receipt are authenticated
    through the existing bounded selected-receipt reader before either supported outcome is returned:
    identity fields including the purpose and each field's actual presence, the exact sent text, the
    receipt's own settings/turn/provider identity, the unique owned user message, the conditional
    prompt-projection binding and, for an already settled failure, its own settlement proof. A
    genuinely attributable failed native receipt of a still uncertain request authenticates the same
    selected request/expectation and input delivery without settling the request, turning the failure
    into success or authorizing any release. A missing, torn, mismatched, unsupported or unfinished
    delivery is never observation authority.

    Returns exactly one of:

    * ``{"status": "absent", "request_id": ..., "reason": "no_frozen_worker_expectations"}`` -- only
      for a retained request whose own completed or failed receipt proves the key was genuinely never
      bound; the legacy request stays readable and is never modified.
    * ``{"status": "frozen", "version": 1, "request_id", "session_id", "purpose",
      "expectation": <the exact frozen value>, "agents": {role: exact expected model},
      "agent_selection": {role: {"kind": "family", "family": ...}},
      "worker_families": {role: family}, "effort": "max", "qualification_sha256",
      "qualification_record", "preparation", "preparation_sha256", "authority",
      "routing": {"sha256", "version", "source": "preparation"|"routing_refresh",
      "routing_refresh": {path, sha256}|None}, "basis"}`` -- the frozen value validated against the
      request's own historical routing epoch and the room-retained qualification/source bytes.

    Raises RoomError when the request is not this room's retained engineer request (including a stale
    copy whose identity fields, purpose, key presence or a present expectation differ), when the key
    is present but null/empty/malformed/unsupported, when the retained owner or purpose contradicts
    the frozen value (a forged nondelegating purpose is refused), when this request's own receipt
    does not authenticate its saved owner/turn/text/projection identity, when the frozen authority is
    not this room's own
    preparation or a committed routing-refresh ancestor whose target carries the frozen digest, or
    when the historical routing record's exact map, family intents, effort or qualification identity
    differ. Current mutable runtime files, the effective executable, the active private selection and
    new readiness are never consulted, so a later refresh cannot change an old request's result.
    """
    import ao_delegates
    import ao_engineering_model
    if not isinstance(request, dict) or not _text(request.get("request_id")):
        raise RoomError(NOT_RETAINED)
    retained = (state.get("requests") or {}).get(request["request_id"])
    if not isinstance(retained, dict):
        raise RoomError(NOT_RETAINED)
    # The shared identity check binds the caller copy's purpose and every identity field's actual key
    # presence, so a copied null or a deleted key is never accepted as a substitute for retained history.
    if ao_engineering_model.check_frozen(directory, state, request) is None:
        raise RoomError(NOT_RETAINED)
    value = None
    if "native_worker_expectations" in retained:
        value = validate_worker_expectation(retained["native_worker_expectations"])
        if (retained.get("role") != "engineer" or retained.get("purpose") != value["purpose"]
                or retained.get("session_id") != value["session_id"]):
            raise RoomError(NOT_DELEGATING)
    # Both supported outcomes -- a frozen expectation and an authentic historical absence -- rest on
    # this request's OWN persisted receipt, its owner/turn/settings/provider identity, the unique
    # owned sent message and the conditional projection binding, all read through the bounded
    # selected-receipt reader. A missing, torn, mismatched, unsupported or unfinished delivery is
    # never observation authority, so a substituted or deleted field can never read as genuine
    # history.
    _authenticated_request(directory, retained)
    if value is None:
        return {"status": "absent", "request_id": retained["request_id"],
                "reason": "no_frozen_worker_expectations"}
    prepared = ao_delegates.preparation(directory, state)
    if (value["preparation"] != state.get("preparation")
            or value["preparation_sha256"] != state.get("preparation_sha256")):
        raise RoomError("Frozen worker expectations name another preparation than this room's own retained one")
    reference = {"routing_sha256": value["routing_sha256"], "routing_version": value["routing_version"],
                 "qualification_sha256": value["qualification_sha256"],
                 "qualification_record": value["qualification_record"]}
    found = ao_routing.historical_routing(directory, state, prepared, reference,
                                          authority=exact_authority(value["authority"]))
    routing = found["routing"]
    agents = ao_routing.recorded_agents(routing)
    if (agents != value["agents"] or routing.get("agent_selection") != value["agent_selection"]
            or routing.get("effort") != value["effort"]
            or ao_routing.worker_qualification_reference(routing) != reference):
        raise RoomError("Frozen worker expectations contradict their own historical routing record")
    return {"status": "frozen", "version": value["version"], "request_id": retained["request_id"],
            "session_id": value["session_id"], "purpose": value["purpose"],
            "expectation": copy.deepcopy(value), "agents": copy.deepcopy(value["agents"]),
            "agent_selection": copy.deepcopy(value["agent_selection"]),
            "worker_families": copy.deepcopy(value["worker_families"]), "effort": value["effort"],
            "qualification_sha256": value["qualification_sha256"],
            "qualification_record": value["qualification_record"],
            "preparation": value["preparation"], "preparation_sha256": value["preparation_sha256"],
            "authority": copy.deepcopy(value["authority"]),
            "routing": {"sha256": value["routing_sha256"], "version": value["routing_version"],
                        "source": found["source"], "routing_refresh": copy.deepcopy(found["routing_refresh"])},
            "basis": value["basis"]}


def current_root_boundary(directory, state):
    """The current effective committed root boundary, or None when no transition was committed."""
    import ao_engineering_model
    history = ao_engineering_model.epochs(directory, state)
    epoch = history[-1]
    record = epoch.get("record_sha256")
    if not record:
        return None
    previous = history[-2] if len(history) > 1 else None
    reset = bool(previous) and previous["configured_model"] == epoch["configured_model"]
    return {"kind": ROOT_RESET if reset else ROOT_CHANGE,
            "authority": {"kind": TRANSITION_AUTHORITY, "request_id": epoch["request_id"],
                          "record_sha256": record},
            "epoch": epoch, "reset": reset}


def current_worker_boundary(directory, state):
    """The current effective committed worker boundary, or None without a qualified routing record."""
    import ao_delegates
    import ao_routing_refresh
    if not state.get("preparation"):
        return None
    prepared = ao_delegates.preparation(directory, state)
    pointer = state.get("routing_refresh")
    if pointer is None:
        routing, exact = prepared.get("routing"), {"kind": "preparation"}
    else:
        routing = ao_routing_refresh._read(directory, pointer).get("target")
        exact = {"kind": "routing_refresh", "path": pointer["path"], "sha256": pointer["sha256"]}
    if not isinstance(routing, dict) or routing.get("worker_qualification") is None:
        return None
    reference = ao_routing.worker_qualification_reference(routing)
    found = ao_routing.historical_routing(directory, state, prepared, reference, authority=exact)
    routing = found["routing"]
    authority = {"kind": "preparation" if found["source"] == "preparation" else "routing_refresh",
                 "session_id": _engineer_session(state),
                 "preparation": state.get("preparation"),
                 "preparation_sha256": state.get("preparation_sha256"),
                 "routing_refresh": (copy.deepcopy(found["routing_refresh"])
                                     if found["source"] != "preparation" else None),
                 "routing_sha256": reference["routing_sha256"], "routing_version": reference["routing_version"],
                 "qualification_sha256": reference["qualification_sha256"],
                 "qualification_record": reference["qualification_record"]}
    return {"kind": WORKER_ROUTING, "authority": authority, "routing": routing,
            "agents": ao_routing.recorded_agents(routing)}


def _root_text(boundary):
    authority, epoch = boundary["authority"], boundary["epoch"]
    selector = epoch["selector"]
    label = ("family alias " + selector["family"]) if selector["kind"] == "family" else (
        "exact identifier " + selector["model"])
    qualified = bool(epoch.get("family_qualified"))
    opening = ("Engineering orchestrator boundary (committed " + boundary["kind"] + " record "
               + authority["record_sha256"] + ").")
    if boundary["kind"] == ROOT_RESET:
        opening += (" The same configured value " + epoch["configured_model"] + " was re-committed as a new family"
                    " epoch.")
        if qualified:
            opening += (" Its source-qualified exact expectation stays pinned from retained source evidence before"
                        " inference; only the actually served model and the effective effort remain unobserved.")
        else:
            opening += (" This epoch's exact expectation starts unresolved again until a pre-inference source"
                        " qualification or a verified completed owned turn establishes it.")
    else:
        opening += (" This room's configured engineering orchestrator is now " + label + " (configured value "
                    + epoch["configured_model"] + ").")
    if qualified:
        expectation = ("The source-qualified exact expected model is " + str(epoch.get("expected_model"))
                       + " (qualification " + str(epoch.get("qualification_sha256")) + "). ")
    elif selector["kind"] == "exact":
        expectation = ("The configured exact expected model is " + str(epoch.get("expected_model")) + "; this is a"
                       " configured exact identifier, not a source-qualified family expectation. ")
    elif epoch.get("expected_model"):
        expectation = ("This epoch's exact expected model " + str(epoch["expected_model"]) + " was observed on a"
                       " completed owned turn (resolution " + str(epoch.get("resolution_sha256")) + "); no"
                       " pre-inference source qualification establishes it. ")
    else:
        expectation = ("No pre-inference source qualification and no completed owned turn establish this epoch's"
                       " exact expected model yet. ")
    return (opening + " Effort remains configured MAX. " + expectation
            + "The actually served model and the effective effort remain unobserved. Requests recorded before this "
              "boundary keep the attribution saved with them, including the Fable engineering role; engineering "
              "ownership from this boundary belongs to this room's configured orchestrator and is named only by its "
              "configured value. This notice replays no specification, earlier assignment, completed review or "
              "unrelated policy.")


def _worker_text(boundary):
    authority, agents, routing = boundary["authority"], boundary["agents"], boundary["routing"]
    families = {role: routing["worker_qualification"]["families"][role]["family"] for role in sorted(agents)}
    if authority["kind"] == "preparation":
        opening = ("Native worker boundary (initial source-qualified preparation "
                   + authority["preparation_sha256"] + ").")
    else:
        opening = ("Native worker boundary (committed routing refresh "
                   + authority["routing_refresh"]["sha256"] + ").")
    return (opening + " The enabled native worker roles are pr-opus (family intent " + families["pr-opus"]
            + ") and pr-sonnet (family intent " + families["pr-sonnet"] + ") at configured MAX effort; their "
            "source-qualified exact expected models are " + agents["pr-opus"] + " and " + agents["pr-sonnet"]
            + " from retained qualification " + authority["qualification_sha256"] + ". Actual served child model "
            "identity and effective effort are not attributed by the controller. This notice replays no "
            "specification, earlier assignment or completed review.")


def fragment_text(boundary):
    """The deterministic bounded fragment of one boundary."""
    return _root_text(boundary) if boundary["kind"] in (ROOT_CHANGE, ROOT_RESET) else _worker_text(boundary)


def notice_identity(value):
    """The stable identity of one boundary or carried notice: its kind plus its immutable authority."""
    return digest({"kind": value["kind"], "authority": value["authority"]})


def carried_notice(pending):
    """One versioned carried notice entry: kind, immutable committed authority and exact fragment digest."""
    return {"version": NOTICE_VERSION, "kind": pending["kind"],
            "authority": copy.deepcopy(pending["authority"]),
            "fragment_sha256": digest(pending["fragment"].encode())}


def pending_notices(directory, state, purpose, delivered=()):
    """The boundary notices this exact request must still deliver, in stable order.

    The current effective committed root boundary is disclosed on any normal engineer request
    (including an authorized read-only specification review); the current effective committed worker
    boundary waits for delegation-capable implementation/correction work. Superseded boundaries and
    boundaries already carried by a verified completed request are never replayed.
    """
    if purpose not in NOTICE_PURPOSES:
        return []
    found = [current_root_boundary(directory, state)]
    if purpose in DELEGATING_PURPOSES:
        found.append(current_worker_boundary(directory, state))
    seen = set(delivered or ())
    pending = []
    for boundary in found:
        if boundary is None or notice_identity(boundary) in seen:
            continue
        pending.append({"kind": boundary["kind"], "authority": copy.deepcopy(boundary["authority"]),
                        "fragment": fragment_text(boundary)})
    return pending


def validate_notice(notice):
    """Pure shape validation of one carried boundary notice."""
    if not isinstance(notice, dict) or set(notice) != NOTICE_FIELDS:
        raise RoomError(NOTICE_MALFORMED)
    if type(notice["version"]) is not int or notice["version"] != NOTICE_VERSION:
        raise RoomError(NOTICE_MALFORMED)
    if notice["kind"] not in NOTICE_KINDS:
        raise RoomError(NOTICE_MALFORMED)
    authority = notice["authority"]
    if notice["kind"] in (ROOT_CHANGE, ROOT_RESET):
        if (not isinstance(authority, dict) or set(authority) != ROOT_AUTHORITY_FIELDS
                or authority["kind"] != TRANSITION_AUTHORITY or not _text(authority["request_id"])
                or not _hex(authority["record_sha256"])):
            raise RoomError(NOTICE_MALFORMED)
    else:
        if (not isinstance(authority, dict) or set(authority) != WORKER_AUTHORITY_FIELDS
                or authority["kind"] not in AUTHORITY_KINDS or not _text(authority["session_id"])
                or not _text(authority["preparation"]) or not _hex(authority["preparation_sha256"])
                or not _hex(authority["routing_sha256"])
                or type(authority["routing_version"]) is not int
                or authority["routing_version"] not in (1, 2, 3)
                or not _hex(authority["qualification_sha256"])
                or not _text(authority["qualification_record"])):
            raise RoomError(NOTICE_MALFORMED)
        pointer = authority["routing_refresh"]
        if authority["kind"] == "preparation":
            if pointer is not None:
                raise RoomError(NOTICE_MALFORMED)
        elif (not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}
              or not _text(pointer["path"]) or not _hex(pointer["sha256"])):
            raise RoomError(NOTICE_MALFORMED)
    if not _hex(notice["fragment_sha256"]):
        raise RoomError(NOTICE_MALFORMED)
    return notice


def routing_authority(authority):
    """The exact historical-routing authority one committed worker identity names."""
    if authority["kind"] == "preparation":
        return {"kind": "preparation"}
    return {"kind": "routing_refresh", "path": authority["routing_refresh"]["path"],
            "sha256": authority["routing_refresh"]["sha256"]}


def _root_epoch_at(epoch, order):
    """The exact root-epoch rendering in force at one authenticated dispatch order.

    An unqualified family epoch whose exact expected model was established later by a verified
    observation resolution renders unresolved before that resolution's order and resolved from it,
    the same timing ``ao_engineering_model._frozen`` already expresses (``resolution_order < order``).
    Exact and pre-inference qualified epochs are fixed at creation and never re-read from a later
    resolution. Without an authenticated order an order-dependent epoch is ambiguous, so the reader
    refuses instead of guessing an owner from today's epoch or from a matching fragment digest.
    """
    if (epoch.get("family_qualified") or epoch["selector"]["kind"] == "exact"
            or epoch.get("resolution_order") is None):
        return epoch
    if order is None:
        raise RoomError(NOTICE_ORDER_REQUIRED)
    if epoch["resolution_order"] < order:
        return epoch
    return {**epoch, "expected_model": None, "resolution_sha256": None, "resolution_order": None}


def committed_boundary(directory, state, kind, authority, order=None):
    """Reconstruct one notice's OWN committed boundary, or raise when it is not committed here.

    ``order`` is the authenticated dispatch order of the notice's carrying request. Only an
    unqualified family root epoch renders differently before and after a later observation
    resolution, so it is rendered exactly as of that order; exact, pre-inference qualified and
    worker boundaries ignore the order.
    """
    import ao_delegates
    import ao_engineering_model
    if kind in (ROOT_CHANGE, ROOT_RESET):
        history = ao_engineering_model.epochs(directory, state)
        for index, epoch in enumerate(history):
            if (epoch.get("record_sha256") == authority["record_sha256"]
                    and epoch.get("request_id") == authority["request_id"]):
                previous = history[index - 1] if index else None
                reset = bool(previous) and previous["configured_model"] == epoch["configured_model"]
                if (kind == ROOT_RESET) != reset:
                    raise RoomError(NOTICE_MISMATCH)
                return {"kind": kind, "authority": copy.deepcopy(authority),
                        "epoch": _root_epoch_at(epoch, order), "reset": reset}
        raise RoomError(NOTICE_UNCOMMITTED)
    prepared = ao_delegates.preparation(directory, state)
    if (authority["session_id"] != _engineer_session(state)
            or authority["preparation"] != state.get("preparation")
            or authority["preparation_sha256"] != state.get("preparation_sha256")):
        raise RoomError(NOTICE_MISMATCH)
    reference = {"routing_sha256": authority["routing_sha256"],
                 "routing_version": authority["routing_version"],
                 "qualification_sha256": authority["qualification_sha256"],
                 "qualification_record": authority["qualification_record"]}
    found = ao_routing.historical_routing(directory, state, prepared, reference,
                                          authority=routing_authority(authority))
    if (found["source"] == "preparation") != (authority["kind"] == "preparation"):
        raise RoomError(NOTICE_MISMATCH)
    routing = found["routing"]
    return {"kind": WORKER_ROUTING, "authority": copy.deepcopy(authority), "routing": routing,
            "agents": ao_routing.recorded_agents(routing)}


def _carrying_request(directory, state, request):
    """Authenticate one notice reader's claimed carrying request, returning it with its order.

    The caller's copy must be this room's retained engineer request, its frozen identity and order
    must validate against that request's own recorded engineering epoch, and the request's own
    persisted receipt, sent text, carried record and conditional projection binding must authenticate
    the delivery. A stale or foreign copy, a borrowed notice, an unauthenticated order or a changed
    delivery proof is never notice context, so an ambiguous historical context refuses instead of
    guessing an owner from a matching digest.
    """
    import ao_engineering_model
    if not isinstance(request, dict) or not _text(request.get("request_id")):
        raise RoomError(NOTICE_CONTEXT)
    retained = (state.get("requests") or {}).get(request["request_id"])
    if not isinstance(retained, dict) or (retained is not request and retained != request):
        raise RoomError(NOTICE_CONTEXT)
    if retained.get("state") not in ("completed", "settled_failure"):
        raise RoomError(NOTICE_CONTEXT)
    try:
        epoch = ao_engineering_model.check_frozen(directory, state, retained)
    except RoomError as exc:
        raise RoomError(NOTICE_CONTEXT) from exc
    if epoch is None:
        raise RoomError(NOTICE_CONTEXT)
    order = retained.get("created_order")
    if type(order) is not int or order < 1:
        raise RoomError(NOTICE_CONTEXT)
    carried = retained.get("carried")
    if not isinstance(carried, dict):
        raise RoomError(NOTICE_CONTEXT)
    try:
        receipt = _authenticated_request(directory, retained)
    except RoomError as exc:
        raise RoomError(NOTICE_CONTEXT) from exc
    if receipt.get("carried_sha256") != digest(carried):
        raise RoomError(NOTICE_CONTEXT)
    return retained, order


def _carried_entry(retained, notice):
    """True when one authenticated request's receipt-bound carried record names this notice identity.

    The exact fragment is bound separately by the reader's own digest check, so a substituted digest
    or authority cannot borrow a carried entry merely by keeping the same identity.
    """
    carried = retained.get("carried")
    notices = carried.get("boundary_notices") if isinstance(carried, dict) else None
    if not isinstance(notices, list):
        return False
    identity = notice_identity(notice)
    for entry in notices:
        if (isinstance(entry, dict) and entry.get("kind") in NOTICE_KINDS
                and isinstance(entry.get("authority"), dict) and notice_identity(entry) == identity):
            return True
    return False


def notice_fragment(directory, state, notice, request=None):
    """The deterministic fragment text of one carried notice's own committed boundary.

    Without ``request`` this keeps its explicit standalone meaning for exact, pre-inference qualified
    and worker notices, whose rendering never depends on a later observation resolution; an
    unqualified family root notice whose expectation came from such a resolution is only meaningful at
    its own dispatch order and refuses as ambiguous instead of guessing one.

    With ``request`` the fragment is rendered at that exact authenticated carrying request's order:
    the retained request's identity, order, receipt, sent text, carried record and conditional
    projection binding are verified first, and the notice must be one that request actually carried.
    """
    validate_notice(notice)
    retained, order = (None, None) if request is None else _carrying_request(directory, state, request)
    fragment = fragment_text(committed_boundary(directory, state, notice["kind"], notice["authority"], order))
    if retained is not None and not _carried_entry(retained, notice):
        raise RoomError(NOTICE_CONTEXT)
    return fragment


def delivered_notice(directory, state, notice, request=None):
    """Authenticate one carried notice against its OWN committed authority and exact fragment.

    Returns the notice identity used to decide what is genuinely undelivered. Raises RoomError when
    the notice is malformed, when its authority is not one of this room's own committed boundaries (a
    pending, abandoned or unclaimed transition or refresh is no notice), when its fragment digest does
    not equal the deterministic fragment of that exact boundary at the authenticated dispatch order
    that delivered it, or when a supplied carrying request is not the room's own retained,
    receipt-bound record that actually carried this notice.
    """
    validate_notice(notice)
    fragment = notice_fragment(directory, state, notice, request)
    if digest(fragment.encode()) != notice["fragment_sha256"]:
        raise RoomError(NOTICE_MISMATCH)
    return notice_identity(notice)
