# Claude stream-JSON protocol reference

The current driver is [`ClaudeDriver`](../../server/harness/claude_code.py).
Shared execution ownership belongs to [Harness](../implemented/harness-layer.md);
steering and acceptance belong to [input delivery](../implemented/inline-steering.md).

## Invocation and input

Ordinary turns use `claude --print --output-format=stream-json --verbose`,
`--include-partial-messages` and `--input-format stream-json`. The prompt is a
driver-written stdin user frame with its original UUID, not an argv prompt.
Selected MCP servers use `--mcp-config` plus `--strict-mcp-config`. Explicit
model, resume, effort and credential settings are rendered from the captured
TurnContext; memory uses `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` without relocating
the transcript/auth home. The current renderer is the source for complete argv.

## Consumed output

| Frame | Current interpretation |
| --- | --- |
| `system/init` | Native session identity and diagnostics; driver also observes advertised slash commands |
| `stream_event` / `content_block_delta` / `text_delta` | Nonpersistent display delta; completed assistant content remains authoritative |
| `assistant` | Text, thinking and tool-use blocks; root association is checked before context sampling |
| `user` | Tool-result blocks and their original tool association |
| `result` | Native terminal, error/usage facts; does not replace product settlement authority |
| `system/api_retry` | Attributed retry diagnostics; recognized 401/authentication failure ends the stream for auth classification |
| `system/task_*` | Native subtask updates normalized by original identity |
| `control_request`, `control_response`, `rate_limit_event` | Not a general product control/approval channel |

The driver additionally observes compaction status, boundary and local-command
run/outcome frames. Manual compact must match its original input UUID/session;
automatic boundaries do not complete manual commands. Boundary pre/post counts
are not a root input/cache sample. The exact conditions for effect, successful
no-op, EOF and failure are in [native compact](../implemented/harness-layer.md#claude-native-compaction).

## Context and fixture provenance

[`claude-context-compact-2.1.319.json`](claude-context-compact-2.1.319.json)
retains contiguous frames from an unmodified 2.1.319 CLI with a synthetic
loopback Anthropic provider. Home paths and derived project directory names
use an example account of the same length. Artifact and request hashes refer
to the captures before anonymization. The fixture includes the CLI binary
digest, source revision, inputs and scenario outcomes. The main normal/compact/empty run is
recorded as failed; short and child cases retain their own outcomes. It is not
a real-provider/model or current-deployment check. In particular, its empty
history failure does not prove a successful compact no-op.

Context sample binding, cache accounting and invalidation are maintained in
[model settings](../implemented/model-reasoning.md#上下文样本与配置绑定).
Representative parser/driver entry points are `tests/test_harness_claude.py`,
`tests/test_claude_context.py` and `tests/test_claude_compact.py`.
