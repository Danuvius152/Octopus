"""Canned questions, because collection without analysis is just disk use.

Every function here returns plain rows. The CLI formats them, the API serves
them, and `sqlite3 octopus-metrics.db` answers anything not anticipated — which
is the reason the store is dimensional SQL rather than a step-series.
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite


def _since(window: str) -> float:
    """`7d` / `24h` / `30m` -> a unix timestamp. Anything unparseable is 24h,
    because a mistyped window should show something rather than nothing."""
    units = {"m": 60, "h": 3600, "d": 86400}
    try:
        n, unit = int(window[:-1]), window[-1]
        return time.time() - n * units[unit]
    except (ValueError, KeyError, IndexError):
        return time.time() - 86400


async def _rows(conn: aiosqlite.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list. A free function rather
    than a closure over the loop variable: ruff's B023 is right that capturing
    it is fragile even where, as here, the call is immediate."""
    if not sorted_vals:
        return None
    return round(sorted_vals[min(int(len(sorted_vals) * p), len(sorted_vals) - 1)], 1)


async def turns(conn: aiosqlite.Connection, window: str = "7d") -> list[dict[str, Any]]:
    """Turn latency and cost by backend. Percentiles come from ordering rather
    than a window function so this works on any SQLite build."""
    since = _since(window)
    out = []
    backends = await _rows(
        conn,
        "SELECT backend, COUNT(*) n, SUM(ok) ok_n, "
        "       AVG(duration_ms) avg_ms, MAX(duration_ms) max_ms "
        "FROM events WHERE kind='turn' AND ts >= ? GROUP BY backend",
        (since,),
    )
    for b in backends:
        durs = await _rows(
            conn,
            "SELECT duration_ms FROM events WHERE kind='turn' AND ts >= ? "
            "AND backend IS ? AND duration_ms IS NOT NULL ORDER BY duration_ms",
            (since, b["backend"]),
        )
        vals = [d["duration_ms"] for d in durs]
        b.update(
            p50_ms=_percentile(vals, 0.50),
            p95_ms=_percentile(vals, 0.95),
            p99_ms=_percentile(vals, 0.99),
        )
        out.append(b)
    return out


async def errors(conn: aiosqlite.Connection, window: str = "7d") -> list[dict[str, Any]]:
    """What is failing, most often first."""
    return await _rows(
        conn,
        "SELECT kind, error_code, COUNT(*) n, MAX(ts) last_seen "
        "FROM events WHERE ts >= ? AND (ok = 0 OR error_code IS NOT NULL) "
        "AND NOT (kind = 'delegation' AND ok IS 1 AND error_code IS 'completed') "
        "GROUP BY kind, error_code ORDER BY n DESC",
        (_since(window),),
    )


async def connectors(conn: aiosqlite.Connection, window: str = "7d") -> list[dict[str, Any]]:
    """Logical resource samples, with historical failure-only data separate.

    A legacy failure does not tell us how many requests succeeded unobserved.
    It cannot supply a denominator, duration or last observed resource time.
    """
    return await _rows(
        conn,
        """
        WITH calls AS (
            SELECT json_extract(detail, '$.installation_id') installation,
                   json_extract(detail, '$.unit') unit, ok, duration_ms, ts
            FROM events WHERE kind='connector_call' AND ts >= ?
        )
        SELECT installation,
               COUNT(CASE WHEN unit='logical_resource_v1' THEN 1 END) n,
               SUM(CASE WHEN unit='logical_resource_v1' AND ok=1 THEN 1 ELSE 0 END) ok_n,
               ROUND(100.0 * SUM(CASE WHEN unit='logical_resource_v1' AND ok=1 THEN 1 ELSE 0 END)
                     / COUNT(CASE WHEN unit='logical_resource_v1' THEN 1 END), 1) success_pct,
               ROUND(AVG(CASE WHEN unit='logical_resource_v1' THEN duration_ms END), 1) avg_ms,
               MAX(CASE WHEN unit='logical_resource_v1' THEN duration_ms END) max_ms,
               MAX(CASE WHEN unit='logical_resource_v1' THEN ts END) last_seen,
               SUM(CASE WHEN unit IS NULL AND ok=0 THEN 1 ELSE 0 END) legacy_failure_samples
        FROM calls WHERE unit='logical_resource_v1' OR (unit IS NULL AND ok=0)
        GROUP BY installation ORDER BY (n - ok_n) DESC, legacy_failure_samples DESC, installation
        """,
        (_since(window),),
    )


