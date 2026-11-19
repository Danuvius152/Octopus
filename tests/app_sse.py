"""Read the application event carried by one complete SSE frame."""

import json


def app_sse_event(frame: bytes) -> dict:
    return json.loads(frame.split(b"\ndata: ", 1)[1])
