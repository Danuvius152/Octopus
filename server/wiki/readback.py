"""Final semantic reading obligations carried by existing operation receipts.

Writer copy/insert maps describe this task's changes. Material origins instead
prove original-byte preservation and must not be used as a task's reading list.
"""

import json

from . import format
from .extract import blocks


def merge(ranges):
    result = []
    for first, last in sorted(ranges):
        if first >= last:
            continue
        if result and first <= result[-1][1]:
            result[-1] = [result[-1][0], max(result[-1][1], last)]
        else:
            result.append([first, last])
    return result


def _subtract(ranges, excluded):
    result = []
    for first, last in merge(ranges):
        for left, right in merge(excluded):
            if right <= first or left >= last:
                continue
            if first < left:
                result.append([first, left])
            first = max(first, right)
        if first < last:
            result.append([first, last])
    return result


def _metadata_regions(text, state):
    result = []
    for part in json.loads(state['regions']) if state else []:
        if part['role'] in {'tags', 'aliases'} and text.count(part['raw']) == 1:
            first = text.index(part['raw'])
            result.append([first, first + len(part['raw'])])
    return result


def _effective_metadata(parsed, state):
    result = dict(parsed.fields)
    managed = json.loads(state['metadata']) if state else {}
    native = parsed.metadata({})
    for field in ('tags', 'aliases'):
        if field in native:
            result[field] = native[field]
    for field in ('tags', 'aliases', 'sources'):
        if field in managed:
            result[field] = (list(dict.fromkeys([*native.get(field, []), *managed[field]]))
                             if field in {'tags', 'aliases'} else managed[field])
    return result


def transition(before, after, trace=None, *, fixed=False, managed_before=None, managed_after=None):
    """Use real writer transformations, never infer copies with a text diff."""
    old = (before or b'').decode('utf-8')
    text = (after or b'').decode('utf-8')
    parsed, previous = format.parse(text), format.parse(old)
    old_metadata = [[0, previous.body_start], *_metadata_regions(old, managed_before)]
    new_metadata = [[0, parsed.body_start], *_metadata_regions(text, managed_after)]
    if fixed:
        return {'copies': [], 'semantic': [], 'metadata': {}, 'fixed': True}
    copies, changed, offset, previous_end = [], [], 0, 0
    if trace is None:
        changed = [[parsed.body_start, len(text)]] if before != after else []
    else:
        copied = [piece for piece in trace.pieces if isinstance(piece, tuple)]
        reordered = any(first[1] > second[0] for first, second in zip(copied, copied[1:], strict=False))
        for piece in trace.pieces:
            if isinstance(piece, tuple):
                first, last = piece
                start = len(trace.before[:first].decode('utf-8'))
                end = start + len(trace.before[first:last].decode('utf-8'))
                copies.append([start, end, offset])
                previous_position = len(trace.before[:previous_end].decode('utf-8'))
                metadata_gap = start >= previous_position and not _subtract([[previous_position, start]], old_metadata)
                if first != previous_end and not metadata_gap:
                    # A copy moved across an original join. Its words may be
                    # unchanged while its heading/condition context has changed.
                    changed.append([offset, offset + end - start] if reordered else [max(0, offset - 1), min(len(text), offset + 1)])
                previous_end = last
                offset += end - start
            else:
                length = len(piece.decode('utf-8'))
                changed.append([offset, offset + length])
                offset += length
        previous_position = len(trace.before[:previous_end].decode('utf-8'))
        if previous_end < len(trace.before) and text and _subtract([[previous_position, len(old)]], old_metadata):
            # A suffix deletion has no following copy and may insert no words.
            # Its final join still needs semantic inspection.
            changed.append([max(parsed.body_start, len(text) - 1), len(text)])
    semantic = _subtract(changed, new_metadata)
    old_fields, new_fields = _effective_metadata(previous, managed_before), _effective_metadata(parsed, managed_after)
    metadata = {key: new_fields.get(key) for key in set(old_fields) | set(new_fields)
                if old_fields.get(key) != new_fields.get(key)}
    return {'copies': copies, 'semantic': semantic, 'metadata': metadata, 'fixed': False}


