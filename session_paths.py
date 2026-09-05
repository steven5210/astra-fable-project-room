"""Resolve only one known Claude session UUID, including hashed project directories."""

import json
import errno
from pathlib import Path
import uuid


class SessionPathError(Exception):
    pass


def find_session_transcript(config_dir, session_id, expected_cwd=None, predicted_path=None):
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError()
    except (ValueError, AttributeError, TypeError) as exc:
        raise SessionPathError("Session ID must be an exact canonical UUID") from exc
    projects = (Path(config_dir).expanduser().resolve() / "projects").resolve()
    filename = session_id + ".jsonl"
    predicted = Path(predicted_path).expanduser() if predicted_path else None
    try:
        predicted_exists = predicted is not None and predicted.is_file()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        predicted_exists = False
    if predicted_exists:
        if predicted.name != filename:
            raise SessionPathError("Predicted transcript filename does not match the exact session UUID")
        matches = [predicted]
    else:
        # Enumerate only filenames for this UUID. Never read another transcript or
        # choose a session by recency, title, contents, or approximate matching.
        matches = list(projects.glob("*/" + filename)) if projects.is_dir() else []
    if len(matches) != 1:
        raise SessionPathError(f"Expected one exact UUID transcript; found {len(matches)}")
    selected = matches[0].resolve()
    if projects not in selected.parents:
        raise SessionPathError("Session transcript resolves outside the configured projects directory")
    expected = Path(expected_cwd).resolve() if expected_cwd is not None else None
    seen_session = False
    try:
        with selected.open(encoding="utf-8") as source:
            for line in source:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("sessionId") != session_id:
                    continue
                seen_session = True
                if expected is not None and "cwd" in record:
                    cwd = record["cwd"]
                    if not isinstance(cwd, str) or Path(cwd).resolve() != expected:
                        raise SessionPathError("Session transcript cwd differs from the expected worktree")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise SessionPathError("Cannot validate the exact session transcript") from exc
    if not seen_session:
        raise SessionPathError("Transcript contains no metadata for the exact session UUID")
    return str(selected)
