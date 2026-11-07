# Triage Labels

Use the five default state labels in local task files as `Status:` values.

| Role in mattpocock/skills | Local label | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Maintainer needs to evaluate the request |
| `needs-info` | `needs-info` | Waiting for missing information |
| `ready-for-agent` | `ready-for-agent` | Fully specified, ready for an agent |
| `ready-for-human` | `ready-for-human` | Requires human work |
| `wontfix` | `wontfix` | Will not be actioned |

Triage categories are `bug` and `enhancement`. Record them separately from
state. Execution states `claimed` and `resolved` follow
[issue-tracker.md](issue-tracker.md); they do not rename these triage labels.
