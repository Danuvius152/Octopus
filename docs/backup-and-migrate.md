# Backing up and moving a deployment

Back up the **actual configured installation and its referenced files**. A copy
of the source checkout, the database alone, or a guessed `~/.octopus/` subtree
does not establish a complete backup. Paths below describe current code; use
the active service configuration to locate them.

**Scope, 2026-10-23:** preserve current accounts and data. Importing an old
no-account installation is no longer a future support requirement. Complete
same-version cold export/restore with path and machine-binding changes is
[planned](plans/architecture-simplification-20261023.md), not implemented or
verified. This guide identifies what must be protected and the current restore
limits; it is not an automated cross-machine migration recipe.

**Migration simplification, 2026-10-25:** the selected migration follows the
current layout under home, with an optional home-prefix change. It exports
stable data only; unresolved writes affecting the selected data must be handled
on the source first. Export and import perform their own preflight checks.
Import failure leaves the target inactive; remove this attempt's staging and
retry. Arbitrary multi-root mapping, cross-machine execution recovery, resumable
imports and generic hardcoded-path scanning are outside this scope. An empty
target means the selected Octopus data locations, not the entire home directory.

**Migration scope clarification, 2026-10-23:** the user's move to a new machine
includes persistent contents of `users/<user_id>/`, with agents, memory, the
personal Wiki and applications. Application code and data are retained;
runtime dependencies are reinstalled. Old chats and native thread continuity
are not required, nor are extra external/fork work trees collected for old
sessions. Ordinary files inside the selected account directory remain included.
The same account and resource ownership must work on the target. The directory
alone is insufficient: required database records, keys and any external Wiki
vault must also be collected. The restored agents start new conversations.
Scheduled tasks are excluded from this migration: recurring and one-shot
definitions, enabled/disabled settings and pending triggers are not packaged or
restored. There is no post-import task activation step. Source tasks remain
unchanged. Preserve historical evidence needed by retained knowledge without
recreating executable schedules; a full database backup is a separate scope.
Agent model selections, account-stored API endpoints/keys and credential
associations are retained. Model CLI subscription login state is excluded;
reauthorize those credentials on the target before validating model access.
This exclusion applies to login tokens and authentication caches wherever they
are stored. It does not exclude the Octopus account password verifier or the
keys needed to decrypt retained data, and does not change the source login.
The inventory below describes what a complete backup protects; the migration scope is being confirmed
in [the plan](plans/architecture-simplification-20261023.md#71-接口和数据边界).
Archiving a session keeps its messages and directory path, not a file snapshot.

## 1. Where the state actually lives

**Runtime database.** `Settings.db_path` defaults to the relative `octopus.db`,
resolved from the process working directory, but `OCTOPUS_DB_PATH` and the
selected service environment can override it. The local installation was
previously observed using `/home/jack/.octopus/octopus.db`; recheck the active
configuration rather than assuming either location. See
[local operating procedures](local-wsl.md) and [Settings](../server/config.py).
Do not print environment-file contents containing secrets while locating paths.

The database holds accounts, wrapped data keys, sessions/messages, agents,
schedules, credentials, connectors, applications, inputs and completion
evidence. WAL files belong to the database's live state; see §3.

**Master key.** The effective `OCTOPUS_MASTER_KEY`, or the configured
`master_key_file`, unwraps account DEKs and protects installation secrets.
Account passwords cannot replace it. Restore must verify the supplied key can
unwrap the saved DEKs **before startup**: `crypto.master_key()` creates a new
key when its file is absent and is therefore not a read-only backup check.
Transfer the key through protected storage, not ordinary logs or Git.

**Account files and referenced roots.** `workspace.paths_for(user_id)` places
owned workspace, agents, applications, attachments, Codex and research under
the configured `users_root/<id>/`. Sessions may also refer to external project
roots or retained directories from an earlier setup. Back up the locations
actually referenced, irrespective of when they were created.

Attachment and large-prompt writers currently still use global configured roots.
The confirmed [account-path correction](plans/architecture-simplification-20261023.md#64-附件和长输入文件的账户归属)
will use the owning account's directories and preserve existing files and references;
it is not implemented yet. Inventory the actual global files as well as account
trees until that correction is verified. Do not discard files with unknown ownership.

**Native provider state.** Octopus messages serve the UI; provider transcripts
and associated state supply native resume. For Codex, locate the effective
credential's `CODEX_HOME`, not just `~/.codex/auth.json`: account homes and
per-credential homes may differ from the host default. Preserve required
transcripts and indexes along with authentication. For a used Claude backend,
preserve its configured project transcripts and login state. Reauthentication
does not recover missing conversation history. The current local validation
target is Codex; Claude live checks are outside this installation's scope.

**Wiki.** Include each actual vault, its registration/configuration, state
directory, `index.sqlite3`, history/blobs and pending operation evidence. The
vault may be outside `~/.octopus/`. Despite its name, `index.sqlite3` contains
durable identities, source bindings, grants, jobs and filesystem-event records;
it cannot be discarded as a search cache.

## 2. The minimum backup set

| Content | Required treatment |
| --- | --- |
| Actual runtime database | Consistent snapshot retaining all current accounts, IDs, messages, inputs and receipts |
| Effective master key and selected configuration | Protected transfer; record actual paths and configuration sources without logging secret values |
| Account trees | Memory, attachments, application code and `.data`, workspace files and reports that need to remain accessible |
| Referenced work trees | Include fork copies, external projects, untracked files and archived-session work trees that must remain usable |
| Actual credential homes | Native transcripts/resume indexes and necessary login state; do not substitute the target machine's unrelated default login |
| Wiki vault and state | Preserve business identities, history and all durable ledgers, including unresolved evidence |
| Service profile and ownership references | Retain as source evidence; target installation paths and bindings must be verified or explicitly reconstructed |
| Rebuildable dependencies, caches, metrics and transient prompts | Exclude only after classifying them as reproducible; retained diagnostics are a separate choice |

Record a file inventory and integrity digests. Resolve nested roots without
silently omitting external files; identify symlinks and their required targets
instead of blindly following links outside the selected set. A top-level
directory name is not proof that its contents are disposable.

## 3. Copy the database safely

Do not copy a live SQLite main file alone. Either stop writers and preserve a
consistent database/WAL set, or use SQLite's backup API to obtain a single
self-contained database snapshot. A live database backup only covers that
database: it does **not** synchronize Wiki, application state, work trees or
provider files with it.

For a complete cold backup, stop the source service and relevant external
writers, confirm they have exited, then copy the stable set from §2. Do not
delete a WAL merely because the main file exists. Keep source files intact and
verify the resulting database and file inventory before treating the backup as
usable. Use the read-only publication verifier to inspect current storage.
Ordinary initialization is not a verification method: although startup no longer
performs historical migrations, writers and projection repair may modify the
copy being inspected. See the [current storage contract](plans/current-storage-format.md).

The intended migration workflow produces one complete archive for later import
on another machine. Import need not contact the source. The confirmed export flow
pauses relevant writes only while taking a stable copy, then resumes source use
and compresses that copy. The user requested working export/import interfaces,
verified in isolation; no actual production migration is requested now. This
workflow is not implemented yet. Later source changes are not included in an
earlier archive; online backup of one database does not snapshot all app/Wiki files.

## 4. Path and machine bindings

Moving to another username, home or filesystem affects more than
`sessions.working_dir`. Current bindings include account `extra_roots`, provider
homes and path indexes, service profile/ownership references, Wiki registration
and event-inbox roots, and filesystem device/inode identities.

Claude project slugs are one path-dependent example; renaming those and editing
session rows is not a complete installation migration. Even keeping absolute
paths identical does not preserve filesystem identities or prove installation
ownership on another machine. Current Wiki registration rejects changing an
existing vault root, and service startup checks ownership markers against its
profile. These checks protect current data and must not be bypassed by deleting
markers or rebuilding the entire Wiki database.

The planned migration follows the agreed home layout and adjusts only defined
system path fields and explicitly known configuration for the selected contents,
preserves retained account/agent/vault/document/change identities and content,
and rebinds machine-local
state in a staged target. It does not replace arbitrary path strings inside
messages, source documents or application code. Until that operation is
implemented and validated, keep the source intact and do not treat a manual
copy as a verified cross-machine restore.

## 5. Working copies and applications are persistent data

**Fork work trees have no guaranteed substitute.** `/fork` creates a full,
independent working-directory copy and saves its path on the new session.
Later edits, untracked files and binaries need not exist in the parent or in
conversation history. The session row cannot regenerate them. Include every
fork work tree that must remain usable, including those of archived sessions;
exclude one only after explicitly deciding its files are no longer needed.

**Applications also need their files.** Preserve application code and the
adjacent `.data` state of backend applications. A build session and its messages
are not a complete copy of generated files. `.runtime` dependencies may be
rebuildable, but classify that separately from application-owned state. This
guide does not authorize pruning either kind of directory.

## 6. Restore readiness and verification

The checks below describe complete-backup restoration, not claims that a restore
command already exists. For the selected migration, validate the contents agreed
in the plan; old chats, work trees and native thread continuity are not gates.
For that migration, missing subscription login state is expected: preserve the
selected credential association, request reauthorization, then verify model
access using that credential rather than an unrelated default account.

1. Select an empty target and a supported matching version. Validate package
   integrity, all required roots and the effective master key without creating
   a new key or modifying the source. Importing a completed archive does not
   require the source to remain stopped. The selected migration excludes
   schedules; for a full-backup restore, automatic producers need an explicit
   activation rule before the restored installation runs them.
2. Prepare the target without admitting sessions, observers or schedulers.
   Verify account ownership, file contents and current persistent evidence;
   resolve path and installation bindings from §4. Missing or ambiguous data
   must stop activation rather than produce a new empty store.
3. Preserve unresolved execution/effect records. A stopped process is not
   proof of business success. Do not replay completed inputs, delegation
   returns or one-shot schedules, and do not run source and target schedulers
   concurrently. An interrupted restore must remain identifiable and unable
   to start a partially restored installation.
4. Check current accounts, permissions, memory, attachments, fork/external
   files, application state and Wiki identities/history. A healthy HTTP endpoint
   or a loaded page alone does not prove recovery.
5. If restoring native history from a complete backup, in an isolated validation
   installation, continue a known Codex session and
   verify its expected credential, native session identity and scoped tool
   access to saved data. A plausible answer or fallback login does not prove
   native history survived. Expired provider credentials may independently
   require reauthorization.

For the selected migration, instead start a new Codex conversation under a
restored agent and verify access to its saved memory and Wiki through the
expected scoped tools. This does not require restoring an old native thread.

Validation with a changed home prefix and filesystem identity remains an explicit
deliverable of the [portability plan](plans/architecture-simplification-20261023.md#7-r4当前版账户资料导出与导入).
Source-data deletion is a separate decision after verified recovery.
