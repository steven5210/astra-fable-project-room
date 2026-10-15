# Strict-format re-ask for a prose-only specification verdict

This operation recovers one completed Fable specification review whose final
response is prose instead of the required JSON. It asks the same Fable session to
reissue its existing verdict, then requires Astra to compare both complete
responses. It does not accept a candidate or grant another substantive review.

Each completed prose-only source review may be re-asked once, at any
specification-review dispatch count. The re-ask is budget-neutral: it sends no
`spec_review` request, consumes no review or dispatch slot, and needs no human
grant. Its request is recorded under the separate `spec_verdict_repair` purpose,
never as an ordinary review.

Use ordinary `ao_room_response_normalize` when the completed response already
contains an otherwise valid JSON object with suitable surrounding prose. Do not
use a format re-ask to change a rejected verdict, resolve a substantive finding,
revise a specification, clear a provider hold, or retry uncertain delivery.
This deliberately narrow lane refuses source text containing an opening JSON
brace; ambiguous or embedded objects must be diagnosed through the existing
formatting rules.

## Preconditions

The source request must be the latest completed specification review of the
exact current specification, owned by the bound Fable engineer at its pinned
model and effort. The current effective model and effort must still match that
original request. Its native receipt, final text, semantic outcome, request
identity, registered native source and current candidate must be verifiable.
There must be no active or unresolved owned work or delegates. Previously
authenticated settled failures remain historical failures; this operation does
not erase them or release a current hold.

Only one format re-ask is allowed per source review, and a finished chain —
agreed, closed or settled as a failure — permanently exhausts that source's one
use. A different later prose-only review may open its own chain; a pending chain
blocks all other room work until it resolves. Keep the original final response,
rejected format diagnostic and every original review request unchanged.

The re-ask is not bounded by the room's debate budget or dispatch ceiling, and
it does not extend them. An agreed re-ask marks its source review accepting for
debate accounting; a closed or settled-failure chain leaves the source as an
ordinary debate attempt. A prose-only verdict from an extension-granted review
is eligible like any other completed prose-only review.

The re-ask turn is read-only except for the native task-list tools. TaskList,
TaskGet, TaskCreate and TaskUpdate are permitted for compatibility with the
room's progress bookkeeping, which instructs a turn-start TaskList on
implementation and correction turns; a formatting turn does not require them.
Any other tool, a delegate call, a launch or a malformed native row refuses
the re-ask at commit.

## Operator sequence

1. Read the complete original final response and exact current specification.
   Establish that a format-only reissue is appropriate; do not infer an agreement
   from a keyword or an excerpt. Inspect the current room and all delegate state.
2. Call `ao_room_spec_verdict_recovery_audit` with the room, original request,
   unused re-ask request ID and exact registered native ownership information.
   The audit sends no model request. Inspect its evidence, candidate and source
   identities. A saved audit is not a promise that later admission will succeed.
3. Call `ao_room_spec_verdict_recovery_grant` with that audit digest. No human
   authorization is required; the optional authorization fields, when supplied,
   are recorded verbatim as an operator assertion. The operation rechecks the
   evidence. Use only its returned send arguments; do not add new work, change
   the model or substitute a different instruction.
4. Send once through `ao_room_send`, purpose `spec_verdict_repair`, and wait through
   bounded `ao_room_sync` calls. Durable consumption precedes the provider POST.
   After an interruption or lost acknowledgement, inspect/sync that same request;
   absence of an acknowledgement never proves non-delivery. Do not send under a
   fresh ID or erase a consumed intent.
5. Read both full native responses. Fable's re-ask response must itself be one
   strict JSON object with the required source and exact-spec identities. It must
   preserve the original verdict and interpretation. A rejection, new blocker,
   changed interpretation, additional substantive requirement or scope change
   remains blocking. Do not manufacture JSON or normalize the re-ask response.
6. Only after the semantic comparison, call
   `ao_room_spec_verdict_recovery_commit` with the re-ask request, exact receipt
   and final-text digests, substantive `astra_review`, and
   `confirm_same_verdict: true`. This assertion is Astra's reviewed judgment about
   the complete texts, not a parser's claim of semantic equivalence. If that
   assertion cannot truthfully be made, preserve the result and close the lane.
