"""Question answer validation and trusted, serializable source evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


class QuestionAnswerInvalid(ValueError):
    """An ordinary input error; the pending question remains answerable."""


@dataclass(frozen=True)
class AnswerSource:
    kind: Literal["account", "agent", "system", "unknown"] = "unknown"
    user_id: str | None = None
    session_id: str | None = None
    work_id: str | None = None
    run_lease_id: str | None = None
    delegation_id: str | None = None
    delegation_work_id: str | None = None
    parent_session_id: str | None = None

    def details(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


UNKNOWN_SOURCE = AnswerSource()
SYSTEM_SOURCE = AnswerSource("system")


def validate_answers(questions: list[dict[str, Any]], answers: Any) -> list[dict[str, Any]]:
    """Pure validation against the saved form, before disposition is claimed."""
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise QuestionAnswerInvalid("Provide exactly one answer for every question")
    validated = []
    for index, (question, answer) in enumerate(zip(questions, answers, strict=True)):
        prefix = f"Question {index + 1}: "
        if not isinstance(answer, dict):
            raise QuestionAnswerInvalid(prefix + "answer must be an object")
        selected = answer.get("selected")
        selected = [] if selected is None else selected
        text = answer.get("text")
        if (not isinstance(selected, list) or any(not isinstance(label, str) for label in selected)
                or text is not None and not isinstance(text, str)):
            raise QuestionAnswerInvalid(prefix + "use selected labels or free text")
        has_text = bool(text and text.strip())
        if bool(selected) == has_text:
            raise QuestionAnswerInvalid(prefix + "provide a nonempty selection or free text, using only one")
        options = question.get("options") or []
        if not isinstance(options, list):
            raise QuestionAnswerInvalid(prefix + "saved options must be a list")
        labels = {label for option in options if isinstance(option, dict) and isinstance(label := option.get("label"), str)}
        if (len(set(selected)) != len(selected) or any(label not in labels for label in selected)
                or len(selected) > 1 and not question.get("multiSelect")):
            raise QuestionAnswerInvalid(prefix + "selected labels do not match the question's options and selection mode")
        validated.append({"selected": list(selected), "text": text if has_text else None})
    return validated


def structured_answers(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index, (question, answer) in enumerate(zip(questions, answers, strict=True)):
        selected = []
        for label in answer["selected"]:
            matches = [position for position, option in enumerate(question.get("options") or [])
                       if isinstance(option, dict) and option.get("label") == label]
            option: dict[str, Any] = {"label": label}
            # Repeated form labels do not identify a unique option position.
            if len(matches) == 1:
                option["option_index"] = matches[0]
            selected.append(option)
        result.append({"question_index": index, "selected": selected, "text": answer["text"]})
    return result


def format_answers(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"Q: {question.get('question', '')}\nA: " + (answer["text"] or ", ".join(answer["selected"]))
        for question, answer in zip(questions, answers, strict=True))
