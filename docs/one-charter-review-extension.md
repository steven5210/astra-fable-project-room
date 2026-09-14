# One additional AO charter review

This narrow operation records an actual new user approval for **one** additional
Fable charter review after a normal AO room has consumed exactly three
`spec_review` intents. It is available only once in that room's lifetime. It does
not renew source-review or independent acceptance allowances, release provider
holds, reset counters, replace a room/session, or dispatch a model.

The operator first registers the approved next charter with `ao_room_spec_put`.
Registration preserves the historical acceptance and consumes no review. The
registered revision must be exactly one greater than the highest previously
reviewed revision. The retained accepted candidate must still match its archived
checkpoint and independent review receipt, even though registering the charter
clears the current checkpoint pointer.

1. Call `ao_room_spec_review_extension_audit` with `room_id`, `spec_revision`,
   `spec_sha256`, `retained_candidate_sha256`, `native_session_id`, and the explicit
   absolute `native_owner_database` path. Use the actual identities and the same
   read-only AO database used to establish the retained Claude owner. When a
   native outcome source is already recorded, its database and native session
   must match. The audit verifies complete settled native history through AO GETs,
   including activities; retained receipts, failure evidence, gates and candidate;
   MAX role bindings and existing provider/routing/recovery metadata. It saves a
   private immutable audit and returns `audit_sha256`. Both full raw snapshots
   remain in that audit. The exact serialized UTF-8 record, including its trailing
   newline, must fit the reader's 96,000,000-byte bound; overflow refuses before
   publication or state change.
2. Call `ao_room_spec_review_extend` with `room_id`, that exact `audit_sha256`,
   `authorization`, `diagnosis`, and a durable `request_id`. `authorization` must
   quote or accurately preserve the user's actual new answer **and the proposal
   it approved**, rather than reuse the original build instruction. The
   controller binds those bytes to the audited charter and retained evidence,
   rechecks the observation, and saves an immutable grant receipt before its state
   reference. It cannot authenticate a conversation independently; the operator
   remains responsible for supplying the actual approval.
3. The room's authorized owner uses the ordinary `ao_room_send` with purpose
   `spec_review` when the existing provider and semantic holds permit it. Only the
   exact audited charter, native session and unchanged accepted candidate qualify.
   Before state projection or any AO POST, the controller saves one immutable,
   content-addressed consumption record containing the exact fourth request
   identity and its grant. That durable intent consumes the allowance immediately,
   including when acknowledgement is lost or its native result fails. Deleting or
   relabeling the projected request cannot restore the allowance. The grant digest
   is controller metadata; no extra Fable identity prompt is sent.

Identical extension calls return the same verified grant. If the process stops
after writing the receipt but before saving its state reference, only the
identical call may reconcile it after all original audit evidence revalidates.
Other mutations refuse while that receipt is pending. Changed, missing, partial
or stale evidence stays blocked. Do not edit state, rename a grant, delete an
attempt, repeat a failed review, or create a replacement room to work around it.
Routing audits/adoption and executable repair also check this pending receipt
before writing evidence or state. Existing identical configured results remain
read-only; a repair cannot make the grant's exact reconciliation stale.
Conversely, an uncommitted executable-repair intent blocks both a fresh extension
audit and a grant using an older audit. Reconcile that exact repair first, then
create a fresh extension audit. A missing original executable with no journal
remains auditable; this preflight validates the journal rather than executable
availability.

While the committed grant is unused, registering a different charter refuses
before writing a spec or state. Reading the identical current revision remains
idempotent. Registration after consumption retains its existing semantics and
cannot grant a fifth review.

A crash after the consumption record is written but before its request reaches
saved room state is a separate, fail-closed hold. Status and new mutations refuse
the missing projection. This narrow operation deliberately provides no automatic
reconstruction or resend lane for that boundary, even when native history is
unchanged. Preserve the consumption record and diagnose it; retrying the grant or
using another request ID cannot reopen the allowance. With an intact projected
request, ordinary identical send calls remain read-only and never send again.

`ao_room_status.spec_review_extension` reports the receipt, remaining single
allowance and the consuming request identity, or `pending_uncommitted` after a
projection interruption. Status is offline and refuses corrupted committed
evidence. An available allowance does not establish current native readiness,
clear a narrower provider authorization hold, or mean that quota has reset.
Native owner inspection retains the existing AO database schema compatibility
limit from `ao_native_identity`; an unavailable or changed schema refuses.

The grant permits these existing maintenance operations while retaining its
original evidence and allowance:

- Authorized operating instructions may append through `ao_room_instruction_stage`
  before or after the fourth review. Original amendment pointers remain an exact
  prefix; all referenced bytes, hashes and delivered-instruction evidence are
  revalidated. Staging an instruction neither resumes work nor creates a review.
- An explicit `ao_room_outcome_audit` may establish the first native source or
  correct moved database/transcript paths. The saved outcome record must prove
  the exact retained AO/native owner and complete owned request source. Original
  source evidence remains archived. Relocation preserves quota and unknown holds
  and cannot renew a continuation: an existing release retains its original
  outcome digest and named successor. The separate no-path outcome audit keeps
  its existing diagnostic semantics for resolving an unknown observation.
  A known quota/provider/truncation hold cannot downgrade through a later unknown
  observation and then clear. New uncertainty remains visible as
  `observation_outcome` and blocks continuation eligibility. Ordinary observations
  cannot resolve it, even when an earlier error reappears. An explicit healthy
  audit retains the known hold and records the diagnosed observation's digest,
  so an old continuation cannot become valid again; any continuation still needs
  its separately authorized current outcome hash and named request.
- An authorized executable repair may append to the existing immutable binding
  journal for that same native owner. Validation follows every link back to the
  grant's original binding and preparation, so a rollback or missing link refuses.

A new provider transition or routing adoption after the grant is unsupported and
refuses before any transition receipt, pending projection or profile write.
Identical reads of already committed provider and configured routing results
remain available. Reviewer recovery still requires an unused reviewer; a grant's
retained independent acceptance already makes that recovery lane ineligible.

The operation preserves historical evidence and cannot automatically grant a
fifth review. Implementation, independent acceptance, integration and publication
continue under their existing authorization and exact-spec requirements.
