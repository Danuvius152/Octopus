"""Tests for the agent-scoped ScheduleRunner (agent-refactor.md §5.3/§5.6)."""

import asyncio

import pytest

from server.database import Database
from server.knowledge import encode, for_account
from server.scheduler import ScheduleRunner
from server.session_manager import SessionManager
from tests.accounts import create_account
from tests.fake_run import FakeRunBase
from tests.submission import accepted_receipt


async def save_schedule(db, **fields):
    """Seed a new trusted definition; missing-context legacy rows are tested separately."""
    agent = await db.get_agent(fields["agent_id"])
    context = for_account(session_id=fields.get("origin_session_id") or "schedule-entry",
                          request_id=fields["schedule_id"], user_id=agent["user_id"],
                          prompt=fields["prompt"], selected=fields.get("wiki_mode"))
    await db.save_schedule(**fields, knowledge_context=encode(context))


def capture_model_runs(manager, monkeypatch, *, hold=False):
    """Keep scheduling, admission and settlement real at the Harness seam."""
    from server.harness import HarnessEvent

    started = []

    class Run(FakeRunBase):
        def __init__(self, session_id):
            self.session_id = session_id
            self.release = asyncio.Event()

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            self.prompt = prompt
            self.request_id = manager.dispatch_snapshot(self.session_id)["active_request_id"]
            started.append(self)

        async def stream(self):
            if hold:
                await self.release.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def interrupt(self):
            self.release.set()

        async def stop(self):
            self.release.set()

    monkeypatch.setattr(manager, "_make_run", lambda session, *args, **kwargs: Run(session.id))
    return started


@pytest.fixture
async def setup(tmp_path):
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    mgr = SessionManager()
    await mgr.initialize(db)
    runner = ScheduleRunner(mgr, db)
    try:
        yield mgr, db, runner
    finally:
        if runner._scheduler.running:
            await runner.shutdown()
        await db.close()


@pytest.mark.asyncio
async def test_scheduler_shutdown_waits_for_its_cancelled_callback_cleanup(setup):
    mgr, db, runner = setup
    agent = await db.get_system_agent()
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def scheduled_callback():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            await db.update_schedule("shutdown-schedule", last_run_at="cleanup-confirmed")

    await save_schedule(db,
        schedule_id="shutdown-schedule", agent_id=agent["id"], name="shutdown",
        prompt="do it", interval_seconds=3600, created_at="2026-01-01T00:00:00+00:00",
    )
    await runner.initialize()
    runner._scheduler.add_job(scheduled_callback, "date", id="cleanup-test")
    stopping = None
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        stopping = asyncio.create_task(runner.shutdown())
        await asyncio.wait_for(cleaning.wait(), timeout=5)

        assert not stopping.done(), "scheduler returned while its callback still owned a DB write"
        release.set()
        await stopping
        assert (await db.load_schedules())[0]["last_run_at"] == "cleanup-confirmed"
    finally:
        release.set()
        if stopping is not None:
            await stopping
        else:
            await runner.shutdown()


@pytest.mark.asyncio
async def test_scheduler_shutdown_closes_admission_before_awaiting(setup):
    _mgr, db, runner = setup
    agent = await db.get_system_agent()
    await save_schedule(db,
        schedule_id="too-late", agent_id=agent["id"], name="late", prompt="late",
        interval_seconds=3600, created_at="2026-01-01T00:00:00+00:00",
    )

    runner.begin_shutdown()

    with pytest.raises(RuntimeError, match="shutting down"):
        await runner.add((await db.load_schedules())[0])
    with pytest.raises(RuntimeError, match="shutting down"):
        await runner.initialize()
    await runner.shutdown()


@pytest.mark.asyncio
async def test_scheduler_shutdown_timeout_retains_callback_cleanup_for_later_join(setup):
    _mgr, _db, runner = setup
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    retired = asyncio.Event()

    async def callback():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            retired.set()

    await runner.initialize()
    runner._scheduler.add_job(callback, "date", id="slow-retirement")
    await asyncio.wait_for(started.wait(), timeout=5)
    retry = None
    try:
        with pytest.raises(TimeoutError, match="retirement unconfirmed"):
            await runner.shutdown(timeout=0.01)
        assert cleaning.is_set()
        assert not retired.is_set()
        retry = asyncio.create_task(runner.shutdown())
        await asyncio.sleep(0)
        assert not retry.done()

        release.set()
        await retry
        assert retired.is_set()
    finally:
        release.set()
        if retry is not None:
            await retry
        else:
            await runner.shutdown()


