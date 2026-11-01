"""One current main-database format, initialized only for genuinely empty storage."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from ..account_baseline import persistent_database_file
from ..sqlite_schema import statements
from .transaction import Transaction

CURRENT_VERSION = 7
_DEFAULT_MCP_SERVERS = ["ask", "bg", "ask_agent", "research", "schedule"]


class UnsupportedMainStorage(RuntimeError):
    """Storage does not match this program; publication must prepare it first."""


_NONEMPTY = "SELECT 1 FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' LIMIT 1"


def require_current_main_format(db_path: str) -> None:
    """Match the committed marker, including real WAL, before opening a writer."""
    path = persistent_database_file(db_path)
    if path is None:
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == CURRENT_VERSION:
            return
        if version == 0 and db.execute(_NONEMPTY).fetchone() is None:
            return
        raise UnsupportedMainStorage()


async def initialize_main_storage(tx: Transaction) -> None:
    """Recheck under the writer transaction and create only an empty database."""
    version = (await tx.fetch_one("PRAGMA user_version"))[0]
    if version == CURRENT_VERSION:
        return
    if version != 0 or await tx.fetch_one(_NONEMPTY) is not None:
        raise UnsupportedMainStorage()
    for statement in statements(_SCHEMA):
        await tx.execute(statement)
    await tx.execute(f"PRAGMA user_version={CURRENT_VERSION}")


_SCHEMA = """
-- Complete current main format. Credentials and input recovery retain only
-- their authoritative split ciphertext and successor relationship.

CREATE TABLE agent_connectors (
    agent_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    PRIMARY KEY (agent_id, installation_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (installation_id) REFERENCES connector_installations(id)
        ON DELETE CASCADE
);

CREATE TABLE agents (
    -- Who owns this row (multi-tenancy.md §5). NULL means the install:
    -- true of every row before accounts, of none after.
    user_id TEXT,
    id TEXT PRIMARY KEY,                    -- 12-char hex, same scheme as sessions
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    avatar TEXT,                            -- emoji or URL, optional
    system_prompt TEXT NOT NULL DEFAULT '',
    model TEXT,                             -- e.g. "claude-opus-4-7"; null = backend default
    credential_id TEXT REFERENCES backend_credentials(id) ON DELETE SET NULL,
    backend TEXT NOT NULL DEFAULT 'claude-code',  -- default harness for new sessions
    mcp_servers TEXT NOT NULL DEFAULT '["ask","bg","ask_agent","research","schedule"]',
                                            -- JSON array of built-in Octopus MCP server ids.
    tool_allow TEXT NOT NULL DEFAULT '',    -- newline-separated tool/MCP names; empty = allow all
    tool_deny  TEXT NOT NULL DEFAULT '',    -- newline-separated; deny takes precedence over allow
    subagents TEXT NOT NULL DEFAULT '[]',   -- JSON list of sub-agent definitions this agent
                                            -- brings with it: {name, description, prompt,
                                            -- model?, tools?}. Rendered as Claude Code's
                                            -- `--agents` JSON (native-subagents.md §6);
                                            -- empty list = the CLI's built-ins only.
    is_system INTEGER NOT NULL DEFAULT 0,   -- 1 = the protected Default Agent (cannot be deleted)
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- The sidebar is a set of shortcuts (sidebar-pins.md): `pinned` puts the
    -- agent there, `pin_order` is its place among the pinned. Every live
    -- agent is still listed, callable and schedulable either way.
    pinned INTEGER NOT NULL DEFAULT 1,
    pin_order INTEGER
, reasoning_effort TEXT, service_tier TEXT);

CREATE TABLE applications (
    -- Who owns this row (multi-tenancy.md §5). NULL means the install:
    -- true of every row before accounts, of none after.
    user_id TEXT,
    id TEXT PRIMARY KEY,                   -- 12-char hex, as sessions/agents
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon TEXT,                             -- emoji shown in the sidebar
    icon_src TEXT,                         -- server-owned: the app's own icon
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    app_dir TEXT NOT NULL,                 -- absolute path to the app root
    entrypoint TEXT NOT NULL DEFAULT 'index.html',
    status TEXT NOT NULL DEFAULT 'building',  -- building|ready|failed
    error TEXT,
    -- Archived applications leave the sidebar but keep their row AND their
    -- files, so the Applications page's Archived section can put them back.
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_built_at TEXT,
    -- Sidebar shortcut + its place among the pinned (sidebar-pins.md).
    pinned INTEGER NOT NULL DEFAULT 1,
    pin_order INTEGER,
    -- Application lifecycle owns this unfinished deletion, never build status.
    deletion TEXT CHECK (deletion IS NULL OR COALESCE((json_valid(deletion)
        AND json_extract(deletion, '$.phase') IN ('retiring', 'files')
        AND json_type(deletion, '$.keep_files') IN ('true', 'false')), 0))
);

CREATE TABLE auth_tokens (
    -- The SHA-256 of the token, never the token. Reading this table gives you
    -- no live session; the bearer is 256 bits of randomness, so a slow KDF
    -- would buy nothing a plain digest does not.
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- 'session' expires; 'pat' is a personal access token for scripts and the
    -- CLI, revocable and long-lived (§3.1).
    kind TEXT NOT NULL DEFAULT 'session',
    label TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    last_seen_at TEXT,
    revoked_at TEXT
);

CREATE TABLE backend_credentials (
    -- Who owns this row (multi-tenancy.md §5). NULL means the install:
    -- true of every row before accounts, of none after.
    user_id TEXT,
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,                 -- "claude-code" | "codex" | …
    label TEXT NOT NULL,
    auth_type TEXT NOT NULL,               -- "api_key" | "oauth"
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', -- "active" | "needs_reconnect"
    token_expires_at TEXT,                 -- ISO8601, null for non-expiring keys
    needs_reconnect INTEGER NOT NULL DEFAULT 0,
    last_refresh_error_code TEXT           -- see oauth_errors.RefreshErrorCode
);

CREATE TABLE bg_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    command TEXT NOT NULL,
    description TEXT,
    working_dir TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'pending'|'running'|'completed'|'failed'|'cancelled'|'interrupted'
    exit_code INTEGER,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,  -- bool: at least one stream hit the cap
    started_at TEXT NOT NULL,
    completed_at TEXT,
    idle_timeout_seconds INTEGER,
    timeout_seconds INTEGER,
    stop_reason TEXT,
    work_id TEXT,
    knowledge_context TEXT,
    delivery_disposition TEXT CHECK(delivery_disposition IN ('stopped','unknown')),
    delivery_error TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE client_submissions (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    request_id TEXT NOT NULL,
    receipt TEXT,
    payload TEXT,
    execution_state TEXT NOT NULL DEFAULT 'unknown',
    execution_owner TEXT,
    execution_generation TEXT,
    execution_boot TEXT,
    process_group INTEGER,
    delivery_state TEXT NOT NULL DEFAULT 'none',
    reason_code TEXT,
    state_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    recovery_of TEXT,
    dismissed INTEGER NOT NULL DEFAULT 0,
    native_inputs TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(native_inputs) AND json_type(native_inputs)='array'),
    PRIMARY KEY (session_id, client_id)
);

