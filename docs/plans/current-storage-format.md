# Current storage and publication handoff

> **Status:** implemented — Validation and release handoff are tracked separately from production deployment.

The main database uses format **4**, and Wiki uses format **17**. Each database
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

## Complete record projections

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

The existing private publisher under `~/.octopus/ops/publication/tools/` owns
this sequence:

1. Inspect installation resources read-only before consuming the original
   process's single permit. A failed preflight leaves that service running.
2. Mask starts, switch the prepared release pointer and wait for the original
   process and unit to retire naturally.
3. Keep starts masked while checking the retained data, applying this release's
   finite conversion if needed, and verifying the candidate's current contract.
4. Install/unmask and start the candidate only after all resources pass. Verify
   actual readiness, current domain reads/writes and recovery, assets and the
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

The dispatch-return release starts from the observed main 4 / Wiki 16
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
recovery responsibility. Fresh current jobs start at generation zero.
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
The handoff checks the original declarations read-only, compares exact retained
facts before/after, and verifies the current layout in the conversion transaction.
The original source and finite private script remain until actual acceptance;
implementing this code does not itself convert the running installation.

Only an actual structure or semantic change changes a format marker. Developer
intermediate layouts do not create support obligations. A later format-changing
release supplies its own bounded conversion and verification, then retires it;
there is no permanent older-version ladder or promise to open retired backups
with the newest binary. This work is independent of account portability tickets
19–22 and does not implement export/import.

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
