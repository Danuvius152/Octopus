# Octopus

Octopus is a personal agent platform for Claude Code and Codex. Agents run on
your own machine and are reachable from your phone or any browser.

One install can serve more than one trusted person. Each **account** owns its
agents, sessions, applications, credentials, memory and workspace. This is
application-level account scoping on a shared host; native tools execute with
the server's OS authority. See the [trust boundary](docs/implemented/multi-tenancy.md)
and [remaining hardening](docs/plans/multi-tenancy-gaps.md). Signup is invite-only, and the
first account (created with the installation's setup proof) is the admin who mints
the invites. Accounts have separate business state; cross-account collaboration
is outside the product's scope.

Each agent keeps its own persistent setup (prompt, model, tools, schedules,
connectors), keeps work running in the background across turns, and can reach
real third-party APIs. Octopus uses Claude stream-JSON and Codex App Server for
ordinary interaction, using the backend login or API key you attach.

## How It Works

```
Phone / Browser
  → REST + WebSocket → FastAPI (web UI + API on one port)
      → Agent  (durable: prompt · model · credential · tool policy · connectors)
          → backend: Claude stream-JSON or Codex App Server (local subprocess)
          → MCP tools: bg · ask · ask_agent · schedule · research · connectors
                       (served in-process at /mcp/<name>, not spawned per session)
```

## Features

- Agents have their own prompt, model, credentials, tools, connectors and
  schedules. Open sessions pick up saved agent changes on the next turn.
  Claude Code and Codex support the same chat surface, with backend-specific
  execution capabilities described in the [Harness contract](docs/implemented/harness-layer.md).
  Both use one [per-agent memory directory](docs/implemented/memory.md).
- Accounts use invite-only signup, password authentication, account-scoped
  records, workspace selection and concurrency quotas. Credentials and connector
  tokens are encrypted under an account key wrapped by the installation master
  key. An invalid explicit credential binding fails instead of switching to the
  host login. See [account boundaries](docs/implemented/multi-tenancy.md) and
  [connector setup](docs/connectors-setup.md) for GitHub, Gmail and custom OAuth2 APIs.
- Conversations stream over WebSocket, reconnect after browser disconnects,
  support interruption and queued input, and load long histories on demand.
  `/showme` opens local files in a browser viewer. `/rewind` branches from a prior
  message; `/fork` copies the current workspace and supported native history.
  Side effects and capability limits are covered by the
  [rewind](docs/implemented/session-rewind.md) and
  [fork](docs/implemented/session-fork.md) contracts.
- Background commands return across turns. Scheduled prompts use a live origin
  session when available or a temporary session otherwise. Structured questions
  and named-agent delegations connect work back to its caller; follow-up rounds
  can reuse the delegated session. See [background work](docs/implemented/background-tasks.md),
  [schedules](docs/implemented/scheduled-runs.md) and
  [collaboration](docs/implemented/agent-collaboration.md) for delivery and cancellation.
- `/research` and the research tool produce cited reports using parallel native
  web research, verification and synthesis. The
  [research contract](docs/implemented/native-deep-research.md) owns its phases,
  cancellation and persistence. [Native helper cards](docs/implemented/native-subagents.md)
  expose status and token observations from the model runtime.
- Applications render browser-loadable static pages and can use a supervised
  backend for server work. Rebuilds preserve their separate data directory;
  running apps can converse with agents using an application-scoped token.
  See [applications](docs/implemented/applications.md),
  [backend lifecycle](docs/implemented/application-backends.md) and
  [agent access](docs/implemented/app-agent-access.md).
- The Codex context meter shows the latest valid native root sample, keeping
  stale or unknown readings explicit. The operator's Monitor shows request,
  model-performance, process and database observations. See the
  [performance contract](docs/implemented/codex-performance-monitor.md).
- SQLite persists business state across restarts. Local CLI handoff imports
  Claude sessions, and pull exports supported captured history. One port serves
  API and UI; Cloudflare Tunnel provides optional remote HTTPS access.

## Quick Start

Python 3.12 is the declared minimum. Octopus requires FastAPI 0.133.0 or later
and Starlette 1.7.0 or later for attachment request limits and malformed upload
responses. Octopus requires APScheduler 3.11.3 or later for correct scheduling
across daylight saving transitions. The recommended constraints below target
CPython 3.14.4 on Linux x86_64. If you use a different Python version or
platform, resolve and verify its own snapshot using the
[environment guide](docs/local-wsl.md#环境重建).

```bash
# Clone and set up
git clone https://github.com/Danuvius152/Octopus.git && cd Octopus
python3 -m venv .venv
PIP_CONSTRAINT="$PWD/constraints-python.txt" .venv/bin/pip install \
  -c constraints-python.txt -c constraints-wiki.txt -e "."
cp .env.example .env          # set your own OCTOPUS_AUTH_TOKEN setup proof

# Build the frontend (the server serves web/dist/)
cd web && bun install && bun run build && cd ..

# Run (API + UI on port 8000)
octopus serve
```

Open `http://localhost:8000`. On an empty installation, **Create the first
account** asks for the setup proof from your `.env`, a username and a password.
It creates one administrator, an owned **Octo** agent and the account's
directories. Missing setup configuration or retained business data blocks
setup. The setup proof grants no API or WebSocket business access and is not
saved as a browser login.

From then on, sign in with your username and password; the admin mints **invite
codes** for anyone else who should have their own account. If setup committed
but its response was lost, sign in with the same credentials. Login also
completes missing account directories after a filesystem failure; it preserves
the original account and DEK. Then pick **Octo**, create a session and start
chatting. For standalone phone access, install `cloudflared` and use
`octopus serve --tunnel` for a public HTTPS URL. An existing managed service can
instead use a separate tunnel to its local port; see this installation's
[manual phone access](docs/user-guide.md#6-手机远程访问cloudflare-tunnel).

Auth for the agent's backend uses your existing CLI login (`claude login` /
`codex login`) by default — or add an API key under **Credentials** and attach
it to an agent.

Install development dependencies with
`PIP_CONSTRAINT="$PWD/constraints-python.txt" .venv/bin/pip install -c constraints-python.txt -c constraints-wiki.txt -e '.[test,dev]'`.
`pyproject.toml` owns direct runtime, test and checking-tool requirements;
`constraints-wiki.txt` owns the four-package parsing baseline, while the generated
`constraints-python.txt` records the verified resolved combination. The build
constraint also applies inside pip's isolated build. Install into an isolated
environment when a running service shares the existing venv.

For a managed Linux/WSL installation, start with
`scripts/setup-services.sh` in preview mode and follow the
[installation lifecycle](docs/implemented/installation-lifecycle.md) and
[local service procedure](docs/local-wsl.md#服务管理) before applying it. An
existing installation requires validation, natural drain and confirmation that
old execution has ended. Managed installations use their configured unit and
reject direct ASGI, reload and multiple-worker launches. Startup compatibility,
retained evidence and recovery follow the
[storage contract](docs/implemented/current-storage-format.md).

Compatibility bounds live in `pyproject.toml`, including `mcp>=1.30,<2` for
the supported FastMCP API. Validate dependency updates in a separate virtual
environment with `pip check`, server import and MCP/account isolation tests.

### Development mode

These commands are for a separate standalone development environment. First
configure isolated database, Wiki, uploads, applications, keys, workspace and
provider homes (`CODEX_HOME`, `CLAUDE_CONFIG_DIR` and per-credential homes) using
the [environment guide](docs/local-wsl.md#环境重建). Never point a reload server
at the managed installation's state or provider homes.

```bash
# Terminal 1 — backend (hot-reload)
.venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — frontend (Vite proxies /api + /ws to the backend)
cd web && bun dev
```

Open `http://localhost:5173` for the hot-reloading dev server.

## CLI

```bash
octopus serve                  # Start server (API + UI on port 8000)
octopus serve --tunnel         # ... with a public Cloudflare Tunnel (HTTPS)
octopus handoff                # Import a local Claude Code session
octopus pull <session-id>      # Export an Octopus session as local JSONL
```

Pull reads the complete captured history and verifies it before replacing a
local transcript. Supported formats and failure boundaries are described in the
[transcript export contract](docs/implemented/harness-layer.md#complete-typed-transcript-export-and-local-pull).

## Tech Stack

**Backend**: Python 3.12+ · FastAPI · `claude` + `codex` CLI subprocesses ·
aiosqlite · APScheduler · cryptography (Fernet) · MCP namespaces served
in-process over streamable-HTTP
**Frontend**: React 19 · TypeScript (strict) · Vite · zustand · Tailwind v4 · Radix

## Testing

Offline correctness covers both backends. Live checks depend on the selected
provider and isolated credentials; consult the
[validation policy](docs/testing.md#markers-providers-and-isolation) and
[local setup](docs/local-wsl.md#live-凭据与网络) instead of assuming a CLI is
installed or logged in. Report unexecuted provider coverage accurately.

Use [`testing.md`](docs/testing.md#test-coverage) for suite commands, provider
selection and isolated test state. `./scripts/check.sh` runs the code-commit
gates. Live pytest requires **`--run-real` and the appropriate marker selection**;
`-m real` alone excludes those cases. Counts come from current collection, not
a copied inventory. Documentation-only work checks affected links, both generated
indexes and `git diff --check`.

### Pre-commit hooks (optional)

Install [lefthook](https://github.com/evilmartians/lefthook) and run
`./scripts/setup-hooks.sh`. Commits then run the same gates `./scripts/check.sh`
does, scoped to what you staged: ruff + mypy + the hermetic pytest tier for
Python, eslint + `tsc` + vitest for web, and the generated-contract and
docs-index checks when they could have drifted. Each skips when no relevant file
is staged, so a doc-only commit stays fast.

## Architecture

See the [documentation index](docs/README.md) for all documentation and the
[roadmap](docs/roadmap.md) for upcoming choices and dependencies. Start with
[docs/architecture.md](docs/architecture.md) for system design, data flow, and
the WebSocket protocol. [docs/implemented/](docs/implemented/) owns current
contracts and their reasons; [docs/plans/](docs/plans/) holds unfinished changes.
