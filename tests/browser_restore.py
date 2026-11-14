"""Issue one explicit browser restoration through its public identity protocol."""

import uuid

from httpx import AsyncClient, Response


async def restore_session(client: AsyncClient, session_id: str, *, headers=None) -> Response:
    path = f"/api/sessions/{session_id}"
    status = await client.get(path + "/restoration", headers=headers)
    assert status.status_code == 200, status.text
    return await client.post(path + "/unarchive", headers=headers, json={
        "operation_id": uuid.uuid4().hex, "service_generation": status.json()["service_generation"],
    })
