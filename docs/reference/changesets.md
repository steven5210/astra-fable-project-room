# Deterministic change-set plan, apply, audit and resume

The standalone tool is `python3 changeset_tool.py plan|apply|audit|resume`. It is dependency-free, POSIX, Python 3.10+, and never calls a model, network, shell, subprocess or project check. The runtime never spawns processes; only tests fork children to exercise interruption behavior.

## Boundaries

`plan` validates the closed envelope, every target beneath the explicit workspace, every draft, every pinned input and the JSON renderer profile, simulates all operations in envelope order and writes proposed bytes plus a private content-addressed plan under the explicit state root. It never writes the candidate or creates in-tree temporary files. `apply` requires an independently supplied expected plan SHA256 and a receipt file, revalidates all inputs and pins, stages every replacement on the target filesystem, verifies every staged sibling, commits one immutable prepared journal, then performs per-file atomic rename. `audit` is read-only, takes only an existing shared lock and reports content and identity separately plus leftover staging. `resume` requires the expected plan SHA256 and a fresh audit digest, refuses foreign inodes, preserves earlier records and applies only exact-before targets. Completion returns the existing receipt; it does not apply twice.

## Closed envelope version 1

Top-level fields are exactly `envelope_version` (integer 1), `batch_id` (nonempty text), `execution` (`sequential-v1`), `targets` (1..32) and `conditions` (0..256, key present). A target entry has exactly `target`, `source`, `draft` and `renderer`. `source` has `bytes`, `sha256`, `mode` (an integer, for example 420 for octal 0644); `draft` has `locator`, `bytes`, `sha256`. Paths are normalized relative paths under the selected root. Hashes are lowercase SHA-256 hex. The renderer is null for `anchored-text-v1` and one of `json-2space-unicode-lf0`, `json-2space-unicode-lf1`, `json-2space-ascii-lf0`, `json-2space-ascii-lf1` for `json-ops-v1`.

A draft has required fields `changeset_format`, `document` (equal to the target path), `request_id`, `operations`. Optional advisory fields are `self_check` (object), `inventory` (array), and `location_dispositions` (array of objects, each with an `operation_ids` array referencing operations in that draft). They never grant authority or cause execution. Each operation requires a unique `id` and may have string annotations `location`, `rationale`, `requirement`. Operation IDs are unique across the batch. Empty operation arrays and no-ops are valid.

An `anchored-text-v1` operation has `id`, nonempty `anchor`, and `replacement`, plus the optional annotations. The anchor must occur exactly once, counting overlapping matches. The replacement preserves every other UTF-8 byte, including CRLF and the absence of a final newline.

Every `json-ops-v1` operation also has `op` and `path`, plus exactly these fields:

| `op` | Required fields and checks |
| --- | --- |
| `set_value` | `old`, `new`: the current value must equal `old`, preserving nested types and the sign of floating-point zero. |
| `edit_string` | `anchor`, `replacement`: one nonempty anchor occurrence within the current string. |
| `replace_string` | `old_starts_with`, `old_ends_with`, `new`: both nonempty boundaries must match; replace the whole string. |
| `append_items` | `old_length`, `items`: the target array length must match before appending. |
| `insert_key_after` | `after_key`, `key`, `value`: insert a new object key after one existing key. |
| `insert_items_after` | `after` (exactly `{ "id": "..." }`), `items`: insert after one uniquely selected object in an array. |

Paths are arrays of existing object-key strings, nonnegative integer array indices (never booleans), or exact one-key `{"id":"..."}` selectors matching exactly one current array element. Collection operations allow `[]` to select the document root; the three scalar/string operations require a nonempty path. No implicit key creation, deletion, head insertion, or insertion into an empty object is supported. Existing duplicate string IDs outside an affected array do not prevent unrelated edits; an edit cannot introduce or increase duplicate IDs in the affected array.

