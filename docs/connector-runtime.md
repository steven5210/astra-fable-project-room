# MCP runtime lifetime

The stdio MCP entrypoint retains a complete runtime before importing the room
service. An installed plugin cache can be removed by an update while its Python
process is still running. Without retention, discovery can continue from loaded
code while a later status request fails to import a missing module.

Direct execution of `project_room_mcp.py` copies only the explicit first-party
Python file roster in `project_room_runtime.py` into
`PROJECT_ROOM_HOME/runtimes/<sha256>` (default `~/.project-room/runtimes`). The
digest covers every copied file's SHA-256 and the package name/version. A private
publication lock serializes concurrent starts; the complete copy is verified and
published atomically. Existing copies are verified before reuse. Missing,
modified, linked, or unexpected files are refused rather than replaced or loaded
from another release. The runtime files and directory are read-only, under an
owned private parent directory.

Only the package version is read from `.codex-plugin/plugin.json`. Configuration,
credentials, transcripts, tests, private fixtures, and room state are not copied.
State-home selection stays the same, including a relative `PROJECT_ROOM_HOME`
resolved against the original working directory. The MCP then uses the retained
directory for its imports and working directory. Normal Python API imports and
direct `project_room.py` operator calls retain their existing source semantics.
Supported relative MCP project, workspace, and candidate paths remain anchored
to the original startup directory before dispatch. Prompt text, gate argv,
profile objects, and native evidence paths that already require exact absolute
inputs retain their original validation and meaning.

Retained copies are not automatically deleted, including when the MCP exits.
Legacy workers can outlive their originating MCP and still need its exact source,
lazy imports, and sibling adapter/guard files. This deliberately trades a small
amount of disk space per distinct runtime for that lifetime guarantee. Manual
cleanup must wait until no MCP or worker references the release; this change
does not infer when that is safe.

This creates a coherent MCP runtime; it does not rewrite any recorded native
preparation, provider snapshot, executable binding, or room receipt. Copied
adapter/guard bytes keep their original digests. New MCP connections may use a
new release while existing ones retain their own version. Initialization reports
the actual package version in `serverInfo.version` and the full retained path,
version, and content digest in `_meta["project-room/runtime"]`.

Installing a new plugin does not retroactively update an already-loaded
connector. The supported [app-server operation](https://learn.chatgpt.com/docs/app-server) `config/mcpServer/reload` queues
refresh for loaded tasks; it must reach the existing host app-server connection.
A fresh CLI process proves only that fresh process works. If the current host
does not expose a supported refresh, retain that limitation until the user or a
supported task boundary refreshes the connector. Do not inject commands into a
process, mix newer source files into a removed old version directory, or replay
room work to repair connector loading.

The offline tests in `test_project_room_runtime.py` exercise cold status after
installation eviction, exact release identity, concurrent publication, refusal
of damaged inputs, exclusion of private files, unchanged API imports, and a real
controller worker using only a fake backend that performs a late import and
sibling-file read after its MCP exits and the installation is removed.
