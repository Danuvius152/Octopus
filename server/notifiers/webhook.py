"""Webhook notifier — POST JSON to a URL.

Each delivery uses a five-second HTTP client timeout and reports its outcome
with the notifier ID. Endpoint URLs and raw transport errors are private.
"""

from __future__ import annotations

import logging

import httpx

from ..transport_logging import suppress_http_client_logs
from .base import NotifierBase, NotifierEvent

logger = logging.getLogger(__name__)


def _delivery_error_category(error: Exception) -> str:
    if isinstance(error, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return "invalid_url"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.NetworkError):
        return "network_error"
    if isinstance(error, httpx.ProtocolError):
        return "protocol_error"
    if isinstance(error, httpx.RequestError):
        return "transport_error"
    return "send_error"


class WebhookNotifier(NotifierBase):
    type = "webhook"

    TIMEOUT_SECONDS = 5.0

    async def send(self, event: NotifierEvent) -> None:
        received_status: int | None = None

        async def remember_status(response: httpx.Response) -> None:
            nonlocal received_status
            # HTTPX runs response hooks before redirect handling and body reads.
            received_status = response.status_code

        try:
            url = self.config.get("url") or ""
            if not isinstance(url, str):
                raise httpx.InvalidURL("Webhook URL must be a string")
            url = url.strip()
            if not url:
                logger.warning(
                    "Webhook %s event=%s method=POST error=missing_url has no URL configured",
                    self.id, event.type,
                )
                return

            with suppress_http_client_logs():
                async with httpx.AsyncClient(
                    timeout=self.TIMEOUT_SECONDS,
                    event_hooks={"response": [remember_status]},
                ) as client:
                    response = await client.post(url, json=event.to_payload())
        except Exception as error:
            logger.warning(
                "Webhook %s event=%s method=POST status=%s error=%s",
                self.id, event.type, received_status, _delivery_error_category(error),
            )
            return
        if response.status_code >= 400:
            logger.warning(
                "Webhook %s event=%s method=POST status=%d error=http_status",
                self.id, event.type, response.status_code,
            )
        else:
            logger.info(
                "Webhook %s event=%s method=POST status=%d",
                self.id, event.type, response.status_code,
            )
