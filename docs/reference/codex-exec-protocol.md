# Codex exec reference for constrained Research leaves

Ordinary Codex sessions use [App Server](../implemented/codex-backend.md).
Only the independent Research web leaf selects the exec profile in
[`codex.py`](../../server/harness/codex.py). It has no ordinary resume/fork,
memory, Connector or Octopus MCP injection; native home/project configuration
has its own merge behavior and is not erased merely by an empty MCP table.
The [Research leaf](../../server/research/leaf.py) captures model, effort and tier
in RunConfig, uses a job-specific empty cwd and bounds execution by timeout.

## Parser fields

`CodexEventParser` consumes one JSON object per stdout line. These are exec event
names; they are not App Server RPC notification names.

| Exec event | Normalized meaning |
| --- | --- |
| `thread.started` with `thread_id` | Native session identity |
| `item.completed` / `agent_message` | Completed text |
| `item.*` / `reasoning` | Thinking observation |
| `command_execution` | Tool call/result with command, retained output and exit status |
| `mcp_tool_call` | Tool call/result from separate server and tool names |
| `file_change` | File-change observation; source kind and identity still constrain effect use |
| `turn.completed` / usage | Native terminal and token usage, not an inferred USD cost |
| `turn.failed`, `error` | Failure evidence under this profile's attribution rules |

The parser also handles supported complete item variants declared in source;
this table is a lookup guide, not an exhaustive schema or a live CLI guarantee.
`tests/_fixtures/fake_codex_cli.py` supplies scripted exec input for offline
checks. Original version/provenance attached to raw fixtures remains part of
their evidence scope. Test selection follows [testing](../testing.md).

Resource retirement belongs to [Harness](../implemented/harness-layer.md#execution-and-resources).
The text-only reasoning leaf uses `run_oneshot`, not this exec surface.
