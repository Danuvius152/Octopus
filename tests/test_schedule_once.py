"""New once candidates expire at definition admission; stored ones still recover."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException

from server import schedule_ai
from server.harness import get_harness
from server.routers import schedules
from server.schedule_ai import (
    ScheduleParseError,
    build_explicit_schedule,
    parse_schedule_text,
    validate_parsed,
)
from tests.schedule_support import call_tool, create_session
from tests.schedule_support import client as client


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2030, 1, 1, 12, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(schedule_ai, "datetime", Clock)
    return Clock


@pytest.mark.parametrize("run_at", ["2020-01-01T09:00:00", "2020-01-01T09:00:00+08:00"])
def test_natural_language_once_rejects_past_candidate(run_at):
    with pytest.raises(ScheduleParseError, match="future"):
        validate_parsed(
            {"prompt": "remind me", "recurrence": {"kind": "once", "run_at_iso": run_at}},
            default_tz="Asia/Shanghai", original_text="remind me at nine",
        )


async def test_natural_language_rejects_invalid_timezone_before_model():
    async def forbidden(_ctx):
        pytest.fail("invalid timezone reached the model")

    with pytest.raises(ScheduleParseError, match="IANA"):
        await parse_schedule_text("remind me tomorrow", timezone="Not/AZone", runner=forbidden)


async def test_natural_language_checks_server_clock_after_parsing(clock):
    target = clock.current + timedelta(minutes=1)

    async def parse(ctx):
        assert "2020-01-01T00:00" in ctx.prompt  # Browser time only aids interpretation.
        clock.current = target + timedelta(seconds=1)
        return json.dumps({"prompt": "remind me", "recurrence": {
            "kind": "once", "run_at_iso": target.isoformat(),
        }})

    with pytest.raises(ScheduleParseError, match="future"):
        await parse_schedule_text("remind me at noon", timezone="UTC",
                                  now_iso="2020-01-01T00:00", runner=parse)


@pytest.mark.parametrize("recurrence", [
    {"kind": "once", "run_at_iso": "2030-01-01T09:00", "interval_seconds": 60},
    {"kind": "cron", "cron": "0 9 * * *", "run_at_iso": "2030-01-01T09:00"},
    {"kind": "interval", "interval_seconds": 60, "cron": "0 9 * * *"},
    {"kind": "once", "cron": "0 9 * * *"},
    {"kind": []},
])
def test_natural_language_rejects_conflicting_or_mismatched_recurrence(recurrence):
    with pytest.raises(ScheduleParseError):
        validate_parsed({"prompt": "check", "recurrence": recurrence},
                        default_tz="UTC", original_text="check later")


@pytest.mark.parametrize("phase", ["sql", "cursor_cleanup"])
async def test_once_expiring_during_write_rolls_back_and_shuts_down(client, clock, monkeypatch, caplog, phase):
    sid = await create_session(client)
    runner = schedules._get_runner()
    runner._scheduler.pause()
    jobs_before = {job.id for job in runner._scheduler.get_jobs()}
    db = schedules._get_db()
    execute = db.conn.execute
    entered, release = asyncio.Event(), asyncio.Event()
    target = clock.current + timedelta(minutes=1)

    async def observe(sql, parameters=()):
        cursor = await execute(sql, parameters)
        if sql.startswith("INSERT INTO schedules"):
            if phase == "sql":
                entered.set()
                await release.wait()
            else:
                close = cursor.close

                async def held_close():
                    entered.set()
                    await release.wait()
                    await close()

                monkeypatch.setattr(cursor, "close", held_close)
        return cursor

    monkeypatch.setattr(db.conn, "execute", observe)
    creating = asyncio.create_task(client.post(f"/api/sessions/{sid}/schedules", json={
        "prompt": "remind me", "run_at": target.isoformat(), "timezone": "UTC",
    }))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runner.begin_shutdown()  # The owned write is still pending at this point.
        clock.current = target + timedelta(seconds=1)
    finally:
        release.set()
        response = await creating
    assert response.status_code == 422, response.text
    assert "future" in response.json()["detail"]
    assert (await client.get("/api/schedules")).json() == []
    assert {job.id for job in runner._scheduler.get_jobs()} == jobs_before
    await runner.shutdown()
    assert "Schedule control failed" not in caplog.text


@pytest.mark.parametrize("seconds", [-60, 5, 6])
@pytest.mark.parametrize("naive", [False, True])
def test_explicit_and_natural_language_share_the_five_second_margin(clock, seconds, naive):
    target = clock.current + timedelta(seconds=seconds)
    raw = target.astimezone(ZoneInfo("Asia/Shanghai"))
    if naive:
        raw = raw.replace(tzinfo=None)
    candidate = {"prompt": "remind me", "recurrence": {"kind": "once", "run_at_iso": raw.isoformat()}}
    if seconds <= 5:
        with pytest.raises(ScheduleParseError, match="future"):
            build_explicit_schedule(prompt="remind me", run_at=raw.isoformat(), tz="Asia/Shanghai")
        with pytest.raises(ScheduleParseError, match="future"):
            validate_parsed(candidate, default_tz="Asia/Shanghai", original_text="remind me")
    else:
        explicit = build_explicit_schedule(prompt="remind me", run_at=raw.isoformat(), tz="Asia/Shanghai")
        natural = validate_parsed(candidate, default_tz="Asia/Shanghai", original_text="remind me")
        assert datetime.fromisoformat(explicit.run_at) == target
        assert datetime.fromisoformat(natural.run_at) == target


@pytest.mark.parametrize("entry", ["explicit", "nl", "mcp"])
@pytest.mark.parametrize("seconds", [-60, 5, 60])
@pytest.mark.parametrize("naive", [False, True])
async def test_creation_entries_validate_and_persist_once(client, clock, monkeypatch, entry, seconds, naive):
    sid = await create_session(client)
    agent_id = (await client.get(f"/api/sessions/{sid}")).json()["agent_id"]
    schedules._get_runner()._scheduler.pause()
    target = clock.current + timedelta(seconds=seconds)
    raw = target.astimezone(ZoneInfo("Asia/Shanghai"))
    if naive:
        raw = raw.replace(tzinfo=None)
    body = {"prompt": "remind me", "run_at": raw.isoformat(), "timezone": "Asia/Shanghai"}
    if entry == "nl":
        async def parse(ctx):
            await ctx.authorize_spawn()
            return json.dumps({"prompt": "remind me", "recurrence": {
                "kind": "once", "run_at_iso": raw.isoformat(),
            }})

        monkeypatch.setattr(get_harness("claude-code"), "run_oneshot", parse)
        response = await client.post(f"/api/agents/{agent_id}/schedules/from_text", json={
            "text": "remind me later", "timezone": "Asia/Shanghai", "now": "2020-01-01T00:00",
        })
    elif entry == "mcp":
        loop = asyncio.get_running_loop()
        monkeypatch.setenv("OCTOPUS_API_BASE", "http://test")
        monkeypatch.setenv("OCTOPUS_SESSION_ID", sid)
        monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", client.headers["Authorization"].removeprefix("Bearer "))

        def post(url, **kwargs):
            return asyncio.run_coroutine_threadsafe(client.post(url, **kwargs), loop).result(timeout=3)

        monkeypatch.setattr(httpx, "post", post)
        result = await asyncio.to_thread(call_tool, "create_schedule", **body)
        assert result.startswith("Scheduled." if seconds > 5 else "Couldn't create that schedule:"), result
        if seconds <= 5:
            assert "future" in result
    else:
        response = await client.post(f"/api/sessions/{sid}/schedules", json=body)
    if entry != "mcp":
        assert response.status_code == (201 if seconds > 5 else 422), response.text
        if seconds <= 5:
            assert "future" in response.json()["detail"]
    rows = (await client.get("/api/schedules")).json()
    assert len(rows) == int(seconds > 5)
    if rows:
        assert datetime.fromisoformat(rows[0]["run_at"]) == target
        assert rows[0]["timezone"] == "Asia/Shanghai"
        assert rows[0]["cron"] is None and rows[0]["interval_seconds"] is None
        assert schedules._get_runner().next_run_at(rows[0]["id"]) is not None


async def test_shared_creation_rejects_a_conflicting_internal_candidate(client, clock):
    sid = await create_session(client)
    agent_id = (await client.get(f"/api/sessions/{sid}")).json()["agent_id"]
    with pytest.raises(HTTPException) as error:
        await schedules.create_schedule_for_agent(
            agent_id, "conflict", "remind me", interval_seconds=60,
            run_at=(clock.current + timedelta(minutes=1)).isoformat(), tz="UTC",
        )
    assert error.value.status_code == 422
    assert (await client.get("/api/schedules")).json() == []


async def test_once_loses_margin_while_waiting_for_writer(client, clock):
    sid = await create_session(client)
    agent_id = (await client.get(f"/api/sessions/{sid}")).json()["agent_id"]
    db, runner = schedules._get_db(), schedules._get_runner()
    target = clock.current + timedelta(minutes=1)
    async with db.transaction(write=True):
        creating = asyncio.create_task(runner.create(
            "expired-at-writer", agent_id=agent_id, name="remind me", prompt="remind me",
            run_at=target.isoformat(), timezone="UTC", created_at=clock.current.isoformat(),
        ))
        async with asyncio.timeout(2):
            while not runner._control_tasks:
                await asyncio.sleep(0)
        clock.current = target - timedelta(seconds=5)
    with pytest.raises(ScheduleParseError, match="future"):
        await creating
    assert (await client.get("/api/schedules")).json() == []
    assert runner.next_run_at("expired-at-writer") is None


@pytest.mark.parametrize("scoped", [False, True])
async def test_new_patch_time_expires_but_rereading_overdue_value_is_allowed(client, clock, monkeypatch, scoped):
    sid = await create_session(client)
    target = clock.current + timedelta(minutes=1)
    created = await client.post(f"/api/sessions/{sid}/schedules", json={
        "prompt": "remind me", "run_at": target.isoformat(), "timezone": "UTC",
    })
    assert created.status_code == 201
    identifier = created.json()["id"]
    url = f"/api/sessions/{sid}/schedules/{identifier}" if scoped else f"/api/schedules/{identifier}"
    before = (await client.get("/api/schedules")).json()[0]
    db = schedules._get_db()
    execute = db.conn.execute
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(sql, parameters=()):
        cursor = await execute(sql, parameters)
        if sql.startswith("UPDATE schedules SET"):
            close = cursor.close

            async def held_close():
                entered.set()
                await release.wait()
                await close()

            monkeypatch.setattr(cursor, "close", held_close)
        return cursor

    monkeypatch.setattr(db.conn, "execute", observe)
    # An explicit new value is new intent even when equal to the stored value.
    editing = asyncio.create_task(client.patch(url, json={"run_at": target.isoformat()}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clock.current = target - timedelta(seconds=5)
    finally:
        release.set()
        response = await editing
    assert response.status_code == 422, response.text
    assert (await client.get("/api/schedules")).json() == [before]
    monkeypatch.setattr(db.conn, "execute", execute)
    clock.current = target + timedelta(seconds=1)
    reread = await client.patch(url, json={"timezone": "Asia/Shanghai"})
    assert reread.status_code == 200, reread.text
    assert datetime.fromisoformat(reread.json()["run_at"]) == target
    assert reread.json()["timezone"] == "Asia/Shanghai"
    enabled = await client.patch(url, json={"enabled": True, "prompt": "updated reminder"})
    assert enabled.status_code == 200, enabled.text
    refused = await client.patch(url, json={"run_at": target.isoformat()})
    assert refused.status_code == 422
    assert (await client.get("/api/schedules")).json()[0]["prompt"] == "updated reminder"


async def test_time_passing_after_commit_does_not_turn_a_saved_once_into_rejection(client, clock, monkeypatch):
    sid = await create_session(client)
    db = schedules._get_db()
    execute = db.conn.execute
    commit = db.conn.commit
    target = clock.current + timedelta(minutes=1)
    admitting_once = False

    async def observe(sql, parameters=()):
        nonlocal admitting_once
        cursor = await execute(sql, parameters)
        if sql.startswith("INSERT INTO schedules"):
            admitting_once = True
        return cursor

    async def confirmed_commit():
        await commit()
        if admitting_once:
            clock.current = target + timedelta(seconds=1)

    monkeypatch.setattr(db.conn, "execute", observe)
    monkeypatch.setattr(db.conn, "commit", confirmed_commit)
    response = await client.post(f"/api/sessions/{sid}/schedules", json={
        "prompt": "remind me", "run_at": target.isoformat(), "timezone": "UTC",
    })
    assert response.status_code == 201, response.text
    rows = (await client.get("/api/schedules")).json()
    assert len(rows) == 1 and rows[0]["id"] == response.json()["id"]
    assert datetime.fromisoformat(rows[0]["run_at"]) == target
    assert schedules._get_runner().next_run_at(rows[0]["id"]) is not None


async def test_http_natural_language_candidate_expires_during_model_call(client, clock, monkeypatch):
    sid = await create_session(client)
    agent_id = (await client.get(f"/api/sessions/{sid}")).json()["agent_id"]
    target = clock.current + timedelta(minutes=1)

    async def parse(ctx):
        await ctx.authorize_spawn()
        clock.current = target + timedelta(seconds=1)
        return json.dumps({"prompt": "remind me", "recurrence": {
            "kind": "once", "run_at_iso": target.isoformat(),
        }})

    monkeypatch.setattr(get_harness("claude-code"), "run_oneshot", parse)
    response = await client.post(f"/api/agents/{agent_id}/schedules/from_text", json={
        "text": "remind me shortly", "timezone": "UTC", "now": "2099-01-01T00:00",
    })
    assert response.status_code == 422, response.text
    assert "future" in response.json()["detail"]
    assert (await client.get("/api/schedules")).json() == []