CREATE TABLE connector_installation_secrets (
    installation_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,
    FOREIGN KEY (installation_id) REFERENCES connector_installations(id)
        ON DELETE CASCADE
);

CREATE TABLE connector_installations (
    -- Who owns this row (multi-tenancy.md §5). NULL means the install:
    -- true of every row before accounts, of none after.
    user_id TEXT,
    id TEXT PRIMARY KEY,                   -- 12-char hex
    kind TEXT NOT NULL,                    -- 'gmail' | 'github' | …
    label TEXT NOT NULL,                   -- 'archeryue7@gmail.com'
    auth_type TEXT NOT NULL,               -- 'oauth' | 'api_key'
    external_account_id TEXT,              -- email / github "login:id" / workspace id
    scopes TEXT,                           -- JSON list of granted OAuth scopes
    enable_by_default INTEGER NOT NULL DEFAULT 0,  -- auto-enable on newly-created agents
    needs_reconnect INTEGER NOT NULL DEFAULT 0,
    token_expires_at TEXT,                 -- ISO8601, null = non-expiring
    last_refresh_error_code TEXT,          -- mirrors backend_credentials
    created_at TEXT NOT NULL
);

CREATE TABLE connector_oauth_clients (
    -- No owner column: an OAuth *client* is this install's app
    -- registration with the provider, not a user's secret — the env
    -- fallback below says the same thing. An admin configures it once
    -- and every account connects its own account through it
    -- (multi-tenancy.md §5). The per-account half is
    -- `connector_installations`, which does carry an owner.
    kind TEXT PRIMARY KEY,                 -- 'github' | 'gmail' | …
    client_id TEXT NOT NULL,
    client_secret_encrypted TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE credential_secrets (
    credential_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,
    FOREIGN KEY (credential_id) REFERENCES backend_credentials(id)
        ON DELETE CASCADE
);

CREATE TABLE custom_connectors (
    -- No owner column: a connector *kind* is install-level, for the same
    -- reason its OAuth client is — and `kind` is the primary key, so a
    -- per-account definition would need a different one. An admin adds
    -- the kind; each account connects through it.
    kind TEXT PRIMARY KEY,                 -- user-chosen slug, e.g. 'linear'
    display_name TEXT NOT NULL,
    authorize_url TEXT NOT NULL,
    token_url TEXT NOT NULL,
    scopes TEXT,                           -- JSON list of OAuth scopes
    pkce INTEGER NOT NULL DEFAULT 0,
    api_base TEXT NOT NULL,                -- base URL the agent's request tool calls
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE invites (
    code TEXT PRIMARY KEY,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_use_id TEXT,
    is_error INTEGER,
    session_id_ref TEXT,
    cost REAL,
    attachments TEXT,                       -- JSON list[AttachmentMetadata], null when none
    created_at TEXT,                        -- when the row was written; NULL for rows
                                            -- predating the column (nothing to backfill
                                            -- from — no other table records per-message time)
    -- Per-turn git anchor captured when a user message row is written
    -- (session-rewind.md §4 + §5.6.3). Powers the safe-revert preflight.
    git_head TEXT,                          -- `git rev-parse HEAD`; NULL when not a git repo
    git_status_clean INTEGER,               -- 1 iff `git status --porcelain` was empty
    runtime_metadata TEXT,
    terminal_request_id TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE notifiers (
    -- Who owns this row (multi-tenancy.md §5). NULL means the install:
    -- true of every row before accounts, of none after.
    user_id TEXT,
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                    -- 'webhook' | future: 'email', 'browser_push'
    label TEXT NOT NULL,
    config TEXT NOT NULL,                  -- JSON
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE research_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,                  -- running|completed|failed|cancelled|interrupted
    phase TEXT,                            -- scope|search|verify|synthesize|done
    error TEXT,
    report_path TEXT,
    cost REAL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    injection_status TEXT NOT NULL DEFAULT 'pending',  -- pending|delivered|failed|unknown
    injected_at TEXT,
    delivery_receipt TEXT,
    knowledge_context TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    origin_session_id TEXT,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_seconds INTEGER,
    cron TEXT,
    timezone TEXT,
    recurrence_label TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    -- The session the most recent fire ran in. Two jobs: the UI links "last
    -- run" to what actually happened, and the runner refuses to start a fire
    -- while the previous one is still going (scheduler.md — overlap guard).
    last_run_session_id TEXT,
    run_at TEXT,  -- nullable ISO datetime; when set, fires once at that time then auto-deletes
    knowledge_context TEXT
, wiki_mode TEXT, invalid_reason TEXT);

CREATE TABLE sessions (
    -- Who owns this row (multi-tenancy.md §5). NULL means the install:
    -- true of every row before accounts, of none after.
    user_id TEXT,
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT,                -- backend resume id: a Claude session id
                                           -- OR a Codex thread_id (backend-agnostic;
                                           -- name kept for back-compat — codex-backend.md §4.3)
    archived INTEGER NOT NULL DEFAULT 0,   -- hidden from default list; row kept for history
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,  -- owner; nullable in SQLite, required by API
    origin TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'schedule' | 'wiki' | 'delegation' | 'fork' | 'application' | 'app'
    backend TEXT NOT NULL DEFAULT 'claude-code',  -- 'claude-code' | 'codex' (codex-backend.md §4.1)
    model TEXT,                            -- per-session override (`/model`); NULL = use
                                           -- the agent's, whose NULL means backend default
    -- Agent-to-agent: a delegation child session points at the parent
    -- session it was spawned from. SET NULL on parent delete (orphan beats
    -- mass-delete; sessions are precious). NULL on every non-delegation
    -- session. (agent-collaboration.md §4.1)
    parent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    -- The original prompt for a delegation, stored verbatim so the UI can
    -- render "Octo asked: «…»" without rummaging through the first message.
    -- NULL on every non-delegation session.
    delegation_request TEXT,
    -- Latest future-format delegation round; NULL histories are not recovered.
    delegation_state TEXT,
    -- The application this session belongs to, for the two kinds a running
    -- app owns: its build session and the conversations the app itself holds
    -- with an agent (origin='app'). It's what lets a route prove a
    -- conversation belongs to the app asking for it, what the sidebar filters
    -- on, and what deleting an application follows to take its threads with
    -- it. NULL on every ordinary session. (app-agent-access.md §3)
    app_id TEXT,
    -- Session tree-rewind / fork (session-rewind.md §4). A fork is a
    -- clone of a parent session up to (but not including) a chosen user
    -- message. All NULL/0 on non-fork sessions. forked_from_session_id is a
    -- PLAIN reference (no FK action): parent delete leaves it dangling so the
    -- UI can still render "forked from (deleted session)" and the orphan
    -- bucket in buildForkTree anchors correctly (§5.5).
    forked_from_session_id TEXT,           -- parent session id (dangling-ok)
    fork_after_seq INTEGER,                -- last copied seq; rewound user msg
                                           -- lives at fork_after_seq+1 on parent
    fork_needs_replay INTEGER NOT NULL DEFAULT 0,  -- HISTORY_REPLAY backends
                                           -- (Codex) until first result lands
    fork_metadata TEXT,                    -- EPHEMERAL JSON (prefilled prompt,
                                           -- side-effect summary, first-turn
                                           -- note, label); cleared after first
                                           -- result
    fork_revert_record TEXT,               -- DURABLE JSON safe-revert outcome;
                                           -- NEVER cleared
    fork_status TEXT                       -- 'initializing'|'reverting'|'ready'
                                           -- crash-recovery marker; NULL on
                                           -- non-fork rows
, credential_id TEXT, reasoning_effort TEXT, service_tier TEXT);

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    -- NOCASE so `Archer` and `archer` cannot both exist: two accounts whose
    -- names differ only in case is an impersonation surface, not a feature.
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    -- Self-describing: `scrypt$n$r$p$salt$hash`, so the cost parameters can be
    -- raised later without invalidating everyone's password.
    password_hash TEXT NOT NULL,
    -- This user's data key, wrapped by the server master key (§4). Filled in
    -- by the key work; NULL until then, which is why it is nullable.
    dek_wrapped TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    -- JSON list of extra permitted working-dir prefixes (§6). A single-box
    -- affordance the cloud version drops; NULL means "workspace only".
    extra_roots TEXT,
    created_at TEXT NOT NULL,
    -- Set rather than deleted: a disabled account keeps its rows, and its
    -- sessions and schedules stop running.
    disabled_at TEXT
);

CREATE TABLE wiki_job_notifications (
    vault_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    subscriber_id TEXT NOT NULL,
    user_id TEXT,
    job_id TEXT NOT NULL,
    state_version INTEGER NOT NULL CHECK(state_version>0),
    kind TEXT NOT NULL CHECK(kind IN ('chat','schedule','app','human')),
    target TEXT NOT NULL,
    request_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    body TEXT NOT NULL CHECK(json_valid(body)),
    outcome TEXT NOT NULL CHECK(outcome IN ('delivered','unavailable')),
    received_at TEXT NOT NULL,
    PRIMARY KEY(vault_id,event_id,subscriber_id)
);

CREATE UNIQUE INDEX agents_name_unique ON agents(COALESCE(user_id, ''), name) WHERE archived = 0;

CREATE UNIQUE INDEX applications_name_unique ON applications(COALESCE(user_id, ''), name COLLATE NOCASE) WHERE archived = 0;

CREATE UNIQUE INDEX client_submission_recovery ON client_submissions(session_id,recovery_of) WHERE recovery_of IS NOT NULL;

CREATE UNIQUE INDEX client_submission_request ON client_submissions(session_id,request_id);

CREATE INDEX client_submission_source ON client_submissions(client_id);

CREATE UNIQUE INDEX connector_installations_account_unique ON connector_installations(COALESCE(user_id, ''), kind, external_account_id) WHERE external_account_id IS NOT NULL;

CREATE INDEX idx_agent_connectors_agent
  ON agent_connectors(agent_id);

CREATE INDEX idx_auth_tokens_user ON auth_tokens(user_id);

CREATE INDEX idx_bg_tasks_session
  ON bg_tasks(session_id, started_at);

CREATE INDEX idx_connector_installations_kind
  ON connector_installations(kind);

CREATE INDEX idx_credentials_backend
  ON backend_credentials(backend);

CREATE INDEX idx_messages_session ON messages(session_id, seq);

CREATE INDEX idx_research_jobs_session
  ON research_jobs(session_id, created_at);

CREATE INDEX idx_sessions_forked_from ON sessions(forked_from_session_id);

CREATE UNIQUE INDEX messages_terminal_request ON messages(session_id, terminal_request_id) WHERE terminal_request_id IS NOT NULL;

CREATE UNIQUE INDEX messages_unique_sequence ON messages(session_id, seq);

CREATE INDEX wiki_job_notifications_target
  ON wiki_job_notifications(user_id,target,vault_id,job_id,state_version DESC);
"""
