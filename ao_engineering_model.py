"""Engineering orchestrator selection within a qualified model family, and its audited ancestry.

Four facts stay distinct. The selector is intent: a family alias ("fable", "opus") meaning the
latest available member of that family, or an exact identifier kept for compatibility. The
configured AO value is that alias or identifier, at configured MAX intent. A family epoch's
expected exact model comes either from a private operator-selected family qualification, which
pins a qualified family's exact model before inference, or, in a version 2 room with no such
qualification, from verified native evidence of a completed owned turn recorded once as an
observation resolution; otherwise it is unknown. The observation is the exact model on an owned
turn's native assistant stop rows. A new family dispatch requires the pre-inference qualified
expectation: an unresolved or operator-unqualified family epoch refuses with actionable readiness,
while every already-recorded historical request keeps its original reader semantics. Effective
effort is not evidenced.

Stored bindings never change. Epoch 0 is the room's opening selection and each committed
transition record appends one epoch. This module pins the qualification policy per room, reads
and validates the hash-bound transition and resolution journal, and answers which configured
value and exact expectation are in force, live and for each historical request. It never guesses
a version, never calls a model and never claims provider, account or quota availability.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from ao_delegate_launcher import owned_bytes
from room import RoomError

LEGACY_MODEL = "claude-fable-5-1"
DEFAULT_FAMILY = "fable"  # new rooms select the latest available member of the role's existing family
EFFORT = "max"
HARNESS = "claude-code"
SELECTION_KEY = "engineering_model"
POINTER_KEY = "engineering_model_transition"
RESOLUTIONS_KEY = "engineering_model_resolutions"
BASE = "engineering-model-transitions"
RESOLUTION_KIND = "resolutions"
POLICY_BASE = "engineering-model"
CONFIG_KEY = "engineering_models"
QUALIFICATION_KEY = "family_qualification"
FORMAT = "project_room_engineering_models_v2"
FORMAT_V3 = "project_room_engineering_models_v3"
SELECTION_VERSION = 2
SELECTION_VERSION_V3 = 3
QUALIFICATION_BASE = POLICY_BASE + "/qualifications"
EXECUTION_QUALIFICATION_VERSION = 1
MEANING = ("Claude engineering selectors qualified at max effort. A family alias selects the latest available member "
           "of that family and exact identifiers remain for compatibility. A minimum Claude Code version is a "
           "compatibility floor, never a mapping to an exact model. Qualification is not a claim of provider, account "
           "or quota availability.")
MEANING_V3 = (MEANING + " This version additionally pins an operator-selected family qualification: a qualified "
              "family's exact expected model and compatibility floor come from retained source evidence before "
              "inference. The qualification asserts only its declared source scope; it is not provider availability, "
              "account entitlement or effective effort.")
SELECTION_BASIS_V3 = ("Configured engineering selector chosen when the room opened, with the operator-selected family "
                      "qualification pinned beside it. A qualified family's exact expected model is established from "
                      "retained source evidence before inference; an exact observed mismatch still fails and never "
                      "authorizes fallback. Not provider availability, account entitlement or native attestation")
QUALIFIED_BASIS = ("Operator-selected retained source evidence pins this family's exact expected model before "
                   "inference; not provider availability, account entitlement or effective effort")
UNQUALIFIED_FAMILY = ("This family epoch has no pre-inference operator-selected qualification; a supported audited "
                      "qualification refresh is required before dispatch")
QUALIFICATION_CHANGED = ("The pinned family qualification mapping cannot silently replace an existing expectation; "
                         "perform the explicit audited qualification refresh boundary")
QUALIFIED_SOURCE = ("Qualified dispatch requires the registered prospective native outcome source for the bound "
                    "engineer session; run the pre-dispatch ownership preflight first")
ROLES = ("engineer", "pr-opus", "pr-sonnet")
EFFORT_BASIS = "configured max intent; effective effort not evidenced"
MODEL_ID = re.compile(r"claude-[a-z]+(-[0-9]+)+")
FAMILY_ID = re.compile(r"[a-z]{1,32}")
CLAUDE_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?=\s|$)")
MINIMUM = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
HEX = re.compile(r"[0-9a-f]{64}")
ENTRY_KEYS = frozenset(("harness", "reasoning_effort", "minimum_claude_code_version"))
FAMILY_KEYS = frozenset(("roles", "harness", "reasoning_effort", "minimum_claude_code_version"))
EXECUTION_FIELDS = frozenset(("version", "expected_model", "qualification_sha256"))
FAMILY_KEYS_V3 = FAMILY_KEYS | {"execution_qualification"}
POLICY_FIELDS = frozenset(("format", "default_selector", "meaning", "families", "models"))
POLICY_FIELDS_V3 = POLICY_FIELDS | {"family_qualification"}
QUALIFICATION_FIELDS = frozenset(("artifact", "sha256"))
STATES = ("pending", "committed", "abandoned")
OUTCOMES = {"committed": "committed", "abandoned": "abandoned_unchanged"}
POINTER_STATES = {value: key for key, value in OUTCOMES.items()}
KINDS = ("requests", "attempts", "records")
MAX_CHAIN = 32
MAX_MODELS = 32
MAX_FAMILIES = 8
MAX_CONFIG = 1_000_000

SELECTION_FIELDS = frozenset(("version", "selector", "configured_model", "harness", "reasoning_effort", "selection",
                              "qualification", "policy", "policy_sha256", "provenance", "policy_record", "basis"))
SELECTION_FIELDS_V3 = SELECTION_FIELDS | {"family_qualification"}
POINTER_FIELDS = frozenset(("request_id", "intent", "intent_sha256", "state", "record", "record_sha256"))
INTENT_FIELDS = frozenset(("version", "room_id", "request_id", "inputs", "inputs_sha256", "audit_sha256",
                           "evidence", "before_state_sha256", "previous", "source", "target", "patch",
                           "provider_transition_receipt_sha256", "recorded_at"))
INPUT_FIELDS = frozenset(("request_id", "source_model", "target_model", "audit_sha256", "spec_record_sha256",
                          "candidate_sha256", "native_history_sha256", "native_owner_database",
                          "native_transcript_path", "authorization", "reason"))
EVIDENCE_FIELDS = frozenset(("version", "room_id", "state_sha256", "source", "target", "engineer", "requests",
                             "spec_record_sha256", "candidate", "native", "native_owner", "journals"))
NATIVE_FIELDS = frozenset(("session_id", "conversation_id", "branch_id", "controller", "settings",
                           "history_sha256", "transcript_sha256"))
MARKER_FIELDS = frozenset(("version", "room_id", "request_id", "intent_sha256", "patch", "started_at"))
RECORD_FIELDS = frozenset(("version", "room_id", "request_id", "intent_sha256", "attempt_sha256", "outcome",
                           "patch_result", "observed_before", "observed_after", "source", "target",
                           "boundary_order", "recorded_at"))
OBSERVED_FIELDS = frozenset(("settings", "controller", "history_sha256", "transcript_sha256", "native_owner"))
RESULT_FIELDS = frozenset(("acknowledged", "error", "response_sha256"))
ABANDON_FIELDS = frozenset(("authorization", "diagnosis", "abandon_inputs_sha256"))
SOURCE_FIELDS = frozenset(("selector", "configured_model", "reasoning_effort"))
TARGET_FIELDS = frozenset(("selector", "configured_model", "reasoning_effort", "harness", "qualification", "policy",
                           "policy_sha256"))
PAYLOAD_FIELDS = frozenset(("model", "reasoningEffort", "approvalMode"))
RESOLUTION_FIELDS = frozenset(("version", "room_id", "request_id", "epoch_request_id", "selector", "configured_model",
                               "observed_model", "outcome_record_sha256", "receipt_sha256", "created_order",
                               "recorded_at"))
GENERATION_FIELDS = ("activity_state", "controller_generation")  # the only owner fields an abandonment may see change
# W2: a present frozen worker expectation and the request purpose are part of the request identity a
# caller's copy must match, and each identity field's actual presence is compared as well as its value:
# an explicit null or a deleted key is never accepted as genuine absent history. An absent key stays
# absent (None == None) for old records, so readings and digests are never rewritten.
IDENTITY_FIELDS = ("request_id", "role", "session_id", "harness", "model", "reasoning_effort", "created_order",
                   "purpose", "engineering_resolution", "native_worker_expectations")
NO_PATCH = {"acknowledged": False, "error": None, "response_sha256": None}

DAMAGED = ("Unclaimed, missing or inconsistent engineering model transition evidence; "
           "reconcile the identical request, never edit the journal")
PENDING = ("An uncommitted engineering model transition exists; reconcile only its identical "
           "request before any other room change")
SELECTION_DAMAGED = ("Recorded room engineering model selection is inconsistent with its pinned policy; "
                     "preserve and diagnose")
CONFIGURED = ("Private engineering_models configuration is unreadable or malformed; correct it before "
              "selecting or transitioning an engineering model")
ORDERS = ("Retained request creation order is missing, duplicated, non-integer or out of range; "
          "preserve and diagnose")
REQUEST_EPOCH = "Engineer request model contradicts its recorded engineering epoch; preserve and diagnose"
UNSAFE_WRITE = ("Engineering model transition evidence is written only inside real owned room directories, "
                "never through a symlink")
BASIS = "Configured AO settings and recorded transitions; not provider availability or native attestation"
SELECTION_BASIS = ("Configured engineering selector chosen when the room opened; a family alias is intent for the "
                   "latest available member of that family, not provider availability or native attestation")
OBSERVED_BASIS = "AO stored settings for the next turn; not native adoption"
RESOLUTION_BASIS = ("A family epoch's expected exact model comes only from the native stop rows of a verified "
                    "completed owned turn in that epoch; unverified until then, and not provider or account "
                    "entitlement")

# The bundled tables. They are Python literals because the retained MCP runtime copies only
# the explicit first-party .py roster; a separate JSON file would not be retained. A private
# PROJECT_ROOM_HOME/ao/config.json "engineering_models" object may ADD exact models; it can
# never add or alter a family, nor alter, weaken or remove a bundled qualification or default.
BUNDLED_FAMILIES = {
    "fable": {"roles": ["engineer"], "harness": "claude-code", "reasoning_effort": "max"},
    # Older Claude Code releases resolve the opus alias to an older family member.
    "opus": {"roles": ["engineer", "pr-opus"], "harness": "claude-code", "reasoning_effort": "max",
             "minimum_claude_code_version": "2.1.280"},
    "sonnet": {"roles": ["pr-sonnet"], "harness": "claude-code", "reasoning_effort": "max"},
}
BUNDLED_MODELS = {
    "claude-fable-5-1": {"harness": "claude-code", "reasoning_effort": "max"},
    # Claude Code 2.1.268 was observed to refuse claude-opus-5-5 before inference;
    # 2.1.280 is the operator-qualified version. A minimum gates qualification only.
    "claude-opus-5-5": {"harness": "claude-code", "reasoning_effort": "max", "minimum_claude_code_version": "2.1.280"},
}


def _digest(value):
    from ao_project_room import digest
    return digest(value)


def _identifier(value):
    from ao_project_room import identifier
    return identifier(value)


def _normal(state):
    import ao_workflow
    return ao_workflow.normal(state or {})


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _hex(value):
    return isinstance(value, str) and bool(HEX.fullmatch(value))


# --- selectors and the family matcher -----------------------------------------------

def _exact_id(model):
    return isinstance(model, str) and len(model) <= 64 and bool(MODEL_ID.fullmatch(model))


def family_member(family, model):
    """True only for an exact identifier of this family, claude-<family>(-<digits>)+; never an alias."""
    return (isinstance(family, str) and bool(FAMILY_ID.fullmatch(family)) and _exact_id(model)
            and re.fullmatch("claude-" + family + r"(-[0-9]+)+", model) is not None)


def validate_selector(value):
    """{"kind": "family", "family": F} or {"kind": "exact", "model": id}; nothing else."""
    if isinstance(value, dict) and value.get("kind") == "family" and set(value) == {"kind", "family"}:
        if isinstance(value["family"], str) and FAMILY_ID.fullmatch(value["family"]):
            return value
    elif isinstance(value, dict) and value.get("kind") == "exact" and set(value) == {"kind", "model"}:
        if _exact_id(value["model"]):
            return value
    raise RoomError("An engineering selector is a family alias or an exact Claude identifier")


def configured_value(selector):
    """The AO configured value for a selector: the family alias itself or the exact identifier."""
    return selector["family"] if selector["kind"] == "family" else selector["model"]


def _family_of(request):
    """The family a request froze at creation, or None for exact (including historical) requests."""
    frozen = request.get("engineering_resolution") if isinstance(request, dict) else None
    selector = frozen.get("selector") if isinstance(frozen, dict) else None
    if isinstance(selector, dict) and selector.get("kind") == "family":
        return selector.get("family") if isinstance(selector.get("family"), str) else ""
    return None


def qualification_of(request):
    """The frozen (expected_model, qualification_sha256) of a version 2 request, or None.

    Only a version 2 engineering_resolution carries a pre-inference qualified expectation; every
    unversioned historical request keeps its original reader semantics and bytes.
    """
    frozen = request.get("engineering_resolution") if isinstance(request, dict) else None
    if not isinstance(frozen, dict) or frozen.get("version") != 2:
        return None
    expected, sha256 = frozen.get("expected_model"), frozen.get("qualification_sha256")
    if not _exact_id(expected) or not _hex(sha256):
        return None
    return (expected, sha256)


def model_matches(request, observed_model):
    """Whether one observed native model (or reroute target) agrees with the request's frozen identity.

    Exact requests keep exact string equality with their configured model. A family request accepts
    only an exact identifier of its frozen family and, once an expectation was frozen, only that model.
    """
    family = _family_of(request)
    if family is None:
        return observed_model == request.get("model")
    frozen = request["engineering_resolution"]
    expected = frozen.get("expected_model")
    return (request.get("model") == family and frozen.get("configured_model") == family
            and family_member(family, observed_model) and (expected is None or observed_model == expected))


# --- V2-1/S2 qualification policy -------------------------------------------------------

def _minimum(entry):
    if "minimum_claude_code_version" in entry and not (
            isinstance(entry["minimum_claude_code_version"], str) and MINIMUM.fullmatch(entry["minimum_claude_code_version"])):
        raise RoomError("minimum_claude_code_version must be an exact X.Y.Z Claude Code version")


def _entry(model, entry):
    if not _exact_id(model):
        raise RoomError("Engineering models are exact Claude identifiers, never aliases, suffixes or provider prefixes")
    if (not isinstance(entry, dict) or not set(entry) <= ENTRY_KEYS or entry.get("harness") != HARNESS
            or entry.get("reasoning_effort") != EFFORT):
        raise RoomError("Each qualified engineering model pins harness claude-code at max reasoning effort")
    _minimum(entry)
    return entry


def _execution_qualification(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != EXECUTION_FIELDS
            or type(value["version"]) is not int or value["version"] != EXECUTION_QUALIFICATION_VERSION
            or not _exact_id(value["expected_model"]) or not _hex(value["qualification_sha256"])):
        raise RoomError("A family execution qualification pins its version, an exact expected model and its "
                        "qualification digest")
    return value


def _family(name, entry, v3=False):
    allowed = FAMILY_KEYS_V3 if v3 else FAMILY_KEYS
    if not isinstance(name, str) or not FAMILY_ID.fullmatch(name):
        raise RoomError("Engineering families are lowercase Claude family aliases")
    if (not isinstance(entry, dict) or not set(entry) <= allowed or entry.get("harness") != HARNESS
            or entry.get("reasoning_effort") != EFFORT):
        raise RoomError("Each qualified family pins harness claude-code at max reasoning effort")
    roles = entry.get("roles")
    if not isinstance(roles, list) or not roles or any(r not in ROLES for r in roles) or len(set(roles)) != len(roles):
        raise RoomError("Each qualified family names its supported roles")
    _minimum(entry)
    if v3:
        if "execution_qualification" not in entry:
            raise RoomError("A version 3 family entry declares its execution qualification, or an explicit null")
        _execution_qualification(entry["execution_qualification"])
    return entry


def _v3_entry(name, sha256, declared):
    """The only family entry a version 3 policy derives from one bundled family and its qualification."""
    entry = copy.deepcopy(BUNDLED_FAMILIES[name])
    if declared is None:
        entry["execution_qualification"] = None
        return entry
    floor = declared.get("minimum_claude_code_version")
    if floor is not None:
        entry["minimum_claude_code_version"] = floor
    entry["execution_qualification"] = {"version": EXECUTION_QUALIFICATION_VERSION,
                                        "expected_model": declared["expected_model"],
                                        "qualification_sha256": sha256}
    return entry


def _entry_for(policy, selector, role="engineer"):
    """The qualification entry a policy pins for this selector and role, or None."""
    if selector["kind"] == "family":
        entry = policy["families"].get(selector["family"])
        return entry if entry is not None and role in entry["roles"] else None
    return policy["models"].get(selector["model"]) if role == "engineer" else None


def validate_policy(policy, *, current=True):
    """Validate one engineering model policy value.

    Current validation (the default) additionally requires every version 3 family entry to derive
    exactly from the bundled tables in force and every qualification floor to be at least the
    bundled floor. Historical validation (``current=False``) checks the same structure against the
    recorded snapshot itself, so a later bundled-table change never reinterprets an untouched
    historical record; a newer bundled floor still blocks new readiness through require_executable
    and admission.
    """
    if not isinstance(policy, dict) or set(policy) not in (POLICY_FIELDS, POLICY_FIELDS_V3):
        raise RoomError("Engineering model policy declares exactly format, default_selector, meaning, families and "
                        "models; a version 3 policy additionally declares its family qualification")
    if policy["format"] not in (FORMAT, FORMAT_V3):
        raise RoomError("Unsupported engineering model policy format")
    v3 = policy["format"] == FORMAT_V3
    if ("family_qualification" in policy) != v3:
        raise RoomError("Only a version 3 engineering model policy declares exactly its family qualification")
    if not _text(policy["meaning"]):
        raise RoomError("Engineering model policy must state what qualification does and does not mean")
    artifact = sha256 = None
    if v3:
        snapshot = policy["family_qualification"]
        if (not isinstance(snapshot, dict) or set(snapshot) != QUALIFICATION_FIELDS
                or not _hex(snapshot.get("sha256"))):
            raise RoomError("A version 3 engineering model policy snapshots its complete family qualification and "
                            "digest")
        artifact = snapshot["artifact"]
        import ao_model_qualification
        # Structural only; private bytes stay private. Historical validation never re-derives from
        # today's bundled table, so a later change cannot damage a preserved record.
        ao_model_qualification.validate_qualification(artifact, bundled=current)
        sha256 = _digest(artifact)
        if sha256 != snapshot["sha256"]:
            raise RoomError("The family qualification snapshot does not match its recorded digest")
    families = policy["families"]
    if not isinstance(families, dict) or not 1 <= len(families) <= MAX_FAMILIES:
        raise RoomError(f"Engineering model policy qualifies 1-{MAX_FAMILIES} model families")
    for name, entry in families.items():
        _family(name, entry, v3)
        if v3:
            declared = artifact["families"].get(name)
            if current:
                if entry != _v3_entry(name, sha256, declared):
                    raise RoomError("A version 3 family entry must derive exactly from its pinned qualification "
                                    "snapshot")
            else:
                _historical_entry(name, entry, declared, sha256)
    if v3 and not set(artifact["families"]) <= set(families):
        raise RoomError("A version 3 policy declares every family its qualification snapshot maps")
    models = policy["models"]
    if not isinstance(models, dict) or not 1 <= len(models) <= MAX_MODELS:
        raise RoomError(f"Engineering model policy qualifies 1-{MAX_MODELS} exact models")
    for model, entry in models.items():
        _entry(model, entry)
    if _entry_for(policy, validate_selector(policy["default_selector"])) is None:
        raise RoomError("Engineering model policy default is not a qualified engineering selector")
    return policy


def _historical_entry(name, entry, declared, sha256):
    """Structural consistency of one recorded version 3 family entry with its own snapshot.

    Roles and the bundled-derived parts of the entry cannot be re-derived without today's bundled
    table, so only the parts the snapshot itself fixes are re-checked: the execution qualification
    and the declared compatibility floor must be exactly the recorded ones.
    """
    execution = entry.get("execution_qualification")
    if declared is None:
        if execution is not None:
            raise RoomError("A version 3 family entry for an unmapped family pins no execution qualification")
        return
    expected = {"version": EXECUTION_QUALIFICATION_VERSION, "expected_model": declared["expected_model"],
                "qualification_sha256": sha256}
    if (execution != expected
            or entry.get("minimum_claude_code_version") != declared.get("minimum_claude_code_version")):
        raise RoomError("A version 3 family entry must stay consistent with its pinned qualification snapshot")


def _private_config(ao_root):
    """The optional private controller configuration object. Unreadable/malformed configuration refuses."""
    if ao_root is None:
        return None
    path = Path(ao_root) / "config.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RoomError(CONFIGURED) from exc
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_CONFIG:
            raise RoomError(CONFIGURED)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            value = json.loads(stream.read(MAX_CONFIG + 1))
    except (OSError, ValueError) as exc:
        raise RoomError(CONFIGURED) from exc
    if not isinstance(value, dict):
        raise RoomError(CONFIGURED)
    return value


def _configured(ao_root):
    """The optional private engineering_models map. Unreadable/malformed configuration refuses."""
    value = _private_config(ao_root)
    if value is None or CONFIG_KEY not in value:  # absent means bundled only; an explicit non-object is malformed
        return None
    if not isinstance(value[CONFIG_KEY], dict):
        raise RoomError(CONFIGURED)
    return value[CONFIG_KEY]


def configured_qualification(ao_root):
    """The optional private family_qualification pointer {path, sha256}; absence keeps the version 2 policy.

    The digest is the canonical digest of the complete artifact value, the same digest under which the
    artifact is retained and bound inside the room. A path is private operator input metadata; it is
    never hardcoded or distributed.
    """
    value = _private_config(ao_root)
    if value is None or QUALIFICATION_KEY not in value:
        return None
    pointer = value[QUALIFICATION_KEY]
    if (not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}
            or not _text(pointer.get("path")) or not _hex(pointer.get("sha256"))):
        raise RoomError(CONFIGURED)
    return {"path": pointer["path"], "sha256": pointer["sha256"]}


def effective_policy(ao_root):
    """The bundled families and exact models plus validated private exact additions, with provenance.

    A configured private family_qualification pointer additionally pins version 3: the complete artifact
    is validated, its retained source bytes are verified read-only, and the artifact value is snapshotted
    under family_qualification. Absent configuration keeps the version 2 policy untouched.
    """
    configured = _configured(ao_root)
    pointer = configured_qualification(ao_root)
    models = copy.deepcopy(BUNDLED_MODELS)
    for model, entry in (configured or {}).items():
        _entry(model, entry)
        if model in BUNDLED_MODELS and entry != BUNDLED_MODELS[model]:
            raise RoomError("Configured engineering_models may add exact models; a bundled qualification is never "
                            "altered, weakened or removed")
        models[model] = copy.deepcopy(entry)
    provenance = {"bundled_sha256": _digest({"families": BUNDLED_FAMILIES, "models": BUNDLED_MODELS}),
                  "configuration_sha256": None if configured is None else _digest(configured),
                  "configured_models": sorted(configured) if configured is not None else []}
    if pointer is None:
        policy = validate_policy({"format": FORMAT, "default_selector": {"kind": "family", "family": DEFAULT_FAMILY},
                                  "meaning": MEANING, "families": copy.deepcopy(BUNDLED_FAMILIES), "models": models})
        return policy, provenance
    import ao_model_qualification
    verified = ao_model_qualification.load(pointer)
    sha256, artifact = verified["sha256"], verified["artifact"]
    families = {name: _v3_entry(name, sha256, artifact["families"].get(name)) for name in BUNDLED_FAMILIES}
    policy = validate_policy({"format": FORMAT_V3, "default_selector": {"kind": "family", "family": DEFAULT_FAMILY},
                              "meaning": MEANING_V3, "families": families, "models": models,
                              "family_qualification": {"artifact": artifact, "sha256": sha256}})
    provenance = {**provenance, "family_qualification_sha256": sha256,
                  "family_qualification_revision": artifact["revision"],
                  "qualified_families": sorted(artifact["families"])}
    return policy, provenance


def _choose(policy, value):
    """Parse a caller's engineering value: None (the default), an engineer-role family alias or an exact id."""
    if value is None:
        return copy.deepcopy(policy["default_selector"])
    if isinstance(value, str):
        family = policy["families"].get(value)
        if family is not None and "engineer" in family["roles"]:
            return {"kind": "family", "family": value}
        if value in policy["models"]:
            return {"kind": "exact", "model": value}
    raise RoomError("Qualified engineering selectors are the family aliases "
                    + ", ".join(sorted(n for n, e in policy["families"].items() if "engineer" in e["roles"]))
                    + " (latest available within the family) and the exact identifiers "
                    + ", ".join(sorted(policy["models"])) + "; other aliases, floating names and unlisted models are refused")


