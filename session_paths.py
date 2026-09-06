"""Resolve only one known Claude session UUID, including hashed project directories."""

import errno
import json
import os
from pathlib import Path
import uuid


LINE_LIMIT = 64 * 1024 * 1024  # One transcript record; longer lines are refused rather than buffered without bound.


class SessionPathError(Exception):
    """`code` is a stable allowlisted reason: transcript_missing_or_ambiguous (default), transcript_unparsable (a record
    is not valid JSON), transcript_worktree_mismatch (a record's cwd lies outside the expected worktree) or
    transcript_worktree_unbound (no record names the worktree root itself as the required anchor)."""

    def __init__(self, message, code="transcript_missing_or_ambiguous"):
        super().__init__(message)
        self.code = code


def find_session_transcript(config_dir, session_id, expected_cwd=None, predicted_path=None, require_unique=False,
                            require_cwd=False, opener=None):
    """Locate the exact session transcript. With require_unique, the projects directory is always
    enumerated, exactly one UUID file must exist, and an existing predicted path must be that file."""
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
    if predicted_exists and predicted.name != filename:
        raise SessionPathError("Predicted transcript filename does not match the exact session UUID")
    if predicted_exists and not require_unique:
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
    if require_unique and predicted_exists and predicted.resolve() != selected:
        raise SessionPathError("Predicted transcript path does not resolve to the unique exact-UUID transcript")
    validate_session_metadata(selected, session_id, expected_cwd, require_cwd=require_cwd, opener=opener)
    return str(selected)


def cwd_relation(cwd, expected):
    """'root' when `cwd` names exactly the canonical expected worktree, 'inside' when it names a canonical
    descendant of it, None otherwise. `cwd` must be an absolute string and is resolved canonically (symlinks
    followed), so a relative or malformed value, a lookalike string prefix, a foreign worktree or a symlink that
    escapes the worktree never counts. Path containment is decided on resolved components, never on text."""
    if not isinstance(cwd, str) or not cwd or "\0" in cwd or not os.path.isabs(cwd):
        return None
    try:
        resolved = Path(cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved == expected:
        return "root"
    return "inside" if expected in resolved.parents else None


def validate_session_metadata(path, session_id, expected_cwd=None, require_cwd=False, opener=None):
    """Require records for the exact session UUID whose cwd metadata belongs to the expected worktree.

    Every exact-session record that carries a cwd must name the canonical worktree root or a canonical directory
    inside it (changing into a project subdirectory is normal work within that worktree); anything else is
    contradictory. With require_cwd, at least one exact-session record must name the worktree root itself (the
    anchor); descendant-only metadata does not replace that anchor and is refused as unbound, distinct from
    contradictory evidence. `opener` may supply a bounded, symlink-refusing text handle."""
    expected = Path(expected_cwd).resolve() if expected_cwd is not None else None
    seen_session = seen_root = seen_inside = False
    try:
        with (opener(path) if opener else Path(path).open(encoding="utf-8")) as source:
            while True:
                line = source.readline(LINE_LIMIT + 1)
                if not line:
                    break
                if len(line) > LINE_LIMIT:
                    raise SessionPathError("Transcript record exceeds the bounded line length")
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("sessionId") != session_id:
                    continue
                seen_session = True
                if expected is not None and "cwd" in record:
                    relation = cwd_relation(record["cwd"], expected)
                    if relation is None:
                        raise SessionPathError("Session transcript cwd lies outside the expected worktree", "transcript_worktree_mismatch")
                    if relation == "root":
                        seen_root = True
                    else:
                        seen_inside = True
    except (ValueError, UnicodeDecodeError) as exc:
        raise SessionPathError("Session transcript contains an unparsable record", "transcript_unparsable") from exc
    except OSError as exc:
        raise SessionPathError("Cannot validate the exact session transcript") from exc
    if not seen_session:
        raise SessionPathError("Transcript contains no metadata for the exact session UUID")
    if require_cwd and expected is not None and not seen_root:
        raise SessionPathError("Transcript carries no cwd record naming the expected worktree root itself"
                               + (" (only directories inside it)" if seen_inside else ""), "transcript_worktree_unbound")


def explicit_session_transcript(config_dir, session_id, expected_cwd, explicit_path, require_cwd=False, opener=None):
    """Validate a configured explicit transcript path; the projects directory must hold no other file for the UUID."""
    path = Path(explicit_path)
    if not path.is_absolute() or path.name != session_id + ".jsonl" or path.is_symlink() or not path.is_file():
        raise SessionPathError("Explicit transcript path must be an absolute regular file named for the exact session UUID")
    projects = (Path(config_dir).expanduser().resolve() / "projects").resolve()
    others = [match for match in (projects.glob("*/" + path.name) if projects.is_dir() else []) if match.resolve() != path.resolve()]
    if others:
        raise SessionPathError(f"Expected one exact UUID transcript; found {len(others) + 1}")
    validate_session_metadata(path, session_id, expected_cwd, require_cwd=require_cwd, opener=opener)
    return str(path.resolve())
