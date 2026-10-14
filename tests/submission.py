"""Typed receipt stand-in for tests of consumers, without a model task."""

import asyncio
import uuid

from server.models import TurnOutcome
from server.sessions.submission import RequestCompletion, SubmissionReceipt


def accepted_receipt(request_id=None):
    request_id = request_id or uuid.uuid4().hex
    future = asyncio.get_running_loop().create_future()
    future.set_result(RequestCompletion(
        input_request_id=request_id, request_id=request_id,
        state="completed", outcome=TurnOutcome(request_id=request_id, status="success"),
        recording_state="committed",
    ))
    return SubmissionReceipt("accepted", request_id, future)


async def receipt_from_events(events):
    outcome = None
    async for event in events:
        if event.get("type") == "result":
            request_id = uuid.uuid4().hex
            outcome = TurnOutcome(request_id=request_id, status="failed" if event.get("is_error") else "success", message=event.get("message"))
    receipt = accepted_receipt()
    if outcome is not None:
        future = asyncio.get_running_loop().create_future()
        future.set_result(RequestCompletion(input_request_id=outcome.request_id, request_id=outcome.request_id,
                                           state="completed", outcome=outcome, recording_state="committed"))
        return SubmissionReceipt("accepted", outcome.request_id, future)
    return receipt
