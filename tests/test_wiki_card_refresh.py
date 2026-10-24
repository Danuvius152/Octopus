"""Refresh one original card across pagination and historical vaults."""

import json

from tests import test_wiki_api as api
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_job_notifications import pending, subscribe
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


async def test_exact_card_query_reaches_old_vault_and_job_beyond_first_page(jobs, client, tmp_path):
    manager = api.session_manager
    entry = await manager.create_session(jobs.registration["curator_agent_id"], "Original", str(tmp_path))
    job = enqueue(jobs)
    subscribe(jobs, job, entry.id)
    original = pending(jobs)[0]
    for number in range(32):
        identifier = f"job-{number}"
        body = json.loads(original["body"]) | {"job_id": identifier}
        delivery = original | {"event_id": f"event-{number}", "job_id": identifier, "body": json.dumps(body)}
        await manager.db.receive_wiki_job_notice("old-vault" if number == 0 else "current-vault", jobs.user_id, delivery)
    path = f"/api/sessions/{entry.id}/wiki-jobs"
    page = await client.get(path, headers=client.headers)
    assert page.status_code == 200 and len(page.json()) == 30
    assert all(card["job_id"] != "job-0" for card in page.json())
    exact = await client.get(path, headers=client.headers, params={"job_id": "job-0", "vault_id": "old-vault"})
    assert exact.status_code == 200, exact.text
    assert [(card["vault_id"], card["job_id"]) for card in exact.json()] == [("old-vault", "job-0")]
    wrong = await client.get(path, headers=client.headers, params={"job_id": "job-0", "vault_id": "current-vault"})
    assert wrong.status_code == 200 and wrong.json() == []
    for partial in ({"job_id": "job-0"}, {"vault_id": "old-vault"}):
        assert (await client.get(path, headers=client.headers, params=partial)).status_code == 422
