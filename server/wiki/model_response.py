"""Model projections fit before evidence grants and preserve operation-owned cursors."""

from ..mcp_result import TEXT_RESULT_MAX_BYTES, model_result_bytes, too_large
from .store import WikiError


class ResponseTooLarge(WikiError):
    def __init__(self):
        super().__init__("Model response envelope exceeds its byte limit; use compact view or a smaller selection", 422)
        self.response = too_large() | {"status": 422}


def presentation(value):
    """Only human presentation changes; identifiers, warnings and bodies stay exact."""
    if isinstance(value, list):
        return [presentation(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"title", "heading", "description", "snippet"} and isinstance(item, str) and len(item) > 240:
            result[key], result[key + "_truncated"] = item[:240], True
        elif key == "heading_path" and isinstance(item, list):
            result[key] = [part[:240] if isinstance(part, str) else part for part in item]
            if any(isinstance(part, str) and len(part) > 240 for part in item):
                result[key + "_truncated"] = True
        else:
            result[key] = presentation(item)
    return result


def measured(value, limit=TEXT_RESULT_MAX_BYTES):
    result = value | {"response_limit_bytes": limit, "response_bytes": 0}
    while True:
        size = model_result_bytes(result)
        if result["response_bytes"] == size:
            return result
        result["response_bytes"] = size


def fit_prefix(build, count, *, limit=TEXT_RESULT_MAX_BYTES):
    result = measured(build(count), limit)
    if result["response_bytes"] <= limit:
        return result
    low, high, selected = 1, count - 1, None
    while low <= high:
        middle = (low + high) // 2
        candidate = measured(build(middle), limit)
        if candidate["response_bytes"] <= limit:
            selected, low = candidate, middle + 1
        else:
            high = middle - 1
    if selected is None:
        raise ResponseTooLarge()
    return selected


def page(items, base, offset, total, continuation):
    projected = presentation(items)
    header = presentation(base)

    def build(count):
        end = offset + count
        following = end if end < total else None
        return header | {"items": projected[:count], "returned_count": count,
                         "next_offset": following, "next_cursor": continuation(following)}

    return fit_prefix(build, len(items))
