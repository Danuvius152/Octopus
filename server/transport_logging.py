"""Keep request queries out of Uvicorn's transport logs."""

import logging

_REQUEST_TARGET_ARGUMENT = {
    ("uvicorn.access", '%s - "%s %s HTTP/%s" %d'): 2,
    ("uvicorn.error", '%s - "WebSocket %s" [accepted]'): 1,
    ("uvicorn.error", '%s - "WebSocket %s" 403'): 1,
    ("uvicorn.error", '%s - "WebSocket %s" %d'): 1,
    # Debug handshake request lines from the Sans-I/O and legacy WebSocket drivers.
    ("uvicorn.error", "< %s %s %s"): 1,
    ("uvicorn.error", "< GET %s HTTP/1.1"): 0,
}
_ASGI_SCOPE_LOG = ("uvicorn.asgi", "%s [%d] Started scope=%s")


class _RequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or not isinstance(record.msg, str):
            return True

        message = (record.name, record.msg)
        target_index = _REQUEST_TARGET_ARGUMENT.get(message)
        if target_index is not None and target_index < len(args):
            target = args[target_index]
            if isinstance(target, str) and "?" in target:
                # Preserve the formatter's argument layout and the encoded path.
                path = target.partition("?")[0]
                record.args = (*args[:target_index], path, *args[target_index + 1:])
        elif message == _ASGI_SCOPE_LOG and len(args) == 3:
            prefix, task, scope = args
            if isinstance(scope, dict) and scope.get("query_string"):
                record.args = (prefix, task, {**scope, "query_string": "<...>"})
        return True


_request_log_filter = _RequestLogFilter()


def protect_request_logs() -> None:
    # Attach to the emitting loggers so propagation and handler formatting
    # both see the redacted fields, including in the plain Uvicorn CLI.
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn.asgi"):
        logging.getLogger(name).addFilter(_request_log_filter)
