"""Declared cron calendar semantics through actual trigger dates."""

import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from server.database import Database
from server.harness import HarnessEvent
from server.knowledge import encode, for_account
from server.routers import schedules
from server.schedule_ai import build_explicit_schedule, cron_trigger
from server.scheduler import ScheduleRunner
from server.session_manager import SessionManager, session_manager
from tests.accounts import create_account
from tests.fake_run import FakeRunBase
from tests.schedule_support import client as client
from tests.schedule_support import create_session


def fire_times(expr, start, count, timezone="UTC"):
    trigger = cron_trigger(expr, timezone)
    times = []
    previous = None
    for _ in range(count):
        previous = trigger.get_next_fire_time(previous, previous or start)
        assert previous is not None
        times.append(previous)
    return times


def test_numeric_sunday_range_applies_step_before_alias_normalization():
    assert [time.isoformat() for time in fire_times(
        "0 9 * * 0-7/2", datetime(2026, 11, 1, tzinfo=UTC), 5,
    )] == [
        "2026-11-01T09:00:00+00:00", "2026-11-03T09:00:00+00:00",
        "2026-11-05T09:00:00+00:00", "2026-11-07T09:00:00+00:00",
        "2026-11-08T09:00:00+00:00",
    ]


def test_restricted_days_form_a_union_with_shared_month_and_no_overlap_duplicate():
    assert [time.strftime("%Y-%m-%d") for time in fire_times(
        "0 9 1 11 1", datetime(2026, 10, 31, tzinfo=UTC), 8,
    )] == [
        "2026-11-01", "2026-11-02", "2026-11-09", "2026-11-16",
        "2026-11-23", "2026-11-30", "2027-11-01", "2027-11-08",
    ]


@pytest.mark.parametrize("field,days", [
    ("0", [1, 8, 15]), ("7", [1, 8, 15]),
    ("0-6", [1, 2, 3, 4, 5, 6, 7]), ("0-7", [1, 2, 3, 4, 5, 6, 7]),
    ("1-7", [1, 2, 3, 4, 5, 6, 7]), ("0,7,0-7", [1, 2, 3, 4, 5, 6, 7]),
    ("1-7/2", [1, 2, 4, 6, 8]), ("0-7/3", [1, 4, 7, 8]),
    ("6-1/2", [2, 7, 9, 14]), ("7-1/2", [1, 8, 15]),
    ("7-0", [1, 8, 15]), ("7-7", [1, 8, 15]), ("6-7/2", [7, 14, 21]),
    ("MON-FRI/2", [2, 4, 6, 9]), ("fri-mon/2", [1, 6, 8, 13]),
    ("fri,7,MON", [1, 2, 6, 8]), ("7-MON/2", [1, 8, 15]),
    ("SUN-7/2", [1, 8, 15]), ("*/2", [1, 3, 5, 7]), ("?", [1, 2, 3]),
])
def test_weekday_forms_produce_the_declared_dates(field, days):
    assert [time.strftime("%Y-%m-%d") for time in fire_times(
        f"0 9 * * {field}", datetime(2026, 11, 1, tzinfo=UTC), len(days),
    )] == [f"2026-11-{day:02d}" for day in days]


@pytest.mark.parametrize("day,weekday,days", [
    ("*/2", "1", [9, 23]),
    ("*/2,1", "1", [9, 23]),
    ("1,*/2", "1", [1, 2, 3, 5, 7]),
    ("2", "*/2,1", [2]),
    ("2", "1,*/2", [1, 2, 3, 5, 7]),
    ("2", "*", [2]), ("2", "?", [2]),
    ("*", "1", [2, 9, 16]), ("1-31", "1", [1, 2, 3]),
])
def test_original_leading_star_selects_intersection_including_list_position(day, weekday, days):
    assert [time.strftime("%Y-%m-%d") for time in fire_times(
        f"0 9 {day} 11 {weekday}", datetime(2026, 11, 1, tzinfo=UTC), len(days),
    )] == [f"2026-11-{day:02d}" for day in days]


def test_impossible_month_day_does_not_hide_the_weekday_branch():
    assert [time.strftime("%Y-%m-%d") for time in fire_times(
        "0 9 31 2 mon", datetime(2027, 1, 31, tzinfo=UTC), 3,
    )] == ["2027-02-01", "2027-02-08", "2027-02-15"]


