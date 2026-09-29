# Audited acceptance-review continuation

A normal `fable_engineering` AO room keeps its ordinary three independent
acceptance-review attempts across revisions. When more review is necessary and
the user has authorized it, the operator can audit and grant exactly one named
additional review. The global limit is unchanged. Each later review needs its
own fresh audit, deliberate grant and unused request identity.

This lane does not apply to legacy or `astra_led` rooms. It preserves the
existing [one additional charter review](one-charter-review-extension.md), its
one-ever allowance and all original evidence. Neither lane grants permission
for the other kind of review.

## Trust boundary

The operator records the actual user authorization, its source/provenance and
the diagnosis explaining why this particular review is needed. Those strings
are recorded assertions, not an authentication system. The original build
request, elapsed time, passing tests or a specification hash alone are not a
review-renewal decision. A real broad authorization for further necessary
reviews in the approved scope may support later separately audited grants;
it never creates an automatic or multi-review allowance.

The operator supplies an exact absolute path to read-only AO ownership storage
and the retained native provider session IDs for both roles. The audit checks
the project, workspace, conversation, branch, harness and native owner against
complete AO history and saved role bindings. An AO conversation ID is not a
native provider session ID. A separate Codex ownership helper checks the
reviewer without relaxing the existing Claude-only helper. Reviewer model/MAX
evidence comes from configured bindings, history and receipts; it is not a
claim of provider-side model attestation.

The private journal and evidence hashes detect missing, modified, detached or
inconsistent records within the existing local trust boundary. They do not
authenticate a malicious, mutually consistent rewrite of the entire private
store and its AO evidence. Machine paths, project material, authorization and
receipts stay in private room state, outside the distributed package.

## Eligibility

The first grant requires exactly three retained reviewer-role intents,
including rejected or failed attempts. Later grants require the preceding
grant to be consumed and its request settled. No unused allowance can be
banked before the ordinary limit is exhausted. Every extra intent must have
one intact consumption record. Request and grant IDs cannot be repurposed.

The audit reuses the normal exact-spec agreement, handoff, final engineering
report, delegate/provider integrity, passed verification checkpoint and
candidate checks. Pending operator requests, active or uncertain work,
unfinished verification, unresolved delegate outcomes, model substitution,
busy owners, incomplete history or semantic holds refuse. Read-only acceptance
does not acquire the engineer-only live MCP/delegation readiness gate;
unrelated live delegation-file drift keeps its existing meaning.

Both retained native histories must be complete and every native turn completed.
A failed, interrupted, cancelled, recovered or active turn refuses. The reused
complete-history check supports two exceptions, both only in a provider-transitioned
room: a settled quota failure of the current provider epoch with its validated
settlement, and that room's recorded context imports. The lane never settles or
clears such a turn, and a room refused for one stays at the ordinary
acceptance-review cap.

The audit freezes complete bounded evidence for the current specification,
gates, candidate, checkpoint and gate logs; the final engineering report and
verified delegate facts; all prior reviewer intents, receipts, verdicts,
failures and recoveries; acceptance/verification history; earlier grants and
consumptions; and both retained owners. Freshness is based on those contents
and identities, not a timeout. Extend recomputes the evidence, and dispatch
checks it again.

## Native owner continuity across grants

Every audit after the room's first grant compares both roles' current native
ownership -- AO session, project, harness, mode, workspace, provider
conversation, AO conversation/branch, branch strategy and truncation flags --
with the ownership frozen in every earlier grant audit of this room's
continuation chain. Only controller generation and runtime liveness may
differ, so a successful same-native controller restart and a legitimate
engineering correction made between grants stay compatible. A replacement
provider conversation supplied for either role, even when it is consistent
with the retained-owner database, refuses at audit, extend and send before
any write, and stores nothing. Recovery is restoring the retained native
conversation: the lane never re-anchors to a replacement. An earlier chain
whose own recorded grant audits disagree about ownership refuses as
inconsistent rather than picking one side.

## Operator sequence

1. Sync and settle the existing work. Complete any separately authorized
   outcome recovery and response normalization first. A recovery successor
   must name the same intended new reviewer request ID. Finish necessary
   engineering corrections, obtain the complete final engineering report and
   run `ao_room_verify` successfully. A grant never clears a hold.
2. Choose an unused `review_request_id` and preserve the exact caller-message
   UTF-8 bytes. Compute their SHA256. Preserve whitespace and newlines when
   subsequently passing the message to `ao_room_send`.
3. Call `ao_room_acceptance_review_audit` with the intended request/message
   digest, explicit native-owner database and both retained native session
   IDs. Inspect the returned identities and `audit_sha256`.
4. Record the actual user authorization, its source and a concrete operator
   diagnosis through `ao_room_acceptance_review_extend`, using a distinct
   stable grant `request_id`. Existing authorization may suffice; do not ask
   the user to repeat an already applicable decision.
5. Call `ao_room_send` with role `reviewer`, the exact intended request ID,
   byte-identical caller message and purpose `acceptance_review` (also the
   default for a normal reviewer). The grant is consumed durably before the
   request projection and before any POST.
6. Sync the result, address findings under the ordinary workflow and call
   `ao_room_accept` only for an actual independent approved JSON verdict over
   the current exact specification, candidate and verification evidence.

