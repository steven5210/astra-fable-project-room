# Compaction-failure recovery

## Scope

This document covers the supported recovery lane for one exact native autocompact-thrashing failure observed after an owned root engineer request. It does not cover quota, billing, ordinary provider errors, safety refusals, other invalid_request payloads or reviewer work. It does not make a failed turn successful.

An AO turn recorded as completed is explicitly outside this lane, even when the same native synthetic compaction error is present. Only an exact failed AO transport turn can settle here. Completed compaction evidence must not fall through to the generic completed provider-error recovery with an unmitigated historical guard.

The public terminal error is:

    Autocompact is thrashing: the context refilled to the limit within 3 turns of the previous compact, 3 times in a row. A file being read or a tool output is likely too large for the context window. Try reading in smaller chunks, or use /clear to start fresh.

The proof binds the exact public sentence digest and the positively correlated synthetic typed error. No HTTP status is required. Contradictory HTTP, quota, safety or refusal evidence disqualifies the lane, including a refusal or safety stop reason recorded on the owned AO terminal or in the native stop reasons; such a stop never falls through to generic provider-error recovery.

## Supported sequence

1. Preserve the original failed AO receipt, original native history, prepared settings, model and MAX values.
2. Audit the latest owned failed engineer request with exact native evidence. The audit establishes eligibility only when the current owned root caller, workspace, session, terminal synthetic error role/model/stop reason and full source digest are proven.
3. Use the supported outcome-resume operation with the fresh audit digest, explicit diagnosis and authorization to record settlement. This may occur before routing mitigation; it binds one named unused successor and never dispatches a model.
4. Perform the reviewed stopped-controller routing refresh so the effective refreshed bundle carries the exact reviewed read-admission guard hash.
5. Ordinary send revalidates quality delivery and then requires the effective routing guard to match the exact settled guard hash before model dispatch. A prepared historical guard hash is not sufficient when a refresh chain exists.
6. Only the named unused successor may run. Repeating its unused release requires current matching evidence; renewing it requires a fresh audit and explicit diagnosis and authorization. An existing release cannot be replaced with a different successor identity, and a consumed release cannot dispatch again. A new compaction failure remains a diagnosed stop; this lane never retries it automatically.

## Evidence boundaries

An audit reporting `resume_eligible: true` establishes eligibility for a fresh settlement; it does not certify that an older release still authorizes dispatch.

A historical settlement proof remains structurally valid after a legitimate successor and does not need to be refreshed or replayed. An unused idempotent settlement is checked against fresh native evidence before it can authorize a send. At a routing-refresh boundary the currently unused compaction continuation is re-read read-only and must still reproduce its exact saved proof and source digest together with the unchanged current receipt, provider-turn, activity and session-failure evidence and its exact current diagnosis; a changed native or AO record refuses before any refresh intent, runtime replacement or outcome write, while a consumed successor keeps its immutable historical settlement.

A later record with the same native message id and synthetic model is ignorable only when its role is assistant, its typed error envelope carries no HTTP status, quota, rate-limit, safety or refusal signal, and its content is absent, empty or exactly the public error text. Tool, mixed, malformed, contradictory or unknown content refuses. A later non-synthetic assistant record is not ignorable merely because its content is empty: a refusal or safety stop reason disqualifies the lane even when no text follows. A human packet that appears after the owned caller in source order refuses the lane even when its timestamp is rewound or inconsistent; timestamps are evidence, not permission to sort away a later instruction. Typed safety, refusal or quota evidence is checked in every relevant field of the owned AO terminal, its stop_reason/stopReason, the recorded native stop reasons, activity and session-failure records rather than being shadowed by a generic type. A bare typed `limit` category is ambiguous: the exact upstream mapping is not established, so it conservatively disqualifies this compaction lane without changing generic classification or the exact native quota proof. The quota settlement proof keeps its exact original values: prior state uncertain, AO state failed, and the request's exact receipt SHA-256. The added compaction lane alone carries the additional kind and required-guard fields, and that mitigation requirement is specific to this exact positively diagnosed compaction settlement; it never lets a contradictory or ambiguous record bypass a fresh positive proof.

The exact sentence and digest are intentionally narrow. Ordinary invalid_request errors, quoted prose, context occupancy inferred from metadata and generic provider failures are not quota and are not this lane.

## Verified and unverified distinctions

The unused compaction-release checks also require current AO terminal, session-failure and attributed-activity evidence to remain stable between settlement, refresh and send. Cross-observation stability must be checked on the supported live path; a changed field refuses continuation rather than being ignored. The historical quota lane remains separate and does not acquire this new compaction-specific refresh check.

Tests use fake AO and synthetic local transcripts. They do not prove live provider behavior, real account state or a specific running native process. The read-admission guard itself is assigned separately and must expose `READ_ADMISSION_VERSION = 1`; settlement binds its exact installed source digest. The release pins that exact reviewed guard SHA-256. If the reviewed guard changes after settlement, the old release's pinned value cannot be satisfied by the new effective bundle: the exact compaction diagnosis and settlement must be redone with a fresh explicit audit against the new guard, and the old release cannot be reused or substituted. An unchanged source checkout or a saved version marker alone does not prove that an already-running native process loaded the mitigation; live hook compatibility and loaded-process behavior remain separately unverified, and this lane never retries automatically or promises that a setting changed an already-running native process.