@pytest.mark.asyncio
async def test_scheduler_shutdown_propagates_callback_cleanup_failure(setup):
    _mgr, _db, runner = setup
    started = asyncio.Event()

    async def callback():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise OSError("callback DB cleanup unconfirmed")

    await runner.initialize()
    runner._scheduler.add_job(callback, "date", id="failed-retirement")
    await asyncio.wait_for(started.wait(), timeout=5)

    with pytest.raises(ExceptionGroup, match="callback cleanup failed") as error:
        await runner.shutdown()
    assert str(error.value.exceptions[0]) == "callback DB cleanup unconfirmed"


@pytest.mark.asyncio
async def test_removing_schedule_preserves_its_already_running_callback(setup):
    _mgr, _db, runner = setup
    started = asyncio.Event()
    release = asyncio.Event()
    retired = asyncio.Event()
    was_cancelled = False

    async def callback():
        nonlocal was_cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        finally:
            retired.set()

    await runner.initialize()
    runner._scheduler.add_job(callback, "interval", seconds=0.02, id="removed-schedule")
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await runner.remove("removed-schedule")
        await asyncio.sleep(0)
        assert not was_cancelled
        assert not retired.is_set()

        release.set()
        await asyncio.wait_for(retired.wait(), timeout=5)
    finally:
        release.set()
        await runner.shutdown()


@pytest.mark.asyncio
async def test_fire_materializes_scheduled_session_and_auto_archives(
    setup, monkeypatch
):
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    runs = capture_model_runs(mgr, monkeypatch)

    await save_schedule(db,
        schedule_id="sch1",
        agent_id=agent["id"],
        name="daily",
        prompt="do it",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
    )

    await runner._fire("sch1")

    # A fresh session ran the prompt under the agent...
    assert len(runs) == 1
    assert runs[0].prompt.endswith("do it")
    fired_sid = runs[0].session_id
    original = [row for row in await db.load_messages(fired_sid) if row["role"] == "user"]
    assert len(original) == 1
    assert original[0]["content"].startswith("[scheduled:daily — Every 1m]")
    assert runs[0].prompt.endswith(original[0]["content"])
    rows = await db.load_sessions(include_archived=True)
    fired = next(r for r in rows if r["id"] == fired_sid)
    assert fired["agent_id"] == agent["id"]
    assert fired["origin"] == "schedule"

    # ...and auto-archived on idle: gone from the live map, archived in DB.
    assert mgr.get_session(fired_sid) is None
    assert fired["archived"] is True

    # last_run_at recorded.
    scheds = await db.load_schedules()
    assert scheds[0]["last_run_at"] is not None


@pytest.mark.asyncio
async def test_fire_creates_a_fresh_session_for_each_durable_schedule_run(setup, monkeypatch):
    """Each authorized fire creates its own session, without session reuse."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    created_sessions: list[str] = []
    orig_create = mgr.create_session

    async def tracking_create(agent_id, *args, **kwargs):
        sess = await orig_create(agent_id, *args, **kwargs)
        created_sessions.append(sess.id)
        return sess

    runs = capture_model_runs(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "create_session", tracking_create)

    await save_schedule(db,
        schedule_id="schX", agent_id=agent["id"], name="repeat", prompt="first",
        interval_seconds=3600, created_at="2026-10-17T00:00:00+00:00",
    )
    await runner._fire("schX")
    await db.update_schedule("schX", prompt="second")
    await runner._fire("schX")

    # Two fires → two distinct fresh sessions.
    assert len(created_sessions) == 2
    assert created_sessions[0] != created_sessions[1]
    assert [run.session_id for run in runs] == created_sessions
    assert runs[0].prompt.endswith("first") and runs[1].prompt.endswith("second")


@pytest.mark.asyncio
async def test_fire_appends_into_live_origin_session(setup, monkeypatch):
    """A schedule created from a `/schedule` chat command (origin_session_id set)
    appends each fire into that same, still-live session — queued via
    start_message — and does NOT create or archive a throwaway session."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    # The session the command was issued from (a normal user session).
    origin = await mgr.create_session(agent["id"], name="chat")

    started: dict = {}

    async def fake_start(session_id, prompt, *, source, knowledge_context):
        assert source == "schedule"
        started["session_id"] = session_id
        started["prompt"] = prompt
        return accepted_receipt()

    created: list[str] = []
    orig_create = mgr.create_session

    async def tracking_create(agent_id, *args, **kwargs):
        sess = await orig_create(agent_id, *args, **kwargs)
        created.append(sess.id)
        return sess

    monkeypatch.setattr(mgr, "start_message", fake_start)
    monkeypatch.setattr(mgr, "create_session", tracking_create)

    await save_schedule(db,
        schedule_id="sch1",
        agent_id=agent["id"],
        name="daily",
        prompt="summarize",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id=origin.id,
    )

    await runner._fire("sch1")

    # Ran in the origin session, not a fresh one.
    assert started["session_id"] == origin.id
    # …carrying the marker every machine-injected turn in Octopus carries, so
    # the agent can tell its 07:00 run from something the user typed.
    assert started["prompt"].startswith("[scheduled:daily — Every 1m]")
    assert started["prompt"].endswith("summarize")
    # The session the fire ran in is recorded, so "last run" can open it.
    assert (await db.load_schedules())[0]["last_run_session_id"] == origin.id
    assert created == []  # no throwaway session materialized
    # Origin session is left intact (it's a user session, never archived).
    assert mgr.get_session(origin.id) is not None
    # last_run_at recorded.
    assert (await db.load_schedules())[0]["last_run_at"] is not None


