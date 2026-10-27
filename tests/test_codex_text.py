"""The Codex one-shot model boundary has no executable tool surface."""

import base64
import json

import httpx
import pytest

from server.harness import codex_text


def native_token(label, account="synthetic-account"):
    payload = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account}}).encode()).decode().rstrip("=")
    return f"{label}.{payload}.synthetic-signature"


def test_unbound_native_keystore_needs_no_auth_file_or_file_export(tmp_path, monkeypatch):
    requests = []
    token = native_token("keyring-only")

    async def native(binary, home, method, params):
        requests.append((binary, home, method, params))
        return {"authMethod": "chatgpt", "authToken": token}

    monkeypatch.setattr(codex_text, "native", native)
    assert codex_text.credentials("synthetic-codex", tmp_path) == (token, "synthetic-account")
    assert requests == [("synthetic-codex", tmp_path, "getAuthStatus", {"includeToken": True, "refreshToken": False})]
    assert list(tmp_path.iterdir()) == []


def stream(*events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()


def test_text_request_contains_only_user_input_and_no_tools():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=stream({"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": '{"answer":42}'}]}]}}))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        text = codex_text.complete(client, token="synthetic-token", account="synthetic-account",
                                   prompt="Parse the answer", model="synthetic-model", effort="low", tier="priority")
    assert text == '{"answer":42}'
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["tools"] == [] and payload["tool_choice"] == "none"
    assert payload["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Parse the answer"}]}]
    assert payload["model"] == "synthetic-model" and payload["reasoning"] == {"effort": "low"}
    assert payload["service_tier"] == "priority"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-token"
    assert requests[0].headers["ChatGPT-Account-Id"] == "synthetic-account"


@pytest.mark.parametrize("terminal", [
    {"type": "response.failed"},
    {"type": "response.incomplete"},
    {"type": "error"},
    {"type": "response.completed", "response": {"status": "failed", "output": []}},
    {"type": "response.output_item.done", "item": {"type": "function_call", "name": "exec_command", "arguments": "ignored"}},
])
def test_partial_text_or_tool_requests_cannot_become_success(terminal):
    final = {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "pretend success"}]}]}}
    data = stream({"type": "response.output_text.delta", "delta": "partial text"}, terminal, final)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))) as client:
        with pytest.raises(ValueError):
            codex_text.complete(client, token="synthetic", account="synthetic", prompt="x", model="synthetic")


def test_stream_without_completed_response_rejects_partial_text():
    data = stream({"type": "response.output_text.delta", "delta": "partial text"})
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))) as client:
        with pytest.raises(ValueError, match="before completion"):
            codex_text.complete(client, token="synthetic", account="synthetic", prompt="x", model="synthetic")


def test_text_completion_accepts_multiline_sse_data_and_heartbeats():
    data = b': heartbeat\r\nevent: response.completed\r\ndata:{"type":"response.completed",\r\ndata: "response":{"status":"completed","output":[{"type":"message","role":"assistant",\r\ndata: "content":[{"type":"output_text","text":"complete"}]}]}}\r\n\r\n'
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))) as client:
        assert codex_text.complete(client, token="synthetic", account=None, prompt="x", model="synthetic") == "complete"


