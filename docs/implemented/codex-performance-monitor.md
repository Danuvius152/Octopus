# Codex performance in Monitor

> **Status:** implemented — Passive response usage/TPS, native root and child scope, Owner clocks and resource-retirement phases.

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

Ordinary App Server attempts use one response-accounting source: the existing verified rollout observer in metadata-only mode. It checks native thread identity, file header, secure opening, inode continuity, freshness and bounded incremental reads. It starts at the existing file's EOF; a new thread's not-yet-created file can appear later, while disappearance after verification invalidates the source. Monitor never scans private logs on HTTP requests or backfills historical sessions. The parser projects only timestamps, record types, model/settings, usage and response identity; prompts, reasoning text, arguments and tool outputs are skipped and never persisted in the metrics store.

Only modern `token_usage_record` with response identity supplies ordinary native response usage. `rawResponse/completed` is not a second source: the pinned server disables raw events on cold resume, so that protocol type cannot provide complete ordinary accounting. `thread/tokenUsage/updated` separately retains selected counters from provider cumulative `total`, including history replay; it does not retain `last` or model context window. Snapshots are never summed into response usage or used to infer model-request counts. Independent exec/text-worker uses retain their own verified sources and do not duplicate ordinary accounting.

The latest input or tool-result boundary starts a response window; the last generated reasoning/answer/tool-call item ends it. A late usage timestamp never extends a response across tool execution. Response IDs are deduplicated; legacy usage carriers remain supported only for their independent legacy observer uses. Usage preceding generated items is retained until a boundary can close it. Invalid/zero windows and oversized records retain missing timing rather than inventing a speed. Known usage with missing timing differs from an unavailable usage source.

Scope includes the accepted root and actual accepted `(child thread, turn)` work rounds, including later followups on the same child. A known parent/thread alone cannot accept a foreign-root child turn. Up to 128 original native thread observers share the attempt's finite lifetime. Child model/settings remain unknown without native evidence; root settings are not copied onto them. Failed/cancelled attempts and the root's finite shutdown tail retain completed response observations already received; no partial-turn token count is divided by full attempt time.

Rollout persistence can precede typed root/child acceptance. The Driver temporarily retains sanitized, unassociated measurements within 256 records and 1 MiB serialized metadata. Original root/turn association releases them once; metering never creates work or acceptance. Known unexpected roots are diagnostic, and observations still unknown when finite collection closes become `unbound`. Both scopes are excluded from normal usage, TPS and turn-TTFT aggregates. Capacity loss increments the existing coverage counter without affecting business output, input authority or activity.

Coverage means timed samples / observed response usage records, not every provider request on the host. Source availability, collection completeness and capacity loss are also reported, so missing observations are not presented as zero consumption. Observation drain and close share a one-second deadline; a late file worker closes its descriptor but cannot publish after invalidation. Optional read/close/finish failures cannot change the confirmed process-retirement result.

## 5. Persistence and queries

Sanitized `model_measurement` observations are internal; they bypass the necessary output queue and do not enter transcripts, WebSocket messages or liveness signals. The original HarnessRun writes through the existing bounded disposable Monitor sink with captured session/request/attempt/work/run identity and backend; this native path does not attach an agent ID. Slow or failing metering cannot fail the request. Separate response and native turn-latency records retain their proper scope. Unique response/turn identities make persistence idempotent; provider snapshots update their own identity instead of multiplying on replay.

`GET /api/monitor/codex` groups only by recorded model. It exposes weighted TPS, latest valid completed response TPS/time, observed and timed sample counts, timing coverage, output/reasoning usage and average **turn** TTFT. Unknown values remain null. The same window governs both weighted and latest values. Default UI window is 24h; existing 1h/7d/30d choices remain.

`GET /api/monitor/codex_turns` exposes the latest 20 logical requests within the selected window, grouped by session/request/model, with matching weighted TPS, usage and recorded effort/tier. Retries sharing a request are combined without summing concurrent rates. `/overview` includes both reports. CLI supports `octopus monitor codex --since 24h` and `codex_turns`. All routes retain operator-only access and 30-day retention.

`codex_runs`, `codex_native_usage`, `codex_steers`, `codex_retirement` and `codex_anomalies` expose separate diagnostics through the same public query/overview path. Product attempts, accepted root/child turns, model responses and provider cumulative counters are distinct counts. Run facts retain preparation start/resume, spawn, start ACK, first output, root activity and retirement durations. Steer ACK proves acceptance, not processing. HarnessRun records only entered retirement phases with their original grace and actual elapsed time; cleanup is never labelled generation or parent continuation. Owner supplies its existing overall/idle elapsed values, limits and suspension state read-only. Monitor creates no clock, extends no budget and grants no idle exemption for polling, ACKs, detail reads, duplicate events or native wait.

## 6. UI

Monitor displays a Codex effective output TPS table to Monitor. Weighted value is primary, latest measured response is secondary. State distinguishes measured, partial timing and missing timing; missing is `—`, never 0 TPS. Values are completed observations held during tools/waits, with their measurement timestamp. Expandable logical-request detail carries settings and usage context without complicating main model grouping.

Execution phases, source coverage, cumulative snapshots, steer acceptance, Owner clocks, entered retirement grace phases and unaccepted execution diagnostics remain separately labelled. Older records lacking these optional facts display missing values without invented defaults.

Refresh every 5s only while the page is visible; resume immediately when visible. Requests do not overlap; stale responses cannot overwrite a changed window/token. A refresh error is visible alongside retained data. This is passive performance observation across differing prompts, context sizes, caches, effort and tier, not a controlled model/provider ranking.

## 7. Interpretation limits

A client log window is an estimate of response latency, not a server inference clock. Millisecond batching, transport and CLI versions affect it. First-token delay is available at native-turn scope and labelled accordingly; it is not copied onto every response. Missing usage cannot be reconstructed from text, and unknown model/settings are never backfilled from current configuration.

## 8. Accepted test seams and review baseline

The user confirmed Codex event parsing, Monitor HTTP API and Monitor UI/browser as test seams. `tests/test_monitor.py`, `tests/test_codex_native_observations.py`, `tests/test_codex_app_server_monitor.py` and `MonitorPage.test.tsx` cover windows, deduplication, cold history, actual root/child association races, anomalies, finite close, optional failures, queue pressure, permissions and rendering. Live native activity, fresh-thread and nested/followup cases verify source reachability through the original Owner and Monitor without a benchmark task. Verification policy and browser entry points belong to [testing.md](../testing.md).

## 9. What this defers

- Server decoder TPS and isolated hidden-reasoning TPS need a separately proven clock covering those tokens; client log timing does not supply it.
- Controlled benchmark rankings and extra paid probes are outside the approved passive-observation scope.
- Unobserved or unsupported native records remain outside coverage; this view does not assert host-wide accounting.

## Historical evidence

The [original design and version-bound evidence](https://github.com/Danuvius152/Octopus/blob/a8abac5ae0a9e3b98b7e71df003cfcd88c211571/docs/plans/codex-performance-monitor.md) explain the original implementation. Current behavior is maintained above; historical checks do not establish current deployment.
