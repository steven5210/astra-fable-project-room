# Bounded native history reads

Large AO histories can exceed Project Room's 8 MB response limit even after a
native turn has finished. This is an observation failure: it does not establish
that the model failed and must not trigger another model request.

Ordinary room operations and complete-history audits now use the same pagination
reader. It starts with 100 timeline items per GET. Only the transport's explicit
oversized-response error permits another GET at the same cursor with a smaller
page (100, 50, 25, 12, 6, 3, 1). Network failures, malformed evidence, other GETs,
dispatch and lifecycle requests are not automatically retried. An individually
oversized item still refuses; preserve the native result for diagnosis.

The reader retains every observed message, turn and activity, including older
quota errors and model reroutes. Complete-history audits reject conflicting
cross-page objects. Ordinary reads retain the newest overlapping objects, as
before. Both require explicit arrays and pagination flags, unique identities
within each page, descending cursors and stable conversation, branch, controller,
settings and materialization metadata. Missing history is never treated as empty.

An activity row whose `activityKind` is not `system`, `error`, `approval`,
`user_input`, `plan` or `usage` has its `detail` replaced before accumulation by
a marker recording the omitted canonical byte count and SHA-256. Every other
field and the retained kinds arrive verbatim, so failure classification keeps
its typed details and a payload-only change still fails the overlap comparison.

Resource limits remain explicit:

- 8 MB for each transport response.
- 2,000 successful pages, retaining up to 200,000 entries at the default page
  size; shrinking a page can reduce the reachable history within this bound.
- At most 2,007 GET attempts, with no additional dispatch allowance.
- 160 MB of retained serialized observations after projection, including the
  bounded bytes consumed by oversized responses. Escaped serialization can
  conservatively exceed wire size.
- A 300-second observation deadline checked between and after reads. An in-flight
  request retains the transport's 15-second timeout.

An ordinary read that reaches a resource bound returns only its validated pages
with `history_truncated: true`. A complete-history audit refuses instead. Existing
send, identity, reconciliation, verification and acceptance checks still decide
whether an observation is sufficient; this repair changes no room receipts,
native session, review allowance, model or effort setting.

Offline probes cover an actual oversized synthetic response, the full reconstructed
305-entry timeline, older failure activities, the retained 5,000-item default
window, a 20,000-entry history within the page bound, payload projection with
its digest markers, conflicts, missing fields, resource limits, unchanged
uncertain-turn holds and the ordinary send/sync/verify/accept workflow. These
checks invoke no model.
Smaller history reads do not themselves reduce Claude context or demonstrate
subscription savings.
