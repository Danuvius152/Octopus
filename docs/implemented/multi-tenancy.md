# 账户、授权与资料归属

> **Status:** implemented — 账户认证、业务作用域、账户密钥、受管路径与现行隔离边界。

## Authentication and its limits

Current business execution requires an active account. An empty installation
supports first-account creation; later registration uses invitation codes.
The setup proof creates the first administrator, account DEK and owned protected
Agent as one committed aggregate. A lost setup response goes through normal login,
not repeated account/key creation. Missing proof or retained pre-account data
keeps business closed; startup preserves the original material.
The old pre-account business mode, installation adoption, global access-token
rotation and its recovery route have been retired. Current account transfer is
the explicit [offline operation](../backup-and-migrate.md#account-export-and-import).

Registration checks the invitation before validating the username. The SQLite
write that consumes a use also requires an unrevoked invitation with a remaining
use and checks its expiry against the current time as the statement executes.
The precheck and the write use the same deadline interpretation. A refused
claim leaves usage untouched and ends registration before account creation.
The invitation use commits before account creation, so a later registration
failure leaves that use spent. [SQLite registration tests](../../tests/test_users.py)

The browser's Login, Setup and Register actions share one synchronous POST
admission before the first await. Enter and button activation cannot issue a
second request while it is held. Native IME composition, including key code 229,
returns before submission or consuming Enter. Mode changes, account changes and
unmounting permanently retire the old UI operation: its response, body, error or
cleanup cannot sign in a later screen or unlock a successor. Current failures
remain retryable. The setup proof stays local to that request and never becomes
the account bearer in the shared store.

`CurrentUser` and `ScopeUser` receive their account service through the same
FastAPI `UserMgr` dependency (`Depends(get_user_manager)`). Overriding that one
dependency controls account-token resolution in both paths without setting the
global manager. Both retain real token resolution from the header, query or App
cookie. `CurrentUser` requires an active account bearer and returns the account;
`ScopeUser` returns its ID and also accepts an authorized signed MCP scope.
Malformed/revoked tokens and disabled accounts remain refused.

Account authentication selects the bearer once, in header → query → App-cookie
order, and binds the resolved account to that bearer's digest. A selected invalid
bearer fails authentication without trying a lower-priority source. `CurrentUser`
projects the account from this same result; logout and self-service password
change consume its credential digest instead of re-reading the header. Logout
returns 204 only after revocation commits, leaving other device tokens valid.
Password change preserves this actual current credential while revoking the
account's other tokens. App and MCP capabilities cannot authenticate these
account-only operations. Server acceptance of an explicitly supplied App cookie
does not change its browser Path or make a browser send an `/apps` cookie to `/api`.

This request-dependency override does not replace `verify_token` /
`resolve_principal` or runtime-owner authorization: those retain the account
service bound at startup. A runtime scope still needs its signature, live
work/run lease and available owner; account-only `CurrentUser` rejects it.

Passwords authenticate; opaque server-side tokens authorize API/WebSocket access.
Session/PAT tokens are stored by digest with revocation and applicable expiry.
Changing a password commits the new hash and revocation of other bearers in one
short database transaction, preserving the existing current-token exception.
UserManager owns password policy and hashing; the database owns this complete
operation. A self-service change carries the exact password hash verified at
entry. Its transaction updates only an enabled account whose current hash still
matches that proof; a lost race returns HTTP 409 before revoking any tokens.
Two requests using the same old password cannot overwrite each other. Policy
validation and password hashing stay outside the transaction. The separately
authorized administrative reset retains its own entry and needs no old-password
proof.

Revoking the admitted request's bearer alone does not withdraw its password
proof. The current-token exception never clears an existing revocation. An
account still disabled at commit is refused; disable followed by restore permits
the admitted change if the password remains unchanged. There is no historical
disable epoch or requirement to revalidate the bearer at commit.

Disabling commits account unavailability and revocation of all existing session
and PAT tokens together. Restore changes availability only: it cannot revive
old bearers, and there is no delayed revocation tail that could revoke tokens
legitimately issued after restore. Password change and disable each expose either
the complete old state or complete new state after cancellation or a failed SQL
operation. Cancellation alone does not prove rollback, particularly during
COMMIT; durable state is checked by reopening the database. No recovery queue
finishes half an account operation.

The login route keeps an in-process limit of eight combined in-flight
attempts and failures from the preceding 300 seconds for each normalized
username and request address. It claims a slot before validation and returns
429 when the limit is reached. A failed validation records a failure at
settlement. A successful login clears settled failures, and cancellation
releases its slot. Other in-flight attempts keep their own slots. Opaque tokens
can be checked for revocation against the open SQLite authority.

UserManager runs password derivation and known/dummy verification through one
bounded worker entry, including setup, registration, user creation and both
password-change entries. Four admission slots bound concurrent scrypt work;
waiting for a slot is cancellable. An admitted computation retains its slot
until the actual worker finishes, even if its waiter is cancelled. A separate
worker limiter of equal capacity prevents an occupied default AnyIO pool from
adding another protected queue. Cancellation is checked again before using a
computed result in SQL. The worker accounts for password computation
independently of the route's login budget and holds no cached credential or
authorization proof.
`tests/test_users.py` covers default-pool saturation, waiting/running cancellation,
capacity release and password/enabled-state races with real derivation.

`UserManager.login` verifies the password outside the write transaction, then
the database inserts the session token only if the account still exists, is
enabled and has the exact password hash observed by that verification. A password
change, disable or deletion that wins this race produces an authentication
failure without a bearer; throttling clears only after successful issuance.
Setup, registration and PAT issuance retain their separate business entries,
but their token INSERT also requires a currently enabled account. A disable
between account creation and issuance returns HTTP 403 without a bearer; the
already committed account and invitation use remain committed.
This checks current validity, without adding a historical disable/re-enable epoch.
The route-to-SQLite barriers in `tests/test_auth_routes.py` cover these races;
`tests/test_users.py` retains revocation and current-device exceptions.

HTTP ownership, MCP invocation authority and a running subprocess have distinct
lifetimes. Revoking login is not proof that an already-running child has retired.
The [input contract](input-reliability.md) owns admission and
[Harness](harness-layer.md#execution-and-resources) owns resource retirement; the [remaining tenancy proposal](../plans/multi-tenancy-gaps.md) must
be checked against code before treating any old audit finding as still present.

<a id="websocket-delivery"></a>
## WebSocket delivery

`SocketDelivery` separates current connection identity from each frame's
readability. Its final awaited database read checks the token/account and
session owner in one committed snapshot, avoiding an owner read surviving a
later deletion. Only an invalid account or bearer closes with 4001. Missing or
deleted sessions drop content; foreign ownership drops with an error diagnostic.
Archived sessions remain readable by their owner. The decision-to-send interval
adds no other awaited lookup. This is a check at that snapshot, not a promise
to retract a frame already handed to the transport.

Every drop releases both frame and byte budgets. Closing owns the budget reset,
so cancellation cannot double-decrement. The minimal `session_deleted` event
uses the original deletion lifecycle's trusted captured audience, rather than
looking up the deleted row, while still checking the account at send time.
`test_ws_delivery.py` covers mounted slow sockets, deletion/archive during the
last identity read, foreign/unknown frames, revocation and budget reuse.

## Keys and secrets

The server-held master key wraps one random data-encryption key per account.
Account DEKs encrypt credentials and connector tokens. Passwords do not encrypt
those secrets: a scheduled request must be able to use its account's credentials
while the person is offline, and a password change must not rewrite ciphertexts.
The server/operator can decrypt secrets it must use; this is not a promise of
cryptographic isolation from the host administrator.

`credential_secrets` is the unique stored secret authority. App tokens and MCP
signatures use their scoped master-key mechanisms; neither is the retired global
API access token. User deletion/crypto-shred is not an implemented product route.
Before comparison, both scoped verifiers require the signature's exact producer
encoding: 64 lowercase ASCII hex characters. Empty, malformed, non-ASCII and
noncanonical signatures fail authentication without normalization or a digest
comparison exception. MCP retains its existing 4/5/7-part identities and checks
current session/account/run/work authority separately. A failed App-scoped check
still permits the existing legitimate account-token fallback; it grants no
cross-App access. Internal key/configuration failures propagate as service errors.
The identity/App helper tables and real mounted MCP/App header regressions cover
these boundaries, including legal account fallback and master-key failure.
Secret rotation, current storage and release preparation follow
[storage handoff](current-storage-format.md), not old migration SQL.

<a id="5-business-ownership"></a>
## Business ownership

Agents, sessions, schedules, installations and applications carry account identity.
Routes resolve account scope, and referenced ownership/current authority belong
at each operation's actual effect boundary. A session owner is derived
from its agent. Connector enablement belongs to the agent; cross-account
delegation is refused. Broadcasts use the appropriate account/session audience.
Unscoped internal access is not permission for account-facing routes to read all
rows. Shared registries and provider definitions are installation-level by design;
users' instances and secrets are not.

Session lookup uses the live registry first. Archived ownership and message-window
fallbacks query the existing `get_session_binding(session_id)` and explicitly
compare the account; they do not enumerate every stored session. Missing and
foreign sessions both return 404. Internal calls with `user_id=None` retain their
existing unscoped meaning. Paging still preserves `before_seq=0`, empty history
and duplicate-sequence integrity reporting. Query-spy regressions in
`tests/test_message_window.py` cover the real manager/HTTP paths, including a
live hit that performs no binding query.

Agent DELETE requires an explicit account scope in `AgentManager.delete_agent`.
Absent scope, a foreign target and a missing target all fail before row deletion,
memory cleanup or notification. The manager returns the authorized original row
for the route's account-scoped event; no post-deletion assertion establishes
authority. Protected system agents and agents with sessions remain undeletable.
The existing memory-mutation owner keeps database and directory work ordered;
this is not a database/filesystem atomic transaction. `tests/test_tenant_isolation.py`
checks both the real HTTP and business entries, retained memory and event audience;
Agent API and memory regressions cover legitimate deletion and existing guards.

Notifier PATCH checks target ownership inside one short database transaction
using the admitted account scope. A nonempty update captures its own returned row under the same
account-scoped UPDATE and returns it only after COMMIT succeeds. A concurrent
later PATCH or DELETE does not replace that receipt with another row or a
post-commit existence assertion. Missing and foreign targets both return 404.
Empty or all-null updates perform only the scoped existence read.
Internal calls with `user_id=None` retain their
existing unscoped meaning.

POST, PATCH and DELETE acknowledge their own committed operations. For each
event, the notifier manager reads the current enabled targets for that
account using the shared SQLite transaction boundary. A committed change
before the selection controls that event. A selection already made may
finish delivery while a later write commits. Parallel sends occur after the
read transaction. An ordinary read failure skips that event and logs the
failure. Loss of storage authority at the read or COMMIT retains the database
fail-stop behavior.

`tests/test_notifier_routes.py` covers real HTTP/SQLite receipts, concurrent
selection and account scope. `tests/test_notifier_storage_failure.py` checks
COMMIT and selection failures in isolated processes.

Known limits remain explicit.
`schedules_changed` is a global empty invalidation notification; account-scoped
refetch remains the data boundary, and the event contains no schedule rows.

Host-login fallback applies only when there is no effective explicit credential
binding. A deleted, foreign, backend-incompatible or otherwise invalid explicit
binding is refused; it does not silently select the host's login. Captured
settings still undergo current authorization at spawn/write boundaries.

Per-account turn limits and other admission limits are enforced by the owning
execution paths. Host-default credential behavior must be read from the current
resolver. Additional account policies belong to
[remaining scope](../plans/multi-tenancy-gaps.md).

<a id="6-files-and-workspace"></a>
## Files and workspace

`workspace.paths_for(user_id)` gives the account's workspace, agents, applications,
attachments, large prompts and research roots. A new session without an explicit
directory uses the account workspace. Relative paths resolve there; absolute
paths are validated against the account root and granted extra roots after
symlink resolution. Account application directories and new full-copy fork
directories are siblings of `workspace/`. Archived successors use the same
ordinary directory authorization. See [full-copy fork behavior](session-fork.md#2-behavior).

### Continuous directory authorization

Extra-root replacement remains a full-list PUT. A browser-runtime editing flow
owns its confirmed baseline, input, synchronous write admission and unresolved
sequence; the account form only observes it. Successful replacement adopts the
returned normalized roots before the next edit. Definite rejection preserves
the input. A lost, malformed or uncertain reply keeps new writes closed and
offers confirmation rather than replaying the old list or accepting a plain GET
as proof that a late PUT cannot overwrite the next edit. Closing/reopening or
account A→B→A retires UI observation but retains the original unresolved flow.

`POST /api/auth/users/{id}/workspace-edits` registers the flow, bound to the
operator and target account. PUT `/{id}/extra-roots` additionally carries its
service generation and monotonic sequence. The per-flow gate covers current
bearer, admin and target revalidation in the same DB write transaction as the
replacement, and remains held until the original driver acknowledges commit or
rollback, including cancellation. The sequence is then sealed before release.
Transactions, rather than a flow lock alone, exclude concurrent account disable
or permission changes from bypassing that authorization check.

`POST /api/auth/users/{id}/workspace-edits/confirm` takes the same gate, waits for
an arrived write to finish or seals an unarrived sequence, and returns the
authoritative baseline. A late PUT at or below the sealed high-water mark cannot
write; a late response cannot unlock a later sequence. Failed confirmation stays
unknown and only retries confirmation. Flow identity, lock and high-water mark
remain in the service lifetime, without a second authorization table or TTL.
After service replacement, only the managed installation's actual previous-writer
retirement proof permits confirmation under the new generation; standalone
without that proof remains unknown. A different UUID is not retirement proof.

These guarantees cover continuous editing in one browser runtime, not competing
flows in other tabs or clients. ID generation uses the existing secure-random
fallback; failure is a directory-editor error, never an account-page render
exception. The persisted roots, account export and path validation remain the
authorities for actual directory access.

### Viewer file access

The viewer accepts the original session's authorized working directory, the
active account's workspace and its current extra roots. Account login credentials
can additionally select the account's registered application data directories.
Application records must be available for reads, and their derived data roots
must still resolve inside the account's managed applications directory. Agent and
application runtime credentials keep their own grants. Directory listing, search,
metadata and byte reads apply the same source admission. Application deletion or
revocation removes that application's grant from later admissions. An independent
grant or another readable application at the same root can still authorize it.
If a parent remains authorized, a fresh absolute-path request can select that
parent. A request bound to the removed source still requires that source.

An implicit relative command starts at the session's authorized working directory
and can select a current grant for the resolved destination. An absolute path
selects a source from the supplied path before canonical resolution, preferring
the working directory when it contains that path and otherwise the most specific
grant. Its resolved target must stay within that source. A single leading
backslash in a direct command can denote an absolute host path. Quoted paths
preserve literal characters. An explicit source confines resolution to that root,
and a relative document reference uses its checked parent within that source.
A working directory outside the account root requires a current extra-root grant.
A path through an unavailable root replaced by an alias is rejected before an
overlapping grant can be selected.

The server rechecks the original session binding, account and source when
listing, reading metadata or serving bytes. Directory browsing lists the
selected directory. Filename search bounds the scan and results within the
source. The metadata binds the selected source, canonical file path and parent
directory identity. The picker can reuse the recorded parent identity to
list files beside an open document. A replaced directory invalidates that
binding. Search scans up to 5,000 entries, returns up to 100 results and has a
roughly 200 ms scan budget. Budget exhaustion marks the results as possibly partial.
Markdown body and relative resources carry the applicable binding back to the
server. File responses revalidate path, type and size and snapshot bytes from
the same checked, opened file. A target binding mismatch or a read shorter than
the checked size returns `409 target_changed`. Ordinary content edits can be read
with the same binding. A replaced source document parent returns
`409 source_directory_changed`. Revocation
applies to later admissions, while an admitted bounded read can finish. Metadata,
preview and original-file reads accept up to 2 MiB, including `.env` as text under
the same authorization. File and metadata responses use
`Cache-Control: no-store`. The [viewer interaction](frontend-interaction-state.md#文件查看意图)
owns rendering, navigation and recovery.

Context search reads at most the latest twelve saved messages from the live
session and asks its configured model for one candidate or clarification.
At process admission it rechecks the session and agent, account authorization,
selected credential, and the identity of the original working directory. The
process uses the checked directory handle. A returned candidate goes through
the same file admission check. Candidates that use the original working
directory must still match its identity. The viewer can offer another source
if that directory has become unavailable. A `/showme` file result returns `metadata`
and `target`, with `description` when found by description. A clarification returns
`message` and `description`. Context search adds no chat messages.

### Attachment storage limits

Attachment storage owns filename sanitization and the final UTF-8 byte budget.
The on-disk basename is at most 214 bytes, including the 12-character attachment
ID and `__` separator. Truncation preserves whole characters and extensions of
at most 16 characters; longer extensions follow the ordinary name budget. This
keeps the previous ASCII limit and reserves space for fork-preservation temporary
names. Invalid UTF-8 names produce an attachment error. Existing account paths,
attachment identity and download authorization retain their original owners.

Attachment POST authenticates the account and admits the session before parsing
multipart data. Only this route has a 26 MiB total body budget, including part
headers and boundaries; it does not limit application proxies or MCP streams.
An oversized declared length can be rejected early. All received ASGI chunks
are counted even when Content-Length is absent or inaccurate; the chunk that
crosses the budget is refused and no subsequent chunk is requested. This does
not retract bytes already buffered by the ASGI server or impose an account quota.

The parser accepts one file part named `file` and no ordinary fields. The route
reads at most `MAX_FILE_BYTES + 1` bytes, independently of upload size metadata,
and rejects content over 25 MiB. The extra byte distinguishes an exact-limit
file without reading its remainder; storage still rejects empty files.
The parser owns partial temporary files through failure, cancellation and
truncated input. Once parsing succeeds, the route owns the complete FormData
through its final close. Started disk write/rollover, seek and read operations
are joined before handles close; repeated cancellation cannot interrupt that
cleanup, while waiting for the next body chunk remains cancellable. A failed
destination write removes only that attempt's unpublished file and propagates
cleanup failure. Receive-stream and real spool regressions cover these windows,
exact limits, malformed parts and a subsequent successful upload.

This validates session directory selection. The local deployment runs trusted
agents and applications under the host user; it is not an OS sandbox against
hostile tenants or root. Per-tenant execution isolation would require a separate
design. Application code, durable data and rebuildable dependencies have distinct
[directory ownership](application-backends.md#three-directories-three-owners).

## 附件与长输入归属

新附件和长输入使用账户下 `attachments/<session_id>/` 与
`large-prompts/<session_id>/`，上传、下载、发送、fork 引用/复制、准备、重试和
清理使用同一 owner，不逐请求改全局 Settings。存量文件按真实 owner 及引用交接，
涵盖归档会话、fork 后代和原生 transcript 的绝对路径；归属不明保留。旧 writer
退休且内容与引用核验完成后才清旧位置，删除会话不证明文件无人使用。正常写入
遵守安装资源声明，不能补 marker、删有效 marker 或关闭检查绕过归属。

普通 Codex 大输入沿业务 RPC inline；Research exec leaf 保留自己的 argv 字节
预算与长输入路径，one-shot 使用无工具 worker。各传输核验真实输入边界，不按
backend 名猜 argv 限制。诊断保留可读上下文，仅掩码秘密；显示裁剪、原文可取、
实际历史重读和真正丢弃字节分别判断。凭据 home 的发布和清理归
[凭据合同](harness-credential-reauth.md#codex-device-operation-ownership)。

## MCP 授权与 transport 清理

正式会话使用 HTTP scope；无 session 的 stdio 装配保留参数核验与诊断用途，
不作为 HTTP 失败后的自动接管，也不豁免当前账户和工具授权。

正确签名的退休 scope 仅可 DELETE 它初始化的确切 transport；完整 scope 含账户、
会话、installation、work 和 run lease。新 lease 不继承旧 transport 清理权，
退休 POST 仍拒绝。核验实际 SDK transport 释放，不能只看 HTTP 状态；401 需
区分 method、route 和 runtime PID，迟到清理拒绝不改写已完成结果。工具启动
失败可能没有 is_error 回包，缺错误行不证明可用。

canonical 授权根固定比较，访问路径仍解析 symlink；后来的链接不能扩大原授权。
内部无 owner 的后台查询不等于合法用户身份，按真实调用边界核验。

## Verification and remaining scope

Code: [users](../../server/users.py), [keys](../../server/crypto.py),
[request scopes](../../server/deps.py), [paths](../../server/workspace.py),
[MCP identity](../../server/mcp_identity.py) and [database](../../server/db/).
`tests/test_route_scoping.py`, `tests/test_tenant_isolation.py` and
`tests/test_agent_credential_bindings.py` provide regression entry points.
Password regressions in `tests/test_users.py` reopen real SQLite after cancellation
or failure at the password, revocation and commit boundaries; `tests/test_auth_routes.py`
checks real concurrent HTTP password changes, issuance races and the current-token
exception. `tests/test_account_operations.py` uses driver/lock barriers and real
SQLite trigger failures for atomic disable, proof invalidation, cancellation and
restore/issuance ordering. Concurrent changes under the same proof require
one success and one conflict; the regression checks subsequent
login and bearer validity. `tests/test_attachments.py` covers upload and
download of ASCII, Chinese and emoji filenames at the final byte boundary.

SSO, self-service password recovery, billing and hostile-tenant sandboxing are
not implied by account support. The current storage model validates a complete
baseline; it does not replay the old first-account adoption/backfill workflow.
