# AO prompt and evidence-read diagnostics

Project Room distinguishes the bytes it sends, the text it can prove was delivered,
logged evidence reads, native usage counters and subscription quota. These diagnostics
do not call a model, resume work, release a hold or establish acceptance. A smaller
packet or fewer reads alone does not establish lower total usage or equal quality.

## Exact prompt bytes in saved status

`ao_room_status` includes `latest_prompt` for the latest unambiguously ordered saved
request in normal and Astra-led rooms. New requests save a version-1 projection during
the actual packet assembly. Instrumentation preserves the outgoing UTF-8 text, request
identity and retry behavior; status never rebuilds or resends the packet.

| Field | Meaning |
| --- | --- |
| `total_bytes` | Exact final UTF-8 byte count. |
| `caller_bytes` | Exact caller message bytes. |
| `specification_bytes` | Full specification, changes or actual specification-reference framing. |
| `workflow_bytes` | Other framing, workflow instructions and amendments. |
| `separator_bytes` | Only separators inserted between assembled fragments. |
| `spec_delivery` | `full`, `changes` or `none`; an Astra-led path/hash reference is `none`. |
| `text_sha256` | SHA256 of the exact outgoing UTF-8 bytes. |
| `request_sha256` | Opaque digest of the saved request identifier. |

The four component counts sum to the total. Existing line endings remain part of
their original fragment. The same text appearing in two fragments is counted twice;
the diagnostic does not infer component boundaries from a suffix or substring.

`coverage: known` means the saved request text and projection can be checked. It does
not mean a model received them. `integrity: request_observed` describes validated saved
intent; `receipt_verified` additionally requires a canonical receipt bound to the
projection and saved owner/turn identity. Neither is semantic approval.

Delivery is separate: `prepared` is saved intent, `submitted` adds a matching AO
acknowledgement, and `observed` requires the unique exact user message on the matching
native turn outside the prior baseline, with no contradictory model identity. Lost
acknowledgements can later become observed through the existing sync operation. Failed
or uncertain sends retain their original evidence and cannot infer delivery from a
missing native identity.

For an intact older request without a projection, status exposes only total bytes and
the text hash; component counts and specification delivery are null with
`legacy_components_absent`. Historical receipts are not rewritten. Invalid ordering,
projection or receipt evidence produces fixed reasons and unavailable coverage at the
affected level. Status reads at most the selected bounded receipt for these metrics,
using owned no-follow paths. It adds no model call, transcript scan or subprocess;
the existing local Git preparation integrity check remains part of ordinary status.

`usage.known_primary_subtotal` keeps its existing meaning and
`includes_delegates` remains false. `usage.native_worker_usage` is explicitly
`{"coverage":"unavailable","reason":"native_worker_usage_not_attributed"}`.
Claude's combined cache counter includes reads and writes. These values do not measure
all-worker usage, context occupancy, billing or remaining subscription quota.

## Explicit evidence Read audit

An operator may run one local audit for an existing owned Claude request:

```sh
python3 project_room.py ao-evidence-read-audit \
  --home /absolute/controller-home \
  --room ROOM_ID \
  --request REQUEST_ID \
  --ao-database /absolute/ao.sqlite \
  --evidence-root /absolute/evidence
```

The command runs before mutable Service construction. It does not create a home or
lock, repair state, sync a session, probe a provider, call a model, compact, or start a
monitor. The explicit database is read with SQLite `mode=ro`; application records are
unchanged, although SQLite may coordinate through WAL shared-memory sidecars. The
command closes the transaction before streaming transcripts and rereads the exact
owner tuple afterward.

Collection requires the saved request and canonical receipt, a retained preparation
binding the workspace and Claude configuration root, and the exact corroborated AO
owner. Historical provider records retain their own identity. A delegate-provider
transition does not change the native model. Unsupported harnesses or absent
preparation evidence report unavailable; the command accepts no transcript, native
UUID or configuration-root override.

The native parent interval needs an exact genuine human input within the bounded
controller time window and an unambiguous next human boundary. A last-turn terminal
receipt can close it only with the specified timestamp and unchanged idle owner;
malformed later human records prevent that fallback. Compaction summaries and inline
sidechain copies cannot anchor the parent. Children require a uniquely correlated
structured Agent/Task result and a proven bounded synchronous interval. Missing,
background, reused or ambiguous children stay incomplete with null observations.

Only supported logged `Read` tool inputs and their correlated result blocks are
counted. Path classification is lexical: the audit never opens the evidence target.
Slices include all supplied offset, limit and pages selectors. Exact duplicate
records and tool observations are counted separately; conflicting Read identities
are quarantined. A reported non-error result does not prove full-file retrieval or
understanding. Other tools, including shell commands and browser access, are outside
this counter. Repeated identical inputs do not establish waste.

The closed JSON report contains opaque owner/source/actor digests, parent and child
coverage, supported counts and fixed reasons. It contains no raw paths, session IDs,
prompt, evidence content, reasoning or arbitrary exception text. Its
`redundant_read_verdict` is always `not_established`. Exit status is 0 for complete
coverage, 1 for incomplete/unavailable coverage, and 2 for invalid command arguments.

## Resource and stability limits

| Limit | Version 1 ceiling |
| --- | --- |
| Transcript bytes | 64 MiB per file, 256 MiB aggregate per pass. |
| Transcript passes | Two; at most 512 MiB total admitted data. |
| JSONL line | 4 MiB. |
| Records and record IDs | 100,000 each, aggregate. |
| Tool IDs | 50,000 aggregate. |
| Child actors and files | 32; missing authorized actors also consume the actor limit. |
| Discovery directory | 256 entries, including nonmatching entries. |
| JSON container depth | 64. |
| Path/pages strings | 16 KiB of UTF-8. |
| Binding, receipt and preparation file | 8 MiB each. |

The first pass parses, counts and hashes; the second reopens only the admitted sources
and checks their complete extent, hash, descriptor/name identity and mutation
metadata. Required directory inventories and the owner tuple are rechecked. Rejected
reads still consume the applicable byte budget. Changed, active, torn or ambiguous
sources prevent complete coverage. Missing evidence is never converted into zero.

Run collection at a meaningful operator boundary when it answers a concrete question.
Ordinary status does not run this scan, and neither feature installs a polling loop.
