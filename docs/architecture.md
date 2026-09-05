# Architecture

The package separates the agent workflow from durable execution:

- `skills/project-room/SKILL.md` supplies Astra's roles, review loop, handoff, and product acceptance workflow.
- `project_room_mcp.py` exposes local stdio tools. The plugin manifest starts it from the installed plugin root.
- `project_room.py` provides setup, diagnostics, a project/feature room registry, and the CLI equivalent of the MCP operations.
- `room.py` supplies the underlying specification-review engine: immutable specs, exact digests, persistent Claude identity, idempotency, verification, audited recovery, and bounded review rounds continued by recorded user decisions.
- `implementation.py` supplies the authorized implementation lifecycle and validation evidence.
- `session_paths.py` locates only the saved UUID transcript and validates session/worktree metadata, including hashed directories for long paths.
- `qwen_guard.py` wraps an existing upstream stdio server to enforce fixed submit settings and bounded blocking waits.

Runtime data defaults to `~/.project-room` or `PROJECT_ROOM_HOME`. It is independent of plugin installation/cache paths. Claude uses dedicated saved sessions; the controller does not adopt a running Desktop conversation or scrape unrelated chat histories. A recorded message is explicit context, not implicit access to another model's full conversation.

Claude is launched with argument arrays and the configured exact model. The original `CLAUDE_CONFIG_DIR` override is preserved separately from the resolved transcript lookup directory, so an originally unset override stays unset. The review engine distinguishes a primary result producer from auxiliary usage. Identity reconciliation and local authentication recovery preserve the original evidence and never make a model call; see [recovery](recovery.md). The plugin does not extract account credentials or introduce a hosted relay.

Implementation handoff requires a clean Git source checkout and pins its baseline commit. The isolated worktree holds an uncommitted candidate. Independent gates run in that worktree; fingerprints bind working files, Git index entries, and HEAD so later changes cannot inherit stale evidence. Acceptance records the candidate. A scope discovery creates a blocker requiring a newer spec revision and renewed consensus. The Astra skill then performs integration with normal repository tools when delivery was requested; publication still follows the user's scope. These are separate from the controller's acceptance operation.

MCP job submission and model execution are distinct. A job ID lets an active Astra turn wait in bounded intervals and lets a later turn resume after disconnect. Persistent jobs do not automatically trigger an idle Astra task. Deployment, PR publication, notifications to third parties, and schedules are outside the room's automatic lifecycle unless the user explicitly includes them.

The Qwen proxy enforces tool parameters rather than model quality or server configuration. Fable's engineering judgment determines whether a delegate is suitable. Model/window/health verification must be reported separately from tool discovery, and unavailable capabilities must stay visible.

Enhancement proposals are durable room records that Astra surfaces for the user's opinion and scope approval. Astra creates or links issues through the available GitHub connector or CLI in the feature project's repository, then records the verified link; the model report itself does not execute issue operations. Filing authorization does not expand implementation scope. Missing tracker access leaves an explicit pending-filing record.
