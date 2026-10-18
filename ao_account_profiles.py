"""Fixed Claude account profiles for AO rooms (stdlib only).

A profile enrolls one private ``CLAUDE_CONFIG_DIR`` (mode 0700, owned, already
its own resolved path, NFC) under a stable name. An AO room may pin exactly one
enrolled profile — or ``default`` for the saved default login — at open time.
Every boundary refuses a conflicting credential source, a missing or different
exact ``CLAUDE_CONFIG_DIR`` in the AO project environment, and any drift of the
saved login's ``oauthAccount.accountUuid`` identity digest. Diagnostics name the
profile, the key and the source only; an environment value is never echoed.
"""

import datetime as dt
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
import re
import stat
import unicodedata

from room import RoomError
from ao_delegate_launcher import owned_bytes


PROFILES_FILE = "claude-profiles.json"
DEFAULT = "default"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_HOST_CREDS_FILE",
                  "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_HOST_AUTH_ENV_VAR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
                  "USE_LOCAL_OAUTH", "USE_STAGING_OAUTH")
CUSTOM_HEADERS = "ANTHROPIC_CUSTOM_HEADERS"
HELPER_KEY = "apiKeyHelper"
GLOBAL_CONFIG = ".claude.json"
CREDENTIALS_FILE = ".credentials.json"
MAX_GLOBAL_CONFIG_BYTES = 16_000_000
KEYCHAIN_BASE = "Claude Code-credentials"

_AUTHORIZATION = re.compile(r"^\s*authorization\s*:", re.IGNORECASE)
_MEANING = ("Fixed account profile for this room. identity_digest is the SHA-256 of the saved login's "
            "account UUID; the controller cannot attest which account served a request.")
_TRUST_MEANING = ("Preparation never writes trust. Start Claude Code interactively in the worktree with "
                  "exactly this environment to record it; AO's non-interactive start sets session trust itself.")
_SOURCES_CHECKED = ["AO project environment", "process environment", "user", "managed", "project", "local",
                    CREDENTIALS_FILE]


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def profile_name(value):
    if not isinstance(value, str):
        raise RoomError("A profile name must be text")
    if value == DEFAULT:
        raise RoomError("Profile name 'default' is reserved for the saved default login")
    if not NAME_PATTERN.fullmatch(value):
        raise RoomError("A profile name must match " + NAME_PATTERN.pattern)
    return value


