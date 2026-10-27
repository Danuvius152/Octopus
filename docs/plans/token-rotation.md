# Rotating the installation access token

> **Status:** retired — Installation-token rotation and recovery are removed; current account authentication remains.
>
> The Settings control, `/api/auth/rotate`, WebSocket handoff and automatic
> recovery have been removed. Account login, password changes, bearer
> revocation, account DEKs and master-key MCP signatures remain.
>
> **Retained evidence:** an old-format database is rejected by the current
> format marker before keys or writers. Full retained-layout checking belongs
> to publication; application startup no longer scans the retired intent table.
> Preserve the original database, WAL and environment files and settle unresolved
> operations with their original owner before a finite publication handoff.
> Payloads are neither interpreted nor deleted by current startup.
>
> The planning decisions and protocol below are a historical record of the
> pre-retirement implementation. They do not describe currently callable APIs.

Current account and retirement rules are in the
[architecture contract](architecture-contract.md#公开接口与账户配置).
The protocol below explains the retired implementation's safeguards; its
original removal sequence is historical evidence, not pending work.

## 1. Secret ownership

`OCTOPUS_AUTH_TOKEN` is both the install-era access credential and the input to
`crypto.py`'s Fernet key derivation. Secret ownership determines what a rotation
must change:

| Storage | Owner and encryption key |
| --- | --- |
| `credential_secrets.secret_encrypted` | Install token for NULL owners; account DEK for owned API keys/OAuth bundles |
| `connector_installation_secrets.secret_encrypted` | Install token for NULL owners; account DEK for owned Connector tokens |
| `connector_oauth_clients.client_secret_encrypted` | Master key; verifiable legacy token ciphertext is migrated to that key |

Editing a token file by hand can separate the next startup's key from stored
ciphertext. Rotation must coordinate the files, database and live token. It
must also prevent a provider refresh from capturing the old key, crossing a
rekey commit and writing an obsolete ciphertext afterward.

## 2. One owned command, explicit commit facts

`POST /api/auth/rotate` is an install-era operation. Its command owns the
installation key lock even if the HTTP waiter disconnects. Credential and
Connector operations using this key share the scope through key capture,
external refresh and ciphertext persistence. Account DEKs and the master key
remain independent. Ordinary transcript writes use short SQL transactions;
no SQL transaction spans provider or filesystem work.

| Point | Durable fact and action |
| --- | --- |
| C0 | Validate the new literal token, startup sources and old ciphertext. A short transaction records one prepared intent encrypted with the durable master key, binding the database path, old/new tokens and canonical env-file paths. |
| F1 | Replace only the token definition in every target file using a private temporary file, file fsync, atomic replacement and directory-chain fsync. Other configuration fields remain untouched. |
| C1 | Re-read the current secret set, rekey NULL-owner credential/Connector ciphertext, preserve master-key client secrets and delete the prepared intent in one short transaction. |
| F2 | Update the live token and confirm retirement of CLI processes carrying the old callback environment before reporting success. |

There is no committed phase or follow-up commit. A domain refusal before C0
changes no token files. A known C1 domain rejection rolls back SQL, reconciles
files to the old token and clears the intent. An unconfirmed storage operation,
filesystem write or CLI retirement stops the process through the shared fatal
boundary; it never reports that uncertain work was rolled back.

Startup reconciles before producers are admitted. A remaining intent proves
C1 did not commit: restore old file values, sync them, then remove the intent.
An absent intent after C1 means the durable new files and new ciphertext agree.
Recovery never repeats secret re-encryption. Unknown versions, database
bindings, token values or changed startup sources retain evidence and prevent
admission instead of overwriting an unproved configuration. Recovery changes
only token definitions, preserving later edits to unrelated fields.

The successful path does not restart the service. Application backends retain
their master-key-derived `OCTOPUS_APP_TOKEN` and in-flight requests.

### Which env files

Managed installations use their generated private `<database>.service.env`.
Rotation verifies the loaded unit's actual EnvironmentFile ordering and entry
command. Later files or an unset token that defeat the generated source are
refused before effects. A systemd EnvironmentFile passes a literal value;
python-dotenv interpolation must not reinterpret an existing managed token.
New tokens must be literal and round-trip through the supported startup rules.

Standalone mode requires the selected `OCTOPUS_ENV_FILE`, or the working-directory
`.env`, to define the token. Without an explicit pointer, a package-root `.env`
can also be updated as a compatibility mirror; it cannot alone prove restart
configuration. An explicit pointer is the whole list. Source selection is shared
with `Settings`. Token names follow its case-insensitive matching: one lowercase
or mixed-case file definition is supported, while duplicate aliases and external
environment overrides are refused before effects. A managed unit must use the
generated canonical name without inherited case aliases. Changing a child process
environment cannot change its parent's next-start override. Recovery verifies that
startup still uses the recorded file sources.

One earlier lookup bug fell back to the working directory and repository root
even when `OCTOPUS_ENV_FILE` named a file. Tests then rewrote the developer's
real `.env`. The explicit pointer must remain the whole list; a configuration
lookup that writes secrets must keep its file scope exact.

## 3. Open tabs

After successful rotation, the route broadcasts `auth_token_rotated` carrying
the new token to clients already authenticated with the old one. Connected
clients store it and carry on. With `revoke_other_clients: true`, the key is
omitted and other clients must sign in again.

Clients offline during rotation return with an expired token. A WebSocket
closed with `4001` clears that token and shows sign-in. HTTP waiter cancellation
does not cancel the owned rotation, and does not prove that its effects failed;
a client that missed the result may need the new token to sign in again.
Cancellation of the command owner itself during C0/C1 is a different boundary:
even a normally acknowledged commit can precede the propagated cancellation.
Such a non-domain exit enters shared fatal while holding the installation key
scope, and startup reconciles the retained commit fact before admitting work.

## 4. Verification boundaries

`tests/test_token_rotation.py` covers refusals, key ownership, concurrent SQL,
a changed secret set, case-insensitive Settings reloads, cancelled waiters and
both broadcast modes.
`tests/test_token_rotation_recovery.py` exits real processes around C0/F1/C1/F2,
then uses public ASGI startup. It includes zero secrets, partial multi-file
replacement, command-owner cancellation after confirmed commits, interrupted
recovery, package mirrors without an effective source, unknown evidence and
unrelated configuration edits. Stored ciphertext is compared to prove recovery
does not re-encrypt it.

`tests/test_installation_keys.py` controls actual provider refresh boundaries
while rotation and bootstrap queue, including a cancelled HTTP waiter.
Disposable user-unit checks in `tests/test_service_supervisor.py` verify the
generated startup, cold restart and loaded drop-ins. Browser/client checks
remain separate release gates; no test rotates a shared test server out from
under unrelated cases.

## 5. Account transition

The first-account command closes legacy execution admission and drains old
runs before taking the installation key lock. Its user, rekey, adoption and
working-root facts share one SQL commit. Once accounts exist, the installation
token is retired and rotation is refused; account password and DEK behavior is
unchanged. Captured NULL-owner secret callers are rejected after handoff,
rather than deriving an account's key from an adopted row. Wiki publication
retains its existing migration journal and recovery evidence.

## 6. Retirement as one operation

Before removing the operation, inspect the selected current data baseline:
owned credential/Connector secrets must use their account DEKs, installation
OAuth client secrets must use the master key, and no unresolved rotation intent
or first-account handoff may remain. A prior report of an empty intent table is
not a substitute for this check. If evidence remains, retain it and settle the
original operation with the version that owns its protocol before cutting over.

Remove the Settings control, `/api/auth/rotate`, request/response contracts,
`auth_token_rotated` client event, `token_rotation.py`, the intent schema,
startup recovery and rotation-only tests together. Remove legacy installation
key serialization only after bootstrap and all NULL-owner secret consumers
have also retired. Current per-installation Connector refresh locking and
account ownership checks continue to protect real operations.

Do not globally delete `settings.auth_token`: classify its remaining setup,
callback and diagnostic consumers first. First-account setup still requires
an installation-level proof; ordinary account business must not accept it.
Current MCP signatures use the master key and calls still need their verified
session/account/run scope. Preserve account login/logout/password changes,
bearer revocation, master-key wrapping and account DEKs. Password changes are
not secret re-encryption, and this retirement is not master-key rotation.

Verification must cover empty-install setup, concurrent first-account attempts,
current-account restarts, credential refresh and scoped MCP calls. Preserve the
current-operation cases in mixed installation-key/bootstrap tests; remove cases
that exist only to prove the cancelled installation-era operation. The actual
removal and code validation remain pending.

## 10. What this defers

The former CLI rotation, offline installation-token rotation and per-device
installation-token extensions are cancelled with the old product requirement;
they are not a backlog. Any future account-key or master-key rotation needs its
own concrete requirement and protocol. Current-version machine moves are owned
by the simplification plan, not by this route.
