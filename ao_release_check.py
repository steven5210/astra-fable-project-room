"""Read-only comparison of the running AO daemon with its latest stable release."""

import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import time
from datetime import datetime, timezone
import urllib.request

import ao_project_room


SCHEMA = "ao-release-check/v1"
GITHUB_HOST = "api.github.com"
GITHUB_PATH = "/repos/Untrivial-ai/agent-orchestrator/releases/latest"
GITHUB_RELEASE_PREFIX = "https://github.com/Untrivial-ai/agent-orchestrator/releases/"
MAX_RESPONSE_BYTES = 1_000_000
MAX_PLIST_BYTES = 1_000_000
MAX_STATE_BYTES = 1_000_000
VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
TAG_PATTERN = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
PS_TIME_FORMAT = "%a %b %d %H:%M:%S %Y"
APP_CONTENTS = ("Contents", "Resources", "daemon", "ao")


class _FetchFailure(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _fetch_latest():
    import deepseek_adapter

    connection = http.client.HTTPSConnection(
        GITHUB_HOST, 443, context=deepseek_adapter.tls_context(), timeout=10
    )
    try:
        connection.request(
            "GET",
            GITHUB_PATH,
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


def _fetch_health(ao_url):
    client = ao_project_room.Client(ao_url)
    request = urllib.request.Request(
        client.base + "/healthz", method="GET", headers={"Accept": "application/json"}
    )
    with client.opener.open(request, timeout=15) as response:
        if getattr(response, "status", 200) != 200:
            raise OSError("AO health endpoint returned a non-200 status")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("AO health response exceeds its size bound")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("AO health response is not valid JSON") from exc


def _read_bounded_regular(path, maximum):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
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


def _release(raw):
    if (not isinstance(raw, dict) or raw.get("draft") is not False
            or raw.get("prerelease") is not False):
        return None
    tag = raw.get("tag_name")
    match = TAG_PATTERN.fullmatch(tag) if isinstance(tag, str) else None
    if match is None:
        return None
    url = raw.get("html_url")
    if not isinstance(url, str) or not url.startswith(GITHUB_RELEASE_PREFIX):
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
        executable_mtime = executable.stat().st_mtime
        info_mtime = info.stat().st_mtime
    except OSError:
        return None, None, "bundle_version_unreadable"
    return version, (executable_mtime, info_mtime), None


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
    legacy = path.with_name("version-check.legacy.json")
    if not os.path.lexists(legacy):
        os.replace(path, legacy)
    return None


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def check(root, ao_url, fetch_latest=None, fetch_health=None, run=subprocess.run, now=time.time):
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

    checked_at = _timestamp(now())
    record_path = Path(root) / "version-check.json"
    previous = _existing_record(record_path)
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
    }
    if outcome != "unknown":
        record["last_successful"] = dict(record)
    else:
        record["last_successful"] = previous.get("last_successful") if previous is not None else None
    ao_project_room.atomic(record_path, record)
    last_successful = record["last_successful"]
    return {
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
    }