def qualification(model, ao_root):
    """Today's qualification entry for an engineer-role family alias or exact identifier. Never guesses a version."""
    policy = effective_policy(ao_root)[0]
    return copy.deepcopy(_entry_for(policy, _choose(policy, model)))


def selection(model, ao_root):
    """The pinned snapshot a new normal room records; read the policy once, here only.

    A version 3 selection keeps every outer key and adds the complete qualification snapshot together
    with the room-relative immutable record it is retained under.
    """
    policy, provenance = effective_policy(ao_root)
    selector = _choose(policy, model)
    pinned = _digest(policy)
    pinned_record = {"selector": selector, "configured_model": configured_value(selector),
                     "harness": HARNESS, "reasoning_effort": EFFORT,
                     "selection": "default" if model is None else "explicit",
                     "qualification": copy.deepcopy(_entry_for(policy, selector)), "policy": copy.deepcopy(policy),
                     "policy_sha256": pinned, "provenance": provenance,
                     "policy_record": f"{POLICY_BASE}/policy-{pinned}.json"}
    if policy["format"] == FORMAT_V3:
        import ao_model_qualification
        pinned_record["family_qualification"] = ao_model_qualification.snapshot(policy["family_qualification"])
        return {"version": SELECTION_VERSION_V3, "basis": SELECTION_BASIS_V3, **pinned_record}
    return {"version": SELECTION_VERSION, "basis": SELECTION_BASIS, **pinned_record}


