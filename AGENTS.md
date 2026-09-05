# Repository instructions

This is a standalone Astra/Fable project-room repository. It is separate from TradeDesk and inherits no TradeDesk project policy. Work only on this repository unless the user explicitly authorizes another scope.

- Preserve the room's read-only specification-review scope. Agreement never automatically authorizes implementation, deployment, or delegate execution.
- Invoke Claude and MCP subprocesses using argument arrays, never a shell. Preserve the saved Claude subscription login flow; do not obtain, print, copy, or commit credentials or silently switch to an API/provider override.
- Keep requested model identity exact. Verify a mixed-model response's primary `StructuredOutput` producer against the explicit session transcript, exact payload, session UUID, and attempt time window. Auxiliary usage must remain visible; do not accept a weaker primary model as a fallback.
- Preserve immutable spec bytes/digests, request idempotency, the single-writer lock, explicit UUID resume, the three-attempt cap, and blocked uncertain outcomes. Never replay an unverified turn or change saved state to bypass a failure.
- Keep reconciliation narrow and auditable. Preserve original failures and raw evidence; distinguish measured facts from documented legacy inferences. Never call a model from reconciliation.
- Do not lower the fixed Qwen xhigh effort or 131,072 output-token budget. Keep the proxy's bounded wait semantics and avoid changing upstream server settings without authorization.
- Keep runtime code dependency-free and compatible with Python 3.10+ on POSIX. Run `python3 -m unittest -v test_room test_qwen_guard` after relevant changes. Tests and CI must use fake backends, with no account access or model/GPU calls.
- Do not commit local configuration, environment files, databases, attempts, transcripts, session paths, account data, or conversation/review receipts. Review the staged diff before publication; `.gitignore` is a convenience, not a substitute for inspection.
- Do not add a license or choose repository visibility without the user's direction.
