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
   private immutable audit and returns `audit_sha256`.
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

The operation preserves historical evidence and cannot automatically grant a
fifth review. Implementation, independent acceptance, integration and publication
continue under their existing authorization and exact-spec requirements.