def test_other_fields_keep_their_existing_cron_trigger_vocabulary():
    assert [time.strftime("%Y-%m-%d") for time in fire_times(
        "0 9 last feb mon", datetime(2027, 1, 31, tzinfo=UTC), 5,
    )] == ["2027-02-01", "2027-02-08", "2027-02-15", "2027-02-22", "2027-02-28"]


@pytest.mark.parametrize("expr", [
    "? 9 * * 1", "0 ? * * 1", "0 9 ? * 1", "0 9 * ? 1",
    "0 9 * * 8", "0 9 * * */0", "0 9 * * mon-funday", "0 9 0 * mon",
    "0 9 * * 1-", "0 9 * * -1", "0 9 * *",
])
def test_invalid_fields_are_rejected_without_expanding_question_alias(expr):
    with pytest.raises(ValueError):
        cron_trigger(expr, "UTC")


@pytest.mark.parametrize("expr,start,expected", [
    ("30 1 7,8,9 3 0", datetime(2026, 3, 7), [
        "2026-03-07T06:30:00+00:00", "2026-03-08T06:30:00+00:00",
        "2026-03-09T05:30:00+00:00", "2026-03-15T05:30:00+00:00",
    ]),
    ("30 1 1 11 0", datetime(2026, 10, 31), [
        "2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00",
        "2026-11-08T06:30:00+00:00",
    ]),
])
def test_union_keeps_dst_offsets_and_distinct_fall_fold_instants(expr, start, expected):
    zone = ZoneInfo("America/New_York")
    assert [time.astimezone(UTC).isoformat() for time in fire_times(
        expr, start.replace(tzinfo=zone), len(expected), "America/New_York",
    )] == expected


@pytest.fixture
def calendar_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2030, 10, 31, tzinfo=UTC)
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    # Registration/preview uses the scheduler's clock, not a parallel calculator.
    monkeypatch.setattr("apscheduler.schedulers.base.datetime", Clock)


