"""The current main-database format and its finite preparation path.

Format is owned by SQLite user_version. Inspection never repairs retained
storage; preparation runs under Database's original writer transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing

from ..account_baseline import persistent_database_file
from ..sqlite_schema import definition as _schema_definition
from ..sqlite_schema import statements as _statements
from .transaction import Transaction

CURRENT_VERSION = 4
_DEFAULT_MCP_SERVERS = ["ask", "bg", "ask_agent", "research", "schedule"]


class UnsupportedMainStorage(RuntimeError):
    """Retained storage has no supported preparation path; contains no payload."""


_CATALOG = "SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'"
_LEDGER_SCHEMA = "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
_PREVIOUS_COLUMNS = (
    "ALTER TABLE backend_credentials ADD COLUMN secret_encrypted TEXT NOT NULL",
    "ALTER TABLE client_submissions ADD COLUMN recovered_by TEXT",
)
_MESSAGE_GUARDS = """
CREATE TRIGGER messages_sequence_insert
BEFORE INSERT ON messages
WHEN EXISTS(SELECT 1 FROM messages WHERE session_id = NEW.session_id AND seq = NEW.seq)
BEGIN SELECT RAISE(ABORT, 'duplicate message sequence'); END;
CREATE TRIGGER messages_sequence_update
BEFORE UPDATE OF session_id, seq ON messages
WHEN (NEW.session_id != OLD.session_id OR NEW.seq != OLD.seq)
  AND EXISTS(SELECT 1 FROM messages WHERE session_id = NEW.session_id
             AND seq = NEW.seq AND id != OLD.id)
BEGIN SELECT RAISE(ABORT, 'duplicate message sequence'); END;
"""


def _definition(kind: str, sql: str) -> tuple:
    """Main storage deliberately accepts equivalent physical column orders."""
    try:
        return _schema_definition(kind, sql)
    except ValueError:
        raise UnsupportedMainStorage() from None


def _validate_layout(version: int, rows: Sequence[tuple[str, str, str]]) -> bool:
    """Return whether this is new empty storage; otherwise require a full format."""
    if version not in (0, 1, 2, 3, CURRENT_VERSION):
        raise UnsupportedMainStorage()
    if not rows:
        if version == 0:
            return True
        raise UnsupportedMainStorage()
    actual = {(kind, name): _definition(kind, sql) for kind, name, sql in rows}
    # The private in-memory catalog compiles the single current DDL authority;
    # it never creates or prepares a retained file and is never a version cache.
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in _statements(_SCHEMA):
            reference.execute(statement)
        if version in (0, 1):
            for statement in _PREVIOUS_COLUMNS:
                reference.execute(statement)
        expected = {(kind, name): _definition(kind, sql) for kind, name, sql in reference.execute(_CATALOG)}
        if any(("trigger", name) in actual for name in ("messages_sequence_insert", "messages_sequence_update")):
            if ("index", "messages_unique_sequence") not in actual:
                del expected["index", "messages_unique_sequence"]
            for statement in _statements(_MESSAGE_GUARDS):
                reference.execute(statement)
            expected.update({(kind, name): _definition(kind, sql)
                             for kind, name, sql in reference.execute(_CATALOG) if kind == "trigger"})
    if version == 0:
        expected["table", "schema_migrations"] = _definition("table", _LEDGER_SCHEMA)
        # The retired intent carries no supported interpretation. Presence is
        # allowed only for this finite upgrade and emptiness is checked below.
        actual.pop(("table", "token_rotation_intent"), None)
    if actual != expected:
        raise UnsupportedMainStorage()
    return False


# These are admission predicates for the one supported unversioned baseline,
# not conversions. Format 1 has its own, narrower duplicate-retirement proof.
_UNSUPPORTED_BASELINE_DATA = """
SELECT 1 FROM backend_credentials WHERE secret_encrypted != ''
UNION ALL SELECT 1 FROM client_submissions
 WHERE execution_state NOT IN ('pending','running','done','withdrawn','unknown')
    OR (reason_code='steer_delivery_unknown' AND delivery_state='none')
    OR (reason_code='legacy_rejected' AND recovery_of IS NOT NULL)
UNION ALL SELECT 1 FROM client_submissions original
 WHERE recovered_by IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM client_submissions child WHERE child.session_id=original.session_id
    AND child.client_id=original.recovered_by AND child.recovery_of=original.client_id)
