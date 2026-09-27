# Root Read admission in the deny-only routing guard

`ao_routing_guard.py` is the private PreToolUse guard that is copied alone into
prepared controller state (stdlib only). It never grants a permission: an
admitted call prints nothing and ordinary permission handling still decides
access. This document describes the root `Read` admission added as
`READ_ADMISSION_VERSION = 1`.

## Scope

Covered: root-engineer `Read` calls whose target positively qualifies as plain
UTF-8 text and whose call correlates to the exact native assistant message that
requested them. Qualification is checked before admission and is deliberately
positive, not a signature blacklist alone: the leaf name must not end in a known
auto-detected container suffix (`.pdf`, image, `.ipynb` notebook, archive and
office forms), the leading bytes must not match a known container signature (PDF,
PNG, JPEG, GIF, TIFF, ZIP/office, gzip, WebP), and every inspected byte must be
strict UTF-8 with no NUL byte. Because the native tool selects these forms from
the target file itself, refusing the target is what keeps PDF, image and
notebook reads out of the charged text budget.

The qualification is bounded and conservative: it covers the bytes the guard
inspects (a short header plus the requested range up to the 2 MiB scan window), it
is not a general file-type classifier, and it cannot prove what the native tool
will return for a target it accepts.

Not covered and unchanged by this policy: worker reads, `Grep`, `Glob`,
`TaskSearch`, `TaskOutput`, delegate results, native provider output, compaction
summaries and every other path that adds context. Read admission therefore
cannot guarantee that native compaction failures stop.

## Budgets

| Bound | Value |
| --- | --- |
| Raw selected UTF-8 source bytes per read | 32,768 |
| Raw selected UTF-8 source bytes per native assistant message | 65,536 |
| Conservative framing estimate | 8 bytes per returned line |
| Estimated framed bytes per native assistant message | 98,304 |

These are conservative product-local admission budgets. They are not token
counts, billing estimates or exact bounds on a native tool result. The native
tool adds line-number framing. The exact native slice and line semantics are an
unvalidated assumption here: the guard assumes `offset` is a 1-based line number
and that the selected range is split only on newline bytes, so a tool that
counts lines differently or returns a superset would make the per-read number an
estimate rather than an upper bound. That assumption must be confirmed on the
first supported native turn (see Limitations). The file can also change between
the guard's measurement and the native read.

Observed scale for context only: one retained engineer session issued thirteen
`Read` calls under a single native assistant message identity; the returned
UTF-8 text including native line numbers summed to 594,020 bytes over roughly
562 KB of selected source. The budgets above are far below that scale by
design; they are not derived from it and do not reproduce it.

The per-message charge is raw selected bytes plus the conservative framing
estimate, so the aggregate keeps explicit headroom for line-number wrappers
instead of pretending that raw bytes equal returned text.

## Grouping: the native assistant message is the batch

The batch boundary is the native assistant `message.id`, never the human turn,
a `prompt_id`, a timer or a lone hook `tool_use_id`. The guard reads a bounded
tail of the exact root session transcript, requires the current `tool_use_id` to
appear in a root assistant record with exactly the hook's Read arguments, and
treats every record sharing that `message.id` as the same batch; streamed
splits of one message are merged, exact duplicate records are deduplicated and
conflicting duplicates refuse.