@pytest.mark.asyncio
async def test_fire_falls_back_to_fresh_session_when_origin_gone(setup, monkeypatch):
    """If the recorded origin session no longer exists (deleted/archived), the
    fire degrades to materializing a fresh schedule-origin session that
    auto-archives on idle — the schedule keeps working regardless."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    runs = capture_model_runs(mgr, monkeypatch)

    await save_schedule(db,
        schedule_id="sch2",
        agent_id=agent["id"],
        name="daily",
        prompt="do it",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id="ghost-session-that-was-deleted",
    )

    await runner._fire("sch2")

    # A fresh schedule-origin session ran the prompt and was auto-archived.
    assert len(runs) == 1
    fired_sid = runs[0].session_id
    assert fired_sid != "ghost-session-that-was-deleted"
    rows = await db.load_sessions(include_archived=True)
    fired = next(r for r in rows if r["id"] == fired_sid)
    assert fired["origin"] == "schedule"
    assert fired["archived"] is True
    assert mgr.get_session(fired_sid) is None


@pytest.mark.asyncio
async def test_add_job_captures_only_schedule_identity(setup):
    """The current durable row supplies the target and prompt when fired."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()
    await save_schedule(db,
        schedule_id="sch3",
        agent_id=agent["id"],
        name="n",
        prompt="p",
        interval_seconds=60,
        created_at="t",
        origin_session_id="origin-abc",
    )
    row = (await db.load_schedules())[0]
    await runner.add(row)

    job = runner._scheduler.get_job("sch3")
    assert job is not None
    assert job.args == (row["id"],)


@pytest.mark.asyncio
async def test_cron_schedule_registers_cron_trigger(setup):
    """A schedule with a cron expression registers an APScheduler CronTrigger
    in the agent's timezone (not an interval job)."""
    from apscheduler.triggers.cron import CronTrigger

    mgr, db, runner = setup
    agent = await db.get_system_agent()
    await save_schedule(db,
        schedule_id="cron1",
        agent_id=agent["id"],
        name="morning",
        prompt="summarize",
        created_at="2026-01-01T00:00:00+00:00",
        cron="0 9 * * *",
        timezone="America/Los_Angeles",
        recurrence_label="Every day at 9:00 AM",
    )
    row = (await db.load_schedules())[0]
    await runner.add(row)

    job = runner._scheduler.get_job("cron1")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)


