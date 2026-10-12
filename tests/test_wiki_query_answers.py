"""Counterexamples for the real-query answer gate; no model is invoked here."""

import pytest

from tests.test_wiki_query_real import states_career_is_unrecorded


@pytest.mark.parametrize("answer,accepted", [
    ("你的个人资料没有明确记录职业，只记载大学学习。来源 Me 第3行。", True),
    ("职业没有明确记载，不能从学籍推断。", True),
    ("资料未记录职业，无法确定。", True),
    ("我没有你的完整履历，但职业明确是软件工程师。来源 Me 第3行。", False),
    ("你在大学学习，所以职业是学生。", False),
])
def test_unrecorded_career_gate_rejects_affirmative_inference(answer, accepted):
    assert states_career_is_unrecorded(answer) is accepted
