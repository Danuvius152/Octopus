"""Request knowledge through real dispatch, storage and the backend seam."""

from server.harness import HarnessEvent, get_harness
from server.harness.assembly import compose_system_prompt
from server.wiki.tasks import tasks
from server.wiki.workflow import view
from tests import test_wiki_api as wiki_api
from tests.fake_run import FakeRunBase

client = wiki_api.client


async def test_explicit_wiki_source_contract_reaches_the_actual_backend_input(client, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    await wiki_api.configure(client, vault)
    manager = wiki_api.session_manager
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Source requirements", str(tmp_path), backend="codex")
    observed = []

    def create(config):
        class Run(FakeRunBase):
            async def start(self, prompt, *args, **kwargs):
                task = tasks.active[config.wiki_task_id]
                observed.append((view(task).card, compose_system_prompt(
                    config.system_prompt, "", [], wiki_mode=config.wiki_mode,
                    wiki_task_id=config.wiki_task_id, wiki_capability=True,
                ), config.turn_instructions))

            def set_idle_handler(self, handler):
                pass

            async def stream(self):
                yield HarnessEvent(type="result", is_error=False)

            async def stop(self):
                pass

            async def interrupt(self):
                pass

        return Run()

    monkeypatch.setattr(get_harness("codex"), "create_run", create)
    receipt = await manager.start_message(session.id, "/wiki Compare X and Y using relevant evidence.")
    completed = await receipt.wait()

    assert completed.outcome.status == "success"
    card, system, turn = observed[0]
    assert card["source_requirements"] == ["wiki", "web"]
    assert '"wiki", "web"' in turn
    assert "本轮知识来源要求" not in system


async def test_unconfirmed_request_input_never_opens_wiki_or_starts_the_backend(client, tmp_path, monkeypatch):
    manager = wiki_api.session_manager
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Input acknowledgement", str(tmp_path), backend="codex")
    started = []

    async def unconfirmed(**message):
        raise OSError("request commit could not be independently confirmed")

    def create(config):
        started.append(config)
        raise AssertionError("Unconfirmed input reached the backend")

    monkeypatch.setattr(manager.db, "record_request", unconfirmed)
    monkeypatch.setattr(get_harness("codex"), "create_run", create)
    receipt = await manager.start_message(session.id, "/wiki Explain the source constraint")
    completion = await receipt.wait()
    assert completion.outcome.status == "failed"
    assert started == [] and session._knowledge_context is None and session._wiki_task_id is None
    assert not any(task.session_id == session.id for task in tasks.active.values())
