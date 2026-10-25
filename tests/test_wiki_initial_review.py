"""The saved checked observation determines first and incremental worker review."""

from server.wiki.history import WikiHistory
from tests.test_wiki_jobs import Curator, process
from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import prepare, state


async def test_worker_distinguishes_first_incremental_and_specific_review(client, tmp_path):
    _, _, jobs = await prepare(client, tmp_path, {
        "Library/note.md": "# Note\n\nA claim with its stated condition.\n",
    })
    prompts = []

    class Observed(Curator):
        async def start_message(self, session_id, prompt, **kwargs):
            prompts.append(prompt)
            return await super().start_message(session_id, prompt, **kwargs)

    curator = Observed(jobs)
    first = (await state(client, "Library/note.md"))["items"][0]["review"]
    assert first["baseline_receipt_id"] is None
    job = jobs.maintain(first["review_key"], "", "", [first["path"]], None, [], "human", review=first)
    original_request = job["payload"]["request"]
    finished = await process(jobs, job, curator)
    assert finished["status"] == "completed"
    assert "首次复核（无已接受基线）" in prompts[-1]
    assert "执行 worker 指南中的首次复核检查" in prompts[-1]
    assert "增量复核（已有接受基线）" not in prompts[-1]
    assert finished["payload"]["request"] == original_request

    current = jobs.store.read(first["doc"])
    WikiHistory(jobs.store).write(
        current["path"], (jobs.store.path(current["path"]).read_text() + "\nA later human condition.\n").encode(),
        current["revision"], "human", "Add current evidence without deleting the previous summary",
    )
    changed = (await state(client, first["doc"]))["items"][0]["review"]
    assert changed["baseline_receipt_id"] is not None
    incremental = jobs.maintain(changed["review_key"], "", "", [changed["path"]], None, [], "human", review=changed)
    assert (await process(jobs, incremental, curator))["status"] == "completed"
    assert "增量复核（已有接受基线）" in prompts[-1]
    assert "首次复核（无已接受基线）" not in prompts[-1]

    specific = jobs.maintain("specific-review", "", "Check the explicitly selected wording only",
                             [changed["path"]], None, [], "human")
    assert "review" not in specific["payload"]["request"]
    assert (await process(jobs, specific, curator))["status"] == "completed"
    assert "Check the explicitly selected wording only" in prompts[-1]
    assert "日常复核阶段:" not in prompts[-1]
    assert all("正文与新页仅限这些目标" in prompt and "relations-only" in prompt for prompt in prompts)