* Unrelated native records (for example queue-operation rows without a `uuid`,
system rows, summaries) are ignored; they are not treated as missing identity.
* If the current call is absent from a complete, verified transcript revision, the
guard waits briefly in the same hook invocation for a late native flush: at most
`READ_CORRELATION_ATTEMPTS` (8) checks over at most
`READ_CORRELATION_WAIT_SECONDS` (2.0) wall-clock seconds, sleeping at most
`READ_CORRELATION_SLEEP_SECONDS` (0.2) seconds between attempts. Only that exact
transient outcome is retried; a partial tail, a conflicting duplicate, staleness,
a replacement, a rewind and every state or ownership refusal are final.
* The wait remembers the latest verified absent revision, not only the first: its
device, inode, size and a bounded sha256 anchor over the bytes ending at that size
(at most `READ_CONTINUITY_ANCHOR_BYTES` = 65536 bytes). A resumed attempt is
accepted only when the same device and inode still carry at least that many bytes
and that bounded previous-content evidence still matches on every attempt,
including the successful attempt. A grow-then-rewind below the latest verified
size, or a same-inode rewrite that changes the bounded anchor window, is refused
before any ledger is created. This is bounded evidence over the latest verified
revision, not whole-history integrity and not an append-only proof.
* The wall-clock deadline is checked before each resumed attempt and before a
positive identity is accepted after a wait. A filesystem or kernel stall cannot be
forcibly preempted by Python's monotonic check alone; the guard observes the
deadline after the fact and refuses instead of claiming the hook returned within
it.
* If the bounded wait expires, the guard refuses with a truthful reason that does
not claim the record never existed and does not promise that every later repeat
fails. The record may simply not have been flushed yet, or it may lie before the
bounded 2 MiB inspected window (for example after a large earlier tool result).
No identity and no budget are granted without that correlation, and no evidence is
cleared or repaired. Do not re-issue the same call in a loop; stop and report the
refusal together with the private admission state for operator review, and
continue only in a later native assistant message when work continues.
* Even when an earlier complete record already matched the call, a later partial
record can still change caller authority, so an incomplete tail refuses. The hook
contract does not guarantee that the call was flushed, and the final native log is
not live-flush validation.
* Correlation is revalidated under the reservation lock: a transcript that
advanced to a later caller or assistant message, became incomplete, changed
content or changed inode between the inspection and the reservation is refused
rather than inheriting the earlier clearance.
* A later native assistant message or a later human turn after the recorded call
makes that call stale; it is refused instead of opening or reusing a budget.
* A tool identity reused with different arguments, a different message, a
different tool or a changed file is refused, and the tool identity stays durably
bound to its one native assistant message even after that message leaves the
bounded transcript window.
* The transcript window is finite (2 MiB). If the requesting assistant record
lies before that tail, for example after a large earlier tool result, the call
cannot be positively correlated and is refused with the absent-correlation
reason. That stop is message-scoped: continue remaining work in a later native
assistant message in the same native session, which has its own legitimate
per-message budget. Do not clear state, replace the session or bypass admission
to recover it.
* A rewritten, truncated or replaced transcript cannot open fresh clearance: the
session continuity record binds the transcript device/inode, the last verified
size and a fixed 64 KiB anchor digest ending at that size.

## External SDK caller compatibility

Quota inspection and stale-Read checks recognize the original plain-text root
user record with an explicit human origin. They also recognize the observed
external SDK CLI record without an origin field when all its identifying fields
are present: promptSource and turnOrigin are sdk, userType is external,
entrypoint is sdk-cli, and promptId is a valid UUID. Missing origin alone does
not establish caller identity. Explicit-null, malformed or contradictory caller
identity is refused; task notifications, compact summaries, internal messages,
tool results, meta records and nested workers never establish a fresh caller.
Known harness envelope prefixes are excluded. Unrecognized shapes are not
authorization to infer a different caller dialect.

When the quota hook supplies prompt_id, it must be a non-null UUID string and
match the certified caller's promptId when that field exists. The legacy human
record without promptId retains its historical, weaker caller evidence; a hook
that omits prompt_id cannot establish that additional binding. Read admission
continues to bind the exact assistant message and tool call, not prompt_id.
A later certified SDK caller makes an older Read stale.

These predicates cover the observed shapes, not every future CLI or SDK format.
The standalone CLI and AO's retained SDK transport must each be verified against
actual metadata after an upgrade. A model response or a successful CLI version
check alone does not validate hook compatibility.

## Ranges and denials

Only `file_path` with optional positive integer `offset` and `limit` are
accepted. Booleans, nulls, fractions, out-of-range values, unknown fields and
non-text forms (for example PDF `pages`, image or notebook reads) are refused
before any file or transcript inspection. `offset` is treated as a 1-based line
number, matching the documented parameter. The exact native slice semantics
(1-based lines, newline splitting, no superset) are an unvalidated assumption to
confirm on the first supported native turn, so the guard's number is an
admission estimate, not an exact result size.

Dot and dot-dot path components are refused before any lexical normalization, and
a relative path is joined to the verified hook `cwd` without erasing its own
components, so the guard never measures a different target from the literal path
the native tool is handed. Binary targets, invalid UTF-8 and known auto-detected
image, PDF, notebook, archive and office containers are refused with a plain-text
reason instead of being charged as text.

Denials name a concrete smaller step that actually works:

* too many selected bytes: request explicit smaller line ranges and continue
sequentially;
* too many bytes for the whole message: request a smaller range now, because a
later native assistant message has its own per-message budget;
* a single line that alone exceeds the per-read budget: a smaller line count
cannot help, so the guard says so and asks for a smaller excerpt instead of
suggesting an impossible offset;
* a range that begins beyond the bounded 2 MiB inspection window: no smaller
line count reaches it, so the guard reports the beyond-window region rather
than advising a retry that cannot succeed.

CRLF is counted as bytes, Unicode is counted as UTF-8 bytes (not characters),
an unterminated final line counts as one line, and a range beyond EOF is an
admitted empty read. Symlinked targets, symlinked ancestors, directories,
FIFOs, devices, sockets and missing paths are refused; descriptor-relative
`O_NOFOLLOW`/`O_NONBLOCK` opens make that refusal non-blocking.