JSON is strict: duplicate keys, BOMs, invalid UTF-8, unpaired surrogates, nonfinite values, exponent overflow and nonzero literals that underflow to zero refuse. Booleans, integers and floats are distinct. JSON source must round-trip byte-for-byte under the selected two-space renderer, with original key order, either literal Unicode or ASCII escapes, and exactly zero or one final LF. The tool refuses formatting it cannot preserve. Operations run sequentially against the preceding simulated result.

Conditions have `id`, `phase`, `text`, `description`, `inputs` and, only for `post_apply`, `why_not_before` and `failure_handling`. Pinned inputs are workspace- or input-rooted, must not alias a target and are streamed for hashing only. Receipt templates contain exactly the required pre_apply condition IDs, plan SHA256, staged-set digest and pinned input digests, each result preset to unknown. Apply accepts only a complete one-to-one set of passing pre_apply receipts; post_apply conditions never pass through the template. Editing a receipt is an operator statement; the tool verifies identities, not check execution.

`phase` is `pre_apply` or `post_apply`; descriptions and post-apply explanations are nonempty strings. Each pinned input has exactly `id`, `root` (`workspace` or `input`), `path`, `bytes`, `sha256`. Condition IDs and input IDs each have a batch-wide unique namespace. Post-apply checks must explain why they cannot run before replacement and what the operator will do if they fail. These are reviewed descriptions; the tool never executes them.

## Receipts and private records

Plan writes `plans/<plan-sha>.json`, `templates/<plan-sha>.json`, and exact content-addressed bytes in `sources/`, `drafts/`, `staged/`. These are private evidence outside the candidate. Input-file drafts remain required at their original locator on application and recovery; drafts explicitly supplied through the Python API have retained inline origin instead. Root path, device, inode and mode are pinned. Copying state or replacing a root does not create a new valid identity.

A refused plan may retain bounded private target blobs already staged while validating earlier targets. It never writes the candidate or commits a plan/template before all check pins pass. Pinned check inputs are streamed and are never copied into these stores.

The immutable derived template has exactly `receipt_version` (integer 1), `plan_sha256`, `staged_set_sha256`, `input_digests` and `conditions`. Each input entry has `condition_id`, `input_id`, `sha256`. Each condition entry has `id`, `phase: "pre_apply"`, `result: "unknown"`, `evidence: ""`. Copy the template into a separate input receipt file, run the actual project checks separately, and fill `result: "pass"` plus a string evidence reference. The template itself stays unchanged. Omitting or inventing conditions/inputs, changing pins, duplicate entries, and fail/unknown outcomes refuse. An explicitly empty condition inventory permits an empty passing receipt inventory. A nonempty unchanged template cannot pass. Post-apply conditions are never put in this passing inventory.

The staged-set digest hashes canonical `{"items":[{"target":...,"sha256":...,"bytes":...},...]}` in envelope order. Private structural records use sorted-key compact UTF-8 JSON without floats; a record's own digest field is excluded only when computing that digest. Plans and audits contain no timestamps. Every loaded plan is schema-validated in addition to checking its hash; recomputing a malformed plan's hash cannot make it executable. Public results contain hashes of opaque batch/condition/operation identities, counts and fixed relative evidence references, with no source bytes or absolute paths.

A matching digest or a passing mechanical result is not approval. The authorized operator owns the check statements. Success is `applied_unverified`; product checks, engineering review and independent acceptance still apply.

## Python API

`changeset_tool.plan(workspace, state_root, input_dir, envelope_bytes, supplied_drafts=None)` accepts exact draft bytes through `supplied_drafts` keyed by locator. `changeset_tool.apply(workspace, state_root, input_dir, plan_sha256, receipts=None, receipts_locator=None)` and `changeset_tool.resume(workspace, state_root, input_dir, plan_sha256, audit_sha256, receipts=None, receipts_locator=None)` accept receipt bytes or a relative locator under the input directory. `changeset_tool.audit(workspace, state_root)` is read-only.

## Filesystem and metadata limits

