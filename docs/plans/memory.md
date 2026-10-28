# Agent Memory — Tech Plan (native, agent-written, per-agent)

> **Status:** shipped — Per-agent native memory: one markdown directory per agent, shared by both harnesses.

> Current per-account memory remains durable and
> supported. Moving install-era memory into the first account is an obsolete
> transition retired with bootstrap under the
> [architecture contract](architecture-contract.md#公开接口与账户配置). Current memory
> content and account isolation remain protected. Account portability
> workflow includes current memory as persistent user data,
> rather than invoking the old handoff again.

## Current content ownership

| Content | Owner |
| --- | --- |
| Role, collaboration and delivery behavior | Saved agent persona; [agent-prompts.md](../agent-prompts.md) is a copyable snapshot |
| Current engineering and operational contracts | Existing project guides and subject-owning plans, found through [the documentation index](../README.md) |
| Stable user preferences, project locations and short decision pointers | The responsible role's private memory; shared memory only for agreements that actually apply across roles |
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
Put precise code contracts in the owning project document and leave a short
pointer in memory. Keep the common checkout location in a shared location note;
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

## 0. What we're building, and why this shape

The Agent is the durable entity that owns sessions and schedules
(`agent-refactor.md`). **Memory is the durable knowledge that entity
accrues**, and it must survive across sessions and — as much as
possible — across a backend switch.

This plan supersedes the earlier "Octopus-owned memory store" design
(DB rows + a dedicated memory MCP server + injection of the index every
turn). That approach was built and then **reverted** on the user's
decision. The reason it was rejected:

> If we don't use the native memory, it raises a new risk: the harness
> will *sometimes* use its own native memory anyway. Claude Code's
> native memory is always on; running a second, Octopus-owned store next
> to it means two memory systems fighting. Use the native memory.
> Changing an agent's harness is a low-frequency operation, so we accept
> that the memory format may be mixed on a backend switch.

So the design is: **use each harness's native, agent-written file
memory**, persist and scope it **per agent**, and point both harnesses at
**one shared per-agent markdown directory** so the memory is genuinely
harness-agnostic on disk.

### How the user's "let the agent write the file itself" lands

Claude Code's native memory *is already* "the agent writes markdown
files": a `MEMORY.md` index plus frontmatter `*.md` fact files under
`$CLAUDE_CONFIG_DIR/projects/<cwd-slug>/memory/`, maintained by the agent
via its built-in memory tool, auto-injected at session start. Codex has
full file tools, so it can do the *exact same thing* against the *same*
directory when instructed to. We therefore make Codex match Claude
rather than the reverse — one model, two harnesses, no second store, no
async-consolidation dependency.

## 1. Empirical grounding (verified in real CLI runtimes, not assumed)

The following records the original implementation's headless `claude` / `codex`
experiments. Exact CLI versions were not retained here; these observations explain
the selected design, not the capabilities of every later release. They are not
new live evidence for the current installation, whose validation scope is
[Codex](../testing.md#markers-providers-and-isolation).

- **Claude reads native memory in `--print`.** Seeded a project's
  `memory/MEMORY.md` + a fact file; `claude --print` recalled the facts.
- **Claude writes native memory in `--print`.** Told to remember a
  durable fact, it created a new frontmatter `*.md` and added a
  one-line pointer to `MEMORY.md`, unprompted as to format.
- **Claude has a dedicated memory-dir override** —
  `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` (env) / the `autoMemoryDirectory`
  setting. Verified: with it set to a per-agent dir and `CLAUDE_CONFIG_DIR`
  left at the host default, `claude --print` read seeded memory from the
  override dir **and** the session transcript still landed in
  `~/.claude/projects/<cwd-slug>/` (so `--resume` is unaffected).
- **Do NOT relocate `CLAUDE_CONFIG_DIR`.** (Lesson learned the hard way: an
  earlier version moved it per-agent, which orphaned every session's resume
  transcript — `projects/<cwd-slug>/*.jsonl` lives under the config dir — and
  killed live sessions. The override relocates *only* the memory dir.)
- **Codex reads `MEMORY.md` from a persistent `CODEX_HOME`.** With
  `features.memories=true` + a populated `memories/MEMORY.md`, a fresh
  `codex exec` located and read it (it knew the path from `use_memories`)
  and answered correctly. Codex writing files via its tools is a given.
- **Codex's *native* auto-memory pipeline is NOT used.** Its Phase-2
  consolidation is a background agent that needs a long-lived session;
  it does not complete inside a short `codex exec` (verified: capture
  works, but `MEMORY.md`/`raw_memories.md` stay empty after seconds-long
  exec turns even with idle gates zeroed). We sidestep it entirely: Codex
  memory is a plain directory it reads/writes with file tools, driven by
  `developer_instructions`. `features.memories` stays **off** — no dual
  system, no consolidation wait, no format surprises.

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
  canonical per-agent dir. `CLAUDE_CONFIG_DIR` is **left at the host default**,
  so session transcripts (`projects/<cwd-slug>/*.jsonl`, what `--resume`
  reads) and auth stay where they are. No symlink, no auth copy, no cwd-slug.
- No system-prompt text: Claude's native memory auto-activates and
  auto-injects `MEMORY.md` from the override dir.

**Codex (`codex.py`):**
- `CODEX_HOME` is unchanged (per-credential auth dir).
- A memory blurb is appended to `developer_instructions` naming the absolute
  canonical `memory/` path and the read-at-start / write-durable-facts /
  maintain-`MEMORY.md` protocol, in Claude's format. Codex uses its normal
  file tools (`--dangerously-bypass-approvals-and-sandbox` grants out-of-cwd
  access).
- `features.memories` is **not** enabled (its consolidation doesn't run in
  short `codex exec` turns — see §1).

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

## 5. Accepted trade-offs (explicit)

- **Backend switch may mix formats.** Claude's and Codex's edits share one
  markdown dir; a switch reads the other's prior writes. Low frequency,
  accepted per the user.

(No auth or resume trade-offs: memory never touches `CLAUDE_CONFIG_DIR` /
`CODEX_HOME`, so both harnesses authenticate and resume exactly as before.)

## 6. Verification

- **Unit:** per-agent path derivation + `$HOME`/settings override; idempotent
  provisioning; agent create/delete provisions/removes the dir; Claude
  `build_turn_argv` sets `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` and **never**
  `CLAUDE_CONFIG_DIR` (resume regression guard); Codex `developer_instructions`
  carry the blurb + path and **never** enable `features.memories`.
- **Real-CLI (gated on `claude`/`codex` on PATH):** each harness reads its
  seeded per-agent memory; **and** a Claude session that `--resume`s across two
  processes with the memory override on still recalls the prior turn (proving
  transcripts survived — the regression guard for the original incident).