@pytest.mark.asyncio
async def test_initialize_retains_and_registers_missed_run_at_schedules(setup):
    """Overdue one-shots retain their original fire until input admission."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    # A one-time schedule with a fire time in the distant past.
    await save_schedule(db,
        schedule_id="missed1",
        agent_id=agent["id"],
        name="missed",
        prompt="should have run already",
        created_at="2026-01-01T00:00:00+00:00",
        run_at="2020-01-01T09:00:00+00:00",
        recurrence_label="Once on Jan 1 2020",
    )
    # A normal interval schedule that should survive.
    await save_schedule(db,
        schedule_id="recurring1",
        agent_id=agent["id"],
        name="recurring",
        prompt="keep going",
        created_at="2026-01-01T00:00:00+00:00",
        interval_seconds=3600,
    )
    assert len(await db.load_schedules()) == 2

    await runner.initialize(paused=True)

    remaining = await db.load_schedules()
    assert {r["id"] for r in remaining} == {"missed1", "recurring1"}
    assert runner._scheduler.get_job("missed1") is not None
    assert runner._scheduler.get_job("recurring1") is not None


@pytest.mark.asyncio
async def test_run_at_schedule_registers_date_trigger(setup):
    """A schedule with run_at registers an APScheduler DateTrigger."""
    from apscheduler.triggers.date import DateTrigger

    mgr, db, runner = setup
    agent = await db.get_system_agent()
    run_at = "2030-01-15T09:00:00+00:00"
    await save_schedule(db,
        schedule_id="once1",
        agent_id=agent["id"],
        name="one-time",
        prompt="do it once",
        created_at="2026-01-01T00:00:00+00:00",
        run_at=run_at,
        recurrence_label="Once on Jan 15 at 9:00 AM",
    )
    row = (await db.load_schedules())[0]
    await runner.add(row)

    job = runner._scheduler.get_job("once1")
    assert job is not None
    assert isinstance(job.trigger, DateTrigger)
    # The durable run_at still controls once-only cleanup when this ID fires.
    assert job.args == ("once1",)


@pytest.mark.asyncio
async def test_run_at_schedule_deleted_after_fire(setup, monkeypatch):
    """After a one-time (run_at) schedule fires, its DB row is deleted."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    runs = capture_model_runs(mgr, monkeypatch)

    await save_schedule(db,
        schedule_id="once2",
        agent_id=agent["id"],
        name="one-time",
        prompt="do it once",
        created_at="2026-01-01T00:00:00+00:00",
        run_at="2030-01-15T09:00:00+00:00",
        recurrence_label="Once on Jan 15 at 9:00 AM",
    )
    assert len(await db.load_schedules()) == 1

    # Successful durable admission consumes the one-shot.
    await runner._fire("once2")

    # Row gone from DB.
    assert await db.load_schedules() == []
    assert len(runs) == 1 and runs[0].prompt.endswith("do it once")


