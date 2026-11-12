# 当前存储格式与发布交接

> **Status:** implemented — 现行完整格式、发布期存量核验与有限转换；验证和生产部署分别取证。

The main database uses format **9**, and Wiki uses format **17**. Each database
has one `PRAGMA user_version` marker. An existing database must match exactly
before a writer opens; a genuinely empty database creates the complete current
layout and marker in one transaction. Startup does not interpret older layouts,
select upgrade routes, scan retired requests or backfill historical business data.

The declarations in `server/db/schema.py` and `server/wiki/schema_ddl.py` are the
current authorities. `server/storage_verification.py` verifies complete retained
layouts and current facts at publication. The marker is an admission check,
not evidence of content integrity. Current transaction failure handling, account
ownership, input recovery and Wiki filesystem/job recovery continue to apply.
Search, catalog, material hashes and maintenance projections remain rebuildable
under their existing owners; durable jobs, receipts, sources and event ledgers
are never recreated as caches.

Empty Wiki creation prepares jobs before their dependent grants, selection and
event guards, then installs maintenance projections once. All steps share the
creation transaction; the Registry publishes only the completed ledger. There
is no separate no-jobs layout or later job attachment. This assembly change
uses the complete current declarations and protocol set.

## 事务与失信边界

主库使用一个写连接和明确的短事务。`Database.transaction()` 统一连接锁、驱动与
游标结束、总期限及 COMMIT/ROLLBACK；SQL helper 接收事务，不重入公共事务或
跨调用留下未提交 SQL。接纳、claim、终态结算各自完整提交后才发布成功或外部动作。
取消等待者与取消已接管命令不同；取消不能丢失仍在运行的驱动操作。

主库使用 FULL，Wiki 权威连接使用 EXTRA。正常 COMMIT 返回确认提交；存储或
提交结束失信进入统一 fail-stop，不在同代独立回读猜结果、补写或重放业务。
`RETURNING` 只返回事务内的 SQL 结果，提交前不能当作对外成功证据。
接受一个权威存储故障中断本代全部会话的代价，以减少失信后多处在线恢复。
业务冲突、正常取消、认证失败和通知断线仍按各自语义处理，不泛化为 fatal。

## 格式演进与资料保全

演进以实际可用的完整资料为基线，保全账户、知识、文件、密钥、归属和仍有效的
状态/效果；退出旧 API 或不搬聊天不授予删除源资料的权利。旧格式任务的限定
清理同时要求已结束、原责任已收束；终态标签和日期不足以证明，不删除仍需的
receipt、正文、来源、当前任务或未决效果。实际清理另受当前操作授权约束。

长期维护一种当前表示。只在结构或语义改变时推进 marker；纯代码优化不改。
跨库与文件不是全局事务，部分完成或提交未知保持业务关闭，按持久事实前向恢复；
不用旧库覆盖新文件，不创建第二份正式知识库。有限转换及专属夹具在交接、验收
与恢复责任完成后退出，不承诺任意旧库或旧包可以由最新代码直接打开。

`credential_secrets` 唯一保存秘密正文，`recovery_of` 唯一保存恢复后继关系。
删除物理列或接口要同步真实生产者、消费者与测试，不按同名字段全局删除，
不把旧 rejected/unknown 改成可执行或已成功事实。

## GitHub identity and finite publication handoff

