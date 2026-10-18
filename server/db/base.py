from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import aiosqlite

from ..config import settings
from ..monitor import Event as _MonEvent
from ..monitor import record as _mon_record
from .schema import _DEFAULT_MCP_SERVERS_JSON, _SCHEMA

P = ParamSpec("P")
T = TypeVar("T")


def serialized_write[**P, T](method: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Keep one connection's write/commit bookkeeping in the same operation."""
    @wraps(method)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        database: Any = args[0]
        async with database._writing():
            return await method(*args, **kwargs)
    return guarded


logger = logging.getLogger(__name__)

# Bounds on the deferred commit (B4). Whichever trips first flushes, so the
# worst case is ~half a second or ~32 messages of transcript at risk from a
# hard kill, instead of an entire turn.
_FLUSH_AFTER_SECONDS = 0.5
_FLUSH_EVERY_APPENDS = 32

# Built-in MCP servers attached to the Default Agent (and the default for
# any newly-created agent). Kept here so the migration backfill and the
# CREATE TABLE default stay in lock-step.







def _load_json_list(raw: Any) -> list[Any]:
    """A JSON-list column as a list, whatever shape the column is in.

    Columns like `agents.subagents` are additive: rows that predate them read
    back NULL, and a hand-edited row can hold anything. A decode failure
    degrades to "none defined" rather than breaking the agent.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []

class DatabaseBase:
    """Connection, transactions and migrations — everything the domain
    mixins stand on.

    Separate from them so the lifecycle has one home: opening the file, the
    pragmas, the deferred-commit bound, the migration ledger. A mixin inherits
    this rather than declaring the attributes it borrows, which keeps the type
    checker honest about what it may touch.
    """

    # Row mappers for the tables more than one domain reads. Each sits beside
    # the column list it consumes — they are one unit, and separating them is
    # how a column gets added to one and not the other.

    @staticmethod
    def _row_to_application(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "icon": row[3],
            "icon_src": row[4],
            "agent_id": row[5],
            "session_id": row[6],
            "app_dir": row[7],
            "entrypoint": row[8],
            "status": row[9],
            "error": row[10],
            "archived": bool(row[11]),
            "created_at": row[12],
            "updated_at": row[13],
            "last_built_at": row[14],
            "pinned": bool(row[15]),
            "pin_order": row[16],
            "user_id": row[17],
        }
    @staticmethod
    def _row_to_bg_task(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row[0],
            "session_id": row[1],
            "command": row[2],
            "description": row[3],
            "working_dir": row[4],
            "status": row[5],
            "exit_code": row[6],
            "stdout": row[7] or "",
            "stderr": row[8] or "",
            "truncated": bool(row[9]),
            "started_at": row[10],
            "completed_at": row[11],
            "idle_timeout_seconds": row[12],
            "timeout_seconds": row[13],
            "stop_reason": row[14],
            "work_id": row[15],
        }
    @staticmethod
    def _row_to_connector(row: sqlite3.Row) -> dict[str, Any]:
        try:
            scopes = json.loads(row[5]) if row[5] else []
        except (json.JSONDecodeError, TypeError):
            scopes = []
        return {
            "id": row[0],
            "kind": row[1],
            "label": row[2],
            "auth_type": row[3],
            "external_account_id": row[4],
            "scopes": scopes,
            "enable_by_default": bool(row[6]),
            "needs_reconnect": bool(row[7]),
            "token_expires_at": row[8],
            "last_refresh_error_code": row[9],
            "created_at": row[10],
            "user_id": row[11],
        }
    @staticmethod
    def _row_to_research_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row[0],
            "session_id": row[1],
            "question": row[2],
            "status": row[3],
            "phase": row[4],
            "error": row[5],
            "report_path": row[6],
            "cost": row[7],
            "created_at": row[8],
            "completed_at": row[9],
            "injection_status": row[10],
            "injected_at": row[11],
        }

    # Column lists and canned SQL shared by the domain mixins.
    #
    # They live on the base rather than each mixin because several genuinely
    # cross domains: deleting a session has to clear its background tasks,
    # research jobs and application threads, so `sessions` needs those column
    # lists too. Putting them here states that overlap once instead of
    # duplicating three tuples, and every mixin inherits this class anyway.
    _CREDENTIAL_COLS = (
        "c.id",
        "c.backend",
        "c.label",
        "c.auth_type",
        "COALESCE(s.secret_encrypted, c.secret_encrypted) AS secret_encrypted",
        "c.created_at",
        "c.status",
        "c.token_expires_at",
        "c.needs_reconnect",
        "c.last_refresh_error_code",
        # Appended, never inserted: these rows are mapped by POSITION below.
        # It is here because the secret is encrypted with its *owner's* key —
        # a row that cannot say who owns it is a row nothing can decrypt
        # (multi-tenancy.md §4).
        "c.user_id",
    )
    _CONNECTOR_COLS = (
        "id, kind, label, auth_type, external_account_id, scopes, "
        "enable_by_default, needs_reconnect, token_expires_at, "
        "last_refresh_error_code, created_at, "
        # Appended, never inserted — see `_CREDENTIAL_COLS`. Same reason: the
        # stored token is encrypted with its owner's key.
        "user_id"
    )
    _CUSTOM_COLS = (
        "kind, display_name, authorize_url, token_url, scopes, pkce, "
        "api_base, created_at, updated_at"
    )
    _AGENT_COLS = (
        "id, name, description, avatar, system_prompt, model, credential_id, "
        "mcp_servers, tool_allow, tool_deny, is_system, archived, "
        "created_at, updated_at, backend, subagents, pinned, pin_order, "
        # Appended, never inserted: `_row_to_agent` maps by position.
        "user_id, reasoning_effort, service_tier"
    )
    # Subquery counting live (non-archived) sessions for an agent — shared
    # by load_agents and get_agent so the UI can show "3 sessions".
    _ACTIVE_SESSION_COUNT = (
        "(SELECT COUNT(*) FROM sessions s "
        " WHERE s.agent_id = a.id AND s.archived = 0)"
    )
    _BG_TASK_COLS = (
        "id, session_id, command, description, working_dir, status, "
        "exit_code, stdout, stderr, truncated, started_at, completed_at, "
        "idle_timeout_seconds, timeout_seconds, stop_reason, work_id"
    )
    _RESEARCH_COLS = (
        "id, session_id, question, status, phase, error, report_path, cost, "
        "created_at, completed_at, injection_status, injected_at"
    )
    _APPLICATION_COLS = (
        "id, name, description, icon, icon_src, agent_id, session_id, "
        "app_dir, entrypoint, status, error, archived, created_at, "
        # Appended, never inserted: `_row_to_application` maps by position.
        "updated_at, last_built_at, pinned, pin_order, user_id"
    )

    # The position after every existing one, for a row being pinned now: it
    # lands at the bottom of the sidebar. A subquery rather than a Python
    # read-then-write so the value is computed inside the statement that
    # uses it. `{table}` is always a literal from this module.
    _NEXT_PIN_ORDER = "(SELECT COALESCE(MAX(pin_order), 0) + 1 FROM {table})"

    # Additive column migrations, as (table, column, DDL). Declared as data so
    # the list reads as an inventory and every entry goes through the same
    # guarded path. Order matters only where a later rebuild would drop a
    # `schedules` columns are applied after the schedule migrations, which
    # rebuild that table.
    _COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        # Old bg records never stored their effective limits or stop cause.
        ("bg_tasks", "idle_timeout_seconds",
         "ALTER TABLE bg_tasks ADD COLUMN idle_timeout_seconds INTEGER"),
        ("bg_tasks", "timeout_seconds",
         "ALTER TABLE bg_tasks ADD COLUMN timeout_seconds INTEGER"),
        ("bg_tasks", "stop_reason", "ALTER TABLE bg_tasks ADD COLUMN stop_reason TEXT"),
        ("bg_tasks", "work_id", "ALTER TABLE bg_tasks ADD COLUMN work_id TEXT"),
        # Per-backend auth.
        ("sessions", "credential_id", "ALTER TABLE sessions ADD COLUMN credential_id TEXT"),
        # /archive — hides the row from the default list, keeps it in the DB.
        ("sessions", "archived",
         "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"),
        # Credential status / refresh tracking (B-4/B-5).
        ("backend_credentials", "status",
         "ALTER TABLE backend_credentials ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
        ("backend_credentials", "token_expires_at",
         "ALTER TABLE backend_credentials ADD COLUMN token_expires_at TEXT"),
        ("backend_credentials", "needs_reconnect",
         "ALTER TABLE backend_credentials ADD COLUMN needs_reconnect INTEGER NOT NULL DEFAULT 0"),
        ("backend_credentials", "last_refresh_error_code",
         "ALTER TABLE backend_credentials ADD COLUMN last_refresh_error_code TEXT"),
        # File/image upload.
        ("messages", "attachments", "ALTER TABLE messages ADD COLUMN attachments TEXT"),
        # codex-backend.md §4.1 — DEFAULT backfills existing rows, no behaviour change.
        ("sessions", "backend",
         "ALTER TABLE sessions ADD COLUMN backend TEXT NOT NULL DEFAULT 'claude-code'"),
        # Application archive/restore.
        ("applications", "archived",
         "ALTER TABLE applications ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"),
        # The app's own icon, re-evaluated on each build; NULL until then.
        ("applications", "icon_src", "ALTER TABLE applications ADD COLUMN icon_src TEXT"),
        # When a message was written. No backfill is possible — nothing else
        # recorded per-message time — so rows written before this stay NULL and
        # the UI shows no time for them rather than inventing one.
        ("messages", "created_at", "ALTER TABLE messages ADD COLUMN created_at TEXT"),
        # `/model`: a per-session model, the same shape `backend` already has.
        # NULL means "use the agent's", whose NULL means "backend default".
        ("sessions", "model", "ALTER TABLE sessions ADD COLUMN model TEXT"),
        ("sessions", "reasoning_effort",
         "ALTER TABLE sessions ADD COLUMN reasoning_effort TEXT"),
        ("agents", "reasoning_effort", "ALTER TABLE agents ADD COLUMN reasoning_effort TEXT"),
        ("sessions", "service_tier", "ALTER TABLE sessions ADD COLUMN service_tier TEXT"),
        ("agents", "service_tier", "ALTER TABLE agents ADD COLUMN service_tier TEXT"),
        # sidebar-pins.md — the sidebar shows pinned applications. DEFAULT 1
        # pins every existing app, so the sidebar looks the same the day this
        # lands; `pin_order` is backfilled in `_backfill_pin_order`.
        ("applications", "pinned",
         "ALTER TABLE applications ADD COLUMN pinned INTEGER NOT NULL DEFAULT 1"),
        ("applications", "pin_order", "ALTER TABLE applications ADD COLUMN pin_order INTEGER"),
    )

    # Applied after `_migrate_agents` / the schedule migrations, because those
    # rebuild `schedules` and a column added earlier would not survive the
    # rebuild.
    _LATE_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        # Default harness for an agent's new sessions.
        ("agents", "backend",
         "ALTER TABLE agents ADD COLUMN backend TEXT NOT NULL DEFAULT 'claude-code'"),
        # agent-collaboration.md §4.1 — a delegation child points at its parent
        # (SET NULL on parent delete: orphaning beats mass-delete) and carries
        # the original request for the UI header. `origin` gains 'delegation',
        # which is a caller change, not DDL.
        ("sessions", "parent_session_id",
         "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT "
         "REFERENCES sessions(id) ON DELETE SET NULL"),
        ("sessions", "delegation_request",
         "ALTER TABLE sessions ADD COLUMN delegation_request TEXT"),
        ("sessions", "delegation_state",
         "ALTER TABLE sessions ADD COLUMN delegation_state TEXT"),
        # native-subagents.md §6 — '[]' means "the CLI's built-ins only".
        ("agents", "subagents",
         "ALTER TABLE agents ADD COLUMN subagents TEXT NOT NULL DEFAULT '[]'"),
        # app-agent-access.md §3 — names the owning application; `origin` gains
        # 'app' as a caller change.
        ("sessions", "app_id", "ALTER TABLE sessions ADD COLUMN app_id TEXT"),
        # session-rewind.md §4 — six nullable columns on sessions, two on
        # messages. forked_from deliberately has no FK action, so a dangling
        # reference survives a parent delete.
        ("sessions", "forked_from_session_id",
         "ALTER TABLE sessions ADD COLUMN forked_from_session_id TEXT"),
        ("sessions", "fork_after_seq", "ALTER TABLE sessions ADD COLUMN fork_after_seq INTEGER"),
        ("sessions", "fork_needs_replay",
         "ALTER TABLE sessions ADD COLUMN fork_needs_replay INTEGER NOT NULL DEFAULT 0"),
        ("sessions", "fork_metadata", "ALTER TABLE sessions ADD COLUMN fork_metadata TEXT"),
        ("sessions", "fork_revert_record",
         "ALTER TABLE sessions ADD COLUMN fork_revert_record TEXT"),
        ("sessions", "fork_status", "ALTER TABLE sessions ADD COLUMN fork_status TEXT"),
        ("messages", "git_head", "ALTER TABLE messages ADD COLUMN git_head TEXT"),
        ("messages", "git_status_clean",
         "ALTER TABLE messages ADD COLUMN git_status_clean INTEGER"),
        # sidebar-pins.md — as for applications: every existing agent starts
        # pinned, so nothing leaves the sidebar until the user unpins it.
        ("agents", "pinned",
         "ALTER TABLE agents ADD COLUMN pinned INTEGER NOT NULL DEFAULT 1"),
        ("agents", "pin_order", "ALTER TABLE agents ADD COLUMN pin_order INTEGER"),
        # multi-tenancy.md §5 — who owns the row. Seven root tables, plus
        # `sessions`, which breaks the own-it-through-a-foreign-key rule on
        # purpose: its `agent_id` is nullable and the WebSocket fan-out routes
        # by owner on every frame, so one denormalised column beats a join on
        # the hottest path in the product.
        #
        # Nullable with no default and no REFERENCES: the column has to exist
        # before the first user does (this runs on a database that predates
        # accounts entirely), and a NOT NULL would have nothing to put there.
        # `_backfill_owner` fills it in once a user exists; NULL means
        # "belongs to the install", which is what every row is before the
        # upgrade and what no row is after it.
        ("agents", "user_id", "ALTER TABLE agents ADD COLUMN user_id TEXT"),
        ("sessions", "user_id", "ALTER TABLE sessions ADD COLUMN user_id TEXT"),
        ("applications", "user_id", "ALTER TABLE applications ADD COLUMN user_id TEXT"),
        ("backend_credentials", "user_id",
         "ALTER TABLE backend_credentials ADD COLUMN user_id TEXT"),
        ("connector_installations", "user_id",
         "ALTER TABLE connector_installations ADD COLUMN user_id TEXT"),
        ("notifiers", "user_id", "ALTER TABLE notifiers ADD COLUMN user_id TEXT"),
    )

    # The tables above, for the backfill and for anything else that has to
    # treat "every owned table" as one list rather than eight literals.
    OWNED_TABLES: tuple[str, ...] = (
        "agents",
        "sessions",
        "wiki_job_notifications",
        "applications",
        "backend_credentials",
        "connector_installations",
        "notifiers",
    )

    # Deliberately NOT owned: `connector_oauth_clients` and `custom_connectors`
    # are install-level configuration — an OAuth app registration and a
    # connector kind, both of which an admin sets up once for everybody, and
    # both keyed by `kind` alone. The per-account half is
    # `connector_installations` (multi-tenancy.md §5).


    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._dirty: bool = False
        self._closed: bool = False
        self._pending_appends: int = 0
        self._last_flush: float = time.monotonic()
        self._flush_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._dirty_since: float | None = None
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._write_owner: asyncio.Task[Any] | None = None
        # Work that has to unwind before the connection goes away; see
        # `add_close_hook`.
        self._close_hooks: list[Callable[[], Awaitable[None]]] = []

    @asynccontextmanager
    async def _writing(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if self._write_owner is task:
            yield
            return
        async with self._write_lock:
            self._write_owner = task
            try:
                yield
            finally:
                self._write_owner = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        # Wait rather than fail if another connection holds the write lock —
        # the CLI's own tooling and the test suite both open this file, and the
        # default is to raise "database is locked" immediately (B5).
        await self.conn.execute("PRAGMA busy_timeout=5000")
        # NORMAL is the standard pairing with WAL: a commit does not fsync, so
        # a crash can lose the last commits but cannot corrupt the database.
        # Stated explicitly because the durability story below (the timed
        # flush) depends on knowing which it is.
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.executescript(_SCHEMA)
        await self._apply_migrations()
        await self.conn.commit()

    async def _stamp(self, version: str) -> None:
        """Record a migration as applied. The ledger exists so "what has run"
        is a fact in the database rather than an inference from the schema."""
        await self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )

    async def _make_index_per_user(self, name: str, definition: str) -> None:
        """Rebuild a unique index so it is unique per account, not per box.

        Guarded on the index's own SQL rather than on a ledger stamp, because
        the question "is this index already per-user" has an answer in the
        database and a stamp is only a claim about one. Idempotent either way;
        the stamp is still written, so the ledger records the migration ran.
        """
        cur = await self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        )
        row = await cur.fetchone()
        if row and row[0] and "user_id" not in row[0]:
            await self.conn.execute(f"DROP INDEX {name}")  # noqa: S608
            await self.conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {name} {definition}"  # noqa: S608
            )
            logger.info("migration applied: %s -> per user", name)
        await self._stamp(f"index:{name}_per_user")

    async def _add_column(self, table: str, column: str, ddl: str) -> None:
        """Apply one additive column migration, guarded by introspection.

        This replaces `try: ALTER ...; except Exception: pass`. SQLite has no
        `ADD COLUMN IF NOT EXISTS`, and the old form papered over that by
        swallowing *every* exception — a misspelled column, a renamed table, a
        locked database and a full disk all looked identical to "already
        applied", so a broken migration silently no-opped and resurfaced much
        later as an inexplicably missing column. Asking `PRAGMA table_info`
        first means the expected case is tested for, and anything else raises.

        Stamping after a column is found to *already* exist is deliberate: on a
        database that predates the ledger the column is the evidence, so we
        record what we verified rather than assuming a clean history.
        """
        if await self._has_column(table, column):
            await self._stamp(f"column:{table}.{column}")
            return
        await self.conn.execute(ddl)
        await self._stamp(f"column:{table}.{column}")
        logger.info("migration applied: %s.%s", table, column)

    async def _guard_message_sequences(self) -> None:
        """Protect future writes without rewriting ambiguous historical rows."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM messages GROUP BY session_id, seq HAVING COUNT(*) > 1 LIMIT 1"
        )
        if await cursor.fetchone() is None:
            await self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS messages_unique_sequence ON messages(session_id, seq)"
            )
        else:
            # A global UNIQUE index cannot be created over old duplicates. The
            # guards enforce the same rule for new keys and leave every old
            # row intact, including its original storage id and sequence.
            await self.conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS messages_sequence_insert
                BEFORE INSERT ON messages
                WHEN EXISTS(SELECT 1 FROM messages WHERE session_id = NEW.session_id AND seq = NEW.seq)
                BEGIN SELECT RAISE(ABORT, 'duplicate message sequence'); END;
                CREATE TRIGGER IF NOT EXISTS messages_sequence_update
                BEFORE UPDATE OF session_id, seq ON messages
                WHEN (NEW.session_id != OLD.session_id OR NEW.seq != OLD.seq)
                  AND EXISTS(SELECT 1 FROM messages WHERE session_id = NEW.session_id
                             AND seq = NEW.seq AND id != OLD.id)
                BEGIN SELECT RAISE(ABORT, 'duplicate message sequence'); END;
            """)
            logger.warning("Historical duplicate message sequences retained; affected transcripts require review")
        await self._stamp("guard:message_sequences")

    async def _apply_migrations(self) -> None:
        """Bring an existing database up to the current schema.

        Idempotent and safe on every boot. Every step either verifies it has
        already been applied or applies it; nothing is inferred from a
        swallowed exception, so a genuine DDL failure now propagates instead of
        masquerading as "already done".
        """
        for table, column, ddl in self._COLUMN_MIGRATIONS:
            await self._add_column(table, column, ddl)

        # Storage split (B-4): copy pre-split secrets into their own table.
        # New writes go there directly; this is a one-off catch-up, and
        # INSERT OR IGNORE makes re-running a no-op. Kept in a try because it
        # reads a legacy column that fresh databases do not have at all — the
        # one place where "the statement may be invalid here" is the expected
        # case rather than a bug, so it logs instead of passing silently.
        try:
            await self.conn.execute(
                "INSERT OR IGNORE INTO credential_secrets "
                "(credential_id, secret_encrypted) "
                "SELECT id, secret_encrypted FROM backend_credentials"
            )
            await self._stamp("backfill:credential_secrets")
        except Exception:
            logger.exception("credential storage-split backfill failed")

        # The applications name index was originally unconditional, which
        # reserved a name forever: archiving an app then blocked reusing it.
        # Rebuild it live-only (the same rule agents use). Guarded on the
        # index's own SQL, so it runs exactly once.
        cur = await self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'applications_name_unique'"
        )
        row = await cur.fetchone()
        if row and row[0] and "archived" not in row[0]:
            await self.conn.execute("DROP INDEX applications_name_unique")
            await self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS applications_name_unique"
                " ON applications(name COLLATE NOCASE) WHERE archived = 0"
            )
            logger.info("migration applied: applications_name_unique -> live-only")
        await self._stamp("index:applications_name_unique_live_only")


        await self._migrate_agents()
        await self._migrate_schedule_recurrence()
        await self._migrate_schedule_run_at()

        for table, column, ddl in self._LATE_COLUMN_MIGRATIONS:
            await self._add_column(table, column, ddl)

        await self._add_column("messages", "runtime_metadata", "ALTER TABLE messages ADD COLUMN runtime_metadata TEXT")
        await self._add_column("messages", "terminal_request_id", "ALTER TABLE messages ADD COLUMN terminal_request_id TEXT")
        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS messages_terminal_request "
            "ON messages(session_id, terminal_request_id) WHERE terminal_request_id IS NOT NULL"
        )
        await self._guard_message_sequences()

        await self._backfill_fork_owners()

        # After the columns, necessarily: these indexes name `user_id`, which
        # the loop above is what adds to a database that predates accounts.
        # Placed earlier they fail with "no such column" on exactly the
        # installs the migration exists for — which is how the pre-pins fixture
        # found it.
        #
        # Unique per *account* (multi-tenancy.md §5). A name or an external
        # account reserved across the whole box would mean the second person who
        # wants an app called "Notes", an agent called "Octo", or their own
        # GitHub connected cannot have one — and would be told, in a message
        # about a conflict, that somebody else already does.
        # `COALESCE(user_id, '')` and not the bare column: in a UNIQUE index
        # SQLite holds that NULL is not equal to NULL, so indexing the column
        # itself would drop the constraint entirely for the era where every row
        # has `user_id IS NULL` — the pre-accounts install, where these
        # constraints have always held. Found by the connector dedupe test, and
        # true of all three.
        # Two tables briefly carried `user_id` before it was settled that a
        # connector kind and its OAuth client are install-level (see
        # OWNED_TABLES). A column nothing reads is a column that will one day be
        # read by mistake, so it goes. Guarded on its presence, and after the
        # ADD loop above so the two cannot fight over the same database.
        for table in ("custom_connectors", "connector_oauth_clients"):
            if await self._has_column(table, "user_id"):
                await self.conn.execute(
                    f"ALTER TABLE {table} DROP COLUMN user_id"  # noqa: S608
                )
                logger.info("migration applied: %s.user_id dropped", table)
        await self._stamp("drop:connector_config_user_id")

        await self._make_index_per_user(
            "applications_name_unique",
            "ON applications(COALESCE(user_id, ''), name COLLATE NOCASE)"
            " WHERE archived = 0",
        )
        await self._make_index_per_user(
            "agents_name_unique",
            "ON agents(COALESCE(user_id, ''), name) WHERE archived = 0",
        )
        await self._make_index_per_user(
            "connector_installations_account_unique",
            "ON connector_installations(COALESCE(user_id, ''), kind,"
            " external_account_id) WHERE external_account_id IS NOT NULL",
        )

        await self._backfill_pin_order()

        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_forked_from "
            "ON sessions(forked_from_session_id)"
        )
        await self._stamp("index:idx_sessions_forked_from")

        # Backfill default built-in MCP servers into every existing agent as new
        # ones land (`ask_agent` — agent-collaboration.md §5.1; `research` —
        # native-deep-research.md §7; `schedule` — schedule-tool.md §4). The
        # CREATE TABLE default only reaches brand-new rows on fresh databases;
        # pre-existing rows already hold a value and need this catch-up.
        # Set-membership makes re-running a no-op.
        await self._backfill_builtin_mcp_servers(
            ("ask_agent", "research", "schedule")
        )
        await self._stamp("backfill:builtin_mcp_servers_v3")

        # The Telegram bridge was removed (2026-09-25). Octopus is usable from a
        # phone browser directly, which is what the bridge existed to provide;
        # the "an agent can be @-mentioned on a platform that has bots" idea
        # that might replace it is a different design, not this table.
        #
        # Dropped rather than left behind: a table nothing reads is a question
        # every future reader has to answer. The rows were chat->agent bindings
        # — routing state, regenerated by re-binding a chat — so there is
        # nothing here a user would miss.
        await self.conn.execute("DROP TABLE IF EXISTS bridge_mappings")
        await self._stamp("drop:bridge_mappings")

    async def _backfill_fork_owners(self) -> None:
        """Recover forks whose creation omitted the parent's account.

        Include archived parents and repeat for fork chains. A deleted parent
        can be resolved through the inherited agent. Conflicting owners and
        unresolved chains stay untouched; existing ownership never changes.
        """
        repaired = 0
        while True:
            cur = await self.conn.execute(
                """
                UPDATE sessions AS fork
                   SET user_id = COALESCE(parent.user_id, agent.user_id)
                  FROM sessions AS source
                  LEFT JOIN sessions AS parent ON parent.id = source.forked_from_session_id
                  LEFT JOIN agents AS agent ON agent.id = source.agent_id
                 WHERE fork.id = source.id AND fork.origin = 'fork'
                   AND fork.user_id IS NULL
                   AND (
                       (parent.user_id IS NOT NULL AND
                        (agent.user_id IS NULL OR agent.user_id = parent.user_id))
                       OR (parent.id IS NULL AND agent.user_id IS NOT NULL)
                   )
                """
            )
            if cur.rowcount == 0:
                break
            repaired += cur.rowcount
        if repaired:
            logger.info("Restored account ownership for %d fork sessions", repaired)
        await self._stamp("backfill:fork_session_owners")

    async def _set_pinned(self, table: str, row_id: str, pinned: bool) -> None:
        """Pin a row to the sidebar (at the bottom) or unpin it.

        Unpinning leaves `pin_order` alone — it only means something while
        pinned, and pinning again always appends, because an item coming back
        to the sidebar is news and belongs where the eye lands last.
        """
        await self._ensure_connected()
        if pinned:
            await self.conn.execute(
                f"UPDATE {table} SET pinned = 1, "
                f"pin_order = {self._NEXT_PIN_ORDER.format(table=table)} "
                "WHERE id = ? AND pinned = 0",
                (row_id,),
            )
        else:
            await self.conn.execute(
                f"UPDATE {table} SET pinned = 0 WHERE id = ?", (row_id,)
            )
        await self.conn.commit()

    async def _reorder_pins(self, table: str, ordered_ids: list[str]) -> None:
        """Lay the pinned rows out in `ordered_ids` order.

        Any pinned, live row the list doesn't name — pinned from another tab
        after this one last looked — keeps its relative order and follows
        the named ones, so a reorder never drops someone else's pin.
        """
        await self._ensure_connected()
        cursor = await self.conn.execute(
            f"SELECT id FROM {table} WHERE pinned = 1 AND archived = 0 "
            "ORDER BY pin_order, created_at, id"
        )
        named = set(ordered_ids)
        rest = [r[0] for r in await cursor.fetchall() if r[0] not in named]
        for position, row_id in enumerate([*ordered_ids, *rest], start=1):
            await self.conn.execute(
                f"UPDATE {table} SET pin_order = ? WHERE id = ?",
                (position, row_id),
            )
        await self.conn.commit()

    async def _backfill_pin_order(self) -> None:
        """Give every agent and application without a sidebar position one.

        Rows that predate `pin_order` read back NULL. They are numbered in the
        order the sidebar already showed them (the Default Agent first, then
        by creation), after any row that already has a position, so the
        first boot on this schema changes nothing the user can see. Rows
        written since always carry a position, so re-running touches nothing.
        """
        for table, order_by in (
            ("agents", "is_system DESC, created_at, id"),
            ("applications", "created_at, id"),
        ):
            await self.conn.execute(
                f"""
                UPDATE {table} SET pin_order = (
                    SELECT base + rn FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (ORDER BY {order_by}) AS rn,
                               (SELECT COALESCE(MAX(pin_order), 0)
                                  FROM {table}) AS base
                          FROM {table} WHERE pin_order IS NULL
                    ) AS numbered WHERE numbered.id = {table}.id
                )
                WHERE pin_order IS NULL
                """
            )
            await self._stamp(f"backfill:{table}_pin_order")

    async def _backfill_builtin_mcp_servers(self, names: tuple[str, ...]) -> None:
        cursor = await self.conn.execute("SELECT id, mcp_servers FROM agents")
        rows = list(await cursor.fetchall())
        for agent_id, raw in rows:
            try:
                current = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                # A corrupted blob is left alone — manual rescue from
                # SQLite is safer than guessing what the user meant.
                continue
            if not isinstance(current, list):
                continue
            added = False
            for name in names:
                if name not in current:
                    current.append(name)
                    added = True
            if added:
                await self.conn.execute(
                    "UPDATE agents SET mcp_servers = ? WHERE id = ?",
                    (json.dumps(current), agent_id),
                )

    async def _migrate_schedule_recurrence(self) -> None:
        """Schedules gained cron/timezone/recurrence_label and `interval_seconds`
        became nullable (natural-language + time-of-day scheduling), then later
        an `origin_session_id` (a `/schedule` created in a chat remembers its
        session so fires append into that conversation). Rebuild the table once
        for the recurrence shape — guarded on the `cron` column being absent —
        then additively ensure the origin column. Fresh DBs (already the full
        shape from _SCHEMA) and re-boots no-op. Runs after `_migrate_agents`, so
        the table already has `agent_id` and no `session_id`. Existing rows are
        interval schedules: cron/timezone/recurrence_label stay NULL and the UI
        formats interval_seconds."""
        if not await self._has_column(
            "schedules", "cron"
        ) and await self._has_column("schedules", "interval_seconds"):
            await self.conn.executescript(
                """
                CREATE TABLE schedules__rec (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    interval_seconds INTEGER,
                    cron TEXT,
                    timezone TEXT,
                    recurrence_label TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT
                );
                INSERT INTO schedules__rec
                    (id, agent_id, name, prompt, interval_seconds, enabled,
                     created_at, last_run_at)
                    SELECT id, agent_id, name, prompt, interval_seconds, enabled,
                           created_at, last_run_at FROM schedules;
                DROP TABLE schedules;
                ALTER TABLE schedules__rec RENAME TO schedules;
                """
            )
        # origin_session_id is additive on top of the recurrence shape. Guarded
        # so re-running (and fresh DBs that already have it from _SCHEMA) no-op.
        if not await self._has_column("schedules", "origin_session_id"):
            await self.conn.execute(
                "ALTER TABLE schedules ADD COLUMN origin_session_id TEXT"
            )

    async def _migrate_schedule_run_at(self) -> None:
        """Add run_at column to schedules (one-time schedule support). Fresh DBs
        already have it from _SCHEMA; re-runs are no-ops."""
        if not await self._has_column("schedules", "run_at"):
            await self.conn.execute(
                "ALTER TABLE schedules ADD COLUMN run_at TEXT"
            )
            await self.conn.commit()
        # Which session the last fire ran in. Additive; NULL on every
        # pre-existing row means "we don't know", which both readers handle.
        if not await self._has_column("schedules", "last_run_session_id"):
            await self.conn.execute(
                "ALTER TABLE schedules ADD COLUMN last_run_session_id TEXT"
            )
            await self.conn.commit()
        # Wiki authorization is separate from the scheduled body. Legacy
        # schedules remain unprivileged until explicitly configured.
        if not await self._has_column("schedules", "wiki_mode"):
            await self.conn.execute("ALTER TABLE schedules ADD COLUMN wiki_mode TEXT")
            await self.conn.commit()
        if not await self._has_column("schedules", "invalid_reason"):
            await self.conn.execute("ALTER TABLE schedules ADD COLUMN invalid_reason TEXT")
            await self.conn.commit()

    async def _column_info(self, table: str) -> list[tuple[Any, ...]]:
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        return list(await cursor.fetchall())

    async def _has_column(self, table: str, column: str) -> bool:
        return any(row[1] == column for row in await self._column_info(table))

    async def _column_is_not_null(self, table: str, column: str) -> bool:
        # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
        for row in await self._column_info(table):
            if row[1] == column:
                return bool(row[3])
        return False

    async def _migrate_agents(self) -> None:
        """First-class Agents refactor migration (agent-refactor.md §4.5).

        Adds agent ownership to sessions and schedules,
        creates the protected Default Agent, and backfills every
        pre-existing row to it. Idempotent: safe on every boot, a second
        run no-ops (system agent present, no null agent_id rows, the
        column-shape rebuilds already applied). `schedules.session_id`
        and the schedules table's old shape are removed by
        table-rebuild rather than ALTER … DROP/MODIFY, because SQLite
        forbids dropping a column that's part of a foreign key and can't
        relax NOT NULL in place.
        """
        # 1. Additive columns (wrapped — SQLite has no IF NOT EXISTS for ALTER).
        #    Adding a column with a REFERENCES clause is allowed because the
        #    default value is NULL.
        for table, column, ddl in (
            (
                "sessions",
                "agent_id",
                "ALTER TABLE sessions ADD COLUMN agent_id TEXT "
                "REFERENCES agents(id) ON DELETE CASCADE",
            ),
            (
                "sessions",
                "origin",
                "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'",
            ),
            (
                "schedules",
                "agent_id",
                "ALTER TABLE schedules ADD COLUMN agent_id TEXT "
                "REFERENCES agents(id) ON DELETE CASCADE",
            ),
        ):
            # Asked, not attempted-and-swallowed: `except Exception: pass` around
            # an ALTER absorbs a typo'd column, a locked database and a full disk
            # as readily as the "column already exists" it was written for
            # (polish-2026-09.md §3 A3).
            if not await self._has_column(table, column):
                await self.conn.execute(ddl)

        # 2. The protected Default Agent — exactly one, created once.
        cursor = await self.conn.execute(
            "SELECT id FROM agents WHERE is_system = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            default_id = uuid.uuid4().hex[:12]
            now = datetime.now(UTC).isoformat()
            await self.conn.execute(
                "INSERT INTO agents "
                "(id, name, description, system_prompt, mcp_servers, "
                " is_system, created_at, updated_at, backend) "
                "VALUES (?, 'Octo', '', '', ?, 1, ?, ?, ?)",
                (default_id, _DEFAULT_MCP_SERVERS_JSON, now, now, settings.default_backend),
            )
        else:
            default_id = row[0]
            # One-time rename of the auto-created system agent from its old
            # 'Default' name to 'Octo'. Guarded on the exact old name so a
            # user-renamed system agent is left alone; try/except so it no-ops
            # if an agent named 'Octo' already exists (unique-name index).
            try:
                await self.conn.execute(
                    "UPDATE agents SET name = 'Octo' "
                    "WHERE id = ? AND name = 'Default'",
                    (default_id,),
                )
            except sqlite3.IntegrityError:
                # An agent already holds the name 'Octo' (unique-name index).
                # Leave the system agent's old name rather than fail the boot.
                logger.info(
                    "system agent kept its old name: 'Octo' is already taken"
                )

        # 3. Backfill sessions → Default Agent. (origin defaults to 'user'.)
        await self.conn.execute(
            "UPDATE sessions SET agent_id = ? WHERE agent_id IS NULL",
            (default_id,),
        )

        # 4. Schedules: derive agent_id through the (about-to-be-removed)
        #    session_id, then rebuild the table without it. Guarded on the
        #    presence of session_id so it runs exactly once.
        if await self._has_column("schedules", "session_id"):
            await self.conn.execute(
                "UPDATE schedules SET agent_id = ("
                "  SELECT s.agent_id FROM sessions s WHERE s.id = schedules.session_id"
                ") WHERE agent_id IS NULL"
            )
            # Orphans whose session was deleted fall back to Default.
            await self.conn.execute(
                "UPDATE schedules SET agent_id = ? WHERE agent_id IS NULL",
                (default_id,),
            )
            await self.conn.executescript(
                """
                CREATE TABLE schedules__new (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT
                );
                INSERT INTO schedules__new
                    (id, agent_id, name, prompt, interval_seconds, enabled,
                     created_at, last_run_at)
                    SELECT id, agent_id, name, prompt, interval_seconds, enabled,
                           created_at, last_run_at FROM schedules;
                DROP TABLE schedules;
                ALTER TABLE schedules__new RENAME TO schedules;
                """
            )

    async def _ensure_connected(self) -> None:
        # A closed Database is dead — never silently re-open. The
        # previous "reconnect" path was load-bearing for nothing in
        # production and was the root cause of a pytest atexit hang:
        # tests that closed the DB still had pending consumer tasks
        # that would call flush() during loop teardown, the reconnect
        # spawned a brand-new aiosqlite worker thread right before
        # the loop died, and that orphaned non-daemon thread pinned
        # the process. We raise CancelledError so in-flight callers
        # (e.g. session_manager._consume_message) exit cleanly via
        # their existing CancelledError handling.
        if self._closed:
            raise asyncio.CancelledError("Database is closed")
        assert self._conn is not None, "Database not initialized"

    def add_close_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register work to unwind *before* this connection goes away.

        The database is the resource; whoever holds long-running work against
        it has to be given the chance to finish that work while the connection
        still answers. A turn in flight is the case this exists for: cancelled
        after the connection is gone, its own `finally` — which flushes and
        broadcasts — has nothing to await and nothing to cancel it, so the event
        loop stops making progress instead of failing. Cancelled *before*, it
        unwinds normally.

        Hooks run in reverse registration order, like a stack of context
        managers. A failed hook keeps the resource open and remains registered
        so a later close can confirm teardown before retiring the connection.
        """
        self._close_hooks.append(hook)

    async def close(self) -> None:
        # Closing owns the timer and connection. Caller cancellation must not
        # abandon either resource halfway through their teardown.
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close(), name="database-close")
            self._close_task.add_done_callback(self._observe_close)
        await asyncio.shield(self._close_task)

    @staticmethod
    def _observe_close(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Database close failed; connection retained for retry",
                         exc_info=(type(error), error, error.__traceback__))

    async def _close(self) -> None:
        """Unwind the hooks, commit, then close. The flag goes up *before* the
        connection is torn down, deliberately.

        `await self.conn.close()` yields, and in that window a task that was
        waiting elsewhere can reach `_ensure_connected`, find `_closed` still
        False, and submit work to an aiosqlite worker thread that is stopping.
        That future is never resolved by anyone: the task cannot be cancelled
        out of it either, so the *loop* cannot finish tearing down, and what you
        see is a test run — or a shutdown — that stops making progress instead
        of failing. Setting the flag first turns that window into the
        `CancelledError` the callers already handle.

        The commit below goes through `self.conn` rather than
        `_ensure_connected`, so closing can still flush what it has to.
        """
        async with self._close_lock:
            failures: list[Exception] = []
            for hook in reversed(tuple(self._close_hooks)):
                try:
                    await hook()
                except Exception as error:
                    logger.exception("Database teardown remains unconfirmed")
                    failures.append(error)
                else:
                    self._close_hooks.remove(hook)
            if failures:
                raise RuntimeError("Database teardown remains unconfirmed; connection kept open") from failures[0]
            async with self._writing():
                if not self._conn:
                    return
                timer = self._flush_task
                if timer is not None:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
                    self._flush_task = None
                try:
                    await self._flush_locked()
                except Exception:
                    self._schedule_flush()
                    raise
                self._closed = True
                await self.conn.close()
                self._conn = None

    async def flush(self) -> None:
        """Commit pending writes."""
        await self._ensure_connected()
        async with self._writing():
            await self._ensure_connected()
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Acknowledge the batch while append/touch/close cannot alter it."""
        if self._dirty:
            await self.conn.commit()
            self._dirty = False
            self._dirty_since = None
            pending, waited = self._pending_appends, time.monotonic() - self._last_flush
            self._pending_appends = 0
            self._last_flush = time.monotonic()
            _mon_record(_MonEvent(
                kind="db_flush",
                duration_ms=waited * 1000,
                ok=True,
                detail={"rows": pending},
            ))

    async def _maybe_flush(self) -> None:
        """Size pressure may flush early; the first write's timer bounds idle batches."""
        if not self._dirty:
            return
        if (
            self._pending_appends >= _FLUSH_EVERY_APPENDS
            or (time.monotonic() - (self._dirty_since or self._last_flush)) >= _FLUSH_AFTER_SECONDS
        ):
            await self._flush_locked()

    def _defer_commit(self) -> None:
        self._dirty = True
        if self._dirty_since is None:
            self._dirty_since = time.monotonic()
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_deferred(), name="database-batch-flush")

    async def _flush_deferred(self) -> None:
        while self._dirty and not self._closed:
            deadline = (self._dirty_since or time.monotonic()) + _FLUSH_AFTER_SECONDS
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            try:
                async with self._writing():
                    await self._flush_locked()
            except Exception as error:
                logger.exception("Deferred database commit failed; batch retained for retry")
                _mon_record(_MonEvent(kind="db_flush", ok=False, error_code=type(error).__name__))
                # Failed storage cannot honor the normal deadline. Preserve
                # dirty state and retry at a bounded rate, without a busy loop.
                await asyncio.sleep(_FLUSH_AFTER_SECONDS)

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn

    async def _count(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """One `SELECT COUNT(*)`, as an int.

        An aggregate returns exactly one row, which is what the callers that
        used to `execute` / `fetchone` / `row[0]` were each relying on without
        saying so. Said here once, and phrased as the one thing they all want.
        """
        await self._ensure_connected()
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