async def schedules(conn: aiosqlite.Connection, window: str = "7d") -> list[dict[str, Any]]:
    return await _rows(
        conn,
        "SELECT json_extract(detail, '$.schedule_id') schedule, "
        "       SUM(ok) fired, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) skipped, "
        "       MAX(ts) last_seen "
        "FROM events WHERE kind='schedule_fire' AND ts >= ? "
        "GROUP BY schedule ORDER BY skipped DESC",
        (_since(window),),
    )


async def resources(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    """Latest and peak for each gauge. `mcp_sidecar_count` is the one to watch:
    it should be 0 now that the namespaces are served in-process (§4 B1)."""
    return await _rows(
        conn,
        "SELECT metric, COUNT(*) n, "
        "       ROUND(AVG(value), 2) avg, ROUND(MAX(value), 2) peak, "
        "       (SELECT ROUND(value, 2) FROM samples s2 "
        "        WHERE s2.metric = samples.metric ORDER BY ts DESC LIMIT 1) latest "
        "FROM samples WHERE ts >= ? GROUP BY metric ORDER BY metric",
        (_since(window),),
    )


async def http(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    return await _rows(
        conn,
        "SELECT json_extract(detail, '$.route') route, COUNT(*) n, "
        "       ROUND(AVG(duration_ms), 1) avg_ms, ROUND(MAX(duration_ms), 1) max_ms "
        "FROM events WHERE kind='http' AND ts >= ? "
        "GROUP BY route ORDER BY n DESC LIMIT 25",
        (_since(window),),
    )


# Shared validity rules keep the model and logical-turn views comparable.
_CODEX_RESPONSES = """
            SELECT ts, duration_ms, session_id,
                   COALESCE(json_extract(detail, '$.request_id'),
                            json_extract(detail, '$.native_turn_id'), 'unknown') request_id,
                   json_extract(detail, '$.effort') effort,
                   json_extract(detail, '$.service_tier') service_tier,
                   json_extract(detail, '$.input_tokens') input_tokens,
                   json_extract(detail, '$.cached_input_tokens') cached_input_tokens,
                   json_extract(detail, '$.scope') scope,
                   json_extract(detail, '$.native_thread_id') native_thread_id,
                   json_extract(detail, '$.native_turn_id') native_turn_id,
                   COALESCE(json_extract(detail, '$.model'), 'unknown') model,
                   json_extract(detail, '$.output_tokens') output,
                   json_extract(detail, '$.reasoning_tokens') reasoning,
                   duration_ms > 0 AND duration_ms < 1e12
                       AND json_type(detail, '$.output_tokens') = 'integer'
                       AND json_extract(detail, '$.output_tokens') >= 0
                       AND (json_extract(detail, '$.reasoning_tokens') IS NULL
                            OR json_extract(detail, '$.reasoning_tokens') BETWEEN 0
                               AND json_extract(detail, '$.output_tokens')) valid
            FROM events WHERE kind = 'codex_response' AND ts >= ?
              AND COALESCE(json_extract(detail, '$.scope'), 'managed_session') NOT IN ('anomalous_root', 'unbound')
"""


async def codex(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    """Effective output TPS, weighted by observed response time, never turn time.

    Coverage counts observed usage records, not unobserved provider requests.
    Missing/invalid windows stay in that denominator, outside the TPS ratio.
    """
    return await _rows(conn, """
        WITH responses AS (""" + _CODEX_RESPONSES + """
        ), measured AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY model ORDER BY ts DESC) position
            FROM responses WHERE valid
        )
        SELECT r.model,
               ROUND(1000.0 * SUM(CASE WHEN r.valid THEN r.output END)
                     / SUM(CASE WHEN r.valid THEN r.duration_ms END), 2) weighted_tps,
               (SELECT ROUND(1000.0 * output / duration_ms, 2) FROM measured m
                WHERE m.model = r.model AND position = 1) latest_tps,
               (SELECT ts FROM measured m WHERE m.model = r.model AND position = 1) latest_at,
               COUNT(*) responses, SUM(CASE WHEN r.valid THEN 1 ELSE 0 END) timed_responses,
               ROUND(100.0 * SUM(CASE WHEN r.valid THEN 1 ELSE 0 END) / COUNT(*), 1) coverage_pct,
               SUM(r.output) output_tokens, SUM(r.reasoning) reasoning_tokens,
               (SELECT ROUND(AVG(e.duration_ms), 1) FROM events e
                WHERE e.kind = 'codex_turn_latency' AND e.ts >= ?
                  AND COALESCE(json_extract(e.detail, '$.scope'), 'managed_session')
                      NOT IN ('anomalous_root', 'unbound')
                  AND json_extract(e.detail, '$.model') = r.model) avg_turn_ttft_ms
        FROM responses r GROUP BY r.model ORDER BY r.model
    """, (_since(window), _since(window)))


async def codex_turns(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    """Recent logical requests, by session and actual observed model."""
    return await _rows(conn, "WITH responses AS (" + _CODEX_RESPONSES + """
        ) SELECT model, session_id, request_id, MAX(ts) last_seen,
                 ROUND(1000.0 * SUM(CASE WHEN valid THEN output END)
                       / SUM(CASE WHEN valid THEN duration_ms END), 2) weighted_tps,
                 COUNT(*) responses, SUM(CASE WHEN valid THEN 1 ELSE 0 END) timed_responses,
                 SUM(output) output_tokens, SUM(reasoning) reasoning_tokens,
                 SUM(input_tokens) input_tokens, SUM(cached_input_tokens) cached_input_tokens,
                 SUM(CASE WHEN scope='root' THEN 1 ELSE 0 END) root_responses,
                 SUM(CASE WHEN scope='child' THEN 1 ELSE 0 END) child_responses,
                 COUNT(DISTINCT json_array(native_thread_id, native_turn_id)) native_turns,
                 GROUP_CONCAT(DISTINCT effort) effort, GROUP_CONCAT(DISTINCT service_tier) service_tier
          FROM responses GROUP BY session_id, request_id, model
          ORDER BY last_seen DESC LIMIT 20
    """, (_since(window),))


async def _codex_details(conn: aiosqlite.Connection, window: str, kind: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    # Callers supply fixed field names. Provider counters are snapshots, never
    # input to the response/TPS aggregates above.
    columns = ", ".join(f"json_extract(detail, '$.{field}') {field}" for field in fields)
    return await _rows(conn, f"SELECT session_id, ts last_seen, {columns} FROM events "
                       "WHERE kind=? AND ts >= ? ORDER BY ts DESC LIMIT 20", (kind, _since(window)))


async def codex_runs(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    return await _codex_details(conn, window, "codex_run", (
        "request_id", "attempt_id", "root_thread_id", "root_turn_id", "root_turns", "child_turns",
        "input_state", "native_status", "resource_state", "end_reason", "host_ms", "preparation_ms",
        "start_ack_ms", "first_output_ms", "root_active_ms", "retirement_ms", "overall_elapsed_ms",
        "preparation_mode", "spawn_ms", "metrics_collection_complete",
        "idle_elapsed_ms", "overall_limit_ms", "idle_limit_ms", "idle_suspended", "metrics_threads",
        "metrics_unavailable", "metrics_capacity_drops",
    ))


async def codex_native_usage(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    return await _codex_details(conn, window, "codex_native_usage", (
        "request_id", "attempt_id", "native_thread_id", "native_turn_id", "historical",
        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
    ))


async def codex_retirement(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    """Only entered phases, with the original Harness timeout and elapsed time."""
    return await _rows(conn, """
        SELECT e.session_id, json_extract(e.detail, '$.request_id') request_id,
               json_extract(e.detail, '$.attempt_id') attempt_id,
               json_extract(p.value, '$.phase') phase,
               json_extract(p.value, '$.grace_ms') grace_ms,
               json_extract(p.value, '$.elapsed_ms') elapsed_ms
        FROM events e, json_each(e.detail, '$.retirement_phases') p
        WHERE e.kind='codex_run' AND e.ts >= ?
        ORDER BY e.ts DESC, p.key LIMIT 100
    """, (_since(window),))


async def codex_steers(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    return await _rows(conn, "SELECT session_id, ts last_seen, duration_ms ack_ms, "
        "json_extract(detail, '$.request_id') request_id, json_extract(detail, '$.input_id') input_id, "
        "json_extract(detail, '$.state') state FROM events WHERE kind='codex_steer' AND ts >= ? "
        "ORDER BY ts DESC LIMIT 20", (_since(window),))


async def codex_anomalies(conn: aiosqlite.Connection, window: str = "24h") -> list[dict[str, Any]]:
    return await _rows(conn, "SELECT session_id, json_extract(detail, '$.request_id') request_id, "
        "json_extract(detail, '$.attempt_id') attempt_id, json_extract(detail, '$.native_thread_id') native_thread_id, "
        "json_extract(detail, '$.native_turn_id') native_turn_id, json_extract(detail, '$.scope') scope, "
        "json_extract(detail, '$.model') model, COUNT(*) responses, "
        "SUM(json_extract(detail, '$.output_tokens')) output_tokens, MAX(ts) last_seen "
        "FROM events WHERE kind='codex_response' AND ts >= ? "
        "AND json_extract(detail, '$.scope') IN ('anomalous_root', 'unbound') "
        "GROUP BY session_id, request_id, attempt_id, native_thread_id, native_turn_id, scope, model "
        "ORDER BY last_seen DESC LIMIT 20", (_since(window),))


async def overview(conn: aiosqlite.Connection, window: str = "24h") -> dict[str, Any]:
    """Everything at a glance — what the in-app page renders."""
    counts = await _rows(
        conn,
        "SELECT kind, COUNT(*) n, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) failures "
        "FROM events WHERE ts >= ? GROUP BY kind ORDER BY n DESC",
        (_since(window),),
    )
    return {
        "window": window,
        "counts": counts,
        "counts_unit": "event_samples",
        "connectors": await connectors(conn, window),
        "turns": await turns(conn, window),
        "errors": (await errors(conn, window))[:10],
        "resources": await resources(conn, window),
        "codex": await codex(conn, window),
        "codex_turns": await codex_turns(conn, window),
        "codex_runs": await codex_runs(conn, window),
        "codex_retirement": await codex_retirement(conn, window),
        "codex_native_usage": await codex_native_usage(conn, window),
        "codex_steers": await codex_steers(conn, window),
        "codex_anomalies": await codex_anomalies(conn, window),
    }


REPORTS = {
    "codex": codex,
    "codex_turns": codex_turns,
    "codex_runs": codex_runs,
    "codex_retirement": codex_retirement,
    "codex_native_usage": codex_native_usage,
    "codex_steers": codex_steers,
    "codex_anomalies": codex_anomalies,
    "turns": turns,
    "errors": errors,
    "connectors": connectors,
    "schedules": schedules,
    "resources": resources,
    "http": http,
}