def details(db, publication, doc_id, before, after, values):
    if not publication:
        return values
    mapping = values.get('readback_transition')
    if mapping and mapping['fixed']:
        return values | {'readback': {'revision': after, 'ranges': [], 'metadata': {}, 'uncertain': False}}
    prior = db.execute('''SELECT o.output_revision,o.details FROM operation_receipts o
        LEFT JOIN changes c ON c.id=o.change_id
        WHERE o.job_id=? AND o.doc_id=? AND (o.change_id IS NULL OR c.status='applied')
        ORDER BY o.rowid DESC LIMIT 1''', (publication['job_id'], doc_id)).fetchone()
    previous = json.loads(prior['details']).get('readback') if prior else None
    continuous = not prior or prior['output_revision'] == before
    uncertain = bool(prior and (not previous or not continuous))
    ranges = previous['ranges'] if previous and continuous else []
    uncertain |= bool(previous and previous.get('uncertain'))
    metadata = dict(previous.get('metadata', {})) if previous and continuous else {}
    if before != after:
        if mapping is None:
            # Older/generic writers have no reliable mapping; require an actual
            # full final read, rather than claiming an empty semantic effect.
            uncertain = True
            ranges = []
        else:
            ranges = merge((target + max(first, start) - start, target + min(last, end) - start)
                           for first, last in ranges for start, end, target in mapping['copies']
                           if first < end and start < last)
            ranges = merge([*ranges, *mapping['semantic']])
            metadata.update(mapping['metadata'])
    return values | {'readback': {'revision': after, 'ranges': ranges, 'metadata': metadata, 'uncertain': uncertain}}


def _context(text, ranges):
    parsed = format.parse(text)
    parts, offset = [], 0
    for part in blocks(text, parsed=parsed):
        parts.append((offset, offset + len(part)))
        offset += len(part)
    result = []
    for first, last in ranges:
        selected = [index for index, (start, end) in enumerate(parts) if first < end and start < last]
        if not selected:
            continue
        start, end = max(0, selected[0] - 1), min(len(parts) - 1, selected[-1] + 1)
        result.append((max(parsed.body_start, parts[start][0]), parts[end][1]))
        for heading in parsed.headings:
            if heading['start'] <= first < heading['section_end']:
                result.append((heading['start'], heading['end']))
    return merge(result)


def response(store, result):
    receipt = result.get('receipt_id')
    if not receipt:
        return result
    with store.connect() as db:
        row = db.execute('SELECT details,doc_id FROM operation_receipts WHERE id=?', (receipt,)).fetchone()
    state = json.loads(row['details']).get('readback') if row else None
    if state is None:
        return result
    from .history import WikiHistory

    blob = WikiHistory(store).blobs / result['revision']
    text = blob.read_bytes().decode('utf-8')
    ranges = [[0, len(text)]] if state['uncertain'] else _context(text, state['ranges'])
    full = state['uncertain'] or len(ranges) > 32
    if full:
        ranges = [[0, len(text)]]
    selected, offset = [], 0
    for index, part in enumerate(blocks(text)):
        end = offset + len(part)
        if any(first < end and offset < last for first, last in ranges):
            selected.append(str(index))
        offset = end
    if len(selected) > 100:
        full, ranges, selected = True, [[0, len(text)]], []
    metadata = state['metadata']
    metadata_complete = len(json.dumps(metadata, ensure_ascii=False).encode()) <= 4096
    return result | {'readback': {
        'required': bool(ranges), 'doc': row['doc_id'], 'revision': result['revision'],
        'coordinate_system': 'raw_unicode_codepoints', 'ranges': ranges, 'full_document': full,
        'block_ids': selected,
        'metadata': metadata if metadata_complete else {}, 'metadata_complete': metadata_complete,
        'reason': 'mapping_unavailable_read_full_final_note' if state['uncertain'] else 'final_changes_and_structural_context' if ranges else 'no_new_semantic_content',
    }}