Workspace, input and state roots are explicit absolute paths with no symlink components. State and input roots must be owned private 0700 directories, outside each other, the workspace and the tool installation. Lexical paths and descriptor ancestry must agree; nested roots refuse. Private state is sensitive and should remain outside version control.

The tool refuses non-normalized target paths, dot components, NUL, backslash separators, symlink components or targets, non-regular files, hardlinks, duplicate target inodes, special mode bits, unsupported inode flags, nontrivial ACLs and any xattr that cannot be read and reproduced exactly. It preserves ordinary mode, uid/gid and the exact supported opaque xattr inventory. macOS uses descriptor ACL/xattr APIs, treats a verified empty ACL as absent, and uses F_FULLFSYNC for durable data. It never strips an ACL or retries after removing metadata to make a write succeed.

macOS attribute enumeration deliberately uses `XATTR_SHOWCOMPRESSION` so attributes cannot be silently hidden. A compressed source has an unsupported inode flag and refuses before attribute staging; the tool does not decompress files to make them eligible.

Linux support requires proof of access to the trusted xattr namespace on the actual filesystem; effective root alone is insufficient, especially in a user namespace. Metadata on a different device from the retained private capability proof conservatively refuses. Unsupported hosts refuse before any target write. Read-only audit does not manufacture a capability proof. No privilege escalation is attempted. Native macOS tests exercise actual descriptors, attributes, durability and process interruption. Pure and refusal tests run on restricted Linux CI; those tests do not establish successful metadata-preserving Linux application.

## Resources

Limits: 32 targets, 10 MiB per source/draft/proposed/check input, 64 MiB aggregate retained plan bytes, 4096 operations, 256 conditions, 64 pinned inputs, JSON depth 64, path depth 32, 128-byte IDs, 4300-digit integers, 64 xattr names and 64 KiB xattrs per file. Persisted plan, journal, template and receipt JSON contain no floats; ordinary draft values are bound by exact byte digests. Plans and audits contain no timestamps.

Discovery admits at most 256 journals. Each journal has a budget of 256 outcome records and 512 scratch records, and an audit report is bounded to 10 MiB. Each staged replacement normally uses an intent and an ownership record; a crash may leave only the intent. Apply and recovery reserve two scratch records per remaining replacement before creating another attempt.

## Recovery

The prepared journal is the commit point. Before it, a crash leaves target bytes unchanged and permits a new apply. After it, a new apply refuses until explicit verified resume. Audit classifies missing, unreadable, before, after, unchanged, other, content and identity separately. Resume accepts the original inode for exact-before or no-op files and the journaled staged inode for exact-after files; a foreign inode with equal bytes refuses. Leftover staging is reported and only a conforming owned partial may be restaged. No journal-retirement, rollback, abandon or automatic retry command exists. The tool never claims power-loss durability from interruption tests; per-file atomic replacement leaves a documented check-to-rename window and does not protect against writers that ignore the cooperative lock.

Writer locks are persistent, nonblocking and keyed by workspace device/inode. Audit uses an existing shared lock and returns no reusable audit digest while a writer is active. New application is blocked by an unresolved journal matching either the recorded root path or identity. Journal discovery is bounded to 256 entries. A prepared journal binds the plan, passing receipt, backups, all proposed files and each staged replacement's exact inode before the first rename. Hash-linked immutable records detect corruption; they do not defend against a same-user attacker rewriting all private evidence and recomputing its hashes.

All cooperating invocations must use the same configured state root. A different state root, a removed lock file, or a writer ignoring the lock falls outside that isolation guarantee. The tool never deletes or steals a lock.

Resume rechecks every target, root, input, backup, retained draft and journal before another replacement. Scratch handling requires recorded ownership, inode, mode, metadata and an exact proposed-byte prefix; unrecorded or nonconforming scratch is preserved and refuses. Restaging preserves the old partial as evidence. Audit includes recorded `precommit_staging` and earlier-attempt leftovers. These `.changeset-*.stage` files may remain inside the candidate after an interrupted apply; do not commit them.

