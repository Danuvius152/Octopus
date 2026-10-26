"""Locate current passages before fitting responses and recording delivery."""

from .links import source_span
from .store import WikiError


def window(text, start, length, location=None):
    first, last = 0, len(text)
    if location is not None:
        span = source_span(text, location)
        if span is None:
            raise WikiError('The source location is missing or ambiguous in this revision', 422)
        first, last = span
        if start > last:
            raise WikiError('Continuation lies outside the source location', 422)
    start = min(max(first, start), last)
    return start, min(start + length, last), last


def read(store, doc, user_id, *, start=0, length=16000, expected_revision=None,
         page=None, view='full', location=None, source_cursor=None, source_limit=8, task=None):
    from .history import WikiHistory
    from .read_response import compact_read, validate_view
    from .source_details import page as sources
    from .tasks import tasks

    validate_view(view)
    if source_cursor is not None and (start or page is not None or location is not None):
        raise WikiError('Source continuation is independent of body pagination', 422)
    if location is not None and page is not None:
        raise WikiError('A source location cannot be combined with a page', 422)
    with WikiHistory(store).locked():
        document = store.read(doc, start=start, length=length, expected_revision=expected_revision, page=page)
        details = sources(store, document, user_id, cursor=source_cursor, limit=source_limit)
        if source_cursor is not None:
            return {key: document[key] for key in ('id', 'path', 'revision')} | {'sources': details}
        if location is not None:
            with store.connect() as db:
                text = db.execute('SELECT text FROM documents WHERE id=?', (document['id'],)).fetchone()[0]
            first, end, boundary = window(text, start, length, location)
            document = store.read(document['id'], start=first, length=max(1, end - first), expected_revision=document['revision'])
            if first == end:
                document.update(content='', end=first, line_end=document['line_start'])
            document['next_start'] = end if end < boundary else None
            document['source_location'] = location
        document['sources'] = details
        if view == 'compact' or task is not None:
            document = compact_read(document, view=view)
        tasks.record_read(task, document)
        return document