Format 9 requires every GitHub installation's `external_account_id` to be the
provider's positive numeric ID in canonical ASCII decimal text. Login is a
display label. The current table CHECK and complete verifier reject NULL,
`login:id` and other noncanonical keys; other connector kinds retain their
existing identity rules. Per-account/kind uniqueness is unchanged, including
the existing shared uniqueness scope for NULL and empty account IDs. This
changes no collection root or Wiki format. Online behavior belongs to the
[Connector contract](connectors.md#github-identity).

This installation's format-8-to-9 handoff is complete. Its finite helper and
exclusive source fixtures have retired; the private publisher now verifies only
the complete current format. Application resource-family checks remain in
read-only preflight, missing-history recovery and offline pre-cutover verification,
including archived applications and unscoped rows. Conflicts do not authorize
moving resources. The following records the completed handoff's safety rules,
not an entry point for admitting old formats.

Source admission checked a frozen complete catalog and two explicitly known
full physical layouts, including every table's `table_xinfo`, integrity, foreign
keys and retained typed facts. A normalized declaration alone did not prove
column order, and per-table mixtures of the known layouts remained unrecognized.
Read-only preflight and the write transaction shared that admission check.

Before quiescing the old service, read-only inspection enumerated all GitHub
installations, including disabled accounts and NULL owners. Only recognized
`login:positive-ID` keys can map to current IDs. Unrecognized keys, unaddressable
installation identities or owners, and duplicate target IDs within the existing
account scope block publication with the affected installation/account/key
details. Different accounts may retain the same provider ID. The helper neither
chooses a token or owner nor merges or deletes conflicting installations.
It performs no conversion during preflight or missing-history recovery.

The publisher converted only after the original writer retired, starts were
masked and offline installation ownership was held. Inside `BEGIN IMMEDIATE`
it repeated source/fact/resource checks, created the new installation table,
copied by explicit column names while applying the ID mapping, dropped the old
table, renamed the new one and recreated indexes. It never renamed the old parent
first. FK enforcement was disabled only on that private offline connection before
the transaction to prevent cascading removal during reconstruction. Independent
column-by-column installation, secret-ciphertext and Agent-link comparisons,
foreign-key checks and the complete current verifier were required before COMMIT.
Only intended GitHub keys could change; metadata, constraints and marker 9
committed together. IDs, labels, created times, enablement, other kinds and grants
remained.

Confirmed rollback retained format 8. Unconfirmed COMMIT kept business closed;
a fresh attempt under the same plan inspected actual committed facts. Complete
format 9 was verified without replaying the mapping or resetting later writes;
only complete known format 8 could convert within the original offline boundary.
Strict current main/Wiki verification preceded switching and starting the
candidate and ran again afterward. Startup, ordinary reads, Connect and
account import provide no old-key conversion. The finite helper and exclusive
fixtures have retired after publication, acceptance and forward-recovery
responsibilities closed. There is no permanent old-format route. Operator steps
belong to [local publication](../local-wsl.md#自动发布).

## Complete record projections

App Server command `tool_input.output_capture` uses existing message JSON.
Completed content stores the actual retained output, with command retention
semantics owned by [Codex](codex-backend.md#saved-command-output). This adds no DDL,
format marker, startup conversion, backfill, body store or storage root. Complete
reads, both message exports, message copies and consistent full backups preserve
the saved JSON and provenance. Old records without capture remain unchanged;
missing metadata establishes no full-log guarantee, and historical body copies
are not removed. Account archives continue to exclude chats under the existing
[portability scope](../backup-and-migrate.md#account-export-and-import).

Format 8 adds nullable `sessions.context_state` JSON and
`agents.context_settings_revision INTEGER NOT NULL DEFAULT 0`. The typed
ContextState verifier checks complete retained facts; Agent revisions must be
nonnegative integers. Session positional reads and Agent record projections
include their appended columns. No context history, native transcript or
additional storage root is created. Observation and binding semantics belong to
[the context contract](model-reasoning.md#上下文样本与配置绑定).

Typed compact payloads/native evidence and maintenance outcomes use existing
client-submission/message JSON, adding no DDL, format marker or storage root.
Inspection validates operation/evidence kinds and independent ACK/root facts;
withdrawn compact with possible or confirmed execution fails verification.
Legacy message digests and flat decoding stay unchanged. Restore grants no
execution authority; see [input reliability](input-reliability.md#native-compaction-requests).
Claude's additive `compact.native_command` retains the original input UUID and
raw local-command run/outcome JSON alongside its native reason. Full backup and
history reads preserve those facts, including failed/interrupted + applied;
account transfer still excludes them with chats. No new format version or
conversion is required for this JSON addition.

The completed format-7→8 publication handoff added both fields and marker 8 in
one transaction, preserving original facts and initializing only NULL and 0.
It fabricated no historical context sample. That finite converter and its
exclusive fixtures have retired; they are not a route for opening format 7
with current code or a step in a permanent migration chain.

Complete-record SELECTs for agents, applications, background tasks, research
jobs, connector installations, custom connectors and credentials use explicit
`RecordProjection` declarations in `server/db/base.py`. Each declaration supplies
both SQL expressions and output field names; readers require the exact declared
length before applying their existing domain conversions. Agent active-session
counts are an explicit derived field, and credential JOIN expressions name the
metadata owner and separate secret source. Reordering a declaration does not
require editing a second positional map. These internal records do not widen
public response schemas. Partial/aggregate queries retain their own projections;
there is no global row factory or storage format change.

## Publication boundary

`ForkRevertRecord.stash_ref` keeps its existing nullable string representation.
New recovery snapshots store a verified full commit OID; legacy `stash@{n}`
selectors remain unchanged and must not be reinterpreted as stable identities.
The OID is stable across later stash pushes, but does not guarantee retention
after object deletion or garbage collection. There is no schema conversion,
new backup root or historical backfill. Existing session JSON, API projection
and complete backup retain the original value. Creation, verification and
uncertain effects belong to [safe rewind](session-rewind.md#563-the-one-revert-we-offer-file-edits-via-git).

Question resolution uses the existing nullable message `tool_input` JSON:
`question_resolution.version=1` carries original form/execution association,
disposition, structured answers and trusted provenance. This additive message
fact changes no DDL, format marker or storage root and needs no startup
conversion. Legacy messages without it retain unknown structure/source; no text
parsing or provenance backfill is performed. Text and `question_closed.reason`
remain compatible projections. Older consumers can ignore the nested facts;
retaining original message JSON retains them, while stripped/legacy text cannot
reconstruct them. Live question callbacks and reply authority are not
persisted or restored. Backup and transfer scope remains with
[account portability](../backup-and-migrate.md#account-export-and-import).

The same resolution may include the submitting `client_command_id`; missing
legacy correlation stays unknown. Human native optional snooze uses an ordinary
persisted `question_snoozed` event and its seq, with the pending-question JSON
projection retaining the cleared deadline and snoozed flag. Bounded original
attempt input-cleanup facts can appear in outcome diagnostics; they do not
restore a callable MCP request or prove provider consumption. Page question
drafts remain volatile. These additions use existing message/outcome JSON, add
no import object, storage root, DDL or marker, and require no historical backfill.

Original input/work `KnowledgeContext` JSON also carries nullable
`interaction_policy` (`human` or `unattended`), inherited through existing child
and return bindings. Missing legacy fields remain `None`; there is no origin-
based backfill. Existing question-request `tool_input` retains policy, wait mode,
answer target and, for MCP ask, authenticated original scope plus actual SDK
RPC/session/server/tool and supplied metadata. The metadata is association
evidence, not invocation authority, and missing native/call identities remain
unknown. `needs_input` uses the existing question-closure reason and resolution;
Wiki reads that original request's saved closure without a new domain status.

These additions change no tables, format markers or collection roots. Complete
JSON preservation retains them; text-only projections cannot reconstruct them.
The original MCP call record, accepted command Future, runner, waiter-release
evidence and parent receipt callbacks remain process-local and cannot be restored
as permission or pending execution. Existing input, message, task and schedule
backup/exclusion rules apply without a new import object or startup replay.

Ordinary App Server adds typed native source to the existing nullable
`messages.runtime_metadata` JSON: original request/attempt plus native
thread/turn/item, root thread/turn, known parent and phase. `tool_call_id` is
independent optional tool provenance; text does not fabricate it. Turn facts
may omit item identity. Complete child text/tool messages keep their original
rows; `native_subagent` status messages retain card/work association in existing
`tool_input` and native source at the message level, without copying body text.
Native question requests retain their immutable original binding in the
existing request message's `tool_input.native_question`; the original resolution
keeps structured answers and provenance. These are additive JSON facts: no DDL,
format-marker increment, startup conversion or additional collection root.

Native item observations use the existing message `tool_input.native_item` JSON
for original status, failure and item-specific fields. Text bodies keep their
existing content field and native source/phase. Complete empty items stay valid.
Generated images retain the native file ID, or an `inlineImage` byte count and
SHA-256 after bounded wire decoding; inline base64 is not stored. `savedPath`
remains the original absolute path. Image-view actions retain a path without a
snapshot; hook fragments retain hook-run source; dynamic-tool rejection remains
an observation rather than a second execution or input record.

These facts are additive inside existing JSON and require no schema change or
backfill. History, fork, transcript export and consistent complete backup retain
the original JSON; account archives still exclude chats and their native facts.
No new media root enters collection or restoration: current file access still
depends on the original file and viewer policy. A saved path does not promise
that file exists after restoration. Existing workspace/attachment backup scope
is unchanged; see
[account portability](../backup-and-migrate.md#account-export-and-import).

Legacy metadata remains readable without inventing native ancestry. History,
fork/message copying and a complete backup preserve the original JSON, seq and
associations; a stripped text projection cannot reconstruct them. Runtime
callbacks, reply eligibility and the bounded card cache are not restored as
execution authority. A reloaded card derives its body/status from collected
messages; the live snapshot is only an additional bounded view. Main-chat
filtering does not delete child rows or change the global high water.

Current background-task rows retain nullable `bg_tasks.delivery_disposition` (`stopped`/`unknown`)
and `delivery_error` after `knowledge_context`. Delegation round JSON requires
an independent nullable `terminal_delivery_error` and validates the finite
terminal-disposition vocabulary, including `stopped` and `unknown`. These are
source-side delivery observations; they do not change command/child business
results, duplicate input receipts or prove parent processing. Empty diagnostics
do not authorize replay. Live question delivery controls stay with the original
round/question and are not a new persistent queue.

The same `sessions.delegation_state` JSON now retains `child_work_ids` and an
optional successful `candidate_request_id`. These are the outstanding parent-work
responsibilities and final-candidate identity that cannot be reconstructed from
the child's current round after follow-up. Original `client_submissions` payload,
execution owner, delivery/native proof and terminal derive consumption; result
bodies and historical rounds are not copied. Parent responsibility and child
round admission share the original CAS transaction; final sealing rechecks those
inputs in its own CAS transaction. Unsealed running snapshots restore as
interrupted, while a committed completed snapshot retains its seal. Legacy
boolean markers are accepted only for reading and never infer completion or get
written again. There is no DDL, format-marker change, startup backfill or new root.
Complete backup retains these session facts; account archives exclude them under
their existing chat/execution-history boundary.

Model `CredentialUse` and Connector `grant_stamp` are derived comparison evidence,
not new persistent revisions. The original credential/installation transaction
compares current encrypted material and ownership before applying rejection.
OAuth reauthorization atomically updates the existing `auth_type` and secret fields.

Codex device login's original committer coordinates its staged `auth.json` and
credential metadata under the short write transaction, with historical success
only after that original COMMIT acknowledgment. Status reads perform no writes.
Target reservations, cleanup tasks, deletion phase/proof and confirmed process-group
exit are process-local ownership facts, not persistent revisions or new storage
roots. Explicit deletion retries use the original confirmed row deletion to
finish local cleanup; they do not repeat a DB write. No publication ledger,
startup recovery, format bump or guarantee across crash/fail-stop is introduced.
The complete failure and compensation rules belong to the
[credential contract](harness-credential-reauth.md#codex-device-operation-ownership).

The current main declaration includes `client_submissions.native_inputs`, a checked JSON array with
default `[]`. Each version-1 entry belongs to the original product input and one
attempt, retaining separate RPC/thread/turn identities, acceptance proof and
controller uncertainty. Additive version-1 fields distinguish start from steer,
retain connection generation and the expected target turn, and identify a steer
response as an acceptance source. Existing entries without those fields remain
readable as start evidence. A rejected or unsent steer returned to ordinary
dispatch retains its old evidence alongside the later attempt; its current
receipt and execution owner determine whether a steer notice still applies.
The declaration and `NativeInputEvidence` own its shape;
the verifier checks typed entries, input association and attempt uniqueness.
Legacy rows remain `[]`: local write/drain or an old confirmed receipt does not
become native acceptance or model consumption. Lifecycle and recovery semantics
are in [the input contract](input-reliability.md#codex-native-input-evidence).
No additional storage root, execution queue or answer ledger is introduced.

Passive Monitor records use the existing separate bounded metrics store and its
JSON detail fields. Native phases, response scope, provider snapshots and Owner
clock projections do not add a main-database column or alter the main format. They do
not restore input or execution authority. Their collection and deduplication
contract belongs to [Monitor](codex-performance-monitor.md).

New App Server outcomes add nullable `final_answer` and `native_status` within
existing result-message JSON. `final_answer` contains the bound root thread/turn
and ordered original message seqs; it stores no copied body. `native_status`
retains completed/failed/interrupted provider evidence independently of the
Owner's business outcome. This adds no column, storage root or format transition.
Missing fields remain legacy records. Result readers validate referenced rows in
the same session/request/attempt and report invalid/missing refs explicitly;
an empty reference list is a legal empty answer. They never repair a bad reference
from another request, child, turn or session. Typed metadata on ordinary text does
not require tool provenance. Timing and consumers belong to the
[execution/result contract](input-reliability.md#原请求结果读取).

Provider failure diagnostics use the existing recovery-message JSON. Its typed
runtime metadata retains original request/attempt/native source, and
`tool_input.provider_error` retains the driver's structured error payload. The
diagnostic remains independent of the selected business status and answer refs.

Fork history copies terminal request keys, original seqs, native metadata and
final refs without remapping historical identities. Existing user-message
`tool_input.copied_input` captures immutable source-session/request, carrier,
delivery state and native input evidence. It is a historical observation, with
no copied submission, process identity or recovery permission. Result reads use
that observation to retain input uncertainty; malformed refs never read a parent
session to reconstruct text.

An initializing full-copy saga commits its row before copying. Its existing
`fork_metadata.owned_copy_dir` JSON field is written only after successful
exclusive directory creation and names that exact destination. The initializing
state, full-copy identity, receipt match and allowed scope jointly permit
directory cleanup; native artifact cleanup and the original writer's exit remain
separate prerequisites. An existing directory without a matching creation
receipt is retained with the recovery row for inspection. Ready sessions are
outside the sweep; normal first-turn metadata clearing need not keep this
initialization receipt. There is no new table, storage root, format transition or
startup backfill. Backup and transfer follow the existing session scope below.

The existing `fork_metadata.native_fork` records a
native creation's unknown/created phase, selected home/strategy, RPC connection
and actual returned ID/path for its original initializing saga. Its
`cleanup_allowed` qualification is closed through ACK and while a management
host remains unconfirmed; only trusted, confirmed retirement can open it.
Starting another management operation first persists it closed. Missing or false
qualification retains the recovery material instead of authorizing deletion.
Ready history does not restore management hosts or execution authority from that metadata.
These facts add no DDL or storage root and require no format transition.

Native `fileChange` result records retain their producer's tool name in the
existing message field. The original JSON body remains the patch payload;
the Harness decoder checks that producer kind and item identity before exposing
paths to rewind. Ordinary tool/model text is not promoted to file-effect evidence.

The nullable, checked `applications.deletion` JSON column records only
unfinished deletion (`retiring`/`files`,
`keep_files`, optional error); successful deletion removes the owning app row.
Existing `sessions.app_id` records new development sessions and their archive
successors; the current app-session reference remains valid for existing rows.
No historical relationship is inferred from working directories. Research
additionally retains an `unknown` admission observation in its existing status
field, without converting it into permission to replay an input.

Account snapshots retain unfinished deletion and its original file choice/error
while excluding sessions, BG and delegation execution history, including the
new return diagnostics. A complete installation backup retains those fields
with the original results and input receipts. Clearing the
exported `session_id` does not reopen the app or restore an in-flight grant.
Cold opening keeps it closed and does not delete files; an explicit retry owns
the remaining phase. Application HTTP, process, cold-open and account-snapshot
regressions cover these facts in `tests/test_applications.py` and
`tests/test_delegation_work.py`.

The existing private publisher under `~/.octopus/ops/publication/tools/` owns
this sequence:

1. Inspect installation resources read-only before consuming the original
   process's single permit. A failed preflight leaves that service running.
2. Confirm natural drain and the original process's handoff permit, mask starts
   and wait for the original process and unit to retire.
3. Acquire offline installation ownership and keep starts masked while checking
   retained data, applying this release's finite conversion if needed, and
   strictly verifying the candidate's current contract. Do not advance the
   release pointer before these checks pass.
4. Switch the prepared release pointer under the offline lock only after all
   resources pass, then release the lock, install/unmask and start the candidate.
   Verify actual readiness, current domain reads/writes and recovery, assets and the
   installation binding through the existing release acceptance.
5. Retire the finite conversion and its exclusive fixtures after acceptance and
   all recovery duties finish. Preserve decision rationale and regression evidence.

Unchanged formats still receive real read-only verification. Conversion, target
constraints and marker share one database transaction. Independent databases and
files do not form a global transaction: partial completion or an uncertain commit
keeps business closed. Reentry uses the original candidate/permit and fresh
inspection of committed facts; it does not trust an older success receipt, restore
old databases or replay model/tool effects. An already running candidate is
verified without another conversion or restart.

### Wiki dispatch generations

Fresh current jobs start at generation zero.

An accepted explicit retry or scope re-admission advances
the generation and subscribes its actual entry in the same writer transaction.
Worker claims, automatic recovery, completed-result retrieval, ordinary reuse
and control replay do not advance it. Prior generations retain their effects
and cards but cannot automatically report a later dispatch.

An account-scoped in-process admission gate orders explicit dispatch transfer
against automatic return acceptance. It is held only through these bounded
admissions, never model execution; no registry lock or database transaction is
held across the asynchronous session submission. Cold restart relies on durable
generations and existing source receipts. Native cancellation joins the captured
control writer before releasing that gate. Aggregation and operation counts only
include current subscriptions; the return identity remains stable for the original
request even when a transferred member leaves the group, preventing a second
report after a lost ACK. Already accepted return inputs remain original facts.
Only an actual structure or semantic change changes a format marker. Developer
intermediate layouts do not create support obligations. A later format-changing
release supplies its own bounded conversion and verification, then retires it;
there is no permanent older-version ladder or promise to open retired backups
with the newest binary. This storage-format convergence was independent of
the historical account-portability work. Current offline export/import is
implemented; its collection and restoration contract is maintained in
[account portability](../backup-and-migrate.md#account-export-and-import).

## Wiki 任务表示与恢复资料

任务 items 使用原 JSON 列的版本1与当前状态/执行阶段；业务 Python/SQL 不解释
历史编码。每批 ExecutionRef 固定原 attempt/session/request，同一 attempt 的多批
不能混结算。收据精确匹配保留原入队序号，job_id 只作来源坐标；普通删除、替换
和语义改写仍撤销失效证明。bound 创建证明、applied/indexed、正文及仍需的来源
和 receipt 独立保全。任务实体缺失时原聊天只保留已提交摘要，不另存请求档案；
旧 attempt 控制冲突为409。

12/13格式准备、旧标志转换和旧编码任务清退属于一次性历史发布范围，见
[固定原文](https://github.com/Danuvius152/Octopus/blob/fd7ab9c9bf312c1366eaa111d887c120cebb5e6f/docs/implemented/wiki-development-summary.md#L197-L201)。
终态或日期不授予清理权限；现行格式准入与转换见本文的发布边界。

## 账户搬迁的开发约定

`server.portability` 提供一个公共离线导出/导入实现，CLI 是薄入口，各领域维护自己
的选择、排除和恢复规则。同版空数据目标、当前 home 布局及可变 home 前缀是支持
边界；用户侧范围和操作步骤只维护在
[账户导出与导入](../backup-and-migrate.md#account-export-and-import)。

每次按真实账户归属、资源注册和目录收集，不能写死今天的应用名、vault 位置或数量。
普通内容增长自动纳入。新增持久类型、字段/关系、根或格式时，同次变更更新
`server/db/portability.py`、`server/portability.py` 或 `server/wiki/portability.py`
的相关规则，明确排除项并验证往返；不预造迁移插件框架。

源端只导出稳定资料：未决写入交原 owner 收束；稳定副本完成后恢复源端，再压缩。
导入先核验，再私有暂存、重绑明确的系统路径和本机身份，持久化后启用。不以单库
一致性冒充跨资源快照，不扫描改写任意正文/第三方代码，不把源执行过程搬到目标。

账户搬迁专项的隔离验收覆盖首次导出、增长后再次导出、源端离线后的独立导入、
原账户登录、agent 配置/memory/Wiki、应用依赖重装后的数据读写，以及中断拒绝。
专项用例、夹具和审查副本按用户要求退役；这是保留的历史验收结论，未来语义变更
仍需当次验证，不免除现行权限、完整性与恢复测试，也不证明生产曾执行搬迁。


## Regression coverage and retired assertions

| Previous assertion | Current coverage |
| --- | --- |
| Main 0–3 upgrades, retired columns/modes/requests and historical message guards | Their success matrices and frozen constructors/SQL are removed. Minimal mismatched-marker refusal remains, including nonempty zero and real WAL. |
| Current account/configuration, credentials, messages, input recovery, pins, schedules, attachments, applications, forks and delegations prepared through old fixtures | The same domain assertions use owned current data; current DEKs, password changes, unknown inputs and successor relationships remain covered. |
| Full current structure/constraint checks during startup | Publication tests retain missing indexes, quoted SQL constraints, extra structures, lost origin/source/grant/selection authority and read-only refusal. |
| Wiki 12/13 preparation, no-jobs layouts, old emitters and task retirement | Old success matrices and frozen DDL are removed. Current grants/material ingestion, creation receipts, blob roots, notifications and metadata recovery use registered current Wiki fixtures. |
| Wiki format history | One current marker; mismatch rejection, current reopening and derived-index recovery. Current tasks are retained. |
| Startup performs release upgrades | Private publication tests verify retirement before conversion, masked failures, fresh reentry, actual SQLite process loss/commit acknowledgment loss, partial completion across two Wiki ledgers and current service behavior after handoff. |

Targeted checks precede the repository commit gates. Gate definitions and live
provider scope remain in [AGENTS](../../AGENTS.md#verification); deployment follows
[the existing local procedure](../local-wsl.md#自动发布). Passing isolated conversion
or startup tests does not establish that production was converted or released.

## Historical evidence

The following handoff examples are frozen implementation/validation evidence
recorded by source revision `fd7ab9c9bf312c1366eaa111d887c120cebb5e6f`.
Statements about an observed installation or unconverted production database
refer to those original checks; they are not a current deployment inspection.
The finite converters and temporary fixtures are owned by that release/recovery
work, not ordinary startup or a permanent migration API.

### Main 6→7 example and retained-fact verification

A format-6 installation requires this release's finite publication conversion
after its original writers retire: append only `native_inputs` as `[]` and commit
marker 7 in the same SQLite transaction. Ordinary startup never performs it.
Read-only preflight checks complete declarations, integrity, foreign keys and
exact `table_xinfo` against the declared baseline and the four verified historical
column orders. The writer transaction repeats those checks, preserves original
column metadata and compares every original table's typed row multiset, including
duplicate counts and NULL/TEXT/BLOB/numeric distinctions. The candidate's complete
verifier runs before COMMIT. Format-7 reentry verifies and retains already committed
native evidence; uncertain COMMIT keeps the publisher closed for fresh inspection
without restoring old data or replaying inputs. This conversion does not admit
arbitrary historical/intermediate layouts or revive deleted conversations.

The 6→7 boundary was checked on an isolated fresh snapshot of the actual retained
installation; all twenty original tables preserved their metadata and typed facts.
Temporary private checks cover populated canonical/historical layouts, malformed
baseline refusal, process loss before COMMIT, lost COMMIT acknowledgment, current
reentry and the real preflight entry before consuming a publication permit.
Current regressions cover native proof validity, terminal/restart preservation,
fresh creation and mismatch refusal. These are implementation/validation facts;
the production database has not been converted by this development task.

### Earlier Wiki 16→17 dispatch-return example

The preceding dispatch-return release started from the observed main 4 / Wiki 16
installation. Main has no physical format change. The finite Wiki handoff adds
`dispatch_generation` to the existing job state and subscription uniqueness,
preserves subscription IDs, snapshots and ACKs, and commits marker 17 with
notification protocol 4. The observed installation has only completed jobs,
acknowledged notices and settled automatic return inputs. The bounded handoff
rechecks these facts, retains historical subscriptions in generation zero and
initializes the completed jobs' current generation to one without a subscriber.
This closes consumed return scopes without relabelling their original identities
or guessing historical dispatchers. A new caller explicitly retrieves the saved
result; unfinished legacy owners block this handoff and retain their actual
recovery responsibility.

The handoff checks the original declarations read-only, compares exact retained
facts before/after, and verifies the current layout in the conversion transaction.
The original source and finite private script remain until actual acceptance;
implementing this code does not itself convert the running installation.

### Earlier main 5→6 evidence


The 2026-10-31 main 5→6 handoff check used a fresh read-only backup of the actual
running installation, including nonempty BG records and a persisted delegation
round. Conversion touched only the isolated copy. All twenty original tables
retained their column metadata and typed row multisets under the declared round
JSON projection; an independent comparison removed only the new null
`terminal_delivery_error` and checked every original JSON field. The copy was
deleted after validation. This supplies retained-data evidence alongside the
finite interruption/commit-ack tests; actual production conversion and release
remain facts of the publication receipt.

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/current-storage-format.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.