# --- V2-4/R1 durable, symlink-safe writes and reads ---------------------------------------

def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True) + "\n").encode("utf-8")


def _relative(directory, relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise RoomError("Engineering model transition evidence uses room-relative paths only")
    return Path(directory).joinpath(relative)


def _owned_directory(path, message):
    """True for a real owned directory, False for genuine absence; a symlink is never absence."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RoomError(message) from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RoomError(message)
    return True


def _fsync(path, folder=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if folder else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _store(directory, relative, data):
    """Create-exclusive fsynced byte write inside verified real owned ancestors.

    Every call, for new or identical bytes, fsyncs the file and then republishes every ancestor
    directory entry from the file's parent up to and including the room directory, so a retry
    after any lost directory barrier (including an existing ancestor's) crosses it again.
    Different existing bytes, symlinks and unowned paths refuse.
    """
    path = _relative(directory, relative)
    folder = root = Path(directory)
    if not _owned_directory(root, UNSAFE_WRITE):
        raise RoomError(UNSAFE_WRITE)
    ancestors = [root]
    for part in Path(relative).parts[:-1]:  # one level at a time, each published into its parent
        folder = folder / part
        if not _owned_directory(folder, UNSAFE_WRITE):
            os.mkdir(folder, 0o700)
            _fsync(folder.parent, folder=True)
        ancestors.append(folder)
    if folder != path.parent:
        raise RoomError(UNSAFE_WRITE)
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RoomError(UNSAFE_WRITE)
        if owned_bytes(path) != data:
            raise RoomError("Engineering model transition evidence already exists with other bytes: "
                            + relative + "; never edit or replace it")
        _fsync(path)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    for ancestor in reversed(ancestors):  # the file's parent first, the room directory last
        _fsync(ancestor, folder=True)


def store_once(directory, relative, value):
    """Create-exclusive fsynced write of one canonical JSON value; see ``_store``."""
    _store(directory, relative, json_bytes(value))
    return _digest(value)


def store_bytes_once(directory, relative, data):
    """Create-exclusive fsynced write of complete immutable bytes, e.g. one retained source capture.

    The same durable barriers as ``store_once`` apply, and repeating an identical retention
    republishes them instead of accepting a directory entry that may have been lost.
    """
    if not isinstance(data, bytes):
        raise RoomError("Retained room bytes must be exact bytes")
    _store(directory, relative, data)
    return hashlib.sha256(data).hexdigest()


def _read(directory, relative, message=DAMAGED):
    try:
        return json.loads(owned_bytes(_relative(directory, relative)))
    except (RoomError, OSError, ValueError) as exc:
        raise RoomError(message) from exc


def _named(name):
    if not name.endswith(".json"):
        return False
    try:
        _identifier(name[:-5])
    except RoomError:
        return False
    return True


def _present(directory, kind):
    """Names in one journal subdirectory; absent means a genuinely absent owned path."""
    root = Path(directory) / BASE
    if not _owned_directory(root, DAMAGED):
        return set()
    folder = root / kind
    if not _owned_directory(folder, DAMAGED):
        return set()
    names = set()
    for child in folder.iterdir():
        try:
            info = child.lstat()
        except OSError as exc:
            raise RoomError(DAMAGED) from exc
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not _named(child.name):
            raise RoomError(DAMAGED)
        names.add(child.name)
    return names


# --- V2-2 pinned selection snapshot ------------------------------------------------------

def initial(directory, state):
    """The room's epoch 0, verified against its own immutable policy record, never today's table.

    A version 3 selection additionally verifies the retained qualification bytes inside the room's own
    qualification area; the private operator source files are not required to read history.
    """
    record = (state or {}).get(SELECTION_KEY)
    if record is None:
        selector = {"kind": "exact", "model": LEGACY_MODEL}
        return {"selector": selector, "configured_model": LEGACY_MODEL, "model": LEGACY_MODEL,
                "reasoning_effort": EFFORT, "harness": HARNESS, "qualification": {}, "policy_sha256": None,
                "source": "legacy_default"}
    snapshot = None
    try:
        version = record.get("version") if isinstance(record, dict) else None
        fields = SELECTION_FIELDS_V3 if version == SELECTION_VERSION_V3 else SELECTION_FIELDS
        if (not isinstance(record, dict) or version not in (SELECTION_VERSION, SELECTION_VERSION_V3)
                or set(record) != fields or record["harness"] != HARNESS or record["reasoning_effort"] != EFFORT
                or record["selection"] not in ("explicit", "default") or not _text(record["basis"])
                or not isinstance(record["provenance"], dict)):
            raise RoomError("Recorded room engineering model selection has an unsupported shape")
        policy = validate_policy(record["policy"], current=False)
        selector = validate_selector(record["selector"])
        pinned = _digest(policy)
        entry = _entry_for(policy, selector)
        if (entry is None or record["qualification"] != entry
                or record["configured_model"] != configured_value(selector)
                or (record["selection"] == "default" and selector != policy["default_selector"])
                or record["policy_sha256"] != pinned or record["policy_record"] != f"{POLICY_BASE}/policy-{pinned}.json"
                or _read(directory, record["policy_record"], SELECTION_DAMAGED) != policy):
            raise RoomError("Recorded room engineering model selection left its pinned policy record")
        if version == SELECTION_VERSION_V3:
            if policy["format"] != FORMAT_V3:
                raise RoomError("A version 3 selection records a version 3 pinned policy")
            import ao_model_qualification
            snapshot = ao_model_qualification.read_retained(directory, record["family_qualification"],
                                                            SELECTION_DAMAGED)
            if policy["family_qualification"] != {"artifact": snapshot["artifact"], "sha256": snapshot["sha256"]}:
                raise RoomError("Recorded room engineering model selection left its pinned policy record")
        elif policy["format"] != FORMAT:
            raise RoomError("A version 2 selection records a version 2 pinned policy")
    except (RoomError, KeyError, TypeError) as exc:
        raise RoomError(SELECTION_DAMAGED) from exc
    result = {"selector": copy.deepcopy(selector), "configured_model": record["configured_model"],
              "model": record["configured_model"], "reasoning_effort": EFFORT, "harness": HARNESS,
              "qualification": copy.deepcopy(record["qualification"]), "policy_sha256": pinned,
              "source": "room_selection"}
    if snapshot is not None:
        result["family_qualification"] = copy.deepcopy(snapshot)
    return result


def parse_version(text):
    match = CLAUDE_VERSION.match(text.strip()) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def executable_identity(directory, state, prepared):
    """The recorded Claude executable identity for the preparation in force, or None.

    A missing or unsupported preparation returns None; no executable check is then claimed to
    have happened, and callers that require a floor must refuse rather than guess.
    """
    if not isinstance(prepared, dict) or not prepared.get("worktree"):
        return None
    import ao_executable_binding
    target = ao_executable_binding.effective(directory, state, prepared)
    return target or (prepared.get("routing") or {}).get("claude") or None


def _newer_floor(*values):
    """The newest valid X.Y.Z floor among the recorded and the currently bundled compatibility floors."""
    candidates = [value for value in values if isinstance(value, str) and MINIMUM.fullmatch(value)]
    if not candidates:
        return None
    return max(candidates, key=parse_version)


def required_minimum(family, qualification):
    """The strongest recorded/current compatibility floor for a family and its qualification entry.

    A floor recorded in a historical or pending qualification and a floor bundled today are both
    binding for new readiness: a later bundled floor blocks a new dispatch without rewriting the
    historical record, and a removed floor never weakens the bundled one.
    """
    entry = qualification if isinstance(qualification, dict) else {}
    bundled = BUNDLED_FAMILIES.get(family) if isinstance(family, str) else None
    return _newer_floor(entry.get("minimum_claude_code_version"),
                        (bundled or {}).get("minimum_claude_code_version"))


def require_executable(directory, state, prepared, model=None, qualification=None):
    """Refuse a selection whose qualification floor needs a newer Claude Code than the recorded executable.

    A family floor is a compatibility check (older releases resolve the alias to an older member),
    never a mapping to an exact identifier. A compatibility floor bundled now may be newer than the
    floor recorded in a historical qualification: the newer floor blocks new readiness without
    rewriting the historical record.
    """
    family = None
    if qualification is None:  # default to the epoch in force
        epoch = current(directory, state)
        model, qualification = model or epoch["configured_model"], epoch["qualification"]
        selector = epoch.get("selector") or {}
        family = selector.get("family") if selector.get("kind") == "family" else None
    elif isinstance(model, str) and model in BUNDLED_FAMILIES:
        family = model
    minimum = required_minimum(family, qualification)
    if not minimum:
        return None
    identity = executable_identity(directory, state, prepared) or {}
    observed = None if identity.get("error") else parse_version(identity.get("version"))
    if observed is None or observed < parse_version(minimum):
        raise RoomError(f"{model} requires Claude Code {minimum} or newer; the recorded executable reports "
                        + str(identity.get("version") or identity.get("error") or "no version")
                        + ". Use the audited executable upgrade or repair lane first")
    return identity


# --- V2-3 transition journal ---------------------------------------------------------------

def _pointer(value):
    if (not isinstance(value, dict) or set(value) != POINTER_FIELDS or value["state"] not in STATES
            or not _hex(value.get("intent_sha256")) or not isinstance(value["request_id"], str)
            or value["intent"] != f"{BASE}/requests/{value['request_id']}.json"):
        raise RoomError(DAMAGED)
    try:
        _identifier(value["request_id"])
    except RoomError as exc:
        raise RoomError(DAMAGED) from exc
    if value["state"] == "pending":
        if value["record"] is not None or value["record_sha256"] is not None:
            raise RoomError(DAMAGED)
    elif value["record"] != f"{BASE}/records/{value['request_id']}.json" or not _hex(value["record_sha256"]):
        raise RoomError(DAMAGED)
    return value


def _damaged_selector(value):
    try:
        return validate_selector(value)
    except RoomError as exc:
        raise RoomError(DAMAGED) from exc


def _source(value):
    if not isinstance(value, dict) or set(value) != SOURCE_FIELDS or value["reasoning_effort"] != EFFORT:
        raise RoomError(DAMAGED)
    if value["configured_model"] != configured_value(_damaged_selector(value["selector"])):
        raise RoomError(DAMAGED)
    return value


def _target(value):
    """A transition target is a selector that pins its own qualification and immutable policy, like a room."""
    if (not isinstance(value, dict) or set(value) != TARGET_FIELDS or value["reasoning_effort"] != EFFORT
            or value["harness"] != HARNESS):
        raise RoomError(DAMAGED)
    try:
        policy = validate_policy(value["policy"], current=False)
    except RoomError as exc:
        raise RoomError(DAMAGED) from exc
    selector = _damaged_selector(value["selector"])
    entry = _entry_for(policy, selector)
    if (entry is None or value["configured_model"] != configured_value(selector)
            or _digest(policy) != value["policy_sha256"] or entry != value["qualification"]):
        raise RoomError(DAMAGED)
    return value


def _inventory(value):
    """Every request that existed at audit, sorted, with orders exactly 1..N."""
    if not isinstance(value, list):
        raise RoomError(DAMAGED)
    orders, names = [], set()
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"request_id", "created_order"}
                or not isinstance(item["request_id"], str) or type(item["created_order"]) is not int):
            raise RoomError(DAMAGED)
        orders.append(item["created_order"]); names.add(item["request_id"])
    if orders != list(range(1, len(value) + 1)) or len(names) != len(value):
        raise RoomError(DAMAGED)
    return value


def _patch(intent, evidence):
    """V2-7: the payload carries the target's configured value at max and preserves a nonempty approvalMode."""
    patch, native = intent["patch"], evidence["native"]
    payload = patch.get("payload") if isinstance(patch, dict) else None
    if (not isinstance(patch, dict) or set(patch) != {"method", "path", "payload"} or patch["method"] != "PATCH"
            or patch["path"] != f"/sessions/{native['session_id']}/conversation/settings"
            or not isinstance(payload, dict) or not set(payload) <= PAYLOAD_FIELDS
            or payload.get("model") != intent["target"]["configured_model"] or payload.get("reasoningEffort") != EFFORT):
        raise RoomError(DAMAGED)
    approval = native["settings"].get("approvalMode")
    keep = approval if isinstance(approval, str) and approval else None
    if ("approvalMode" in payload) != (keep is not None) or payload.get("approvalMode") != keep:
        raise RoomError(DAMAGED)
    return patch


def _evidence(state, intent, evidence):
    engineer = (state.get("bindings") or {}).get("engineer")
    inputs, target = intent["inputs"], intent["target"]
    if (not EVIDENCE_FIELDS <= set(evidence) or evidence["version"] != 1
            or evidence["room_id"] != state["room_id"] or evidence["state_sha256"] != intent["before_state_sha256"]
            or evidence["source"] != intent["source"] or not isinstance(evidence["target"], dict)
            or any(evidence["target"].get(k) != target[k]
                   for k in ("selector", "configured_model", "qualification", "policy_sha256"))
            or not engineer or evidence["engineer"] != engineer
            or evidence["spec_record_sha256"] != inputs["spec_record_sha256"]
            or not isinstance(evidence["candidate"], dict)
            or evidence["candidate"].get("sha256") != inputs["candidate_sha256"]
            or not isinstance(evidence["native_owner"], dict) or not isinstance(evidence["journals"], dict)
            or "provider_transition" not in evidence["journals"]):
        raise RoomError(DAMAGED)
    native = evidence["native"]
    settings = native.get("settings") if isinstance(native, dict) else None
    if (not isinstance(native, dict) or not NATIVE_FIELDS <= set(native)
            or native["session_id"] != engineer.get("session_id")
            or native["conversation_id"] != engineer.get("conversation_id")
            or native["branch_id"] != engineer.get("branch_id") or native["controller"] != "ready"
            or not isinstance(settings, dict) or settings.get("model") != intent["source"]["configured_model"]
            or settings.get("reasoningEffort") != intent["source"]["reasoning_effort"]
            or native["history_sha256"] != inputs["native_history_sha256"]
            or not _hex(native.get("transcript_sha256"))):
        raise RoomError(DAMAGED)
    # R2: the retained native owner is the bound engineer's own conversation/branch, the audited
    # transcript's session and idle; later observations are compared against exactly this owner.
    owner = evidence["native_owner"]
    native_session = owner.get("provider_conversation_id")
    if (owner.get("id") != engineer.get("session_id") or owner.get("ao_conversation_id") != engineer.get("conversation_id")
            or owner.get("active_branch_id") != engineer.get("branch_id") or not _text(native_session)
            or Path(inputs["native_transcript_path"]).name != native_session + ".jsonl"
            or owner.get("activity_state") != "idle"):
        raise RoomError(DAMAGED)
    receipt = evidence["journals"]["provider_transition"]
    if (receipt is not None and not isinstance(receipt, dict)
            or intent["provider_transition_receipt_sha256"] != (receipt or {}).get("receipt_sha256")):
        raise RoomError(DAMAGED)
    _inventory(evidence["requests"])
    return evidence


def _reset(intent):
    """A same-selector family resolution reset: no PATCH, a new epoch whose expectation starts unknown."""
    return intent["target"]["configured_model"] == intent["source"]["configured_model"]


def _intent(state, request_id, intent):
    if (not isinstance(intent, dict) or set(intent) != INTENT_FIELDS or intent["version"] != 1
            or intent["room_id"] != state["room_id"] or intent["request_id"] != request_id
            or not _hex(intent["before_state_sha256"]) or not _number(intent["recorded_at"])):
        raise RoomError(DAMAGED)
    source, target, inputs = _source(intent["source"]), _target(intent["target"]), intent["inputs"]
    if _reset(intent) and (target["selector"] != source["selector"] or target["selector"]["kind"] != "family"
                           or intent["patch"] is not None):
        raise RoomError(DAMAGED)
    if (not isinstance(inputs, dict) or set(inputs) != INPUT_FIELDS
            or any(not isinstance(value, str) for value in inputs.values())
            or _digest(inputs) != intent["inputs_sha256"] or inputs["request_id"] != request_id
            or inputs["source_model"] != source["configured_model"] or inputs["target_model"] != target["configured_model"]
            or inputs["audit_sha256"] != intent["audit_sha256"]
            or not isinstance(intent["evidence"], dict) or _digest(intent["evidence"]) != intent["audit_sha256"]):
        raise RoomError(DAMAGED)
    evidence = _evidence(state, intent, intent["evidence"])
    if not _reset(intent):
        _patch(intent, evidence)
    return intent


def _marker(state, link, intent, value):
    if (_reset(intent) or not isinstance(value, dict) or set(value) != MARKER_FIELDS or value["version"] != 1
            or value["room_id"] != state["room_id"] or value["request_id"] != link["request_id"]
            or value["intent_sha256"] != link["intent_sha256"] or value["patch"] != intent["patch"]
            or not _number(value["started_at"])):
        raise RoomError(DAMAGED)
    return value


def _observation(value):
    if (not isinstance(value, dict) or set(value) != OBSERVED_FIELDS or not isinstance(value["settings"], dict)
            or not isinstance(value["native_owner"], dict) or not isinstance(value["controller"], str)):
        raise RoomError(DAMAGED)
    return value


def _stable_owner(owner):
    return {key: value for key, value in owner.items() if key not in GENERATION_FIELDS}


def _record(state, request_id, intent_sha256, pointer_state, intent, record, marker):
    """A terminal record proves its own attempt, observations and boundary; nothing is inferred.

    R2: observed_before equals the audited evidence exactly and no intervening work is admitted:
    history and transcript digests are unchanged; a commit keeps a ready controller and the identical
    retained native owner; an abandonment admits only a ready/stopped controller and a changed owner
    liveness (idle/exited) with a nonempty controller generation.
    """
    extra = {"abandonment"} if pointer_state == "abandoned" else set()
    if (not isinstance(record, dict) or set(record) != RECORD_FIELDS | extra or record["version"] != 1
            or record["room_id"] != state["room_id"] or record["request_id"] != request_id
            or record["intent_sha256"] != intent_sha256 or record["outcome"] != OUTCOMES[pointer_state]
            or record["source"] != intent["source"] or record["target"] != intent["target"]
            or type(record["boundary_order"]) is not int or record["boundary_order"] < 0
            or not _number(record["recorded_at"])):
        raise RoomError(DAMAGED)
    result = record["patch_result"]
    if (not isinstance(result, dict) or set(result) != RESULT_FIELDS
            or type(result["acknowledged"]) is not bool
            or not (result["error"] is None or isinstance(result["error"], str))
            or not (result["response_sha256"] is None or _hex(result["response_sha256"]))):
        raise RoomError(DAMAGED)
    before, after = _observation(record["observed_before"]), _observation(record["observed_after"])
    evidence, native = intent["evidence"], intent["evidence"]["native"]
    audited = {"settings": native["settings"], "controller": native["controller"],
               "history_sha256": native["history_sha256"], "transcript_sha256": native["transcript_sha256"],
               "native_owner": evidence["native_owner"]}
    if (before != audited or after["history_sha256"] != before["history_sha256"]
            or after["transcript_sha256"] != before["transcript_sha256"]):
        raise RoomError(DAMAGED)
    reset = _reset(intent)
    if pointer_state == "committed":
        expected = (before["settings"] if reset else
                    {**before["settings"], "model": intent["target"]["configured_model"], "reasoningEffort": EFFORT})
        if (after["settings"] != expected or after["controller"] != "ready"
                or after["native_owner"] != before["native_owner"]
                or record["boundary_order"] != len(evidence["requests"])):
            raise RoomError(DAMAGED)
        if reset:
            if marker is not None or record["attempt_sha256"] is not None or result != NO_PATCH:
                raise RoomError(DAMAGED)
        elif marker is None or record["attempt_sha256"] != _digest(marker):
            raise RoomError(DAMAGED)
        return record
    abandonment, owner = record["abandonment"], after["native_owner"]
    if (record["attempt_sha256"] != (None if marker is None else _digest(marker))
            or after["settings"] != before["settings"] or after["controller"] not in ("ready", "stopped")
            or _stable_owner(owner) != _stable_owner(before["native_owner"])
            or owner.get("activity_state") not in ("idle", "exited") or not _text(owner.get("controller_generation"))
            or not isinstance(abandonment, dict) or set(abandonment) != ABANDON_FIELDS
            or not _text(abandonment["authorization"]) or not _text(abandonment["diagnosis"])
            or not _hex(abandonment["abandon_inputs_sha256"])):
        raise RoomError(DAMAGED)
    return record


def _chain(directory, state, allow_pending):
    """Validate the whole create-once transition chain and every file it must claim."""
    pointer = (state or {}).get(POINTER_KEY)
    present = {kind: _present(directory, kind) for kind in KINDS}  # symlinked roots refuse here
    if pointer is None:
        if any(present.values()):
            raise RoomError(DAMAGED)
        return {"pointer": None, "attempts": [], "committed": [], "pending": None}
    chain, seen, link = [], set(), _pointer(pointer)
    while True:
        if link["request_id"] in seen or len(chain) >= MAX_CHAIN:
            raise RoomError(DAMAGED)
        seen.add(link["request_id"])
        intent = _read(directory, link["intent"])
        if _digest(intent) != link["intent_sha256"]:
            raise RoomError(DAMAGED)
        chain.append((link, _intent(state, link["request_id"], intent)))
        if intent["previous"] is None:
            break
        link = _pointer(intent["previous"])
        if link["state"] == "pending":  # only the latest attempt may still be pending
            raise RoomError(DAMAGED)
    chain.reverse()
    attempts, claimed = [], {kind: set() for kind in KINDS}
    for link, intent in chain:
        request_id, name = link["request_id"], link["request_id"] + ".json"
        claimed["requests"].add(name)
        marker = None
        if name in present["attempts"]:
            claimed["attempts"].add(name)
            marker = _marker(state, link, intent, _read(directory, f"{BASE}/attempts/{name}"))
        record, pointer_state = None, link["state"]
        if link["state"] != "pending":
            record = _read(directory, link["record"])
            claimed["records"].add(name)
            if not isinstance(record, dict) or _digest(record) != link["record_sha256"]:
                raise RoomError(DAMAGED)
        elif name in present["records"]:
            # Crash after the record write, before the pointer commit: validate, never discard.
            record = _read(directory, f"{BASE}/records/{name}")
            claimed["records"].add(name)
            pointer_state = POINTER_STATES.get((record or {}).get("outcome") if isinstance(record, dict) else None)
            if pointer_state is None:
                raise RoomError(DAMAGED)
        if record is not None:
            _record(state, request_id, link["intent_sha256"], pointer_state, intent, record, marker)
        attempts.append({"request_id": request_id, "intent": link["intent"], "intent_sha256": link["intent_sha256"],
                         "record": link["record"], "record_sha256": link["record_sha256"], "state": link["state"],
                         "outcome": None if record is None else record["outcome"], "intent_value": intent,
                         "attempt_sha256": None if marker is None else _digest(marker), "attempt_value": marker,
                         "record_value": record})
    for kind, names in claimed.items():
        if present[kind] != names:
            raise RoomError(DAMAGED)
    pending = attempts[-1] if attempts[-1]["state"] == "pending" else None
    if pending is not None and not allow_pending:
        raise RoomError(PENDING)
    return {"pointer": pointer, "attempts": attempts, "pending": pending,
            "committed": [a for a in attempts if a["state"] == "committed"]}


# --- V2-5 request order contract and epochs ----------------------------------------------

def _orders(state):
    """With a committed transition every retained order must be a unique int in 1..N.

    An absent or null requests mapping means no requests; any other non-mapping retained container
    or request is refused with this same ordinary order error, never a raw shape error.
    """
    requests = state.get("requests")
    if requests is None:
        requests = {}
    if not isinstance(requests, dict):
        raise RoomError(ORDERS)
    orders = []
    for request in requests.values():
        order = request.get("created_order") if isinstance(request, dict) else None
        if type(order) is not int or order < 1:
            raise RoomError(ORDERS)
        orders.append(order)
    if sorted(orders) != list(range(1, len(orders) + 1)):
        raise RoomError(ORDERS)
    return sorted(requests.values(), key=lambda r: r["created_order"])


def _qualification_reference(policy_qualification, sha256):
    """The compact room-relative reference to one immutable qualification snapshot, or None.

    The reference names the retained artifact record and every selected source capture that belongs
    to it; it never re-derives today's configuration. The two-field policy qualification a
    transition target pins derives its capture entries; a snapshot that declares its own captures
    reports exactly those, and a historical snapshot that holds no retained source bytes reports
    only its artifact record instead of pretending otherwise.
    """
    if not isinstance(policy_qualification, dict) or sha256 is None:
        return None
    if not _hex(sha256):
        raise RoomError(DAMAGED)
    import ao_model_qualification
    reference = {"sha256": sha256, "record": ao_model_qualification.record_path(sha256)}
    declared = policy_qualification.get("evidence")
    if isinstance(declared, list):
        reference["evidence"] = copy.deepcopy(declared)
    elif set(policy_qualification) == {"artifact", "sha256"}:
        reference["evidence"] = ao_model_qualification.evidence_entries(policy_qualification.get("artifact"),
                                                                       sha256)
    return reference


def _epoch(selector, qualification, from_order, request_id=None, record_sha256=None, receipt=None,
           policy_qualification=None):
    configured = configured_value(selector)
    execution = qualification.get("execution_qualification") if isinstance(qualification, dict) else None
    execution = execution if isinstance(execution, dict) else None
    sha256 = policy_qualification.get("sha256") if isinstance(policy_qualification, dict) else None
    if sha256 is not None and not _hex(sha256):
        raise RoomError(DAMAGED)
    if execution is not None and execution.get("qualification_sha256") != sha256:
        raise RoomError(DAMAGED)
    expected = (selector["model"] if selector["kind"] == "exact"
                else (None if execution is None else execution["expected_model"]))
    return {"selector": copy.deepcopy(selector), "configured_model": configured, "model": configured,
            "reasoning_effort": EFFORT, "harness": HARNESS, "qualification": copy.deepcopy(qualification),
            "from_order": from_order, "expected_model": expected,
            "resolution_sha256": None, "resolution_order": None, "request_id": request_id,
            "record_sha256": record_sha256, "provider_transition_receipt_sha256": receipt,
            "qualification_sha256": sha256, "family_qualified": execution is not None,
            "qualification_reference": _qualification_reference(policy_qualification, sha256)}


def _transition_epochs(directory, state, chain):
    """Epoch 0 plus one epoch per committed record, in chronological order, before any resolution."""
    start = initial(directory, state)
    history = [_epoch(start["selector"], start["qualification"], 1,
                      policy_qualification=start.get("family_qualification"))]
    if not chain["committed"]:
        return history
    ordered, boundary = _orders(state), 0
    for attempt in chain["committed"]:
        record, intent, previous = attempt["record_value"], attempt["intent_value"], history[-1]
        source, target, order = record["source"], record["target"], record["boundary_order"]
        if (source["selector"] != previous["selector"] or source["configured_model"] != previous["configured_model"]
                or source["reasoning_effort"] != previous["reasoning_effort"]
                or not boundary <= order <= len(ordered)
                or [{"request_id": r["request_id"], "created_order": r["created_order"]} for r in ordered[:order]]
                != intent["evidence"]["requests"]):
            raise RoomError(DAMAGED)
        boundary = order
        epoch = _epoch(target["selector"], target["qualification"], order + 1, attempt["request_id"],
                       attempt["record_sha256"], intent["provider_transition_receipt_sha256"],
                       policy_qualification=(target.get("policy") or {}).get("family_qualification"))
        if (previous["family_qualified"] and target["selector"] == previous["selector"]
                and target["configured_model"] == previous["configured_model"]):
            # The committed record is the explicit audited qualification boundary: a same-alias
            # qualification update is authorized by exactly this immutable record, and its artifact
            # must be retained in the room before admission can rely on it. An unqualified
            # replacement of a qualified expectation is a downgrade and is refused; an unchanged
            # mapping stays idempotent because it derives the same epoch from the same bytes.
            if not epoch["family_qualified"]:
                raise RoomError(QUALIFICATION_CHANGED)
        history.append(epoch)
    return history


def epoch_for_order(history, order):
    if type(order) is not int or order < 1:
        raise RoomError(ORDERS)
    chosen = history[0]
    for epoch in history:
        if epoch["from_order"] <= order:
            chosen = epoch
    return chosen


def _engineer_requests(state, epoch):
    """Retained engineer requests of the bound session created within this epoch, oldest first."""
    engineer = (state.get("bindings") or {}).get("engineer") or {}
    rows = [r for r in (state.get("requests") or {}).values()
            if isinstance(r, dict) and r.get("role") == "engineer" and r.get("session_id") == engineer.get("session_id")
            and type(r.get("created_order")) is int and r["created_order"] >= epoch["from_order"]]
    return sorted(rows, key=lambda r: r["created_order"])


def _resolution(directory, state, history, value, name):
    """One resolution record, validated against its epoch, its request and its immutable outcome record."""
    if (not isinstance(value, dict) or set(value) != RESOLUTION_FIELDS or value["version"] != 1
            or value["room_id"] != state["room_id"] or not isinstance(value["request_id"], str)
            or value["request_id"] + ".json" != name or not _hex(value["outcome_record_sha256"])
            or not _hex(value["receipt_sha256"]) or type(value["created_order"]) is not int
            or value["created_order"] < 1 or not _number(value["recorded_at"])):
        raise RoomError(DAMAGED)
    selector = _damaged_selector(value["selector"])
    request = (state.get("requests") or {}).get(value["request_id"])
    engineer = (state.get("bindings") or {}).get("engineer") or {}
    epoch = epoch_for_order(history, value["created_order"])
    # An observation resolution is never a qualification: a qualified epoch keeps the expectation the
    # immutable qualification established before inference, so such a record cannot claim it.
    if epoch.get("qualification_sha256") is not None:
        raise RoomError(DAMAGED)
    if (selector["kind"] != "family" or epoch["selector"] != selector
            or value["configured_model"] != epoch["configured_model"]
            or value["epoch_request_id"] != epoch["request_id"]
            or not family_member(selector["family"], value["observed_model"])
            or not isinstance(request, dict) or request.get("role") != "engineer" or not engineer
            or request.get("session_id") != engineer.get("session_id")
            or request.get("state") not in ("completed", "settled_failure")
            or request.get("created_order") != value["created_order"]
            or request.get("receipt_sha256") != value["receipt_sha256"]
            or request.get("model") != value["configured_model"]):
        raise RoomError(DAMAGED)
    record = _read(directory, "outcomes/" + value["request_id"] + "/" + value["outcome_record_sha256"] + ".json")
    native = record.get("native") if isinstance(record, dict) else None
    if (_digest(record) != value["outcome_record_sha256"] or record.get("version") != 1
            or record.get("room_id") != state["room_id"] or record.get("request_id") != value["request_id"]
            or record.get("receipt_sha256") != value["receipt_sha256"]
            or not isinstance(native, dict) or native.get("unknown")
            or not isinstance(native.get("source"), dict)
            or native["source"].get("session_id") != request["session_id"]
            or native.get("observed_models") != [value["observed_model"]]):
        raise RoomError(DAMAGED)
    return epoch


def _resolutions(directory, state, history):
    """Every claimed resolution in order, at most one per family epoch, and every file claimed.

    One unclaimed file is tolerated only as the exact interrupted-adoption record: it validates
    completely for the current unresolved epoch and its latest engineer request. It is reported,
    never applied, and the next adoption boundary claims those same bytes idempotently.
    """
    pointers = state.get(RESOLUTIONS_KEY)
    pointers = [] if pointers is None else pointers
    present = _present(directory, RESOLUTION_KIND)
    prefix = f"{BASE}/{RESOLUTION_KIND}/"
    if not isinstance(pointers, list) or len(pointers) > len(history):
        raise RoomError(DAMAGED)
    claimed, names, resolved, last = [], set(), set(), 0
    for pointer in pointers:
        if (not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"} or not _hex(pointer["sha256"])
                or not isinstance(pointer["path"], str) or not pointer["path"].startswith(prefix)):
            raise RoomError(DAMAGED)
        name = pointer["path"][len(prefix):]
        if name in names or name not in present:
            raise RoomError(DAMAGED)
        value = _read(directory, pointer["path"])
        if _digest(value) != pointer["sha256"]:
            raise RoomError(DAMAGED)
        epoch = _resolution(directory, state, history, value, name)
        if id(epoch) in resolved or value["created_order"] <= last:
            raise RoomError(DAMAGED)
        resolved.add(id(epoch)); names.add(name); last = value["created_order"]
        claimed.append((epoch, {"path": pointer["path"], "sha256": pointer["sha256"], "value": value}))
    unclaimed, pending = present - names, None
    if unclaimed:
        name = unclaimed.pop()
        path = prefix + name
        value = _read(directory, path)
        epoch = _resolution(directory, state, history, value, name)
        latest = _engineer_requests(state, history[-1])
        if (unclaimed or epoch is not history[-1] or id(epoch) in resolved
                or not latest or latest[-1]["request_id"] != value["request_id"]):
            raise RoomError(DAMAGED)
        pending = {"path": path, "sha256": _digest(value), "value": value}
    return claimed, pending


def _ancestry(directory, state, allow_pending):
    """(journal, epochs): the validated transition chain and resolutions, and the epochs they define."""
    chain = _chain(directory, state, allow_pending)
    history = _transition_epochs(directory, state, chain)
    claimed, pending = _resolutions(directory, state, history)
    for epoch, item in claimed:
        epoch.update(expected_model=item["value"]["observed_model"], resolution_sha256=item["sha256"],
                     resolution_order=item["value"]["created_order"])
    chain = {**chain, "resolutions": [item for _, item in claimed], "pending_resolution": pending}
    return chain, history


def journal(directory, state, allow_pending=False):
    """The validated transition and resolution journal; a pending transition refuses unless allowed."""
    return _ancestry(directory, state, allow_pending)[0]


def guard_pending(directory, state):
    journal(directory, state)


def epochs(directory, state):
    """Epoch 0 plus one epoch per committed record, each with its recorded resolution, if any."""
    return _ancestry(directory, state, True)[1]


def current(directory, state):
    return epochs(directory, state)[-1]


def current_qualification(directory, state):
    """The qualification of the epoch in force: epoch 0 from the room, later from its intent target."""
    return copy.deepcopy(current(directory, state)["qualification"])


def qualification_context_for_target(target):
    """Structural context for one selected or pending transition target.

    Validates the complete target with the journal's own target validator and exposes its selector,
    frozen qualification entry and compact room-relative qualification reference. It makes no
    commitment or authorization claim and writes no state; a future transition caller
    authenticates that its selected/pending target is the one passed here.
    """
    validated = _target(target)
    policy_qualification = (validated.get("policy") or {}).get("family_qualification")
    execution = (validated.get("qualification") or {}).get("execution_qualification")
    return {"selector": copy.deepcopy(validated["selector"]),
            "qualification": copy.deepcopy(validated["qualification"]),
            "reference": _qualification_reference(policy_qualification, (policy_qualification or {}).get("sha256")),
            "expected_model": execution.get("expected_model") if isinstance(execution, dict) else None}


def qualification_context(directory, state, request_id=None):
    """The reusable epoch/target interface for this room's operator-selected qualification.

    Returns the exact current epoch's own pinned selector, frozen family entry and compact
    room-relative reference, or, for an explicitly named committed transition request id, that
    record's validated target. Today's private configuration is never consulted for a room's
    history, and a reference is None when the epoch or target pins no qualification. The
    committed-target branch is the authenticated historical wrapper over the structural
    qualification_context_for_target helper.
    """
    chain, history = _ancestry(directory, state, True)
    if request_id is None:
        epoch = history[-1]
        return {"source": "current_epoch", "request_id": epoch["request_id"],
                "selector": copy.deepcopy(epoch["selector"]),
                "qualification": copy.deepcopy(epoch.get("qualification")),
                "reference": copy.deepcopy(epoch.get("qualification_reference")),
                "expected_model": epoch["expected_model"] if epoch.get("family_qualified") else None}
    for attempt in chain["committed"]:
        if attempt["request_id"] == request_id:
            return {"source": "committed_target", "request_id": request_id,
                    **qualification_context_for_target(attempt["intent_value"]["target"])}
    raise RoomError("The named engineering model transition is not a committed record of this room")


def model_at_provider_epoch(directory, state):
    """The epoch in force when this room's provider transition was committed."""
    chain, history = _ancestry(directory, state, True)
    return history[sum(a["intent_value"]["provider_transition_receipt_sha256"] is None for a in chain["committed"])]


# --- S3 resolution adoption and request freezing -------------------------------------------

def _frozen(epoch, order):
    """The engineering_resolution a request created at this order in this epoch must carry.

    A version 3 epoch freezes version 2: its expectation comes from the immutable qualification
    before inference and never from an observation. This is also the historical reader: a valid
    historical record keeps the semantics it was created with, and the new-dispatch guard lives in
    freeze_request. An unsupported inconsistent qualified-family record still refuses: an epoch
    whose pinned qualification establishes no expectation for its own family is never given one.
    """
    qualified = epoch.get("qualification_sha256") is not None
    if epoch["selector"]["kind"] == "exact":
        expected, resolution = epoch["expected_model"], None
    elif qualified:
        if not epoch.get("family_qualified"):
            raise RoomError(UNQUALIFIED_FAMILY)
        expected, resolution = epoch["expected_model"], None
    elif epoch["resolution_order"] is not None and epoch["resolution_order"] < order:
        expected, resolution = epoch["expected_model"], epoch["resolution_sha256"]
    else:
        expected, resolution = None, None
    frozen = {"selector": copy.deepcopy(epoch["selector"]), "configured_model": epoch["configured_model"],
              "expected_model": expected, "resolution_sha256": resolution, "effort_basis": EFFORT_BASIS}
    if qualified:
        return {"version": 2, **frozen, "qualification_sha256": epoch["qualification_sha256"]}
    return frozen


def adopt_resolution(directory, state, now=None):
    """At a normal engineer dispatch boundary, record the family resolution the latest owned turn proved.

    Only when the epoch in force is an unresolved family epoch that carries no operator-selected
    qualification, and the latest engineer request created in it is completed or settled_failure
    with a validated semantic outcome whose verified native evidence observed exactly one exact model
    of that family. The standing latest-within-family decision authorizes this inside the family only.
    A qualified epoch keeps the expectation its immutable qualification established; an observation
    never replaces it. Returns the claimed pointer (the caller saves state) or None. An interrupted
    earlier adoption's record is re-published through the same durable writer and claimed as is.
    """
    engineer = (state.get("bindings") or {}).get("engineer")
    if not _normal(state) or not engineer:
        return None
    chain, history = _ancestry(directory, state, False)
    epoch = history[-1]
    if (epoch.get("qualification_sha256") is not None or epoch["selector"]["kind"] != "family"
            or epoch["resolution_sha256"] is not None):
        return None
    pending = chain["pending_resolution"]
    if pending is not None:
        # The unclaimed record is the interrupted adoption's exact validated bytes. Re-run the safe
        # immutable writer before its pointer is claimed, so an interrupted ancestor-directory
        # publication barrier is crossed again; the value and its digest must be exactly the ones
        # already validated by the journal, and any failure propagates.
        if store_once(directory, pending["path"], pending["value"]) != pending["sha256"]:
            raise RoomError("Interrupted resolution adoption evidence does not match its claimed digest; "
                            "preserve and diagnose")
        pointer = {"path": pending["path"], "sha256": pending["sha256"]}
    else:
        latest = _engineer_requests(state, epoch)
        request = latest[-1] if latest else None
        if request is None or request.get("state") not in ("completed", "settled_failure"):
            return None
        from ao_outcomes import load
        record = load(directory, request)
        native = (record or {}).get("native")
        observed = native.get("observed_models") if isinstance(native, dict) and not native.get("unknown") else None
        if (not isinstance(observed, list) or len(observed) != 1
                or not family_member(epoch["selector"]["family"], observed[0])
                or not isinstance(native.get("source"), dict)
                or native["source"].get("session_id") != request["session_id"]):
            return None
        check_request(directory, state, request, history)
        value = {"version": 1, "room_id": state["room_id"], "request_id": request["request_id"],
                 "epoch_request_id": epoch["request_id"], "selector": copy.deepcopy(epoch["selector"]),
                 "configured_model": epoch["configured_model"], "observed_model": observed[0],
                 "outcome_record_sha256": request["semantic_outcome_sha256"],
                 "receipt_sha256": request["receipt_sha256"], "created_order": request["created_order"],
                 "recorded_at": time.time() if now is None else now}
        path = f"{BASE}/{RESOLUTION_KIND}/{request['request_id']}.json"
        pointer = {"path": path, "sha256": store_once(directory, path, value)}
    had, previous = RESOLUTIONS_KEY in state, state.get(RESOLUTIONS_KEY)
    state[RESOLUTIONS_KEY] = list(previous or []) + [pointer]
    try:
        _ancestry(directory, state, False)  # the claimed record must validate before anything relies on it
    except RoomError:
        if had:
            state[RESOLUTIONS_KEY] = previous
        else:
            state.pop(RESOLUTIONS_KEY)
        raise
    return pointer


def freeze_request(directory, state):
    """The engineering_resolution a new engineer request records at creation.

    New family execution requires a pre-inference operator-selected expectation: an epoch that is
    not operator-qualified refuses here with actionable readiness, while every historical reader of
    an already-recorded request keeps its original semantics. A qualified family dispatch
    additionally verifies the registered prospective native outcome source, read-only, against the
    current bound session and prepared workspace before anything is sent.
    """
    epoch = epochs(directory, state)[-1]
    if epoch["selector"]["kind"] == "family" and not epoch.get("family_qualified"):
        raise RoomError(UNQUALIFIED_FAMILY)
    frozen = _frozen(epoch, len(state.get("requests") or {}) + 1)
    if epoch.get("family_qualified"):
        _require_qualified_source(directory, state)
    return frozen


def _require_qualified_source(directory, state):
    """Qualified dispatch needs the registered prospective native source fully verified before send."""
    from ao_native_outcome import validate_registered_source
    try:
        return validate_registered_source(directory, state, "engineer")
    except RoomError as exc:
        raise RoomError(QUALIFIED_SOURCE + ": " + str(exc)) from exc


# --- ancestry consumers ---------------------------------------------------------------------

def _engineer(state, target):
    engineer = (state.get("bindings") or {}).get("engineer") or {}
    return bool(engineer) and _normal(state) and target.get("session_id") == engineer.get("session_id")


def effective_binding(directory, state):
    """The stored engineer binding with the configured value and effort of the current epoch."""
    binding = (state.get("bindings") or {}).get("engineer")
    if not binding:
        raise RoomError("Bind the prepared native engineering orchestrator first")
    if not _normal(state):
        return copy.deepcopy(binding)
    history = epochs(directory, state)
    if binding["model"] != history[0]["configured_model"] or binding["reasoning_effort"] != history[0]["reasoning_effort"]:
        raise RoomError("Stored engineer binding contradicts this room's initial engineering model; preserve and diagnose")
    return {**copy.deepcopy(binding), "model": history[-1]["configured_model"],
            "reasoning_effort": history[-1]["reasoning_effort"]}


def check_request(directory, state, request, history=None):
    """A retained engineer request keeps the exact epoch in force when it was created.

    Its model equals that epoch's configured value (exact string equality, never widened to family
    matching) and any frozen engineering_resolution equals what the epoch and the resolution in
    force at its creation require. A family-epoch request must carry one.
    """
    if not _engineer(state, request):
        return None
    retained = (state.get("requests") or {}).get(request.get("request_id"))
    if retained is not request and retained != request:
        raise RoomError("Engineer request is not this room's retained record; preserve and diagnose")
    epoch = epoch_for_order(history if history is not None else epochs(directory, state), request.get("created_order"))
    if request.get("model") != epoch["configured_model"] or request.get("reasoning_effort") != epoch["reasoning_effort"]:
        raise RoomError(REQUEST_EPOCH)
    frozen = request.get("engineering_resolution")
    if (frozen is None and epoch["selector"]["kind"] == "family") or (
            frozen is not None and frozen != _frozen(epoch, request["created_order"])):
        raise RoomError(REQUEST_EPOCH)
    return epoch


def check_frozen(directory, state, request, history=None):
    """The identity a request froze at creation, bound to its retained record and recorded epoch.

    Native matching relies only on these identity fields, so a caller's copy may carry stale
    observation fields; the identity itself, including each field's actual key presence and the
    request purpose, must equal the retained record, which check_request then validates against the
    ancestry. A copied explicit null is never accepted in place of a genuinely absent key.
    """
    if not _engineer(state, request):
        return None
    retained = (state.get("requests") or {}).get(request.get("request_id"))
    if (not isinstance(retained, dict) or any(retained.get(k) != request.get(k) for k in IDENTITY_FIELDS)
            or any((k in retained) != (k in request) for k in IDENTITY_FIELDS)):
        raise RoomError("Engineer request is not this room's retained record; preserve and diagnose")
    return check_request(directory, state, retained, history)


def binding_for_request(directory, state, binding, request, history=None):
    """A frozen binding expressed in the epoch that owned this request."""
    if not _engineer(state, binding):
        return copy.deepcopy(binding)
    epoch = epoch_for_order(history if history is not None else epochs(directory, state), request.get("created_order"))
    return {**copy.deepcopy(binding), "model": epoch["configured_model"], "reasoning_effort": epoch["reasoning_effort"]}


def expected_settings(directory, state, target):
    """Live AO settings the engineer session must report; other sessions keep their own."""
    if not _engineer(state, target):
        return {"model": target["model"], "reasoning_effort": target["reasoning_effort"]}
    history = epochs(directory, state)
    if "request_id" in target:
        check_request(directory, state, target, history)
    elif ((target.get("model"), target.get("reasoning_effort")) not in
            [(e["configured_model"], e["reasoning_effort"]) for e in (history[0], history[-1])]):
        raise RoomError("Engineer binding contradicts this room's recorded engineering epochs; preserve and diagnose")
    return {"model": history[-1]["configured_model"], "reasoning_effort": history[-1]["reasoning_effort"]}


def summary(directory, state):
    """Offline status projection; it never raises, never observes AO and never infers adoption."""
    try:
        start = initial(directory, state)
        chain, history = _ancestry(directory, state, True)
        latest = chain["committed"][-1] if chain["committed"] else None
        observed = latest and {"request_id": latest["request_id"],
                               "settings": copy.deepcopy(latest["record_value"]["observed_after"]["settings"]),
                               "basis": OBSERVED_BASIS}
        now = history[-1]
        if now["selector"]["kind"] == "exact":
            status = "exact"
        elif now.get("family_qualified"):
            status = "qualified"
        elif now.get("qualification_sha256") is not None:
            status = "unqualified"
        elif now["resolution_sha256"] is not None:
            status = "observed"
        else:
            status = "unverified"
        return {"initial": {**{k: start[k] for k in ("selector", "configured_model", "model", "reasoning_effort",
                                                        "harness", "source")},
                            "selection": (state.get(SELECTION_KEY) or {}).get("selection"),
                            "policy_sha256": start["policy_sha256"],
                            "qualification": copy.deepcopy(start["qualification"])},
                "configured": {k: copy.deepcopy(now[k]) for k in ("selector", "configured_model", "model",
                                                                  "reasoning_effort", "harness", "qualification")},
                "resolution": {"status": status, "expected_model": now["expected_model"],
                               "resolution_sha256": now["resolution_sha256"],
                               "qualification_sha256": now.get("qualification_sha256"),
                               "basis": QUALIFIED_BASIS if status == "qualified" else RESOLUTION_BASIS},
                "effort": {"configured": EFFORT, "effective": "not evidenced"},
                "epochs": history,
                "attempts": [{"request_id": a["request_id"], "state": a["state"], "outcome": a["outcome"],
                              "source": {k: a["intent_value"]["source"][k] for k in ("configured_model", "reasoning_effort")},
                              "target": {k: a["intent_value"]["target"][k] for k in ("configured_model", "reasoning_effort")},
                              "record_sha256": a["record_sha256"],
                              "boundary_order": (a["record_value"] or {}).get("boundary_order")}
                             for a in chain["attempts"]],
                "pending": chain["pending"] and {k: chain["pending"][k] for k in ("request_id", "intent", "intent_sha256")},
                "resolutions": [{"request_id": r["value"]["request_id"], "observed_model": r["value"]["observed_model"],
                                 "epoch_request_id": r["value"]["epoch_request_id"], "sha256": r["sha256"]}
                                for r in chain["resolutions"]],
                "pending_resolution": chain["pending_resolution"] and {
                    "request_id": chain["pending_resolution"]["value"]["request_id"],
                    "sha256": chain["pending_resolution"]["sha256"]},
                "observed": observed or None, "basis": BASIS}
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"error": str(exc), "basis": BASIS}
