"""Deterministic JSONL producer; no tools, network, credentials or model.

Only the pure-UI test entrypoint uses this program. It exercises the real
HarnessRun readers, session turn engine, WS broadcasts and durable messages.
"""

import json

for event in [
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Offline UI execution complete."},
    ]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "duration_ms": 1, "num_turns": 1, "total_cost_usd": 0},
]:
    print(json.dumps(event), flush=True)