def test_native_opaque_subscription_token_does_not_invent_an_account_header(tmp_path, monkeypatch):
    async def native(*args):
        return {"authMethod": "chatgpt", "authToken": "opaque-synthetic-token"}

    monkeypatch.setattr(codex_text, "native", native)
    token, account = codex_text.credentials("synthetic-codex", tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=stream({"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "legacy login"}]}]}}))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert codex_text.complete(client, token=token, account=account, prompt="x", model="synthetic") == "legacy login"
    assert requests[0].headers["Authorization"] == "Bearer opaque-synthetic-token"
    assert "ChatGPT-Account-Id" not in requests[0].headers


def test_expired_login_is_refreshed_by_native_codex_before_retry(tmp_path, monkeypatch):
    calls, authorization = [], []
    current = {"token": "old-synthetic-token"}

    async def native(binary, home, method, params):
        calls.append((binary, home, method, params))
        if params["refreshToken"]:
            current["token"] = "new-synthetic-token"
        return {"authMethod": "chatgpt", "authToken": current["token"]}

    def respond(request):
        authorization.append(request.headers["Authorization"])
        if len(authorization) == 1:
            return httpx.Response(401)
        return httpx.Response(200, content=stream({"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "renewed"}]}]}}))

    monkeypatch.setattr(codex_text, "native", native)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = codex_text.resolve_and_complete(client, home=tmp_path, binary="synthetic-codex", prompt="x", model="synthetic")
    assert result == "renewed"
    assert authorization == ["Bearer old-synthetic-token", "Bearer new-synthetic-token"]
    assert calls == [("synthetic-codex", tmp_path, "getAuthStatus", {"includeToken": True, "refreshToken": flag})
                     for flag in (False, False, True)]
    assert current["token"] == "new-synthetic-token"
    assert list(tmp_path.iterdir()) == []


def test_missing_options_use_native_effective_project_config_and_catalog(tmp_path, monkeypatch):
    requests = []

    async def native(binary, home, method, params):
        requests.append((method, params))
        if method == "config/read":
            return {"config": {"model": "project-model", "model_reasoning_effort": None}}
        return {"data": [{"model": "project-model", "isDefault": False, "defaultReasoningEffort": "high"},
                         {"model": "other-model", "isDefault": True, "defaultReasoningEffort": "low"}], "nextCursor": None}

    monkeypatch.setattr(codex_text, "native", native)
    model, effort = codex_text.effective_options("synthetic", tmp_path, str(tmp_path / "project"), None, None)
    assert (model, effort) == ("project-model", "high")
    assert requests[0] == ("config/read", {"includeLayers": False, "cwd": str(tmp_path / "project")})


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_failures_have_one_bounded_native_refresh_and_no_general_retry(tmp_path, monkeypatch, status):
    requests, refreshed = [], []

    async def native(binary, home, method, params):
        refreshed.append(params["refreshToken"])
        return {"authMethod": "chatgpt", "authToken": "synthetic-token"}

    def respond(request):
        requests.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(codex_text, "native", native)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            codex_text.resolve_and_complete(client, home=tmp_path, binary="synthetic", prompt="x", model="synthetic")
    assert error.value.response.status_code == status
    assert len(requests) == (2 if status == 401 else 1)
    assert refreshed == ([False, False, True] if status == 401 else [False])


def test_concurrent_reauthorization_takes_precedence_without_another_refresh(tmp_path, monkeypatch):
    authorization = []
    current = {"token": "old-token"}

    async def native(binary, home, method, params):
        assert not params["refreshToken"], "The captured login was already replaced"
        return {"authMethod": "chatgpt", "authToken": current["token"]}

    def respond(request):
        authorization.append(request.headers["Authorization"])
        if len(authorization) == 1:
            current["token"] = "reauthorized-token"
            return httpx.Response(401)
        return httpx.Response(200, content=stream({"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "current login"}]}]}}))

    monkeypatch.setattr(codex_text, "native", native)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert codex_text.resolve_and_complete(client, home=tmp_path, binary="synthetic", prompt="x", model="synthetic") == "current login"
    assert authorization == ["Bearer old-token", "Bearer reauthorized-token"]


@pytest.mark.parametrize("supplied,configured,expected", [
    ((None, None), {}, ("catalog-default", "low")),
    (("explicit", None), {"model": "project-model", "model_reasoning_effort": "medium"}, ("explicit", "medium")),
    ((None, "high"), {"model": "project-model", "model_reasoning_effort": "low"}, ("project-model", "high")),
    (("explicit", "low"), {"model": "project-model"}, ("explicit", "low")),
])
def test_option_precedence_and_native_default(tmp_path, monkeypatch, supplied, configured, expected):
    async def native(binary, home, method, params):
        if method == "config/read":
            return {"config": configured}
        return {"data": [{"model": "catalog-default", "isDefault": True, "defaultReasoningEffort": "low"}], "nextCursor": None}

    monkeypatch.setattr(codex_text, "native", native)
    assert codex_text.effective_options("synthetic", tmp_path, str(tmp_path), *supplied) == expected


@pytest.mark.parametrize("data", [None, [], {}, {"authMethod": "apikey", "authToken": "synthetic"},
                                  {"authMethod": "chatgpt", "authToken": ""}, {"authMethod": "chatgpt", "authToken": []}])
def test_invalid_or_api_key_login_cannot_start_subscription_request(tmp_path, monkeypatch, data):
    async def native(*args):
        return data

    monkeypatch.setattr(codex_text, "native", native)
    with pytest.raises(ValueError):
        codex_text.credentials("synthetic-codex", tmp_path)