UNION ALL SELECT 1 FROM sessions fork
 LEFT JOIN sessions parent ON parent.id=fork.forked_from_session_id
 LEFT JOIN agents agent ON agent.id=fork.agent_id
 WHERE fork.origin='fork' AND fork.user_id IS NULL AND (
   (parent.user_id IS NOT NULL AND (agent.user_id IS NULL OR agent.user_id=parent.user_id))
   OR (parent.id IS NULL AND agent.user_id IS NOT NULL))
UNION ALL SELECT 1 FROM agents WHERE pin_order IS NULL
UNION ALL SELECT 1 FROM applications WHERE pin_order IS NULL
LIMIT 1
"""

_UNSUPPORTED_RETIRED_DATA = """
SELECT 1 FROM backend_credentials original
 WHERE original.secret_encrypted != '' AND NOT EXISTS (
   SELECT 1 FROM credential_secrets secret WHERE secret.credential_id=original.id
    AND secret.secret_encrypted=original.secret_encrypted)
UNION ALL SELECT 1 FROM client_submissions original
 WHERE original.recovered_by IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM client_submissions child WHERE child.session_id=original.session_id
    AND child.client_id=original.recovered_by AND child.recovery_of=original.client_id)
LIMIT 1
"""

_SCHEDULE_MODES = """
SELECT schedule.id, schedule.knowledge_context, agent.id, agent.user_id
FROM schedules schedule LEFT JOIN agents agent ON agent.id=schedule.agent_id
WHERE schedule.wiki_mode='off'
"""


def _validate_schedule_modes(version: int, rows: Sequence[tuple]) -> list[str]:
    """Authorize only the finite projection change, never a new request."""
    from ..knowledge import decode_context

    if version >= 3 and rows:
        raise UnsupportedMainStorage()
    missing = []
    for schedule_id, raw, agent_id, user_id in rows:
        context = decode_context(raw)
        if context is None:
            missing.append(schedule_id)
        elif (context.mode != "read" or context.maintenance.may_submit
              or agent_id is None or context.origin.user_id != user_id):
            # The origin is a source request, not the current execution target.
            # Contradictory authority cannot be repaired by changing its bytes.
            raise UnsupportedMainStorage()
    return missing


_RETIRED_INPUTS = """
WITH knowledge_contracts(raw,source_session,source_client) AS MATERIALIZED (
 SELECT knowledge_context,NULL,NULL FROM bg_tasks
 UNION ALL SELECT knowledge_context,NULL,NULL FROM research_jobs
 UNION ALL SELECT knowledge_context,NULL,NULL FROM schedules
 UNION ALL SELECT CASE WHEN json_valid(delegation_state)
   THEN json_extract(delegation_state,'$.knowledge_context') END,NULL,NULL FROM sessions
 UNION ALL SELECT CASE WHEN json_valid(tool_input)
   THEN json_extract(tool_input,'$.request_context.knowledge') END,NULL,NULL FROM messages
 UNION ALL SELECT CASE WHEN json_valid(payload)
   THEN json_extract(payload,'$.knowledge') END,session_id,client_id FROM client_submissions
)
SELECT i.session_id,i.client_id,i.request_id,i.receipt,i.payload,i.execution_state,
 i.execution_owner,i.execution_generation,i.execution_boot,i.process_group,
 i.delivery_state,i.reason_code,i.recovery_of,
 EXISTS(SELECT 1 FROM messages m WHERE m.session_id=i.session_id AND (
   m.terminal_request_id=i.request_id OR CASE WHEN json_valid(m.tool_input) THEN
   json_extract(m.tool_input,'$.request_context.request_id')=i.request_id OR
   json_extract(m.tool_input,'$.attempt_context.request_id')=i.request_id ELSE 0 END OR
   CASE WHEN json_valid(m.runtime_metadata)
     THEN json_extract(m.runtime_metadata,'$.request_id')=i.request_id ELSE 0 END)),
 EXISTS(SELECT 1 FROM client_submissions other WHERE other.session_id=i.session_id
   AND other.execution_owner=i.request_id AND other.client_id!=i.client_id),
 EXISTS(SELECT 1 FROM client_submissions child WHERE child.session_id=i.session_id
   AND child.recovery_of=i.client_id),
 EXISTS(SELECT 1 FROM knowledge_contracts c
   WHERE (c.source_client IS NULL OR c.source_session!=i.session_id OR c.source_client!=i.client_id)
   AND CASE WHEN json_valid(c.raw) THEN
     (json_extract(c.raw,'$.value.origin.session_id')=i.session_id AND
      json_extract(c.raw,'$.value.origin.request_id')=i.request_id) OR
     (json_extract(c.raw,'$.value.execution.origin.session_id')=i.session_id AND
      json_extract(c.raw,'$.value.execution.origin.request_id')=i.request_id) OR
     (json_extract(c.raw,'$.value.return_binding.reference.session_id')=i.session_id AND
      json_extract(c.raw,'$.value.return_binding.reference.request_id')=i.request_id) OR
     (json_extract(c.raw,'$.value.return_binding.context.origin.session_id')=i.session_id AND
      json_extract(c.raw,'$.value.return_binding.context.origin.request_id')=i.request_id)
   ELSE 0 END)
 OR EXISTS(SELECT 1 FROM sessions s WHERE s.id=i.session_id AND CASE WHEN json_valid(s.delegation_state)
   THEN json_extract(s.delegation_state,'$.deferred_request_id')=i.request_id ELSE 0 END)
 OR EXISTS(SELECT 1 FROM wiki_job_notifications n WHERE n.kind IN ('chat','schedule')
   AND n.target=i.session_id AND n.request_id=i.request_id),
 (SELECT m.content FROM messages m WHERE m.session_id=i.session_id
   AND m.terminal_request_id=COALESCE(i.execution_owner,i.request_id)),
 EXISTS(SELECT 1 FROM client_submissions original WHERE original.session_id=i.session_id
   AND original.request_id=i.execution_owner AND original.execution_owner=original.request_id
   AND original.execution_state='done' AND original.delivery_state!='unknown'
   AND original.execution_generation=i.execution_generation
   AND CASE WHEN json_valid(original.receipt) THEN json_type(original.receipt,'$.accepted')='true' ELSE 0 END)
