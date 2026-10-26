"""Bounded current dependency facts; continuations convey no reading authority."""

import base64
import json

from .maintenance_contract import digest
from .store import WikiError

MAX_BYTES = 4096
ACTION_FIELDS = ('doc', 'path', 'resource', 'revision', 'current_revision', 'location', 'status',
                 'confirmed_revision', 'confirmed_location', 'confirmation_receipt_id')


def _entry(reference, ordinal, document):
    item = {'ordinal': ordinal, **{key: reference[key] for key in ACTION_FIELDS if key in reference}}
    for key in ('id', 'title'):
        if key in reference:
            value = reference[key]
            item[key] = value[:240] if isinstance(value, str) else value
            if isinstance(value, str) and len(value) > 240:
                item[key + '_truncated'] = True
    if len(json.dumps(item, ensure_ascii=False).encode()) <= MAX_BYTES // 2:
        return item
    # Preserve short actionable facts even when an individual selector/locator
    # cannot fit. A digest is not a replacement for an actual reading path.
    item['recovery'] = {
        'read': {'doc': document['id'], 'expected_revision': document['revision'], 'view': 'full', 'length': 1},
        'blocks': {'doc': document['id'], 'expected_revision': document['revision'], 'view': 'full', 'offset': 0, 'limit': 1},
    }
    item['omitted_fields'] = []
    candidates = [key for key in ACTION_FIELDS if key in item and key not in {'doc', 'current_revision', 'status'}]
    candidates += [key for key in ('id', 'title') if key in item]
    while len(json.dumps(item, ensure_ascii=False).encode()) > MAX_BYTES // 2 and candidates:
        key = max(candidates, key=lambda name: len(json.dumps(item[name], ensure_ascii=False).encode()))
        candidates.remove(key)
        item.pop(key)
        item['omitted_fields'].append(key)
    item['details_omitted'] = bool(set(item['omitted_fields']) & set(ACTION_FIELDS))
    return item


def page(store, document, user_id, *, cursor=None, limit=8):
    refs = document.get('metadata', {}).get('source_refs', [])
    generation = digest(refs)
    binding = {'owner': user_id, 'vault': store.vault_id, 'registration': store.registration_generation,
               'doc': document['id'], 'revision': document['revision'], 'dependencies': generation}
    offset = 0
    if cursor is not None:
        try:
            if len(cursor) > 8192:
                raise ValueError('oversized cursor')
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if not isinstance(value, dict):
                raise ValueError('invalid cursor object')
            offset = value.pop('offset')
            if type(offset) is not int or offset < 0:
                raise ValueError('invalid offset')
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise WikiError('Invalid source continuation', 422) from error
        if value != binding:
            raise WikiError('Document or dependencies changed; restart the source observation', 409)
        if offset >= len(refs):
            raise WikiError('Invalid source continuation offset', 422)

    def continuation(end):
        return base64.urlsafe_b64encode(json.dumps(binding | {'offset': end}, separators=(',', ':')).encode()).decode() if end < len(refs) else None

    items, omitted = [], False
    for ordinal in range(offset, min(len(refs), offset + limit)):
        item = _entry(refs[ordinal], ordinal, document)
        if items and len(json.dumps([*items, item], ensure_ascii=False).encode()) > MAX_BYTES // 2:
            break
        omitted |= bool(item.get('details_omitted'))
        items.append(item)
    end = offset + len(items)
    return {'items': items, 'total': len(refs), 'offset': offset,
            'complete': end == len(refs) and not omitted, 'details_omitted': omitted,
            'dependency_generation': generation, 'next_cursor': continuation(end)}


def review_context(jobs, document, observation):
    """Preserve the admitted baseline separately from execution-time sources."""
    baseline = None
    if observation['baseline_receipt_id']:
        with jobs.store.connect() as db:
            row = db.execute('SELECT id,output_revision,operation,path FROM operation_receipts WHERE id=? AND doc_id=?',
                             (observation['baseline_receipt_id'], document['id'])).fetchone()
        baseline = dict(row) if row else {'id': observation['baseline_receipt_id'], 'unavailable': True}
    reasons = []
    if baseline is None:
        reasons.append('first_review')
    elif baseline.get('output_revision') != document['revision']:
        reasons.append('article_changed')
    if any(ref.get('status') in {'changed', 'missing', 'unknown', 'version_unknown'}
           for ref in document['metadata'].get('source_refs', [])):
        reasons.append('direct_sources_need_review')
    return {'observed': {key: observation[key] for key in ('revision', 'baseline_receipt_id', 'dependency_generation')},
            'baseline': baseline, 'reasons': reasons,
            'sources': page(jobs.store, document, jobs.user_id)}
