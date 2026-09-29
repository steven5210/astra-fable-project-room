# DeepSeek delegate quick reference

The full delegate contract is [DeepSeek delegate](deepseek.md).

## DeepSeek delegate

The DeepSeek lane serves AO rooms and legacy rooms alike. Its key-free configuration selects exactly one fixed TLS transport: `official` uses `https://api.deepseek.com/chat/completions`; `deepinfra` uses `https://api.deepinfra.com/v1/openai/chat/completions`. There are no arbitrary endpoints, redirects or automatic provider/model fallbacks. New DeepInfra profiles default to `deepseek-ai/DeepSeek-V4.1-Flash`, `max` effort, 131,072 output tokens and an advertised 1,048,576 context window. Health and probe receipts distinguish the backend, endpoint, requested settings and observed model; hosted capacity, reasoning behavior and quality require their own evidence. Existing rooms retain their recorded settings.

DeepInfra's [data policy](https://docs.deepinfra.com/account/data-privacy) says inference data is not used for training and inputs/outputs are normally deleted after processing, with exceptions for debugging or security logging. This is not an unconditional zero-retention guarantee. The adapter sends no batch, webhook or explicit prompt-retention option; [automatic provider caching](https://docs.deepinfra.com/chat/prompt-cache-retention) is a separate behavior.

The adapter executes nothing, edits no files and calls no tools; the designated native worker applies and verifies its proposed changes under Fable's direction. Each room's status includes the latest 20 jobs from its private ledger, and admission checks use the full ledger. The one-request live probe is an explicit paid CLI action that takes the room's own snapshot directory. For a legacy room:

```sh
python3 deepseek_adapter.py probe --home ~/.project-room --room ROOM_ID --room-root ~/.project-room/rooms/ROOM_ID --config ~/.project-room/rooms/ROOM_ID/profiles/deepseek.json
```

For an AO room, whose `ROOM_ID` already starts with `ao-`, the snapshot lives under the AO state directory:

```sh
python3 deepseek_adapter.py probe --home ~/.project-room --room ROOM_ID --room-root ~/.project-room/ao/rooms/ROOM_ID --config ~/.project-room/ao/rooms/ROOM_ID/profiles/deepseek.json
```

An unresolved delivery failure stops new paid jobs in that room only; `deepseek_adapter.py resolve` at your own terminal is the only way to clear it, and no MCP tool or agent performs it. Setup, the key file, the state table, calibration order, export folders and privacy limits are in [the DeepSeek delegate guide](deepseek.md).