FROM client_submissions i
WHERE i.reason_code='legacy_rejected' OR CASE WHEN json_valid(i.payload)
 THEN json_extract(i.payload,'$.wiki_mode')='off' ELSE 0 END
"""


def _retired_inputs(version: int, rows: Sequence[tuple], *, previous_writers_retired: bool) -> list[tuple[str, str]]:
    """Authorize the finite retirement; unresolved evidence blocks the whole step."""
    if rows and version == CURRENT_VERSION:
        raise UnsupportedMainStorage("Retired requests remain in the current format")
    retired = []
    for row in rows:
        (session_id, client_id, request_id, raw_receipt, raw_payload, state,
         owner, generation, boot, group, delivery, reason, recovery_of,
         messages, dependents, child, sources, raw_terminal, shared_owner) = row
        try:
            receipt = json.loads(raw_receipt)
            payload = json.loads(raw_payload) if raw_payload is not None else None
        except (ValueError, TypeError):
            raise UnsupportedMainStorage("Retired request evidence is incomplete") from None
        if (not isinstance(receipt, dict) or type(receipt.get("accepted")) is not bool
                or receipt.get("request_id", request_id) != request_id
                or receipt.get("session_id", session_id) != session_id
                or receipt.get("client_request_id", client_id) != client_id
                or payload is not None and (not isinstance(payload, dict) or payload.get("source") not in (None, "human"))
                or reason != "legacy_rejected" and (not isinstance(payload, dict) or payload.get("source") != "human")):
            raise UnsupportedMainStorage("Retired request evidence is incomplete")
        refused = receipt["accepted"] is False
        unstarted = (state in {"pending", "withdrawn"} and delivery == "none"
                     and owner in (None, request_id) and generation is None and boot is None and group is None
                     and not messages and not dependents and not sources)
        if refused:
            settled = (unstarted and state == "withdrawn" and owner is None and recovery_of is None and not child
                       and receipt.get("disposition", "rejected") == "rejected")
        else:
            from ..execution_identity import process_group_exited
            from ..models import TurnOutcome

            try:
                terminal = TurnOutcome.model_validate_json(raw_terminal) if raw_terminal is not None else None
            except ValueError:
                terminal = None
            completed = (state == "done" and owner is not None and delivery in {"none", "confirmed"}
                         and terminal is not None and terminal.request_id == owner
                         and terminal.recording_integrity == "complete"
                         and (owner == request_id or delivery == "confirmed" and shared_owner)
                         and (previous_writers_retired or process_group_exited(group, boot)))
            settled = (reason != "legacy_rejected" and isinstance(payload, dict) and payload.get("source") == "human"
                       and receipt.get("disposition", "accepted") in ("accepted", "steered", "duplicate")
                       and (unstarted or completed))
        if not settled:
            raise UnsupportedMainStorage("Retired requests require settlement by their original owner")
        retired.append((session_id, client_id))
    return retired


def require_supported_main_format(db_path: str, *, previous_writers_retired: bool = False) -> None:
    """Inspect one consistent committed snapshot, including its real WAL."""
    path = persistent_database_file(db_path)
    if path is None:
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        version = reader.execute("PRAGMA user_version").fetchone()[0]
        rows = reader.execute(_CATALOG).fetchall()
        new = _validate_layout(version, rows)
        if not new and version == 0:
            if reader.execute(_UNSUPPORTED_BASELINE_DATA).fetchone():
                raise UnsupportedMainStorage()
            if any(name == "token_rotation_intent" for _, name, _ in rows):
                if reader.execute("SELECT 1 FROM token_rotation_intent LIMIT 1").fetchone():
                    raise UnsupportedMainStorage()
        elif not new and version == 1:
            if reader.execute(_UNSUPPORTED_RETIRED_DATA).fetchone():
                raise UnsupportedMainStorage()
        if not new:
            _validate_schedule_modes(version, reader.execute(_SCHEDULE_MODES).fetchall())
            _retired_inputs(version, reader.execute(_RETIRED_INPUTS).fetchall(),
                            previous_writers_retired=previous_writers_retired)


async def prepare_main_storage(tx: Transaction, *, previous_writers_retired: bool = False) -> None:
    """Recheck under the original writer transaction; never commit here."""
    version = (await tx.fetch_one("PRAGMA user_version"))[0]
    rows = await tx.fetch_all(_CATALOG)
    new = _validate_layout(version, rows)
    if new:
        for statement in _statements(_SCHEMA):
            await tx.execute(statement)
    else:
        rotation_table = any(name == "token_rotation_intent" for _, name, _ in rows)
        if version == 0:
            if await tx.fetch_one(_UNSUPPORTED_BASELINE_DATA):
                raise UnsupportedMainStorage()
            if rotation_table:
                if await tx.fetch_one("SELECT 1 FROM token_rotation_intent LIMIT 1"):
                    raise UnsupportedMainStorage()
        elif version == 1 and await tx.fetch_one(_UNSUPPORTED_RETIRED_DATA):
            raise UnsupportedMainStorage()
        missing = _validate_schedule_modes(version, await tx.fetch_all(_SCHEDULE_MODES))
        retired = _retired_inputs(version, await tx.fetch_all(_RETIRED_INPUTS),
                                  previous_writers_retired=previous_writers_retired)
        if version == CURRENT_VERSION:
            return
        if version == 0:
            if rotation_table:
                await tx.execute("DROP TABLE token_rotation_intent")
            await tx.execute("DROP TABLE schema_migrations")
            _validate_layout(1, await tx.fetch_all(_CATALOG))
        if version in (0, 1):
            await tx.execute("ALTER TABLE backend_credentials DROP COLUMN secret_encrypted")
            await tx.execute("ALTER TABLE client_submissions DROP COLUMN recovered_by")
        await tx.execute("UPDATE schedules SET wiki_mode='read' WHERE wiki_mode='off'")
        for schedule_id in missing:
            await tx.execute(
                "UPDATE schedules SET invalid_reason=CASE WHEN enabled=0 AND invalid_reason IS NOT NULL "
                "THEN invalid_reason ELSE 'knowledge_contract_missing' END, enabled=0 WHERE id=?",
                (schedule_id,),
            )
        for session_id, client_id in retired:
            await tx.execute("DELETE FROM client_submissions WHERE session_id=? AND client_id=?", (session_id, client_id))
    _validate_layout(CURRENT_VERSION, await tx.fetch_all(_CATALOG))
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
    pin_order INTEGER
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
    injection_status TEXT NOT NULL DEFAULT 'pending',  -- pending|delivered|failed
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
