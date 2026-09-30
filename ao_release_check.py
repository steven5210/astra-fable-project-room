"""Read-only comparison of the running AO daemon with its latest stable release."""

import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import time
from datetime import datetime, timezone
import urllib.parse
import urllib.request

import ao_engineering_model
import ao_model_qualification
import ao_project_room
import deepseek_adapter


SCHEMA = "ao-release-check/v1"
GITHUB_HOST = "api.github.com"
GITHUB_PATH = "/repos/Untrivial-ai/agent-orchestrator/releases/latest"
GITHUB_RELEASE_PREFIX = "https://github.com/Untrivial-ai/agent-orchestrator/releases/"
CLAUDE_GITHUB_PATH = "/repos/anthropics/claude-code/releases/latest"
CLAUDE_RELEASE_PREFIX = "https://github.com/anthropics/claude-code/releases/"
MAX_RESPONSE_BYTES = 1_000_000
MAX_PLIST_BYTES = 1_000_000
MAX_STATE_BYTES = 1_000_000
_LEGACY_EVIDENCE_NOT_PRESERVED = object()
VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
TAG_PATTERN = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
MODEL_ID_PATTERN = re.compile(r"claude-(fable|opus|sonnet)-\d+(?:-\d+)*")
QUALIFICATION_SOURCE_HOSTS = frozenset(
    {"code.claude.com", "docs.claude.com", "docs.anthropic.com", "platform.claude.com",
     "www.anthropic.com"})
PS_TIME_FORMAT = "%a %b %d %H:%M:%S %Y"
APP_CONTENTS = ("Contents", "Resources", "daemon", "ao")