def validate_directory(text):
    """The exact config-dir string, refused unless it already selects its credential item safely."""
    if not isinstance(text, str) or not text:
        raise RoomError("A profile config directory must be a nonempty text path")
    if not Path(text).is_absolute():
        raise RoomError("A profile config directory must be an absolute path; '~' and relative paths are refused")
    if len(text) > 1 and text.endswith("/"):
        raise RoomError("A profile config directory must not carry a trailing slash; the exact string selects the credential item")
    if unicodedata.normalize("NFC", text) != text:
        raise RoomError("A profile config directory must be NFC-normalized; the exact string selects the credential item")
    if str(Path(text).resolve()) != text:
        raise RoomError("A profile config directory must already be its own resolved path: no symlinked component, "
                        "'.'/'..' or repeated slash")
    try:
        info = os.stat(text)
    except OSError as exc:
        raise RoomError("A profile config directory must be an existing directory") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RoomError("A profile config directory must be an existing directory")
    if info.st_uid != os.getuid():
        raise RoomError("A profile config directory must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise RoomError("A profile config directory must have mode 0700")
    return text


def keychain_service(exact_or_none):
    """The Keychain item name the exact config-dir string selects; the base item for unset."""
    if not exact_or_none:
        return KEYCHAIN_BASE
    return KEYCHAIN_BASE + "-" + hashlib.sha256(unicodedata.normalize("NFC", exact_or_none).encode()).hexdigest()[:8]


def _global_config(global_config_path):
    try:
        data = owned_bytes(global_config_path, MAX_GLOBAL_CONFIG_BYTES)
    except (OSError, ValueError) as exc:
        raise RoomError("The saved Claude global configuration has no readable saved login") from exc
    try:
        return json.loads(data)
    except ValueError as exc:
        raise RoomError("The saved Claude global configuration is not valid JSON; no readable saved login") from exc


def identity_digest(global_config_path):
    """SHA-256 of the saved login's account UUID; nothing else from the file is kept or returned."""
    config = _global_config(global_config_path)
    account = config.get("oauthAccount") if isinstance(config, dict) else None
    uuid = account.get("accountUuid") if isinstance(account, dict) else None
    if not isinstance(uuid, str) or not uuid:
        raise RoomError("The saved Claude global configuration has no readable saved login")
    return hashlib.sha256(uuid.encode()).hexdigest()


def _profiles_path(home):
    return Path(home) / PROFILES_FILE


def load(home):
    path = _profiles_path(home)
    if not path.exists():
        return {"version": 1, "profiles": {}}
    try:
        data = owned_bytes(path)
    except (OSError, ValueError) as exc:
        raise RoomError("The Claude account profiles file is unreadable") from exc
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise RoomError("The Claude account profiles file is not valid JSON") from exc
    profiles = value.get("profiles") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(profiles, dict):
        raise RoomError("The Claude account profiles file is malformed")
    for name, record in profiles.items():
        if (not isinstance(name, str) or name == DEFAULT or not NAME_PATTERN.fullmatch(name)
                or not isinstance(record, dict) or record.get("name") != name
                or not isinstance(record.get("config_dir_exact"), str)
                or not isinstance(record.get("keychain_service"), str)
                or not isinstance(record.get("identity_digest"), str)):
            raise RoomError("The Claude account profiles file is malformed")
    return value


def _recorded_default(controller_config):
    recorded = (controller_config or {}).get("claude_config_dir")
    base = recorded if isinstance(recorded, str) and recorded else Path.home() / ".claude"
    return str(Path(base).expanduser().resolve())


def enroll(home, name, config_dir, controller_config):
    """Record one private config directory under a stable profile name; refuses before any write."""
    name = profile_name(name)
    config_dir = validate_directory(config_dir)
    default_dir = str((Path.home() / ".claude").resolve())
    if config_dir in (_recorded_default(controller_config), default_dir):
        raise RoomError("Profile " + name + " cannot enroll the default directory: the default directory stays the "
                        "default profile; an explicit CLAUDE_CONFIG_DIR selects another credential item; "
                        "pin `default` instead")
    digest = identity_digest(Path(config_dir) / GLOBAL_CONFIG)
    document = load(home)
    existing = document["profiles"].get(name)
    if existing is not None and existing.get("config_dir_exact") != config_dir:
        raise RoomError("Profile " + name + " is already enrolled for another directory; profile names are "
                        "stable, use a new name")
    for other, record in document["profiles"].items():
        if other != name and record.get("identity_digest") == digest:
            raise RoomError("Profile " + name + " duplicates the saved login already enrolled as " + other +
                            "; one account in two profiles risks refresh-token rotation logging one out")
    record = {"name": name, "config_dir_exact": config_dir, "keychain_service": keychain_service(config_dir),
              "identity_digest": digest, "enrolled_at": _now()}
    document["profiles"][name] = record
    from project_room import atomic_json
    atomic_json(_profiles_path(home), document)
    return record


def pin(home, name, controller_config):
    """The immutable account-profile pin one new room records; never mutates enrollment state."""
    config = controller_config if isinstance(controller_config, dict) else {}
    if name == DEFAULT:
        default_dir = str((Path.home() / ".claude").resolve())
        if config.get("claude_config_dir_override") is not None or _recorded_default(config) != default_dir:
            raise RoomError("Profile default selects the saved default login, but the controller records an explicit "
                            "Claude configuration directory; pin an enrolled profile instead")
        global_config = str(Path.home() / GLOBAL_CONFIG)
        return {"version": 1, "name": DEFAULT, "config_dir_exact": None, "claude_config_dir": default_dir,
                "global_config_path": global_config, "identity_digest": identity_digest(global_config),
                "keychain_service": KEYCHAIN_BASE, "enrolled_at": None, "pinned_at": _now(), "meaning": _MEANING}
    name = profile_name(name)
    record = load(home)["profiles"].get(name)
    if record is None:
        raise RoomError("Unknown Claude account profile " + name + "; enroll it with setup --claude-profile first")
    exact = validate_directory(record["config_dir_exact"])
    digest = identity_digest(Path(exact) / GLOBAL_CONFIG)
    if digest != record["identity_digest"]:
        raise RoomError("Account identity of profile " + name + " changed since enrollment; re-enroll the profile")
    return {"version": 1, "name": name, "config_dir_exact": exact, "claude_config_dir": exact,
            "global_config_path": exact + "/" + GLOBAL_CONFIG, "identity_digest": digest,
            "keychain_service": record["keychain_service"], "enrolled_at": record.get("enrolled_at"),
            "pinned_at": _now(), "meaning": _MEANING}


def attribution(pin):
    return {"name": pin["name"], "identity_digest": pin["identity_digest"]}


def check_project_env(project_env, pin):
    """The AO project environment must carry exactly the pinned exact string (none for `default`)."""
    if pin["config_dir_exact"] is None:
        if "CLAUDE_CONFIG_DIR" in project_env:
            raise RoomError("Profile default refuses a CLAUDE_CONFIG_DIR in the AO project environment")
        return
    if "CLAUDE_CONFIG_DIR" not in project_env:
        raise RoomError("Profile " + pin["name"] + " requires the AO project environment to carry exactly the "
                        "pinned CLAUDE_CONFIG_DIR; the key is absent")
    value = project_env["CLAUDE_CONFIG_DIR"]
    if not isinstance(value, str) or value != pin["config_dir_exact"]:
        raise RoomError("Profile " + pin["name"] + " requires the pinned exact CLAUDE_CONFIG_DIR string; the AO "
                        "project environment value differs from the pinned exact string (value withheld)")


def check_credential_env(env, label):
    """Refuse a credential source in one environment mapping; values are never echoed."""
    if not isinstance(env, Mapping):
        raise RoomError(label + " must be a mapping of text keys")
    for key in CREDENTIAL_ENV:
        if env.get(key) not in (None, ""):
            raise RoomError(label + " sets " + key + "; a pinned account profile refuses conflicting credential "
                            "sources (value withheld)")
    headers = env.get(CUSTOM_HEADERS)
    if isinstance(headers, str) and any(_AUTHORIZATION.match(line) for line in headers.splitlines()):
        raise RoomError(label + " " + CUSTOM_HEADERS + " carries an Authorization header (value withheld)")


def check_credential_settings(pin, worktree, environ):
    """Refuse apiKeyHelper or credential env keys in the settings the pinned launch would read."""
    import ao_routing
    config_dir = Path(pin["claude_config_dir"])
    sources = (("user", config_dir / "settings.json"),
               ("managed", ao_routing.managed_settings_path(environ)),
               ("project", Path(worktree) / ".claude" / "settings.json"),
               ("local", Path(worktree) / ".claude" / "settings.local.json"))
    for label, path in sources:
        data = ao_routing._settings_file(path)
        if data is None:
            continue
        if HELPER_KEY in data:
            raise RoomError(label + " Claude settings define " + HELPER_KEY +
                            "; a pinned account profile refuses helper-provided credentials")
        check_credential_env(data.get("env") or {}, label + " Claude settings env")
    credentials = config_dir / CREDENTIALS_FILE
    if credentials.exists() or credentials.is_symlink():
        raise RoomError("A plaintext credential file is present beside the pinned profile; pinned profiles "
                        "require the Keychain-backed saved login")


def check_identity(pin):
    if identity_digest(pin["global_config_path"]) != pin["identity_digest"]:
        raise RoomError("Account identity of profile " + pin["name"] + " changed since the room pinned it; "
                        "re-enroll the profile and open a new room")


def check_context(room_settings, pin):
    if (not isinstance(room_settings, dict)
            or room_settings.get("claude_config_dir") != pin["claude_config_dir"]
            or room_settings.get("claude_config_dir_override") != pin["config_dir_exact"]):
        raise RoomError("Room account profile pin and saved Claude configuration snapshot disagree")


def trust_status(pin, worktree, claude_bin):
    """Read-only report whether the pinned global config records trust for this worktree."""
    try:
        config = _global_config(pin["global_config_path"])
        projects = config.get("projects") if isinstance(config, dict) else None
        entry = projects.get(str(worktree)) if isinstance(projects, dict) else None
        trust = isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True
    except RoomError:
        trust = None
    env = {"CLAUDE_CONFIG_DIR": pin["config_dir_exact"]} if pin["config_dir_exact"] else {}
    return {"trust_recorded": trust,
            "explicit_setup": {"cwd": str(worktree), "env": env, "argv": [claude_bin]},
            "meaning": _TRUST_MEANING}


def _checked(service, state, prepared):
    """Identity + AO project env + credential settings; the boundaries that never see the daemon's env."""
    import ao_routing
    pin = state["account_profile"]
    check_identity(pin)
    project_env = ao_routing._project_env(service.client(state), state)
    check_project_env(project_env, pin)
    check_credential_env(project_env, "AO project environment")
    check_credential_settings(pin, Path(prepared["worktree"]), os.environ)
    return {**attribution(pin), "config_dir_exact": pin["config_dir_exact"], "project_env_checked": True,
            "credential_sources_checked": [label for label in _SOURCES_CHECKED if label != "process environment"]}


def at_prepare(service, state, worktree, claude_bin):
    """Full preparation evidence for a pinned room; also checks the process environment."""
    import ao_routing
    pin = state["account_profile"]
    project_env = ao_routing._project_env(service.client(state), state)
    check_project_env(project_env, pin)
    check_credential_env(project_env, "AO project environment")
    check_credential_env(os.environ, "process environment")
    check_credential_settings(pin, Path(worktree), os.environ)
    check_identity(pin)
    return {**attribution(pin), "config_dir_exact": pin["config_dir_exact"],
            "project_env_checked": True, "process_env_checked": True,
            "credential_sources_checked": list(_SOURCES_CHECKED),
            "trust": trust_status(pin, worktree, claude_bin)}


def at_bind(service, state, prepared):
    return _checked(service, state, prepared)


def before_send(service, state, prepared):
    return _checked(service, state, prepared)


def observe_sync(service, state):
    """The refusal text when a pinned room's account evidence drifted, else None; never raises."""
    try:
        pin = state.get("account_profile")
        if not pin:
            return None
        check_identity(pin)
        import ao_routing
        project_env = ao_routing._project_env(service.client(state), state)
        check_project_env(project_env, pin)
        check_credential_env(project_env, "AO project environment")
        import ao_delegates
        prepared = ao_delegates.preparation(service.root / "rooms" / state["room_id"], state)
        check_credential_settings(pin, Path(prepared["worktree"]), os.environ)
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        return str(exc)
    return None
