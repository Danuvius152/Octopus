"""Use the managed installation's natural-idle handoff, then restore its service."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

from .config import Settings
from .service_installation import read_service_profile


def _request(url: str, *, token: str = "", payload: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, json.load(response)


@contextmanager
def managed_source(config: Settings, *, token: str = "", timeout: float = 1800):
    """Wait without mutation, seal through the service owner, stop, copy, restart.

Uncertain acquisition is reported as such; it is never treated as an idle or
stopped source. Once the service confirms this request's handoff, every exit
restores that service. Export releases its offline lock before this exit.
"""
    profile = read_service_profile(config)
    if profile is None:
        yield
        return
    command = ["systemctl", *(["--user"] if profile.manager == "user" else [])]
    result = subprocess.run([*command, "show", "--no-pager", "--property=ActiveState,MainPID,Job", "--", profile.unit],
                            check=True, capture_output=True, text=True, timeout=10)
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if state.get("ActiveState") in {"inactive", "failed"} and state.get("MainPID") == "0" and not state.get("Job"):
        yield
        return
    if not token:
        raise ValueError("Exporting a running managed installation requires an administrator bearer")
    base = f"http://127.0.0.1:{profile.port}"
    request_id = str(uuid.uuid4())
    deadline = time.monotonic() + timeout
    acquired = False
    uncertain = False
    try:
        while not acquired:
            uncertain = True
            try:
                status, body = _request(base + "/api/installation/quiesce", token=token, payload={"request_id": request_id})
            except (OSError, ValueError) as error:
                try:
                    _status, readiness = _request(base + "/ready")
                except (OSError, ValueError) as inspection:
                    raise RuntimeError("Source handoff outcome is unknown; inspect the managed service before retrying") from inspection
                acquired = _status == 503 and readiness.get("status") == "stopping" and readiness.get("offline_request") == request_id
                if not acquired:
                    raise RuntimeError("Source handoff outcome is unknown; the original request may still seal admission; inspect the managed service") from error
            else:
                if status in {400, 401, 403}:
                    uncertain = False
                    raise RuntimeError(f"Source handoff refused (HTTP {status}): {body.get('detail', 'invalid request')}")
                acquired = status == 200 and body.get("request_id") == request_id and body.get("status") == "stopping"
                if not acquired:
                    if status != 409 or body.get("request_id") or "natural completion" not in body.get("detail", ""):
                        raise RuntimeError(f"Source handoff refused (HTTP {status}): {body.get('detail', 'invalid receipt')}")
                    uncertain = False  # This request completed with an explicit busy refusal.
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Source still has work; no work was interrupted and no archive was created")
                    time.sleep(0.2)
        subprocess.run([*command, "stop", "--", profile.unit], check=True, timeout=60)
        yield
    finally:
        # Cancellation can arrive after the HTTP response but before receipt
        # parsing records ownership. Cover the entire acquisition phase.
        if uncertain and not acquired:
            try:
                _status, readiness = _request(base + "/ready")
            except (OSError, ValueError) as error:
                raise RuntimeError("Source handoff outcome is unknown; inspect the managed service before retrying") from error
            acquired = _status == 503 and readiness.get("status") == "stopping" and readiness.get("offline_request") == request_id
            if not acquired:
                raise RuntimeError("Source handoff outcome is unknown; the original request may still seal admission; inspect the managed service")
        if acquired:
            try:
                subprocess.run([*command, "restart", "--", profile.unit], check=True, timeout=60)
                deadline = time.monotonic() + 60
                while True:
                    try:
                        status, body = _request(base + "/ready")
                    except (OSError, ValueError):
                        status, body = 0, {}
                    if status == 200 and body.get("status") == "ready":
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Source service did not become ready")
                    time.sleep(0.2)
            except BaseException as error:
                raise RuntimeError("Source resumption failed; inspect its managed service") from error