## Durable reservations and private state

The guard keeps a private ledger beside the native project transcript
directory, under a hidden admission directory keyed by the native session UUID.
Each native assistant message gets one atomic record, and one additional bounded
per-session record (`continuity.json`) holds the transcript continuity evidence
and the tool-identity bindings. Every reservation is written to a fresh scratch
name, fsynced, atomically renamed and confirmed with a directory fsync before the
read is admitted, and the private namespace entries themselves are confirmed with
a parent-directory fsync (syncing only the session directory cannot establish its
own newly created entry in its parent).

Directory durability is never tolerated on faith. A platform that cannot fsync a
directory descriptor at all (for example `EINVAL`, `ENOTSUP`, `ENOSYS`) gets a
truthful refusal naming the platform error instead of an admitted read, because
the crash-persistence of the reservation cannot be established there. An
idempotent retry re-runs the directory confirmation rather than trusting a rename
whose earlier directory fsync failed.

Missing durable components fail closed with their evidence preserved. The
continuity record is written before any message ledger that claims a new tool
identity, so an interrupted first reservation can leave a durable binding without
its ledger. That state cannot be distinguished from a ledger lost after an
accepted reservation, and the bytes an earlier admission charged cannot be
reconstructed from either, so the guard refuses instead of completing an
unprovable reservation or opening a fresh per-message allowance. That stop is
message-scoped: after operator review, remaining work may continue in a later
native assistant message in the same native session, which has its own
legitimate per-message budget; the failed message is not reset or healed and its
evidence is not deleted. The same rule refuses a session whose message ledgers
exist while `continuity.json` is missing:
the transcript and tool-identity protections cannot be re-established from
evidence, and rebuilding or resetting them would silently restore allowance for a
rewound or reused identity. Nothing is deleted, rebuilt or refunded in either
case.

* Concurrency uses a stable, owned lock inode that is never removed, with a
  non-blocking lock attempt, a documented bounded wait and a fail-closed refusal
  when the wait expires.
* Records are bound to the session, the native `message.id`, the transcript
  device/inode, the hook's exact arguments and the measured file extent. An
  idempotent duplicate reuses one reservation; a larger or changed file cannot
  reuse an older, smaller one.
* The continuity record binds the transcript device/inode, the last verified size
  and a fixed 64 KiB anchor digest ending at that size, and binds every admitted
  tool identity to exactly one native assistant message and its arguments. Growth
  keeps the anchor verifiable; a shorter file, a changed anchor, a different
  device/inode or a tool identity reused in another message refuses. This is
  bounded evidence: a rewrite that preserves the whole 64 KiB anchor window while
  changing only older bytes is not detectable by continuity alone, which is why
  tool identities are bound durably as well.
* Records, directories and the lock file are checked for ownership, mode and
  link count; corrupt, partial, unexpected or internally inconsistent records
  refuse the read and are left untouched. The bounded directory inventory is
  validated under the lock on every admission path that relies on it, including
  additions to an existing message and idempotent reuse, and the entry bound is
  checked before anything is written: a reservation is refused unless the
  directory has room for its new persistent entries and for the temporary scratch
  entry its atomic write occupies, so the bound is never crossed even transiently.
* Nothing is deleted, pruned, truncated or refunded, interrupts leave their
  scratch evidence in place, and there is no time-based release. The bounded
  per-session capacity is finite and documented: at most 1024 assistant-message
  records, at most 2048 tool-identity bindings in one continuity record of at
  most 1 MiB, and at most 1280 entries in the private session directory (records,
  the lock, the continuity record and retained scratch evidence). Reaching any
  bound fails closed with a truthful capacity reason that stops the reads and asks
  for operator review of the retained state; the guard never replaces the native
  session, clears state or deletes evidence to regain allowance. Deleting the
  private state by hand would restore allowance for a rewound message and is not a
  supported remedy.

## Limitations

Unavoidable limits of this design:

