"""Actual topic context for fixed index links, separate from reviewing an index."""

from . import format, raw_blocks
from .store import WikiError

GENERIC = {'index', 'readme', 'navigation', 'contents', 'table of contents', '目录', '索引', '导航'}


def select(store, document, text):
    parsed = format.parse(text)
    parts = raw_blocks.coordinates(store, document, text, parsed=parsed).blocks
    result = []
    list_depth = 0
    for index, token in enumerate(parsed.tokens):
        if token.type in {'bullet_list_open', 'ordered_list_open'}:
            list_depth += 1
        elif token.type in {'bullet_list_close', 'ordered_list_close'}:
            list_depth -= 1
        if token.type != 'inline' or token.map is None or not index:
            continue
        opening = parsed.tokens[index - 1]
        if opening.type not in {'heading_open', 'paragraph_open'}:
            continue
        words = format.unlinked_text(token).strip(' :：.。')
        if not words or words.casefold() in GENERIC:
            continue
        first, last = (parsed.line_offsets[number] for number in token.map)
        selected = [part for part in parts if first < part.end and part.start < last]
        headings = [heading for heading in parsed.headings if heading['start'] <= first]
        preceding = next((block for block in reversed(parts) if headings and block.start <= headings[-1]['start'] < block.end), None)
        if preceding and preceding not in selected:
            selected.insert(0, preceding)
        result.append(((bool(list_depth), opening.type != 'paragraph_open', opening.level), selected))
    # Prefer the topic's actual explanation with its heading. A title remains
    # usable when the index has no prose introduction, but is not an intro.
    return [parts for _, parts in sorted(result, key=lambda choice: choice[0])]


def require(task, store, document, text):
    from .tasks import tasks

    choices = select(store, document, text)
    if not choices:
        raise WikiError('Navigation has no readable topic context; leave the entry unchanged', 422)
    try:
        tasks.require_read(task, store, document['path'], document['revision'])
        return
    except WikiError as error:
        if error.status != 422:
            raise
    for parts in choices:
        try:
            for part in parts:
                tasks.require_raw_read(task, document, part.start, part.end)
            return
        except WikiError as error:
            if error.status != 422:
                raise
    raise WikiError('Read the current navigation topic context with blocks(view="navigation") or the relevant raw blocks', 422)