@pytest.mark.asyncio
async def test_run_at_schedule_deleted_after_fire_with_origin_session(setup, monkeypatch):
    """One-time schedule fires into an origin session and is deleted from DB."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()
    origin = await mgr.create_session(agent["id"], name="chat")

    runs = capture_model_runs(mgr, monkeypatch)

    await save_schedule(db,
        schedule_id="once3",
        agent_id=agent["id"],
        name="one-time",
        prompt="remind me",
        created_at="2026-01-01T00:00:00+00:00",
        run_at="2030-01-15T09:00:00+00:00",
        recurrence_label="Once on Jan 15 at 9:00 AM",
        origin_session_id=origin.id,
    )
    assert len(await db.load_schedules()) == 1

    await runner._fire("once3")

    from tests.test_runtime_review_races import wait_dispatch_idle
    await wait_dispatch_idle(mgr, origin.id)
    assert [run.session_id for run in runs] == [origin.id]
    # DB row auto-deleted after the one-time fire.
    assert await db.load_schedules() == []


@pytest.mark.asyncio
async def test_repoint_schedules_origin():
    """repoint_schedules_origin moves only schedules anchored to the old session
    and returns those (post-update) rows; unrelated schedules stay put."""
    db = Database(":memory:")
    await db.initialize()
    await create_account(db)
    try:
        agent = await db.get_system_agent()
        await save_schedule(db,
            schedule_id="a",
            agent_id=agent["id"],
            name="anchored",
            prompt="p",
            created_at="t",
            interval_seconds=60,
            origin_session_id="old-sess",
        )
        await save_schedule(db,
            schedule_id="b",
            agent_id=agent["id"],
            name="other",
            prompt="p",
            created_at="t",
            interval_seconds=60,
            origin_session_id="someone-else",
        )

        # No match → empty, nothing changed.
        assert await db.repoint_schedules_origin("ghost", "new-sess") == []

        affected = await db.repoint_schedules_origin("old-sess", "new-sess")
        assert [r["id"] for r in affected] == ["a"]
        assert affected[0]["origin_session_id"] == "new-sess"

        rows = {r["id"]: r for r in await db.load_schedules()}
        assert rows["a"]["origin_session_id"] == "new-sess"
        assert rows["b"]["origin_session_id"] == "someone-else"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schedule_recurrence_columns_roundtrip():
    """cron / timezone / recurrence_label persist and load; interval is NULL
    for a cron schedule."""
    db = Database(":memory:")
    await db.initialize()
    await create_account(db)
    try:
        agent = await db.get_system_agent()
        await save_schedule(db,
            schedule_id="s1",
            agent_id=agent["id"],
            name="n",
            prompt="p",
            created_at="t",
            cron="*/15 * * * *",
            timezone="UTC",
            recurrence_label="Every 15 minutes",
        )
        row = (await db.load_schedules())[0]
        assert row["cron"] == "*/15 * * * *"
        assert row["timezone"] == "UTC"
        assert row["recurrence_label"] == "Every 15 minutes"
        assert row["interval_seconds"] is None
        assert row["run_at"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_run_at_column_roundtrip():
    """run_at persists and loads correctly; cron/interval stay NULL."""
    db = Database(":memory:")
    await db.initialize()
    await create_account(db)
    try:
        agent = await db.get_system_agent()
        run_at = "2030-01-15T09:00:00+00:00"
        await save_schedule(db,
            schedule_id="once-db",
            agent_id=agent["id"],
            name="one-time task",
            prompt="do it once",
            created_at="t",
            run_at=run_at,
            recurrence_label="Once on Jan 15 at 9:00 AM",
        )
        row = (await db.load_schedules())[0]
        assert row["run_at"] == run_at
        assert row["interval_seconds"] is None
        assert row["cron"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_saved_recurrence_definitions_survive_reopen(tmp_path):
    path = tmp_path / "recurrences.db"
    db = Database(str(path))
    await db.initialize()
    owner, _ = await create_account(db)
    agent = await db.get_system_agent(owner["id"])
    for identity, recurrence in (
        ("interval", {"interval_seconds": 120}),
        ("cron", {"cron": "0 6 * * *", "timezone": "UTC", "recurrence_label": "Every day at 6 AM"}),
        ("once", {"run_at": "2030-01-15T09:00:00+00:00", "recurrence_label": "Once"}),
    ):
        await save_schedule(db, schedule_id=identity, agent_id=agent["id"], name=identity,
                            prompt="Preserve this exact request", created_at="t", **recurrence)
    expected = await db.load_schedules()
    await db.close()
    for _ in range(2):
        db = Database(str(path))
        await db.initialize()
        try:
            assert await db.load_schedules() == expected
            interval = await db.get_schedule_definition("interval")
            assert interval["interval_seconds"] == 120 and interval["cron"] is None and interval["run_at"] is None
            assert (await db.get_agent(agent["id"]))["user_id"] == owner["id"]
        finally:
            await db.close()



# ---------------------------------------------------------------- overlap


@pytest.mark.asyncio
async def test_a_fire_is_skipped_while_the_previous_one_is_still_running(
    setup, monkeypatch
):
    """A schedule that runs every 5 minutes and takes 10 would otherwise stack
    runs on top of each other until the box gives out."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    runs = capture_model_runs(mgr, monkeypatch, hold=True)
    await save_schedule(db,
        schedule_id="busy1",
        agent_id=agent["id"],
        name="slow",
        prompt="do it",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
    )

    firing = asyncio.create_task(runner._fire("busy1"))
    resumed = None
    try:
        async with asyncio.timeout(2):
            while not runs:
                await asyncio.sleep(0)
        first_sid = (await db.load_schedules())[0]["last_run_session_id"]
        assert first_sid == runs[0].session_id
        assert mgr.session_is_executing(mgr.get_session(first_sid))
        await runner._fire("busy1")
        assert len(runs) == 1, "a second run started while the first was going"

        runs[0].release.set()
        await asyncio.wait_for(firing, 2)
        resumed = asyncio.create_task(runner._fire("busy1"))
        async with asyncio.timeout(2):
            while len(runs) != 2:
                await asyncio.sleep(0)
        runs[1].release.set()
        await asyncio.wait_for(resumed, 2)
        assert runs[1].session_id != first_sid
    finally:
        for run in runs:
            run.release.set()
        await asyncio.gather(*([firing, resumed] if resumed is not None else [firing]))


