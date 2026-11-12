# Backing up and moving a deployment

Choose the operation by what must survive. Use the actual configured
installation and referenced files; a source checkout or a guessed
`~/.octopus/` subtree is not a complete inventory.

| Operation | Retained scope | Supported workflow |
| --- | --- | --- |
| In-place upgrade/read | Existing sessions, messages, input evidence, native references and saved details | The finite publication handoff and current-format verifier; no implicit execution |
| Session transcript export | Original message rows, metadata, terminal and answer references | The transcript HTTP export; no submission table, provider store, credentials or new session-import capability |
| Account export/import | One account's durable configuration, files, Wiki and applications; new conversations on the target | The same Octopus build, an archive, and empty target data locations under the selected home layout |
| Complete backup/restore | All required installation data, including chats, schedules, native transcripts and referenced work trees | A consistent backup plus explicit verification of configuration, keys, machine bindings and restored execution state |

Account export/import is the provided migration interface. Complete-backup
restoration is a broader operator procedure; the account importer does not
restore excluded history or arbitrary historical formats. Architecture decisions
live in [the architecture overview](architecture.md#ownership-and-failure-boundaries);
format and publication rules live in [the storage contract](implemented/current-storage-format.md).
Installation admission, drain and original handoff receipts are defined in the
[installation lifecycle](implemented/installation-lifecycle.md); this guide
owns the ordered operator steps, rather than a second lifecycle contract.

`tests/test_codex_app_server_preservation.py` verifies these boundaries separately
through transcript HTTP, real account archives and a reopened consistent SQLite
backup with referenced native artifacts. It preserves root answers, helper
partial details, Question resolutions, unknown input and accepted-but-processing-
unconfirmed steer. A database-only snapshot proves database compatibility; full
restoration also requires the referenced files and the target's closed execution
state. Account transfer success cannot stand in for chat or full-backup evidence.

<a id="7-selected-account-export-and-import"></a>

## Account export and import

### Contents and exclusions

Each export collects resources actually owned by the selected account at that
point in time, including subsequently added agents, files, registered Wiki
content and applications. It does not use a fixed instance-specific file list.

| Content | Account archive behavior |
| --- | --- |
| Account and agents | Preserve account ID, username, password verifier, required keys/DEK, agent configuration, ownership and credential associations |
| Unfinished application deletion | Preserve its phase, original keep-files choice and failure, and collect the remaining code/data. Only `files` with `keep_files=false` permits an already removed code directory; other missing code remains an error. Runtime stays excluded. A retained build status does not resume a writer or block transfer once live writes are retired. Excluded session/BG/delegation history does not restore execution authority. The imported app remains closed; opening the account neither runs it nor resumes file removal. Explicit retry continues the remaining phase. |
| Account files | Preserve private agent memory and the same account's `agents/shared-memory/`, ordinary workspace files, attachments, research materials, application code/configuration and `.data`; reinstall rebuildable `.runtime` dependencies |
| Wiki | Preserve registered vault content, sources/proofs, content history, business identities and the durable facts needed for query and maintenance, including a registered external vault within the supported home layout |
| API configuration | Preserve account-stored endpoints/keys and saved credential associations |
| Connector installations | Preserve the account's installation IDs, metadata, current encrypted grants and Agent enablement links, together with referenced custom definitions and OAuth client configuration. Reconnect updates those existing records; it adds no collection root. |
| Chats and native continuity | Exclude old sessions/messages, input receipts and native input evidence, native transcripts/resume state, BG/delegation execution history and their return diagnostics, and extra external/fork work trees collected solely for old conversations; ordinary files already inside the account's included roots remain included |
| Schedules and execution | Exclude recurring/one-shot definitions, enablement, pending triggers and live process/PID/socket/lease state; no post-import schedule activation step |
| Login state | Exclude model CLI subscription tokens, refresh tokens and authentication caches wherever stored; target credentials require reauthorization. Octopus account password/key material remains included; sign in again on the target |
| Browser text drafts | Current-tab `sessionStorage` text is browser-local and excluded from account archives and server backups. Import does not collect or restore it; [draft recovery](implemented/input-reliability.md#browser-application-text-drafts) grants no submission or execution authority. |
| Host network entry | Host-level tunnel binaries, systemd units and proxy settings outside account roots are not account resources. Reinstall them separately; importing an account does not open a public tunnel |

Knowledge history and source/effect evidence are not executable work and must
not be discarded as a cache. Exclusion from an archive does not delete or
reconfigure source chats, tasks, logins or files.

Pending Connector OAuth logins, including their original Reconnect target, are
process-local and are neither transferred nor resumed. Restoring completed
installation records preserves the saved relationships and grant; it does not
replay a callback or establish current provider access. See the
[Connector lifecycle](implemented/connectors.md#oauth-and-lifecycle).

Current archives contain complete main format 10 and Wiki format 17. GitHub
installations preserve their canonical numeric identity, display label, original
installation ID, encrypted grant and Agent links through export/import. The
importer rejects an older main marker before creating the target; it does not
convert `login:id` keys or restore pending OAuth work. A historical installation
must first complete its authorized source-side publication handoff under the
[storage contract](implemented/current-storage-format.md#github-identity-and-finite-publication-handoff).

Research reports, versioned `{job_id}/evidence.json` summaries and
`synthesis-failure.json` materials follow the existing recursive account-tree
collection. Session and `research_jobs` history remain excluded: import preserves
the original files without restoring task cards or execution authority. Session
deletion likewise retains these account files; unavailable or malformed summaries
are not permission to delete them. A complete backup preserves the existing rows
and matching research tree together. See the [Research file contract](implemented/native-deep-research.md#7-invocation-files-and-verification).

Agent context-setting revisions belong to the source comparison authority,
not the user's model configuration: account import preserves the settings and
rebuilds each target Agent's `context_settings_revision` as 0. Session
`context_state` follows the existing exclusion of chats and native continuity.
A complete consistent backup instead preserves both fields and their original
binding/revision relationships. Neither restoration path revives an attempt or
permits replay; there is no additional collection root. See
[context storage](implemented/current-storage-format.md#complete-record-projections).

Compact payloads, native ACK/root evidence and effect-bearing maintenance outcomes
follow existing sessions/messages/client_submissions ownership. Complete backups
preserve them; account archives exclude them with old chats. Neither restoration
path resumes or replays compaction, and no new collection root is introduced.
This includes Claude's original command UUID, raw local-command outcome and
failure reason; a restored maintenance result retains its separate effect and
business status rather than authorizing another native operation.

Private `messages.git_ignored_baseline` follows that same chat exclusion. A
complete consistent backup preserves the raw nullable versioned JSON with its
original message, HEAD and clean observation. Account transfer neither collects
a new root nor restores this proof. Format-9 history receives NULL during the
finite publication handoff; restoration never invents original absence evidence
or grants permission to replay a file revert. See
[safe rewind](implemented/session-rewind.md#563-the-one-revert-we-offer-file-edits-via-git).

Structured Question resolutions live in existing chat-message JSON, with no
additional collection root. They follow the same chat exclusion in account
archives. A complete consistent backup retains that JSON and its original form
associations; restoring it does not revive callbacks or infer missing legacy
structure/provenance from text. Compatibility is described in the
[current storage contract](implemented/current-storage-format.md#publication-boundary).

Original-work interaction policy stays inside existing input/task/schedule
context JSON. Question wait mode, answer target, MCP call association and
`needs_input` closure stay in existing message JSON. Collect or exclude them
with their owning records above: account archives still exclude chat/execution
and schedule history; a consistent complete backup keeps their original
associations, and the Wiki ledger retains its existing domain facts. No extra
root, migration object or restoration step is introduced. Missing legacy policy
remains unknown. Restoring saved facts does not recreate MCP handlers, command
Futures, REST waiters, parent callbacks or permission to answer an old question,
and never automatically resumes a fire or replays missing-decision work.

Delegation's existing session JSON also retains outstanding child-work references
and its successful candidate request. Complete backup preserves them together
with the original submissions, carriers and terminals. Account archives exclude
these round/execution facts with chats; restore does not infer consumption from
the latest request or restart an unsealed round. No additional collection root
or transfer/import step is introduced. Derived credential-use and Connector grant
comparison evidence is not a persistent revision service or a new backup resource.

Device-login staging, target reservations, in-flight commit/cleanup tasks and
original credential-deletion proofs are process-local and are not transferred or
restarted. Completed credential metadata retains its existing collection rules;
model subscription authorization remains excluded from account archives. A
complete backup requires retired writers and matching DB/provider-home files;
capturing a login's uncommitted staging or an uncertain cross-file/DB publication
does not create a recoverable login. This adds no root or import step; see the
[credential publication boundary](implemented/harness-credential-reauth.md#codex-device-operation-ownership).

App Server native source, child text/tool messages, `native_subagent` status
facts and original question bindings use the same existing chat-message JSON
and follow that same archive exclusion. Complete backup/restore retains their
original request/attempt/thread/turn/item and question associations without
creating another body or storage root. Restoring those facts does not revive
reply eligibility, run leases, pending native work or the in-memory card cache.
Native result `final_answer` references and `native_status` also stay within that
message JSON. A complete backup keeps original seqs and their referenced rows;
account archives exclude them with the chat. No new collection root is added,
and an unavailable reference is not recovered from another session's history.

Native item status/failure, hook source and generated-image metadata in
`tool_input.native_item` follow that same chat exclusion. Inline image bytes are
not stored; a file ID or size/hash cannot restore the image. `savedPath` remains
the original path, with no historical snapshot guarantee. Ordinary files inside
included account roots retain their existing collection rules; this adds no
provider/media root to account transfer. A complete backup that needs those
images must also preserve their actual referenced files and verify access after
restoration. See the [native image contract](implemented/codex-backend.md#native-generated-images).

App Server input evidence belongs to `client_submissions` in the main database,
with no new filesystem root. Account archives exclude it with the owning chat;
import does not restore a native writer or pending execution. A complete backup
retains the evidence, original input/attempt/RPC/thread/turn associations and the
matching provider history. Restoration preserves unknown acceptance and preparation
changes; it does not infer consumption from a saved terminal or automatically
replay an input. An absent original transcript remains a visible resume failure.

Fork `copied_input` observations, copied terminal keys and native creation
checkpoints also belong to existing message/session records. Account archives
exclude them with chats; import does not restore an unfinished fork, writer or
replay right. A complete backup retains those rows plus their selected native
home and copied working directory while recovery still depends on them. Restore
keeps unknown creation unknown and uses only the saved actual artifact identity
and confirmed non-executing retirement qualification for cleanup. A missing or
false qualification retains the recovery material after restore; an ACK alone
cannot supply it. Restore does not substitute a preallocated hint or the target's
current transport for the original fork strategy.

The full-copy directory creation receipt in `fork_metadata.owned_copy_dir` uses
that same existing session JSON and chat exclusion. Complete backup retains an
initializing row, its receipt and the referenced independent directory while
recovery depends on them. Missing or mismatched creation evidence preserves the
directory; restoring or relocating a working directory does not manufacture new
cleanup permission. Neither transfer nor backup restores the original copy
thread. The [fork recovery contract](implemented/session-fork.md#4-crash-safety--limits)
owns the artifact, directory and recovery-row ordering.

### Prepare and export

Wiki document resource refusal does not make the original file disposable.
Archives retain oversized originals, historical blobs and identity/source facts.
Verification may accept an explicitly marked `document_too_large` projection with
an old or empty revision; the archive manifest still verifies the actual file
bytes independently. Unmarked content mismatches, identity conflicts and pending
write evidence retain their existing checks. Import does not reinterpret a stale
index as current evidence or truncate a blob to meet the
[document policy](implemented/wiki-development-summary.md#文档字节支持与资源拒绝).

Use the **same Octopus build** for both commands and retain it with the archive.
The archive fingerprints the installed Python implementation; a different
implementation is refused. The interface supports configured paths under one
home, with an optional home-prefix change on import. It rejects required roots
or symlink targets outside the supported set instead of silently omitting them.
An empty target means the selected Octopus data locations do not exist; unrelated
files elsewhere in the target home are permitted.

Each retained application's stored `app_dir` determines its actual code and
`.data` siblings. Collection preserves their content and the required leaf or
parent symlink entries, then restores the stored access path under the target
home prefix. It does not relocate legacy applications or derive data from the
code alias's final target. Runtime entries and their resolved targets are
excluded. If a runtime exclusion overlaps any application's required code/data
or removes a required alias entry, export reports the application identities
and paths and refuses to publish a partial archive.

Collection, resource exclusion checks and deletion share the application
resource interpretation. Stored paths retain `..` until real symlink traversal;
runtime exclusion includes its own content and access chain while preserving
ancestors still required by code or data. Deletion's policy remains separate:
after original writers retire it removes exclusive contents and owned links
inside the managed root, preserving other applications' access paths.

An unfinished deletion retains its phase and original `keep_files` choice on
transfer and does not restart execution or cleanup at import. If contents have
already been removed but a final unlink failed, their dangling owned chain is
excluded from the archive; remaining durable resources and deletion responsibility
are preserved. Explicit retry on the imported account continues remaining effects.

Locate the active source configuration using [local configuration](local-wsl.md#配置和数据).
Do not infer its database or provider paths from the development checkout, and
do not print secret environment-file values. Resolve pending writes affecting
the selected data with their existing owners before exporting; historical tasks
need not all have succeeded. The archive does not carry unfinished execution to
the target or synchronize changes made after the snapshot.

For a running managed source, supply its administrator account bearer through
a private file. This bearer is distinct from installation setup proof. The
command waits for natural idle, closes admission through the service owner,
stops the unit, and copies stable data under the installation lock. It restores
the source and confirms readiness before compressing the copy.
Only an explicit successful receipt for the original request authorizes stopping
the source; a missing status, another request's marker or partially closed gates
does not. Lost responses are confirmed against that same receipt. See the
[handoff contract](implemented/installation-lifecycle.md#领域事实与封口收据).

```bash
octopus export-account ACCOUNT_ID /private/account.zip \
  --home /home/source --token-file /private/admin-bearer --wait 1800
```

For a standalone source, its process owner must stop the service first. External
editors, sync tools and application writers also need to stop. Paired
`--pause-command` and `--resume-command` options can manage those writers; each
is a program with arguments parsed without a shell. Pause runs before managed
handoff so Wiki can receive final filesystem events. Resume runs after service
restoration, including on copy failure. Pause must return only after its writers
stop, and resume must handle a partial pause. Without hooks, the caller owns this
external-writer boundary.

If a handoff response is lost, only the matching request receipt confirms
ownership. A current `ready` response does not prove that a delayed POST was
rejected. Unconfirmed cancellation reports `outcome is unknown`; inspect the
managed service before retrying. The exporter does not restart another request's
owner.

The archive contains the master key and account secrets. It is created with mode
0600 and needs protected storage and transfer; ZIP compression is not encryption.
Once a complete archive exists, import does not require the source to be online
or remain stopped.

### Import and verify

```bash
octopus import-account /private/account.zip --home /home/target
```

Import validates the inventory, hashes, build, key and empty target paths before
writing. It restores privately, rebinds defined account paths and current Wiki
registration/file identities, then publishes under the installation lock. It
changes known system fields, not arbitrary path strings in prompts, knowledge
or application code, and does not automatically expand authorization roots.

`<db_path>.import.env` contains the resulting configuration with mode 0600. Start
a standalone target with `OCTOPUS_ENV_FILE` pointing at that file, or use the
configuration with the existing managed-service setup procedure. Then verify:

1. Sign in with the original account password; check the expected account and
   unchanged agent settings, private and shared memory, their relative links
   and ordinary files. Arbitrary absolute links inside Markdown are not rebased;
   use the account's project-location note to resolve a changed checkout path.
2. Check Wiki contents, sources, identities/history and normal query/maintenance
   access through the restored account's scoped tools.
3. Reinstall application runtime dependencies; verify retained code and `.data`
   through the application's normal read/write behavior.
4. Reauthorize excluded model CLI subscription credentials. Start a new
   conversation under a restored agent using its expected credential and verify
   access to memory/Wiki. An unrelated default login or plausible answer alone
   does not prove restoration. Old native thread continuity is not required.

Keep the source intact until verification completes. Export/import does not
perform source cleanup or authorize a production migration by itself.

### Interrupted import

An interrupted publication leaves `<db_path>.importing`, which blocks startup
before keys or databases initialize. Preserve its inventory and diagnostics.
Verify and remove only this attempt's published roots and staging, then remove
the guard and retry into empty target data locations; imports are not resumable.

If publication completed but final guard removal/sync failed, the complete data
remains intact. Verify it before deciding to finish activation or clear that
attempt. Do not clear an unexplained guard or delete ownership markers merely to
make startup succeed. Cross-file publication is not one global transaction.

## Complete backup and restore

### Locate the durable state

| Resource | What to locate and preserve |
| --- | --- |
| Runtime database | Effective `Settings.db_path` / `OCTOPUS_DB_PATH` from the active service; the default relative `octopus.db` resolves against that process's working directory. Include all current accounts, messages, inputs, completion evidence, schedules and durable relations |
| Master key and configuration | Effective `OCTOPUS_MASTER_KEY` or `master_key_file`, plus actual configuration sources. The key unwraps account DEKs; passwords do not replace it |
| Account trees | Actual `users_root/<id>/` workspace, memory, attachments, research and application code/`.data`; retain referenced older/global files and unknown ownership until classified |
| Referenced working copies | External projects, fork copies, untracked files and archived-session directories that must remain usable. A session row or transcript cannot regenerate their later edits |
| Provider state | The selected credential's effective Codex home or used Claude configuration, with transcripts/indexes and necessary login state; host defaults may be unrelated |
| Wiki | Every actual vault, registration/configuration, state directory, `index.sqlite3`, history/blobs and pending-effect evidence; roots may lie outside the Octopus directory |
| Service ownership | Active service profile and resource bindings as evidence for restoration; target bindings must be verified or explicitly reconstructed |
| Host network entry | Inventory separately managed tunnel units and private proxy settings outside the account tree. On this WSL installation, preserve or reconstruct `~/.config/systemd/user/octopus-tunnel.service` and `~/.config/octopus-tunnel/proxy.env` (0600); reinstall the verified `~/.local/bin/cloudflared` binary using [local instructions](local-wsl.md#手机公网入口). Live processes and temporary public URLs are not restored |

`index.sqlite3` includes durable identities, source bindings, grants, jobs and
filesystem-event records; its name does not make it disposable. Application
build history does not replace generated files. Archiving retains session data
and a directory reference, not a filesystem snapshot.

Classify dependencies, caches, metrics, transient prompts and diagnostics before
excluding them. Inventory real roots and integrity digests, deduplicate nested
roots, and identify symlinks with their required targets. Do not blindly follow
links outside the chosen set or treat a directory name as proof of disposability.

### Take a consistent copy

For a complete cold backup, stop the source service and relevant external
writers, confirm they exited, then copy the stable inventory. Preserve source
files and verify the resulting database and file inventory.

Never copy only a live SQLite main file. Either preserve a consistent stopped
DB/WAL set or use SQLite's backup API for a self-contained database snapshot.
Do not delete WAL because the main file exists. A database snapshot alone does
not synchronize Wiki, application state, work trees or provider files.

Verify the effective master key can unwrap saved DEKs **without creating a new
key**. `crypto.master_key()` may create one when absent and is not a read-only
backup check. Use the read-only publication verifier for retained current
storage; normal initialization can start writers or projection repair even
though it no longer performs historical upgrades. Keep keys out of logs and Git.

### Resolve target bindings and restore

A changed username, home or filesystem can affect account `extra_roots`, native
provider path indexes, Wiki roots/event inboxes, device/inode identities and
service ownership. Identical path strings do not prove identical filesystem
identity. Renaming Claude project slugs or editing `sessions.working_dir` alone
is not a complete restore; current Wiki registration rejects changing an existing
root. Do not bypass checks by deleting markers or rebuilding the Wiki database.

1. Select compatible code and an empty target. Verify the inventory, required
   roots and master key before creating state. Prepare configuration and explicit
   machine bindings without admitting sessions, observers or schedulers.
2. Preserve unresolved input/effect evidence and let its current owners handle
   recovery. A stopped process does not establish business success. Do not replay
   completed inputs, delegation returns or one-shot schedules.
3. Establish an explicit activation rule for restored automatic producers. Source
   and target schedulers must not run the same restored work concurrently. An
   incomplete restore stays identifiable and closed to business. Preserve this
   installation's manual tunnel policy: no startup enablement or automatic
   restart; only an explicit user request authorizes opening its public entry.
4. Check accounts, permissions, memory, attachments, fork/external files,
   application state and Wiki identities/history. HTTP health or a loaded page
   is only readiness evidence.
5. Where native continuity is required, use an isolated validation installation
   to continue a known session with its expected credential, native identity and
   scoped data access. Reauthentication does not restore missing transcripts;
   expired credentials can independently need reauthorization. This local
   installation validates Codex; used Claude data still needs preservation.

Restoring a compatible complete backup is distinct from rolling an upgraded
installation back over subsequent user changes. Follow the current storage
contract for forward repair and release handoff. Source deletion remains a
separate decision after verified recovery.

## Maintaining the migration interface

Every feature adding persistent types, relationships, roots or formats must
maintain account selection, exclusions and restoration in the same change. The
owning modules and acceptance boundary are in
[the storage development contract](implemented/current-storage-format.md#账户搬迁的开发约定).
Verify new semantics with an isolated round trip, including growth between
exports when collection changes. This supports matching-version archives,
not arbitrary old-package upgrades, hot migration, multi-root mapping or
cross-machine execution recovery.
