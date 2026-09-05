# Repository instructions

This repository contains the reusable Project Room plugin and local controller. Its workflow includes specification review, authorized implementation by Fable, executable verification, and independent Astra acceptance. Preserve planning-only requests and implementation authorization already present; do not add repeated consent steps.

- Keep plugin name `astra-fable-project-room` and skill name `project-room`. Keep package metadata free of machine-specific paths, live session IDs, credentials, and private project content.
- Invoke Claude and MCP processes using argument arrays, never a shell. Use normal saved Claude authentication; do not obtain, print, copy, or commit credentials or silently switch to API billing/provider overrides.
- Bind decisions and implementation to immutable spec bytes, revision, and SHA-256. Preserve exact requested primary model identity, idempotency, explicit session UUIDs, room locking, bounded review attempts, and blocked uncertain outcomes. Never replay an unverified turn or edit saved state to bypass a failure.
- Preserve raw evidence and original failures in supported reconciliation. Reconciliation runs no model. Distinguish primary and auxiliary model usage and measured facts from documented inferences.
- Fable owns engineering and delegates; Astra owns independent product outcome review. Include self-contained task context, verify anchors before applying returned code, run the agreed gates, and record issue dispositions. Return material scope changes to the spec workflow.
- Preserve `skills/project-room/references/fable-policy.md`: Qwen xhigh submit effort, 131,072 output tokens, blocking waits shorter than 50 seconds, diagnosis before escalation, and no delegate self-certification. Do not change upstream settings as a workaround.
- Keep runtime code dependency-free and compatible with Python 3.10+ on POSIX. Run `python3 -m unittest discover -v` after relevant runtime changes. Tests and CI use fake backends, no account access, paid calls, GPU jobs, or network access.
- Keep runtime state outside the installed plugin/cache by default. Do not commit local configuration, environment files, databases, attempts, transcripts, account data, or review receipts. Inspect the staged diff before publication.
- Validate changed manifests and skills with the available plugin-creator and skill-creator validators. Exercise MCP transport with fake backends when interfaces change.
- Do not choose a license or repository visibility without the user's direction.