@pytest.mark.asyncio
async def test_a_gone_or_idle_previous_session_never_blocks(setup, monkeypatch):
    """The guard must not wedge a schedule: a deleted or finished session is
    not a reason to skip forever."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()

    runs = capture_model_runs(mgr, monkeypatch)
    await save_schedule(db,
        schedule_id="gone1",
        agent_id=agent["id"],
        name="n",
        prompt="p",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
    )
    await db.update_schedule("gone1", last_run_session_id="deleted-long-ago")

    await runner._fire("gone1")
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_a_fire_does_not_pile_up_behind_another_fire_in_a_chat(
    setup, monkeypatch
):
    """Origin-session mode queues behind the user's own turn — that's the
    design. Queuing behind a *previous fire* is a pile-up."""
    mgr, db, runner = setup
    agent = await db.get_system_agent()
    origin = await mgr.create_session(agent["id"], name="chat")

    runs = capture_model_runs(mgr, monkeypatch, hold=True)
    await save_schedule(db,
        schedule_id="chat1",
        agent_id=agent["id"],
        name="daily",
        prompt="summarize",
        interval_seconds=60,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id=origin.id,
    )

    human = await mgr.start_message(origin.id, "The user's original task")
    try:
        async with asyncio.timeout(2):
            while not runs:
                await asyncio.sleep(0)
        await runner._fire("chat1")
        first = mgr.dispatch_snapshot(origin.id)["items"]
        assert len(first) == 1 and first[0]["source"] == "schedule"
        assert first[0]["summary"].endswith("summarize")
        await runner._fire("chat1")
        assert mgr.dispatch_snapshot(origin.id)["items"] == first, "a second fire queued behind the first"
        assert len(runs) == 1

        assert await asyncio.wait_for(mgr.cancel_request(origin.id, first[0]["request_id"]), 1)
        await runner._fire("chat1")
        second = mgr.dispatch_snapshot(origin.id)["items"]
        assert len(second) == 1 and second[0]["source"] == "schedule"
        assert second[0]["request_id"] != first[0]["request_id"]
        assert await asyncio.wait_for(mgr.cancel_request(origin.id, second[0]["request_id"]), 1)
    finally:
        await mgr.interrupt(origin.id)
        await asyncio.wait_for(human.wait(), 2)
        await mgr.await_session_execution(origin.id, timeout=2)


async def test_fresh_fire_is_admitted_and_owned_by_dispatch(setup, monkeypatch):
    mgr, db, runner = setup
    agent = await db.get_system_agent()
    entered = asyncio.Event()
    captured = {}

    async def observe(event):
        if event.get("type") != "attempt_started":
            return
        captured.update(session_id=event["session_id"], request_id=event["request_id"], attempt_id=event["attempt_id"])
        entered.set()

    runs = capture_model_runs(mgr, monkeypatch, hold=True)
    mgr.on_broadcast("owned-schedule", observe)
    await save_schedule(db,
        schedule_id="owned-schedule", agent_id=agent["id"], name="owned",
        prompt="scheduled work", interval_seconds=3600,
        created_at="2026-10-17T00:00:00+00:00",
    )
    firing = asyncio.create_task(runner._fire("owned-schedule"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        original = [row for row in await db.load_messages(captured["session_id"]) if row["role"] == "user"]
        assert len(original) == 1
        context = original[0]["tool_input"]["request_context"]
        assert context["source"] == "schedule" and context["request_id"] == captured["request_id"]
        assert captured["attempt_id"]
        assert mgr.running_turns_for(agent["user_id"]) == 1
        await mgr.drain_turns()
        await asyncio.wait_for(firing, 2)
        assert mgr.running_turns_for(agent["user_id"]) == 0
        assert len(runs) == 1
        outcomes = [row["content"] for row in await db.load_messages(captured["session_id"]) if row["type"] == "result"]
        assert len(outcomes) == 1 and outcomes[0]["request_id"] == captured["request_id"]
        assert outcomes[0]["status"] == "interrupted" and outcomes[0]["code"] == "service_shutdown"
    finally:
        for run in runs:
            run.release.set()
        await asyncio.wait_for(firing, 2)
        mgr.remove_broadcast("owned-schedule")