Foreign entries in the journal directory (including a file-browser metadata file) make apply, audit and resume refuse with `unknown_state_entry`, preserving the evidence. A staging-only journal whose plan is missing makes audit and resume refuse with `plan_missing`; no reusable audit digest is issued. Applying that missing plan also refuses. A different valid plan may still apply before the earlier journal's prepared commit point, preserving its leftovers. That new batch can be applied while audit/resume remain unavailable because the earlier precommit evidence cannot be interpreted. Some corrupt or externally modified states therefore have no automatic recovery command. Preserve that evidence for diagnosis rather than changing hashes to force recovery. Completed replay also revalidates current bytes and inputs, including the unchanged receipt at its recorded locator, before returning its old receipt. Files are replaced atomically one at a time; a batch is not a filesystem transaction.

## Commands and complete synthetic example

From the plugin source directory, the CLI forms are:

```sh
python3 changeset_tool.py plan --workspace /absolute/work --state-root /absolute/state --input-dir /absolute/input --envelope envelope.json
python3 changeset_tool.py apply --workspace /absolute/work --state-root /absolute/state --input-dir /absolute/input --plan-sha256 EXPECTED_SHA --receipts checks.json
python3 changeset_tool.py audit --workspace /absolute/work --state-root /absolute/state
python3 changeset_tool.py resume --workspace /absolute/work --state-root /absolute/state --input-dir /absolute/input --plan-sha256 EXPECTED_SHA --audit-sha256 FRESH_AUDIT_SHA --receipts checks.json
```

This runnable example creates temporary synthetic roots, verifies exact expected output independently, copies the template, and applies it. It needs a host satisfying the metadata/durability requirements. It changes only its temporary example file.

```python
import hashlib
import json
import os
import tempfile
from pathlib import Path
import changeset_tool as tool

def sha(data):
    return hashlib.sha256(data).hexdigest()

def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")

with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as temp:
    base = Path(temp)
    work, state, inputs = (base / name for name in ("work", "state", "input"))
    for root in (work, state, inputs):
        root.mkdir(mode=0o700)
    source, expected = b"old value\r\n", b"new value\r\n"
    (work / "note.txt").write_bytes(source)
    (work / "note.txt").chmod(0o600)
    draft = encoded({"changeset_format": "anchored-text-v1", "document": "note.txt",
                     "request_id": "example-request", "operations": [
                         {"id": "replace-note", "anchor": "old", "replacement": "new"}]})
    (inputs / "draft.json").write_bytes(draft)
    envelope = {"envelope_version": 1, "batch_id": "example-batch",
                "execution": "sequential-v1", "targets": [{
                    "target": "note.txt", "source": {"bytes": len(source),
                        "sha256": sha(source), "mode": 0o600},
                    "draft": {"locator": "draft.json", "bytes": len(draft),
                              "sha256": sha(draft)}, "renderer": None}],
                "conditions": [{"id": "exact-output", "phase": "pre_apply",
                    "text": "Expected bytes match", "description": "Literal CRLF oracle",
                    "inputs": []}]}
    planned = tool.plan(str(work), str(state), str(inputs), encoded(envelope))
    expected_plan = planned["plan_sha256"]
    assert (work / "note.txt").read_bytes() == source
    assert (state / "staged" / sha(expected)).read_bytes() == expected
    receipt = json.loads((state / "templates" / (expected_plan + ".json")).read_bytes())
    receipt["conditions"][0].update(result="pass", evidence="Literal expected-byte check above")
    (inputs / "checks.json").write_bytes(encoded(receipt))
    result = tool.apply(str(work), str(state), str(inputs), expected_plan,
                        receipts_locator="checks.json")
    assert (work / "note.txt").read_bytes() == expected
    print(json.dumps(result, sort_keys=True))
```

Run `python3 -m unittest discover -v` for offline tests. The import-only fault hook used by process tests is unavailable through CLI flags, environment settings or draft fields. Tests distinguish an abrupt process exit from an actual power-loss experiment.