* Read admission covers only the root engineer's `Read`; it is not a global
context or output limit, and it does not cover `Grep`, `Glob`, workers, provider
output, compaction summaries or any other path that adds context.
* The estimate is taken before the native read, and admission happens on a hook
that runs immediately before the tool: the target file (and the native
transcript) can still change after the final check and before the read executes.
That final after-admission race cannot be closed from a deny-only hook; it is
documented, not denied.
* The readiness wait is bounded foreground work inside one hook: it re-inspects the
transcript and validates a bounded previous-content anchor over the latest
verified absent revision. That anchor covers at most the 64 KiB ending at the
latest verified size; a same-inode rewrite that preserves that window while
changing older bytes is not detectable by the wait alone, and the durable
continuity record and tool-identity bindings remain the later backstop. The wait
does not establish whole-history integrity or append-only behavior.
* The wall-clock wait deadline is enforced by `time.monotonic` checks before each
resumed attempt and before accepting a positive identity after a wait. Python
cannot preempt a filesystem or kernel stall, so a hook descheduled beyond the
deadline observes and refuses after the fact rather than guaranteeing a hard
return-time bound.
* Whether a running native process actually loaded this copied guard, and whether
the environment supplies the hook fields this admission requires, cannot be
proven from the file itself. Live native hook availability stays unverified until
a supported native turn exercises it.
* The continuity design assumes the native transcript grows by appending.
Whether native compaction rewrites or truncates the `.jsonl` rather than
appending is not validated here. An in-place rewrite that changes a previously
verified position makes continuity refuse for the rest of that transcript, and
that stop is not healed automatically. Treat it as a diagnosed state stop:
report the native transcript and private admission state, and never clear state,
replace the session or bypass admission.
* The guard may refuse a read whose transcript correlation, transcript identity
  or private state cannot be verified. That is fail-closed behaviour, not a grant.
* Partial loss of the private state is refused where the surviving evidence makes
  it detectable: a missing message ledger that the continuity bindings still name,
  or a missing continuity record beside existing ledgers. Deleting the entire
  private state directory is indistinguishable from a first-ever session, and a
  budget that was never durably recorded cannot be reconstructed by a deny-only
  hook, so a capacity or state stop is diagnosed by an operator instead of being
  healed automatically.

Avoidable implementation errors, in contrast, are the ones this revision fixes
and the offline tests now pin down:

* measuring a normalized path instead of the literal supplied path (dot/dot-dot
  traversal and symlinked parents), admitting an incomplete later native record,
  reusing native correlation across the check-to-reservation window, admitting a
  rewound or replaced transcript, admitting a reused tool identity from a fresh
  message ledger, admitting a fresh per-message ledger when the previous one was
  lost (the reproduced 90,000-byte bypass in one native message), resetting
  durable identity protection when the continuity record is missing, admitting an
  idempotent retry after a failed directory durability confirmation, admitting
  binary or container targets as text, skipping the bounded directory inventory on
  existing-message additions, and checking the entry bound only after growing the
  directory.

## Verification

Offline tests use fake native records and synthetic files only; no model,
provider, credential or network is involved. They were **not executed** as part
of writing this document:

```sh
python3 -m unittest test_ao_read_admission -v
python3 -m unittest discover -v
```

The suite covers the observed thirteen-call scale grouped by one native message
identity, message-budget aggregation of individually admissible reads, two
native messages in one human turn, missing/partial native identity refusal
(including a matched call followed by a partial suffix), conflicting identities,
staleness after a later message or human turn, correlation revalidation under the
reservation transaction, same-inode rewind, replacement inode and a reused tool
identity pushed outside the bounded window, range, Unicode, CRLF and EOF
boundaries, huge lines and unreachable prefixes, malformed arguments and
unsupported Read forms, dot/dot-dot and symlinked path refusal, binary, invalid
UTF-8, PDF and notebook container refusal with plain UTF-8 progress still
admitted, special files and symlinks, state tampering/capacity/ownership,
bounded directory inventory on existing-message additions, directory growth at
the exact entry bound including the live scratch peak, a lost message ledger with
retained bindings, a missing continuity record beside retained ledgers, an
interrupted first reservation stopped instead of completed, changed-file reuse,
failed and unsupported directory durability (first attempt and idempotent
retry), interrupted first-time namespace creation, subprocess concurrency for
both repeated and distinct calls, restart durability, copied-alone hook execution
without granting or echoing content, worker/other-tool routing unchanged, bounded
readiness wait cases (late append, latest-absent-revision rewind, same-inode
previous-content rewrite, and wall-clock deadline enforcement), and explicit
null/empty/malformed hook prompt_id refusal distinct from an omitted prompt_id.
Live native hook compatibility remains unverified until a supported native turn
exercises it. On the first supported live turn, before relying on admission:

* confirm the hook supplies non-empty `tool_use_id`, `session_id`,
  `transcript_path` and `cwd`, and that the requesting assistant row with that
  exact `tool_use_id`, `tool_input` and session identity is visible before the
  tool runs;
* confirm the native `offset`/`limit` slice semantics against the native tool
  (1-based lines, newline splitting, no superset) instead of assuming them;
* observe whether root `Read` calls deny uniformly because correlation is
  impossible. If the fields or the correlation evidence are unavailable, or all
  root Reads deny, stop and diagnose: report the transcript and the private
  admission state, and do not clear state, replace the session or bypass
  admission.
