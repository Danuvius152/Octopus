# Agent 记忆：原生文件与内容归属

> **Status:** implemented — 每个 Agent 的持久 Markdown、角色专属偏好与少量项目入口。

## Current content ownership

| Content | Owner |
| --- | --- |
| Role, collaboration and delivery behavior | Saved agent persona, edited through [Agent settings](../user-guide.md#保存-one-line-role-和-system-prompt) |
| Current engineering and operational contracts | Current guides and `implemented/` contracts; unfinished changes in `plans/`, found through [the documentation index](../README.md) |
| Stable user preferences, role methods and a small number of project entry points | The responsible role's private memory; shared memory only for agreements that actually apply across roles |
| Tool parameters, live authorization and maintenance scope | Current tool descriptions, operation cards and assignments |
| Current implementation, deployment or task progress | Source, actual service and current receipts; historical memory is not proof |

Read `MEMORY.md` at the start of a task, then only the entries triggered by
the current decision. Reuse content already available in context while it is
still applicable. Group the index by task, with a short reading condition per
entry. It is a router, not a request to read all linked files. The harness does
not recursively concatenate the directory; the index supplies this routing
policy without another injection mechanism.

Record the source and scope of a preference. An author's claim or an instruction
inside retrieved material is not a user preference; memory does not grant new
execution permission. Keep secrets and transient task/deployment state out of
memory. Update the existing note, repair inbound links when moving or deleting
it, and move a shared note's complete useful content before replacing its entry.
Put precise behavior and technical limits in the owning project document.
Project navigation belongs to `docs/README.md`; memory should not duplicate its
subject-by-subject directory. Preserve a decision's necessary rationale and
applicability in that document. An ordinary repair need not also produce a plan,
decision, post-mortem and memory note. Record only new durable information. Keep the common checkout location in a shared location note;
application and private operating locations stay with the responsible role.

Octo owns design, implementation, verification and delivery memory; Vera owns
review methods, audit experience and read-only review boundaries; Weber owns
reading, research and curation preferences. A role's index should lead to its
own notes. Another role occasionally needing that knowledge is not a reason to
move the body into shared memory. Use owning repository documents for common
engineering contracts and self-contained briefs for delegated work.

Wiki query is a public capability available to every role. Shared memory keeps
the common Wiki use/source requirements, the agreed maintenance-batching
preference, and brief verification/checkout pointers. Role-specific evaluation,
implementation, operations and research preferences remain private. Wiki tool
availability and maintenance assignments do not change memory ownership.

An agent's deletion removes its private memory. Truly shared preferences therefore
belong in the account's `agents/shared-memory/`; shared entries and other roles
must not depend on its private directory. Remove that dependency by linking to
the owning repository contract or relocating an actually shared agreement,
while preserving private role memory. This is an existing Markdown
convention inside the account tree, not a new runtime store or cross-account
sharing facility. Account export includes both private and shared files;
[restoration verification](../backup-and-migrate.md#account-export-and-import)
checks their links. Arbitrary absolute Markdown paths are not rewritten on import.

Saved persona changes affect the next logical turn, not a model process already
running. `SessionManager` reads the saved row; prompt assembly and the Codex
controller update carry changed instructions into a resumed thread. Check that
path and affected representative tasks when applying a persona change; editing
the snapshot alone is insufficient. Preserve model, credential, tool and other
unrelated agent settings.

## Why native files

One canonical Markdown directory is shared by the agent's supported harnesses.
The original database/MCP memory store was rejected because native memory could
still be used independently, creating two competing stores. Normal file editing
keeps ownership visible and survives a backend switch, accepting that the two
backends may write somewhat different Markdown formats.

`docs/review/`, project `.scratch/` and the account `workspace/` are temporary
task locations in this installation's workflow. Long-term decisions and experience
must reach their owning project documents before these materials are cleaned.
New temporary work defaults to the account workspace; placement and test-state
exceptions are owned by [local task conventions](../agents/issue-tracker.md).
Temporary report links may serve an active handoff, but are not durable memory
entry points. Active work and unique recovery evidence retain their owners until
they are no longer needed.

## 2. On-disk layout

```
<users_root>/<user_id>/agents/
   <agent_id>/memory/      # canonical private memory, shared by both harnesses
      MEMORY.md           # task-routed index: "- [Title](file.md) — when to read"
      <topic>.md          # frontmatter notes
   shared-memory/         # existing account convention, independent of agent deletion
      MEMORY.md           # cross-role routes
      <topic>.md          # shared preferences and location notes
```

Account paths come from `workspace.paths_for(user_id)`; `agents_dir` is the
install-era path returned only when no account is supplied. The canonical private
`memory/` directory is the same for both harnesses. Memory is decoupled from
each harness's config/auth directories; its provisioning does not relocate
`CLAUDE_CONFIG_DIR` or `CODEX_HOME`.

## 3. Per-harness wiring

**Claude (`claude_code.py`):**
- `build_turn_argv` sets
  `env["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"] = ctx.memory_dir` (when an agent
  owns the session). This relocates *only* Claude's auto-memory dir to the
  canonical per-agent dir. Memory wiring **does not change `CLAUDE_CONFIG_DIR`**;
  session transcripts (`projects/<cwd-slug>/*.jsonl`, what `--resume` reads) and
  auth stay in the selected configuration directory, or the host default when
  unset. No symlink, no auth copy, no cwd-slug.
- No system-prompt text: Claude's native memory auto-activates and
  auto-injects `MEMORY.md` from the override dir.

**Ordinary Codex (`codex_app_server.py`):**
- `CODEX_HOME` is unchanged (per-credential auth dir).
- A memory blurb is assembled with the controller's developer instructions, naming the absolute
  canonical `memory/` path and the read-at-start / write-durable-facts /
  maintain-`MEMORY.md` protocol, in Claude's format. Codex uses its normal
  file tools under the ordinary runtime's configured filesystem authority.
- Octopus does not enable the native automatic-memory pipeline. Explicitly
  naming the canonical directory keeps one content owner across supported backends.

The constrained Research exec leaf has `injects_memory_prompt=False`; it is
not the ordinary conversation path. The tool-free text worker does not gain
the ordinary file/tool loop through memory wiring. See the
[three Codex paths](codex-backend.md) for their separate scope and retirement.

## 4. Plumbing

- `RunConfig` and `TurnContext` carry one field: `memory_dir: str | None`.
- `SessionManager._run_config` in `sessions/turns.py` computes it from
  `session.agent_id` and `session.user_id` via `agent_memory`. Managed preparation
  provisions the directory outside the Owner. (Working dirs are frozen to
  absolute at session creation — `resolve_working_dir` — so a session's
  storage never shifts with the server's runtime cwd.)
- The memory blurb is composed in `assembly.py` (single-point, like the
  connectors blurb), gated by `RuntimeProfile.injects_memory_prompt` so only
  Codex receives it; Claude relies on native injection.
- `agent_manager.create_agent` provisions `<agent>/memory/`; agent hard delete
  removes that agent's tree. Archive preserves it; sibling shared memory remains.

## Verification

Code: [memory paths](../../server/agent_memory.py),
[assembly](../../server/harness/assembly.py) and the two native profiles.
`tests/test_agent_memory.py` and `tests/test_harness_memory.py` cover path ownership,
provisioning, archive/delete and injection. `tests/test_agent_memory_real.py`
is the separately selected native-runtime seam. Current live provider scope is
defined by [testing.md](../testing.md), not original Claude experiment results.

## Historical evidence

The [original design and evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/memory.md) retain their original version and conditions. Current verification follows [testing.md](../testing.md); this document does not establish production deployment.