class _FetchFailure(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _fetch_github_latest(path):
    connection = http.client.HTTPSConnection(
        GITHUB_HOST, 443, context=deepseek_adapter.tls_context(), timeout=10
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "astra-fable-project-room",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response = connection.getresponse()
        if response.status in (403, 429):
            raise _FetchFailure("latest_rate_limited")
        if response.status != 200:
            raise _FetchFailure("latest_fetch_failed")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise _FetchFailure("latest_fetch_failed")
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise _FetchFailure("latest_fetch_failed") from exc
    finally:
        connection.close()


def _fetch_latest():
    return _fetch_github_latest(GITHUB_PATH)


def _fetch_claude_latest():
    return _fetch_github_latest(CLAUDE_GITHUB_PATH)


def _source_uri_allowed(uri):
    """https on a first-party Claude documentation host only: no userinfo, no non-443 port, no IP."""
    if not isinstance(uri, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(uri)
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname
    if (parsed.scheme != "https" or host is None or host not in QUALIFICATION_SOURCE_HOSTS
            or parsed.username is not None or parsed.password is not None
            or (port is not None and port != 443)):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _fetch_source(uri):
    """One bounded https GET of a qualification source URI; no credentials, no redirects."""
    parsed = urllib.parse.urlsplit(uri) if isinstance(uri, str) else None
    if parsed is None or parsed.scheme != "https" or not parsed.hostname:
        raise _FetchFailure("source_not_https")
    if not _source_uri_allowed(uri):
        raise _FetchFailure("source_host_not_allowed")
    connection = http.client.HTTPSConnection(
        parsed.hostname, parsed.port or 443,
        context=deepseek_adapter.tls_context(), timeout=15,
    )
    try:
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection.request(
            "GET", target,
            headers={"Accept": "*/*", "User-Agent": "astra-fable-project-room"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise _FetchFailure("source_fetch_failed")
        body = response.read(ao_model_qualification.MAX_EVIDENCE_BYTES + 1)
        if len(body) > ao_model_qualification.MAX_EVIDENCE_BYTES:
            raise _FetchFailure("source_too_large")
        return body
    finally:
        connection.close()


def _fetch_health(ao_url):
    client = ao_project_room.Client(ao_url)
    request = urllib.request.Request(
        client.base + "/healthz", method="GET", headers={"Accept": "application/json"}
    )
    with client.opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise OSError("AO health endpoint returned a non-200 status")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("AO health response exceeds its size bound")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("AO health response is not valid JSON") from exc


def _read_bounded_regular(path, maximum):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise OSError("File is not a bounded regular file")
        body = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if len(body) > maximum or before_signature != after_signature:
        raise OSError("File changed while being read")
    return body


def _release(raw, prefix=GITHUB_RELEASE_PREFIX):
    if (not isinstance(raw, dict) or raw.get("draft") is not False
            or raw.get("prerelease") is not False):
        return None
    tag = raw.get("tag_name")
    match = TAG_PATTERN.fullmatch(tag) if isinstance(tag, str) else None
    if match is None:
        return None
    url = raw.get("html_url")
    if not isinstance(url, str) or not url.startswith(prefix):
        url = None
    published_at = raw.get("published_at")
    if not isinstance(published_at, str) or len(published_at) > 64:
        published_at = None
    return {
        "tag_name": tag,
        "html_url": url,
        "published_at": published_at,
        "version": tuple(int(part) for part in match.groups()),
    }


def _daemon(value):
    if (not isinstance(value, dict) or value.get("service") != "agent-orchestrator-daemon"
            or value.get("status") != "ok" or type(value.get("pid")) is not int
            or value["pid"] <= 0 or not isinstance(value.get("executablePath"), str)
            or not Path(value["executablePath"]).is_absolute()):
        return None
    return {
        "pid": value["pid"],
        "executable_path": value["executablePath"],
        "service": value["service"],
    }


def _bundle_timestamps(executable, info):
    executable_stat = executable.stat()
    info_stat = info.stat()
    return (
        executable_stat.st_mtime,
        executable_stat.st_ctime,
        info_stat.st_mtime,
        info_stat.st_ctime,
    )


def _bundle(executable_path):
    try:
        executable = Path(executable_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None, None, "executable_not_app_bundle"
    app = next((parent for parent in executable.parents if parent.name.endswith(".app")), None)
    if app is None:
        return None, None, "executable_not_app_bundle"
    try:
        if executable.relative_to(app).parts != APP_CONTENTS:
            return None, None, "executable_not_app_bundle"
    except ValueError:
        return None, None, "executable_not_app_bundle"
    info = app / "Contents" / "Info.plist"
    try:
        data = _read_bounded_regular(info, MAX_PLIST_BYTES)
        metadata = plistlib.loads(data)
    except Exception:
        return None, None, "bundle_version_unreadable"
    if not isinstance(metadata, dict) or "CFBundleShortVersionString" not in metadata:
        return None, None, "bundle_version_unreadable"
    version = metadata["CFBundleShortVersionString"]
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        return None, None, "bundle_version_dev"
    try:
        timestamps = _bundle_timestamps(executable, info)
    except OSError:
        return None, None, "bundle_version_unreadable"
    return version, timestamps, None


def _process_start(pid, run):
    try:
        result = run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            timeout=5,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"},
        )
        if result.returncode != 0:
            return None
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("ascii")
        if not isinstance(output, str):
            return None
        return time.mktime(time.strptime(output.strip(), PS_TIME_FORMAT))
    except Exception:
        return None


def _existing_record(path):
    if not os.path.lexists(path):
        return None
    try:
        value = json.loads(_read_bounded_regular(path, MAX_STATE_BYTES))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        value = None
    if isinstance(value, dict) and value.get("schema") == SCHEMA:
        return value
    for suffix in range(1, 101):
        name = "version-check.legacy.json" if suffix == 1 else f"version-check.legacy-{suffix}.json"
        try:
            os.link(path, path.with_name(name))
        except FileExistsError:
            continue
        except OSError:
            return _LEGACY_EVIDENCE_NOT_PRESERVED
        try:
            os.unlink(path)
        except OSError:
            return _LEGACY_EVIDENCE_NOT_PRESERVED
        return None
    return _LEGACY_EVIDENCE_NOT_PRESERVED


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _version_tuple(text):
    match = VERSION_PATTERN.search(text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups()) if match is not None else None


def _version_string(value):
    return ".".join(str(part) for part in value) if value is not None else None


def _qualification_artifact(root):
    """The configured family_qualification artifact verified against its pointer digest, or None.

    Raises on a configured pointer whose artifact is unreadable or digest-mismatched; a missing or
    unreadable AO config, or an absent pointer, returns None."""
    try:
        config = json.loads(_read_bounded_regular(Path(root) / "config.json", MAX_STATE_BYTES))
    except (OSError, ValueError, TypeError, RecursionError):
        config = None
    if not isinstance(config, dict):
        return None
    pointer = config.get(ao_engineering_model.QUALIFICATION_KEY)
    if not isinstance(pointer, dict):
        return None
    raw = ao_model_qualification._private_bytes(
        pointer.get("path"), ao_model_qualification.MAX_ARTIFACT_BYTES,
        "Family qualification artifact")
    artifact = json.loads(raw)
    if (not isinstance(artifact, dict)
            or ao_model_qualification.digest(artifact) != pointer.get("sha256")):
        raise ValueError("Family qualification artifact does not match its configured digest")
    return artifact


def _model_ids(data):
    """Ordered unique (family, identifier) pairs of claude-<family>-<digits> matches in raw bytes."""
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else ""
    seen = set()
    pairs = []
    for match in MODEL_ID_PATTERN.finditer(text):
        if match.group(0) not in seen:
            seen.add(match.group(0))
            pairs.append((match.group(1), match.group(0)))
    return pairs


def _claude_version(run, path):
    try:
        completed = run([path, "--version"], capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    output = completed.stdout if completed.returncode == 0 else None
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    return _version_tuple(output)


def _claude_code(root, run, home, fetch_claude_latest):
    """Report-only staleness view of the configured claude_bin: floors, installs and latest release."""
    reasons = set()
    configured = {"version": None, "reasons": []}
    configured_tuple = None
    try:
        parsed = json.loads(
            _read_bounded_regular(Path(root).parent / "config.json", MAX_STATE_BYTES))
        controller = parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, TypeError, RecursionError):
        controller = None
    if controller is None:
        configured["reasons"].append("controller_config_unreadable")
    else:
        claude_bin = controller.get("claude_bin")
        if not isinstance(claude_bin, str) or not claude_bin:
            configured["reasons"].append("claude_bin_unconfigured")
        else:
            try:
                details = os.stat(claude_bin)
                if not stat.S_ISREG(details.st_mode) or not os.access(claude_bin, os.X_OK):
                    raise OSError("claude_bin is not an executable regular file")
            except OSError:
                configured["reasons"].append("claude_bin_missing")
            else:
                configured_tuple = _claude_version(run, claude_bin)
                if configured_tuple is None:
                    configured["reasons"].append("claude_bin_version_unavailable")
                else:
                    configured["version"] = _version_string(configured_tuple)

    floors_incomplete = False
    try:
        policy, _ = ao_engineering_model.effective_policy(root)
        floor_entries = list(policy["families"].values()) + list(policy["models"].values())
    except Exception:
        floors_incomplete = True
        reasons.add("floors_incomplete")
        floor_entries = (list(ao_engineering_model.BUNDLED_FAMILIES.values())
                         + list(ao_engineering_model.BUNDLED_MODELS.values()))
    floors = [entry.get("minimum_claude_code_version") for entry in floor_entries
              if isinstance(entry, dict)]
    floor = ao_engineering_model._newer_floor(*floors)
    floor_tuple = _version_tuple(floor) if floor is not None else None

    candidates = []
    versions_dir = Path(home) / ".local" / "share" / "claude" / "versions"
    try:
        entries = list(versions_dir.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        match = VERSION_PATTERN.fullmatch(entry.name)
        if match is None:
            continue
        executable = entry if entry.is_file() else entry / "claude"
        try:
            details = executable.stat()
        except OSError:
            continue
        if stat.S_ISREG(details.st_mode) and os.access(executable, os.X_OK):
            candidates.append((tuple(int(part) for part in match.groups()), str(executable), True))
    located = shutil.which("claude")
    if located is not None:
        try:
            resolved = str(Path(located).resolve())
        except OSError:
            resolved = None
        if resolved is not None and resolved not in {path for _, path, _ in candidates}:
            located_tuple = _claude_version(run, resolved)
            if located_tuple is not None:
                candidates.append((located_tuple, resolved, False))
    newest_installed = None
    newest_tuple = None
    for name_tuple, path, needs_probe in sorted(candidates, key=lambda pair: pair[0], reverse=True):
        probed = _claude_version(run, path) if needs_probe else name_tuple
        if probed is None or probed != name_tuple:
            reasons.add("installed_version_mismatch")
            continue
        newest_tuple = probed
        newest_installed = {"version": _version_string(probed), "path": path}
        break
    if newest_installed is None:
        reasons.add("no_installed_claude_found")

    latest_published = None
    published_tuple = None
    try:
        released = _release(fetch_claude_latest(), prefix=CLAUDE_RELEASE_PREFIX)
    except _FetchFailure as exc:
        reasons.add(exc.reason)
    except Exception:
        reasons.add("claude_latest_fetch_failed")
    else:
        if released is None:
            reasons.add("claude_latest_invalid")
        else:
            published_tuple = released["version"]
            latest_published = {"version": _version_string(published_tuple),
                                "tag_name": released["tag_name"], "html_url": released["html_url"],
                                "published_at": released["published_at"]}

    floor_satisfied = (configured_tuple >= floor_tuple
                       if configured_tuple is not None and floor_tuple is not None else None)
    if floors_incomplete and floor_satisfied is True:
        floor_satisfied = None  # the lower bound can assert below_floor but never satisfaction
    if floor_satisfied is False:
        outcome = "below_floor"
    elif configured_tuple is not None and (
            (newest_tuple is not None and newest_tuple > configured_tuple)
            or (published_tuple is not None and published_tuple > configured_tuple)):
        outcome = "update_available"
    elif (floor_satisfied is True and published_tuple is not None
          and configured_tuple >= published_tuple
          and (newest_tuple is None or newest_tuple <= configured_tuple)):
        outcome = "up_to_date"
    else:
        outcome = "unknown"
    action = None
    qualifies = (newest_installed is not None and configured_tuple is not None
                 and newest_tuple > configured_tuple
                 and (floor_tuple is None or newest_tuple >= floor_tuple))
    if outcome in ("below_floor", "update_available") and qualifies and floors_incomplete:
        # The bundled lower bound can prove the configured executable insufficient, but it cannot
        # certify a replacement against the unread configured or qualified floors.
        reasons.add("action_withheld_floors_incomplete")
    elif outcome in ("below_floor", "update_available") and qualifies:
        action = "python3 project_room.py setup --claude-bin " + newest_installed["path"]
    elif outcome == "below_floor":
        reasons.add("no_installed_candidate_satisfies_floor")
    return {"configured": configured, "highest_family_floor": floor,
            "floor_satisfied": floor_satisfied, "newest_installed": newest_installed,
            "latest_published": latest_published, "outcome": outcome,
            "reasons": sorted(reasons), "action": action}


def _qualification_sources(root, fetch_source):
    """Report-only drift view: whether each qualification source still serves the captured bytes."""
    try:
        artifact = _qualification_artifact(root)
    except Exception:
        return {"artifact_present": False, "revision": None, "outcome": "unknown",
                "reasons": ["qualification_artifact_unreadable"], "sources": []}
    if artifact is None:
        return {"artifact_present": False, "revision": None, "outcome": "no_qualification",
                "reasons": [], "sources": []}
    reasons = set()
    entries = []
    identifiers_differ = False
    bytes_differ = False
    doubtful = False
    sources = artifact.get("sources")
    for descriptor in sources if isinstance(sources, list) else []:
        if not isinstance(descriptor, dict):
            continue
        entry = {"id": descriptor.get("id"), "outcome": "unreachable",
                 "bytes_match": None, "identifiers_not_in_capture": {},
                 "identifiers_only_in_capture": {}}
        if not _source_uri_allowed(descriptor.get("uri")):
            reasons.add("source_host_not_allowed")
            doubtful = True
            entries.append(entry)
            continue
        try:
            body = fetch_source(descriptor.get("uri"))
        except _FetchFailure as exc:
            reasons.add(exc.reason)
            doubtful = True
        except Exception:
            reasons.add("source_fetch_failed")
            doubtful = True
        else:
            try:
                captured = ao_model_qualification._private_bytes(
                    descriptor.get("evidence_file"), ao_model_qualification.MAX_EVIDENCE_BYTES,
                    "Source evidence")
            except Exception:
                entry["outcome"] = "evidence_unreadable"
                reasons.add("evidence_unreadable")
                doubtful = True
            else:
                if hashlib.sha256(captured).hexdigest() != descriptor.get("sha256"):
                    entry["outcome"] = "evidence_digest_mismatch"
                    reasons.add("evidence_digest_mismatch")
                    doubtful = True
                    entries.append(entry)
                    continue
                entry["bytes_match"] = (
                    hashlib.sha256(body).hexdigest() == descriptor.get("sha256"))
                captured_ids = {identifier for _, identifier in _model_ids(captured)}
                fresh_pairs = _model_ids(body)
                fresh_ids = {identifier for _, identifier in fresh_pairs}
                not_in_capture = {}
                for family, identifier in fresh_pairs:
                    if identifier not in captured_ids:
                        not_in_capture.setdefault(family, []).append(identifier)
                only_in_capture = {}
                for family, identifier in _model_ids(captured):
                    if identifier not in fresh_ids:
                        only_in_capture.setdefault(family, []).append(identifier)
                entry["identifiers_not_in_capture"] = not_in_capture
                entry["identifiers_only_in_capture"] = only_in_capture
                if not_in_capture or only_in_capture:
                    entry["outcome"] = "identifiers_differ"
                    identifiers_differ = True
                elif entry["bytes_match"]:
                    entry["outcome"] = "unchanged"
                else:
                    entry["outcome"] = "bytes_differ"
                    bytes_differ = True
        entries.append(entry)
    if identifiers_differ:
        outcome = "identifiers_differ"
    elif bytes_differ:
        outcome = "bytes_differ"
    elif doubtful:
        outcome = "unknown"
    else:
        outcome = "unchanged"
    revision = artifact.get("revision")
    return {"artifact_present": True,
            "revision": revision if isinstance(revision, int) and not isinstance(revision, bool)
            else None,
            "outcome": outcome, "reasons": sorted(reasons), "sources": entries}


def check(root, ao_url, fetch_latest=None, fetch_health=None, run=subprocess.run, now=time.time,
          fetch_claude_latest=None, fetch_source=None, home=None):
    home = Path.home() if home is None else Path(home)
    claude_code = _claude_code(root, run, home,
        _fetch_claude_latest if fetch_claude_latest is None else fetch_claude_latest)
    qualification_sources = _qualification_sources(
        root, _fetch_source if fetch_source is None else fetch_source)

    latest = None
    latest_version = None
    reasons = set()
    try:
        fetch = _fetch_latest if fetch_latest is None else fetch_latest
        raw_latest = fetch()
    except _FetchFailure as exc:
        reasons.add(exc.reason)
    except Exception:
        reasons.add("latest_fetch_failed")
    else:
        latest = _release(raw_latest)
        if latest is None:
            reasons.add("latest_invalid")
        else:
            latest_version = latest["version"]

    daemon = None
    installed_version = None
    installed_version_tuple = None
    running_version = None
    running_version_tuple = None
    replaced = False
    if ao_url is None:
        reasons.add("daemon_url_unconfigured")
    else:
        try:
            fetch = _fetch_health if fetch_health is None else fetch_health
            raw_health = fetch(ao_url)
        except ao_project_room.RoomError:
            reasons.add("daemon_url_invalid")
        except Exception:
            reasons.add("daemon_unreachable")
        else:
            daemon = _daemon(raw_health)
            if daemon is None:
                reasons.add("daemon_identity_invalid")
            else:
                installed_version, mtimes, bundle_reason = _bundle(daemon["executable_path"])
                if bundle_reason is not None:
                    reasons.add(bundle_reason)
                else:
                    version_match = VERSION_PATTERN.fullmatch(installed_version)
                    installed_version_tuple = tuple(int(part) for part in version_match.groups())
                    start = _process_start(daemon["pid"], run)
                    if start is None:
                        reasons.add("process_start_unavailable")
                    elif max(mtimes) > start + 1:
                        reasons.add("bundle_replaced_since_daemon_start")
                        replaced = True
                    else:
                        running_version = installed_version
                        running_version_tuple = installed_version_tuple

    if replaced:
        outcome = "mismatch"
    elif latest_version is None or running_version is None:
        outcome = "unknown"
    elif running_version_tuple == latest_version:
        outcome = "up_to_date"
    elif running_version_tuple < latest_version:
        outcome = "update_available"
    else:
        reasons.add("running_newer_than_latest_stable")
        outcome = "unknown"

    record_path = Path(root) / "version-check.json"
    lock_path = record_path.with_name(".version-check.lock")
    record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        checked_at = _timestamp(now())
        previous = _existing_record(record_path)
        evidence_saved = previous is not _LEGACY_EVIDENCE_NOT_PRESERVED
        if not evidence_saved:
            reasons.add("evidence_not_saved")
            previous = None
        record = {
            "schema": SCHEMA,
            "checked_at": checked_at,
            "outcome": outcome,
            "reasons": sorted(reasons),
            "latest": ({key: latest[key] for key in ("tag_name", "html_url", "published_at")}
                       if latest is not None else None),
            "installed_version": installed_version,
            "running_version": running_version,
            "daemon": daemon,
            "claude_code": claude_code,
            "qualification_sources": qualification_sources,
        }
        if outcome != "unknown" and latest is not None:
            record["last_successful"] = dict(record)
        else:
            record["last_successful"] = previous.get("last_successful") if previous is not None else None
        if evidence_saved:
            ao_project_room.atomic(record_path, record)
        last_successful = record["last_successful"]
        result = {
            "outcome": outcome,
            "reasons": sorted(reasons),
            "latest_version": latest["tag_name"] if latest is not None else None,
            "installed_version": installed_version,
            "running_version": running_version,
            "release_url": latest["html_url"] if latest is not None else None,
            "checked_at": checked_at,
            "last_successful_checked_at": (
                last_successful.get("checked_at") if isinstance(last_successful, dict) else None
            ),
            "claude_code": claude_code,
            "qualification_sources": qualification_sources,
        }
    finally:
        os.close(lock_fd)
    return result