7. Check supported agreement, then use the ordinary handoff, Fable engineering,
   formal verification and independent acceptance workflow. Each later agreement
   revalidates the retained chain evidence. Missing or altered evidence cannot
   fall back to an older agreement.

## Chains and history

Each source review's re-ask keeps an independent chain of immutable records —
`grant.json`, `intent.json`, `completion.json`, `agreement.json` and
`closure.json` — under `spec-verdict-recovery/chains/<source_request_id>/`,
tracked by the `spec_verdict_recovery` state pointer with its
`source_request_id` and `chain` keys. When a finished chain's grant is succeeded
by a new source's grant, the old pointer rotates into
`spec_verdict_recovery_history` and its records stay in place. Retained audits
live under `spec-verdict-recovery/audits/` regardless of chain.

Rooms created before per-source chains keep the legacy flat layout — the five
record names directly under `spec-verdict-recovery/` with a pointer that has no
`source_request_id` or `chain` key. The controller reads either shape unchanged;
a mixed layout is damaged evidence, never an automatic migration.

`ao_room_status` carries the active lane's state under `spec_verdict_recovery`:
`none`, `granted`, `dispatched`, `completed`, `agreed`, `closed`,
`settled_failure` or `damaged`, with the pending flag, request and record
identities, the retained `history` of finished chains and the sorted
`used_sources` list.

## Lane closure

A consumed lane that cannot produce agreement does not freeze the room, and does
not free that source's one re-ask.

If the re-ask turn failed natively, the ordinary outcome path settles it. Run
`ao_room_outcome_audit`, then `ao_room_outcome_resume` with the recorded
authorization; the request becomes a settled failure. The lane then reports
`settled_failure` and the room returns to its prior state: no agreement from
that source, ordinary limits apply, and no second re-ask for it is possible.
The failed turn's receipt and failure stay immutable.

If the re-ask turn completed but Astra cannot truthfully affirm the same
verdict, call `ao_room_spec_verdict_recovery_close` with the re-ask request,
its exact receipt and final-text digests, and a substantive `astra_review`. The
call stores an immutable closure record with the controller's own verdict-parse
error for the result, closes the lane without agreement, and makes a later commit
refuse. The closure is one-use and idempotent; different inputs refuse. A
rejected, malformed or ambiguous re-ask result therefore no longer locks the
room: the room continues with no agreement from that source.

## Guarantees and limits

The re-ask request is separately accounted. It does not remove or relabel any
ordinary review attempt, consume a dispatch slot, renew an exhausted allowance,
accept an implementation, or waive an engineering finding. A grant is bound to
its original request and receipt, exact specification, candidate and pinned
native owner. Other pending work and stale or incomplete evidence refuse
admission.

Candidate stability is required through the re-ask and its handoff. After a
valid handoff, ordinary authorized engineering can change the candidate; that
does not rewrite the original review or its re-ask evidence. New code still
needs a committed handback, current formal gates and independent acceptance.

Semantic equivalence of arbitrary prose and JSON is an explicit operator review
boundary. The controller validates the identities, lifecycle and structured
claims; it cannot prove that prose has the same meaning. Never affirm the
comparison when either response adds a blocker or changes the original decision.

Local controller persistence remains trusted. Detectable missing, changed or
substituted evidence is rejected; coordinated rewriting of all matching local
state and evidence is outside this guarantee. No external journal or migration
is introduced. Installing this capability and running a live re-ask remain
separate from developing or reviewing its patch.

Each durable record is written before the state pointer that claims it is
saved. A storage failure in between leaves an unclaimed record; the next read
then fails closed with a damaged-evidence refusal and waits for supported
diagnosis. The controller does not silently adopt the record, delete it or
recreate an allowance. Thus no-replay safety does not guarantee automatic
progress after every filesystem failure. A fully saved request can be synced
after a lost acknowledgement without resending. Preserve any unclaimed-record
refusal for a supported recovery decision.