Do not interact with either native session between audit, grant and dispatch.
A new native turn changes the frozen history. While the grant is unused, the
controller refuses new specifications, verification, engineer dispatch,
acceptance records, response normalization, provider/routing adoption or
refresh and executable-binding changes before their invalidating writes.
Identical saved-result reads, status, sync, outcome diagnosis and staged
future instructions remain available. An externally stale unused grant is
preserved and cannot be replaced. After consumption, ordinary engineering
corrections and new verification may proceed; a later grant audits the new
candidate while preserving the earlier evidence.

While a grant is unused, the one-additional-charter-review lane cannot become
eligible: it needs the next charter registered, and the unused-grant freeze
refuses that write. Conversely, once a new charter is registered, the acceptance
audit refuses until the ordinary agreement, handoff, engineering and verification
sequence completes for that revision.

Keep the AO ownership database at the audited path until the named review is
dispatched. The grant pins the supplied path and both native session identifiers,
and admission re-reads that exact source. If an engineer outcome audit run during
the freeze records a different source path, the named send refuses without
consuming anything; re-running that outcome audit against the original source
restores it.

### CLI argument files

These examples are synthetic. Replace each example identity and digest with
the exact inspected value from the intended room. The database path must
remain absolute; the CLI and MCP do not normalize native-evidence paths.

`audit.json`:

```json
{
  "room_id": "ao-example",
  "review_request_id": "acceptance-fourth",
  "message_sha256": "<SHA256 of exact caller-message UTF-8 bytes>",
  "native_owner_database": "/absolute/path/to/ao.sqlite",
  "engineer_native_session_id": "example-engineer-native-session",
  "reviewer_native_session_id": "example-reviewer-native-session"
}
```

```sh
python3 project_room.py call ao_room_acceptance_review_audit --args-file audit.json
```

`extend.json`:

```json
{
  "room_id": "ao-example",
  "audit_sha256": "<audit_sha256 returned by the fresh audit>",
  "request_id": "grant-acceptance-fourth",
  "authorization": "The user authorized further necessary independent reviews in this approved scope.",
  "authorization_reference": "Actual user message identifier and approval context",
  "diagnosis": "The retained reviews are exhausted; the corrected verified candidate needs one further independent review."
}
```

```sh
python3 project_room.py call ao_room_acceptance_review_extend --args-file extend.json
```

The same operations and required string fields are exposed through MCP.
Neither is marked read-only: the audit saves private evidence and may persist
ordinary diagnosed outcome observations, while extend writes a grant. Neither
dispatches a model or performs a native lifecycle action.

## Returned evidence and status

Audit returns `eligible`, `audit_sha256`, the exact intended request/message
digest and purpose, spec revision/hash/record hash, candidate/checkpoint
identity, final engineering request, role sessions and retained attempt count.
Its `evidence_sha256` identifies the passed checkpoint, while `audit_sha256`
identifies the complete audit. Extend returns the grant receipt path/hash,
sequence, target, remaining single allowance and any consuming request.

`ao_room_status.acceptance_review_extension` is `null` for a room without this
journal. Otherwise its read-only local projection reports:

| State | Meaning |
| --- | --- |
| `pending_uncommitted` | A grant receipt exists without its projection. Only the identical extend request may reconcile it. |
| `unconsumed` | An intact journal names one still-unused review. Dispatch must still verify current evidence. |
| `consumed` | Every recorded grant has been consumed. No further review is authorized by those grants. |
| `inconsistent` | Journal/evidence validation failed. The status does not certify the evidence as valid. |

Status makes no model/AO call and does not attest live candidate freshness or
current eligibility. It also reports the retained `acceptance_review_attempts`.

## Single use and failure recovery

A refused admission consumes nothing and sends nothing. Once consumption is
durable, it is irreversible even if POST fails or delivery becomes uncertain.
An identical known send returns its original summary without a second POST;
changed payloads under that identity refuse. Unknown delivery must be
diagnosed through existing reconciliation, never replayed or funded by
another grant.

The grant receipt precedes its state projection. An interruption between
those writes may reconcile only the same extend inputs against matching
evidence, retaining the original receipt bytes. Consumption without the
outgoing request projection instead creates an inconsistent hold: it is
never reconstructed into a model send. Lost acknowledgement after the
projection uses normal exact native-turn reconciliation, preserving the
consumption and earlier failure observations.

Missing, corrupt, duplicate, reordered or orphan records; dropped state
pointers; changed old attempts; and forged continuation linkages refuse
relevant mutations, sync and acceptance. Do not manually repair counters or
delete the journal. There is no unused-grant expiry, revocation, replacement
or retirement in this version. Surface a stale unused grant for diagnosis;
the proposed retirement operation is a separate follow-up.

Audit and journal records are pinned byte-for-byte in the lane's own recorded
serialization, not merely by their content digest. Any byte change to one of
them, including whitespace, re-encoding or re-serialization, makes the
projection `inconsistent` and refuses relevant mutations, sync and acceptance
until the exact original bytes are restored. Earlier records are additionally
pinned by the byte digests frozen in each later audit.

A grant, successful dispatch, completed review or passing gate is not
acceptance. Existing rejected verdicts and failed candidates remain evidence.
Only the ordinary independent approved verdict and all current gates can
accept the candidate.
