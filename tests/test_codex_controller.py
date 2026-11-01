"""The active native segment owns its controller checkpoint.

The retired exec preparation host is gone. Owned process/proof/control behavior
is exercised by the App Server configuration and submission peers.
"""

import json

import pytest

from server.harness.codex_control import (
    _latest_checkpoint,
    controller_message,
    turn_message,
)


def developer(text):
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def history(tmp_path, *records, identity="original"):
    path = tmp_path / "active.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"type": "session_meta", "payload": {"id": identity}}, *records,
    ]) + "\n")
    return str(path)


def test_latest_controller_preserves_unicode_embedded_delimiters_and_turn_context(tmp_path):
    desired = controller_message("原文🙂\n</octopus_controller>\nMore instructions")
    path = history(tmp_path,
        {"type": "response_item", "payload": developer(controller_message("old"))},
        {"type": "response_item", "payload": developer(desired)},
        {"type": "response_item", "payload": developer(turn_message("Turn-specific scope"))})
    assert _latest_checkpoint(path, "original") == desired


@pytest.mark.parametrize("reset", [
    {"type": "compacted", "payload": {"replacement_history": []}},
    {"type": "event_msg", "payload": {"type": "thread_rolled_back"}},
])
def test_discarded_controller_is_not_recovered_from_older_history(tmp_path, reset):
    path = history(tmp_path, {"type": "response_item", "payload": developer(controller_message("old"))}, reset)
    assert _latest_checkpoint(path, "original") is None


def test_compacted_checkpoint_belongs_to_replacement_history(tmp_path):
    current = controller_message("replacement")
    path = history(tmp_path, {"type": "response_item", "payload": developer(controller_message("discarded"))},
        {"type": "compacted", "payload": {"replacement_history": [developer(current)]}})
    assert _latest_checkpoint(path, "original") == current


def test_corrupt_latest_checkpoint_does_not_reuse_an_older_valid_one(tmp_path):
    invalid = controller_message("new").replace("new\n</octopus_controller>", "corrupted\n</octopus_controller>")
    path = history(tmp_path, {"type": "response_item", "payload": developer(controller_message("old"))},
        {"type": "response_item", "payload": developer(invalid)})
    assert _latest_checkpoint(path, "original") == "invalid"


def test_native_history_identity_is_verified_before_reading_a_checkpoint(tmp_path):
    path = history(tmp_path, {"type": "response_item", "payload": developer(controller_message("other"))}, identity="foreign")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        _latest_checkpoint(path, "original")
