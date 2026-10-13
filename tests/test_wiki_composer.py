"""Composer feedback and cancellation must agree with the trusted WS boundary."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocketDisconnect

from server.deps import Principal
from server.routers import ws as ws_router
from server.wiki.tasks import explicit_mode

CASES = json.loads(
    (Path(__file__).parent / "fixtures/wiki-command-cases.json").read_text()
)


@pytest.mark.parametrize("case", CASES)
def test_wiki_command_cases(case):
    assert explicit_mode(case["message"]) == case["mode"]


@pytest.mark.parametrize("case", CASES)
def test_explicit_cancellation_overrides_all_command_text(case):
    assert explicit_mode(case["message"], "off") is None


async def test_websocket_cancelled_wiki_command_never_opens_task(monkeypatch):
    monkeypatch.setattr(
        ws_router.deps, "resolve_principal", AsyncMock(return_value=Principal(None, "legacy"))
    )
    manager = SimpleNamespace(
        on_broadcast=Mock(), remove_broadcast=Mock(),
        get_session=Mock(return_value=SimpleNamespace(user_id=None)),
        start_message=AsyncMock(),
    )
    message = "/wiki /wiki maintain 的用法是什么？"
    socket = SimpleNamespace(
        accept=AsyncMock(), send_json=AsyncMock(),
        receive_text=AsyncMock(side_effect=[
            json.dumps({
                "type": "send_message", "session_id": "session",
                "content": message, "wiki_mode": "off",
            }),
            WebSocketDisconnect(),
        ]),
    )
    await ws_router.websocket_endpoint(manager, socket, token="token")
    manager.start_message.assert_awaited_once_with(
        "session", message, attachment_ids=[], steerable=True
    )
