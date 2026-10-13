"""Counterexamples for the real-query answer gate; no model is invoked here."""

import pytest

from tests.test_wiki_query_real import states_career_is_unrecorded


@pytest.mark.parametrize("answer,accepted", [
    ("你的个人资料没有明确记录职业，只记载大学学习。来源 Me 第3行。", True),
    ("职业没有明确记载，不能从学籍推断。", True),
    ("资料未记录职业，无法确定。", True),
    (
        "我先查一下你个人资料里对职业的记录。\n"
        "目前个人资料里**没有记载你的职业**，我还不能确定。\n\n"
        "个人资料原文 `Library/Me/information.md` 第 3 行记载："
        "你在青峦大学学习，方向是合成视觉推理。"
        "这是学习背景，不能据此推断你的职业。",
        True,
    ),
    ("职业未记载。", True),
    ("你的职业没有记录。", True),
    ("个人资料没有记载你的职业。", True),
    ("目前无法确定你的职业。", True),
    ("职业：未记录。", True),
    ("个人资料没有职业记录。", True),
    ("个人资料没有职业记载。", True),
    ("我没有你的完整履历，但职业明确是软件工程师。来源 Me 第3行。", False),
    ("你在大学学习，所以职业是学生。", False),
    ("学历未记录；你的职业是工程师。", False),
    ("你的职业是工程师，学历没有记录。", False),
    ("你的履历未记载，但你的职业明确是工程师。", False),
    ("你的职业背景只提到学历未记录。", False),
    ("职业资料的学历未记载。", False),
])
def test_unrecorded_career_gate_rejects_affirmative_inference(answer, accepted):
    assert states_career_is_unrecorded(answer) is accepted
