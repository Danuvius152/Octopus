# Multi-tenancy: remaining policy and hardening proposals

> **Status:** proposed — Re-scope the remaining account-policy choices against current code before implementing them.

## Baseline and current owners

The original second-pass audit examined `6d6ba0b` on 2026-10-04. Its live counts,
reproductions and numbered findings describe that revision. The former
`in-progress` index contradicted the document's own “nothing built” header and
later changes; this page is not evidence of an ongoing implementation or of
today's exploitable defects.

Current account behavior belongs to [multi-tenancy](../implemented/multi-tenancy.md),
MCP/ownership/retirement to [the cross-module contract](../implemented/architecture-contract.md),
connector binding to [connectors](../implemented/connectors.md),
and credential refresh to [its owner](../implemented/harness-credential-reauth.md).
The global installation-token rotation route has been removed; it must not be
reintroduced as an unfinished audit fix. Agent/credential ownership and refresh
serialization also have current implementations and regressions.

## Remaining choices and conditions for implementation

| Subject | Original proposal or open choice | Required decision/evidence |
| --- | --- | --- |
| Host-default login | Explicit account policy for using an unbound host login | Preserve the authorized local workflow; decide supported users and refusal behavior before changing the resolver |
| Connector disconnect | Revoke the provider grant as well as removing local state | Establish provider semantics, failure behavior and real product need; local deletion and external revocation are distinct effects |
| Connector refresh failures | Separate temporary provider errors from permanent revocation | Reproduce a current classification defect through the real manager; do not copy a 2026-10-04 finding into a new bug |
| OAuth browser binding | Bind a callback to the browser that initiated it | Verify current state/cancellation/claim behavior and required cross-device UX before adding another token or cookie |
| Disabled-account work | Define effects on already-running jobs, native processes and issued scopes | Trace current authorization and original-resource retirement; signing out alone is not evidence that subprocesses stopped |
| Execution limits | Consistent account/global admission for all requested work classes | Identify a current bypass and its actual owner; preserve already-admitted returns and their ordering |
| Account deletion | Define data removal, provider grants and DEK retirement | Requires a product decision and complete data/retirement scope; currently no account-deletion route |
| Hostile-tenant isolation | Containers/users/network policy beyond HTTP ownership | A different deployment trust model; trusted local accounts do not by themselves authorize this expansion |

SSO, self-service recovery, billing, per-account usage views, connector quotas and
durable multi-process OAuth login state remain conditional product proposals.
Google publishing/verification was explicitly excluded from the original audit.
None of these candidates is made mandatory by retaining it here.

## Acceptance when a choice is activated

Freeze the actual code/configuration baseline and select one concrete responsibility.
Verify ownership on both read and use, exercise two-account interactions where
relevant, retain original work identity through cancellation/late results, and
cover rejection without mutating another account's data. Check provider effects
only within explicitly selected credentials and operations. Follow current
[testing policy](../testing.md); the old audit's live-mailbox rehearsal is not a
standing instruction to copy or exercise production tokens.

Implementations update the corresponding current owner and remove the resolved
proposal here. Actual deployment requires its own evidence.

## Historical evidence

The [original audit, findings and proposed mechanisms](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/multi-tenancy-gaps.md)
retain the C/S/H/X identities, original conditions and test recipes. They are
available for a scoped re-evaluation, not current-state instructions.
