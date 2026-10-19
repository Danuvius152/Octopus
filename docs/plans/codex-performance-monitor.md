# Codex performance in Monitor

> **Status:** completed — effective output TPS implemented and validated for local Codex; client windows, model grouping and passive collection.

## 1. Decision

Monitor compares models using ordinary Octopus Codex sessions. It sends no benchmark requests. The user approved **effective output TPS (including reasoning)** after reviewing other tools. This replaces the earlier Q2/Q7 restriction to a proven server generation clock. No server decoder or isolated hidden-reasoning speed is claimed.

## 2. Formula

```text
response effective TPS = output tokens / client response window seconds
window weighted TPS   = Σ timed response output tokens / Σ matching response seconds
```

Provider output includes reasoning, answer and tool-call tokens. Input/cached-input tokens are excluded; reasoning is a subset of output and is never added again. Total input+output consumption is useful context, not the TPS numerator. Windows include input processing, queue/network delay and first-token waiting; tools, background execution and human pauses are outside them. Concurrent responses retain their own denominators, rather than dividing combined output by wall time.

## 3. Evidence and precedents

These are implementation examples, not an industry standard or a popularity claim:

- [adenta/codex-tps](https://github.com/adenta/codex-tps#what-effective-tps-means) uses effective output TPS. Its [boundary test](https://github.com/adenta/codex-tps/blob/main/analysis_test.go#L62-L78) excludes a long tool run even when usage arrives after the tool result.
- [alicanselcuk/codex-tps-hook](https://github.com/alicanselcuk/codex-tps-hook/blob/main/turn-metrics-reader.ps1#L174-L190) exposes effective TPS and a separate approximate decode metric.
- [pszemraj/codex_speed.py](https://gist.github.com/pszemraj/f8fe69ff5002c3bd0825edfd02001635#file-codex_speed-py-L428-L479) distinguishes effective and streaming client estimates and rejects collapsed streaming windows.
- [dongyue-19/codex-statusbar](https://github.com/dongyue-19/codex-statusbar#1-the-three-primary-metrics--strict-definitions) uses timed reasoning/answer items; [codex-model-benchmarks](https://github.com/zakmandhro/codex-model-benchmarks#what-is-measured) measures visible delivered output. Neither definition should be silently substituted for effective TPS.

An existing completed local Codex 0.163.0 record had output=63, reasoning=49, a 3.315s effective client window, turn duration=6613ms and turn TTFT=5967ms. The reasoning item lasted 0ms: item display duration cannot measure hidden inference. 63/3.315≈19.005 effective TPS is interpretable. Dividing 63 by the 578ms visible-answer item is not a reliable reasoning-inclusive decoder metric. No extra model request was sent for this investigation.

## 4. Collection boundary

Use the existing verified Codex rollout observer: subprocess thread identity, native turn identity, file header, secure file opening, freshness and bounded incremental reads. Monitor never scans arbitrary private logs on HTTP requests. The parser projects only timestamps, record types, model/settings, usage and response identity; prompts, reasoning text, arguments and tool outputs are skipped and never persisted in the metrics store.

The latest input or tool-result boundary starts a response window; the last generated reasoning/answer/tool-call item ends it. A late usage timestamp never extends a response across tool execution. Modern response IDs and legacy usage carriers are deduplicated. Usage preceding generated items is retained until a boundary can close it. Invalid/zero windows and oversized records retain missing timing rather than inventing a speed. Existing historical sessions are not backfilled.

Scope is verified native turns belonging to Octopus-managed sessions, including delegated sessions. Ephemeral native helper transcripts are not scanned or added to parent output. Coverage means timed samples / observed response usage records, not coverage of every provider request on the host. Failed or cancelled attempts contribute only completed response observations already received; no partial-turn token count is divided by full attempt time.

## 5. Persistence and queries

Sanitized `model_measurement` harness events are internal; they do not enter transcripts, WebSocket messages or liveness signals. The session layer attaches session/agent/request/attempt identity and writes through the existing bounded asynchronous Monitor sink. Separate response and native turn-latency records retain their proper scope. Unique response/turn identities make persistence idempotent.

`GET /api/monitor/codex` groups only by recorded model. It exposes weighted TPS, latest valid completed response TPS/time, observed and timed sample counts, timing coverage, output/reasoning usage and average **turn** TTFT. Unknown values remain null. The same window governs both weighted and latest values. Default UI window is 24h; existing 1h/7d/30d choices remain.

`GET /api/monitor/codex_turns` exposes the latest 20 logical requests within the selected window, grouped by session/request/model, with matching weighted TPS, usage and recorded effort/tier. Retries sharing a request are combined without summing concurrent rates. `/overview` includes both reports. CLI supports `octopus monitor codex --since 24h` and `codex_turns`. All routes retain operator-only access and 30-day retention.

## 6. UI

Add a Codex effective output TPS table to Monitor. Weighted value is primary, latest measured response is secondary. State distinguishes measured, partial timing and missing timing; missing is `—`, never 0 TPS. Values are completed observations held during tools/waits, with their measurement timestamp. Expandable logical-request detail carries settings and usage context without complicating main model grouping.

Refresh every 5s only while the page is visible; resume immediately when visible. Requests do not overlap; stale responses cannot overwrite a changed window/token. A refresh error is visible alongside retained data. This is passive performance observation across differing prompts, context sizes, caches, effort and tier, not a controlled model/provider ranking.

## 7. Interpretation limits

A client log window is an estimate of response latency, not a server inference clock. Millisecond batching, transport and CLI versions affect it. First-token delay is available at native-turn scope and labelled accordingly; it is not copied onto every response. Missing usage cannot be reconstructed from text, and unknown model/settings are never backfilled from current configuration.

## 8. Accepted test seams and review baseline

The user confirmed Codex event parsing, Monitor HTTP API and Monitor UI/browser as test seams. Review baseline: `94eaa2935598170263c04d7fb6a9567552503782`. Tests cover long tools with late usage, modern/legacy carriers, weighted windows, deduplication, missing timing, permissions, rendering and foreground refresh. The existing live native-activity test verifies real installed Codex usage reaches the normalized stream without adding a new benchmark task.

## 9. Validation

Required release gates follow [AGENTS.md](../../AGENTS.md) and [local WSL procedures](../local-wsl.md#本机测试). On 2026-10-19:

| Check | Actual result | Raw evidence |
| --- | --- | --- |
| Eight fast gates: backend hermetic, both linters/types, frontend unit, contracts, docs index | All pass; hermetic 4261 passed, 50 deselected; frontend 450 passed | `/tmp/octopus-tps-fast-gates.log` |
| Post-review parsing/API/activity regressions | 60 passed, including both newly added ordering/snapshot regressions | pytest focused run |
| Pure UI browser bucket, separate offline audited execution | 83 passed; real dispatches 0 | `/tmp/octopus-tps-e2e-fast.log` |
| Codex live-provider marker selection | 18 passed, 4294 deselected | `/tmp/octopus-tps-codex-real.log` |
| Codex real browser: ordinary conversation → Monitor | 1 passed | `/tmp/octopus-tps-codex-browser.log` |
| Production frontend build | Passed | `/tmp/octopus-tps-build.log` |

Counts describe each actual invocation, not a claim that deselected tests passed. The full hermetic run began before the last two review regressions were added; the focused post-review run covers the final parser and all related API/activity cases. Ruff and mypy were rerun after these fixes.

Standards review found no documented-standard violations or material smell findings. Spec review found two response-ordering defects and one legacy-snapshot defect; all three have public-observer regressions, were fixed, and independent re-review reports zero remaining findings.

Provider scope: **Codex: actual results; Claude: unavailable, live tests not run**. Knowledge evaluation remains opt-in and outside this release gate. The user explicitly confirmed production was drained and waived reading its queues for this release. Publish the validated commit as an isolated snapshot, verify matching served assets and browser startup, then retire only a clean unused previous worktree; deployment evidence is `/tmp/octopus-tps-deployment.json`.

## 10. What this defers

- Server decoder TPS and isolated hidden-reasoning TPS need a separately proven clock covering those tokens; client log timing does not supply it.
- Controlled benchmark rankings and extra paid probes are outside the approved passive-observation scope.
- Ephemeral native-helper performance requires a verified per-child collection lifecycle; this view covers managed session responses and does not assert host-wide coverage.