async def test_public_create_and_patch_preview_the_single_corrected_job(client, calendar_clock):
    sid = await create_session(client)
    runner = schedules._get_runner()
    runner._scheduler.pause()
    before = {job.id for job in runner._scheduler.get_jobs()}
    expression = "0 9 1,*/2 11 1"
    created = await client.post(f"/api/sessions/{sid}/schedules", json={
        "prompt": "check the build", "cron": expression, "timezone": "UTC",
    })
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["cron"] == expression
    assert row["next_run_at"] == "2030-11-01T09:00:00+00:00"
    assert {job.id for job in runner._scheduler.get_jobs()} == before | {row["id"]}
    patched = await client.patch(f"/api/schedules/{row['id']}", json={"cron": "0 9 */2,1 11 1"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["next_run_at"] == "2030-11-11T09:00:00+00:00"
    listed = (await client.get("/api/schedules")).json()
    assert len(listed) == 1 and listed[0]["cron"] == "0 9 */2,1 11 1"
    assert listed[0]["next_run_at"] == "2030-11-11T09:00:00+00:00"
    assert {job.id for job in runner._scheduler.get_jobs()} == before | {row["id"]}
    rejected = await client.patch(f"/api/schedules/{row['id']}", json={"cron": "0 9 ? * 1"})
    assert rejected.status_code == 422
    assert (await client.get("/api/schedules")).json() == listed


@pytest.mark.parametrize("expression,expected", [
    ("0 9 1,*/2 11 1", ["2030-11-01", "2030-11-03", "2030-11-04"]),
    ("0 9 */2,1 11 1", ["2030-11-11", "2030-11-25", "2031-11-03"]),
])
async def test_original_expression_and_dates_survive_storage_reopen(tmp_path, calendar_clock, expression, expected):
    path = tmp_path / "cron.db"
    for reopening in (False, True):
        db = Database(str(path))
        await db.initialize()
        if not reopening:
            await create_account(db, extra_roots=[tmp_path])
        manager = SessionManager()
        await manager.initialize(db)
        runner = ScheduleRunner(manager, db)
        try:
            await runner.initialize(paused=True)
            if not reopening:
                agent = await db.get_system_agent()
                parsed = build_explicit_schedule(prompt="check the build", cron=expression, tz="UTC")
                context = for_account(session_id="definition", request_id="calendar", user_id=agent["user_id"],
                                      prompt=parsed.prompt)
                await runner.create("calendar", agent_id=agent["id"], name=parsed.name, prompt=parsed.prompt,
                                    cron=parsed.cron, timezone=parsed.timezone,
                                    recurrence_label=parsed.recurrence_label, knowledge_context=encode(context),
                                    created_at="2030-10-31T00:00:00+00:00")
            stored = await db.get_schedule_definition("calendar")
            assert stored["cron"] == expression
            job = runner._scheduler.get_job("calendar")
            assert job is not None
            previous = None
            dates = []
            for _ in expected:
                previous = job.trigger.get_next_fire_time(previous, previous or datetime(2030, 10, 31, tzinfo=UTC))
                dates.append(previous.strftime("%Y-%m-%d"))
            assert dates == expected
            assert runner.next_run_at("calendar") == expected[0] + "T09:00:00+00:00"
            assert sum(item.id == "calendar" for item in runner._scheduler.get_jobs()) == 1
        finally:
            await runner.shutdown()
            await db.close()


@pytest.mark.parametrize("operation", ["pause", "delete"])
async def test_control_does_not_revoke_the_original_in_flight_execution(client, monkeypatch, operation):
    sid = await create_session(client)
    manager, runner, db = session_manager, schedules._get_runner(), schedules._get_db()
    started, release, retired = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runs = []

    class Run(FakeRunBase):
        def __init__(self, session_id):
            self.session_id = session_id

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            self.prompt = prompt
            self.request_id = manager.dispatch_snapshot(self.session_id)["active_request_id"]
            runs.append(self)
            started.set()

        async def stream(self):
            await release.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def stop(self):
            retired.set()

        async def interrupt(self):
            release.set()

        def is_alive(self):
            return not retired.is_set()

    monkeypatch.setattr(manager, "_make_run", lambda session, *args, **kwargs: Run(session.id))
    created = await client.post(f"/api/sessions/{sid}/schedules", json={
        "prompt": "original instructions", "interval_seconds": 3600,
        "in_session": False, "wiki_mode": "read",
    })
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    original = await db.get_schedule_definition(identifier)
    original_knowledge = json.loads(original["knowledge_context"])["value"]
    firing = asyncio.create_task(runner._fire(identifier))
    try:
        await asyncio.wait_for(started.wait(), 2)
        run = runs[0]
        original_session = manager.get_session(run.session_id)
        assert manager.session_is_executing(original_session)
        assert not retired.is_set()
        url = f"/api/schedules/{identifier}"
        if operation == "pause":
            response = await client.patch(url, json={"enabled": False, "prompt": "replacement", "wiki_mode": "auto"})
            assert response.status_code == 200, response.text
            changed = await db.get_schedule_definition(identifier)
            assert not changed["enabled"] and changed["prompt"] == "replacement"
        else:
            response = await client.delete(url)
            assert response.status_code == 204
            assert await db.get_schedule_definition(identifier) is None
        assert runner.next_run_at(identifier) is None
        assert not firing.done() and not retired.is_set()
        release.set()
        await asyncio.wait_for(firing, 3)
        await manager.await_session_execution(run.session_id, timeout=2)
        assert retired.is_set()
        assert not manager.session_is_executing(original_session)
        assert manager.running_turns_for(original_session.user_id) == 0
        messages = await db.load_messages(run.session_id)
        users = [message for message in messages if message["role"] == "user"]
        assert len(users) == 1 and run.prompt.endswith("original instructions")
        assert users[0]["content"].endswith("original instructions")
        request = users[0]["tool_input"]["request_context"]
        assert request["request_id"] == run.request_id and request["source"] == "schedule"
        assert request["knowledge"]["value"] == {**original_knowledge, "interaction_policy": "unattended"}
        results = [message["content"] for message in messages if message["type"] == "result"]
        assert len(results) == 1
        assert results[0]["request_id"] == run.request_id and results[0]["status"] == "success"
        assert (await db.get_session_binding(run.session_id))["archived"]
        await runner._fire(identifier)
        assert len(runs) == 1
    finally:
        release.set()
        await asyncio.wait_for(firing, 3)
