"""Idempotent plan/apply/verify, with optimistic concurrency and an audit trail."""
from __future__ import annotations

import copy
import datetime as dt
import fcntl
import html
import json
import os
from pathlib import Path
import re
import uuid

from zotero_core import (CuratorError, STATE, DEFAULT_DB, digest, inspect_source,
                         load_json, object_key, private_dir, save_json, snapshot)
from zotero_catalog import classify, identifiers, normalized_title, checked_file
from zotero_versions import artifact_history, preserve_archive_revision, attachment_title

COMMON = {'key', 'version', 'itemType', 'creators', 'tags', 'collections', 'relations'}
APPROVABLE = {'itemType', 'title', 'creators', 'date', 'abstractNote', 'DOI', 'url',
              'repository', 'archiveID', 'publicationTitle', 'proceedingsTitle'}


def allowed_fields(schema, item_type):
    for t in schema['itemTypes']:
        if t['itemType'] == item_type:
            return COMMON | {f['field'] for f in t['fields']}
    raise CuratorError('Unsupported Zotero item type: %s' % item_type, 'SCHEMA_MISMATCH')


def validate_data(data, schema):
    allowed = allowed_fields(schema, data['itemType'])
    unknown = set(data) - allowed
    if unknown:
        raise CuratorError('Fields not available on %s: %s' %
                           (data['itemType'], ', '.join(sorted(unknown))), 'SCHEMA_MISMATCH')
    valid_creators = {c['creatorType'] for t in schema['itemTypes']
                      if t['itemType'] == data['itemType'] for c in t['creatorTypes']}
    for creator in data.get('creators', []):
        if creator.get('creatorType') not in valid_creators:
            raise CuratorError('Creator type does not match item schema', 'SCHEMA_MISMATCH')
        if 'name' in creator and ('firstName' in creator or 'lastName' in creator):
            raise CuratorError('Do not mix single-field and two-field creator names')
    return data


def creator_names(creators):
    return [' '.join((c.get('name') or
                     (c.get('firstName', '') + ' ' + c.get('lastName', ''))).split()).casefold()
            for c in creators]


def same_value(field, old, new):
    if field == 'creators':
        return creator_names(old or []) == creator_names(new or [])
    return old == new


def match_existing(data, items):
    incoming = identifiers(data)
    strong, title_only = [], []
    for item in items:
        old = item['data']
        if old.get('itemType') in ('note', 'attachment', 'annotation'):
            continue
        known = identifiers(old)
        agreement = any(incoming[k] and incoming[k] == known[k] for k in incoming)
        contradiction = any(incoming[k] and known[k] and incoming[k] != known[k] for k in incoming)
        title_match = normalized_title(data['title']) == normalized_title(old.get('title', ''))
        if agreement:
            if contradiction:
                return None, 'Strong identifiers contradict each other; manual identity review required'
            strong.append(item)
        elif title_match:
            title_only.append(item)
    if len(strong) > 1:
        return None, 'Multiple existing items have the same identifier; never auto-merge'
    if len(strong) == 1:
        return strong[0], None
    if title_only:
        return None, 'Title-only duplicate candidate; verify identity instead of creating a duplicate'
    return None, None


def provenance_note(record, source_url):
    payload = {
        'record_id': record['record_id'], 'metadata_source': record['provenance'],
        'official_record': record.get('official'),
        'selected_publication': record.get('publication', {}).get('data'),
        'publication_version': record.get('publication', {}).get('raw', {}).get('version'),
        'publication_relations': record.get('publication', {}).get('raw', {}).get('relation', {}),
        'translation_date': record.get('translation_date'),
        'version_policy': 'Per-attachment source_edition is distinct from the metadata edition; unknown versions are not inferred.',
        'source_repository': source_url, 'source_path': record.get('source_path'),
        'source_paths': record.get('source_paths'),
        'attachments': [{k: a.get(k) for k in (
            'role', 'language', 'ai_translated', 'filename', 'sha256', 'source_edition',
            'archive_revision', 'translation_revision', 'translation_date', 'derived_from_sha256',
            'download_receipt_sha256', 'translation_content_verified')}
                        for a in record['attachments']],
        'warnings': record.get('warnings', []),
    }
    # Do not expose absolute local evidence cache paths in a potentially synced note.
    payload['metadata_source'] = copy.deepcopy(payload['metadata_source'])
    payload['metadata_source'].get('raw', {}).pop('file', None)
    return '<h2>文献来源与版本记录</h2><p>由 zotero-library 维护；不替代人工阅读笔记。</p><pre>' + html.escape(
        json.dumps(payload, ensure_ascii=False, indent=2)) + '</pre>'


def snapshot_attachment_path(item, db):
    data = item['data']
    value = data.get('_path', '') or ''
    if not value.startswith('storage:'):
        return None
    root = Path(db).expanduser().resolve().parent / 'storage'
    path = (root / item['key'] / value[len('storage:'):]).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def build_plan(catalog, library, schema, source_audit, db=DEFAULT_DB, approvals=None):
    approvals = approvals or {}
    if not library.get('server_id'):
        raise CuratorError('No local server ID in database; start Zotero once before planning')
    items = library['items']
    history = artifact_history(library)
    plan = {'format': 1, 'server_id': library['server_id'], 'database': str(Path(db).expanduser().resolve()),
            'source_audit': source_audit, 'source_root': catalog['source_root'],
            'source_name': catalog['source_name'], 'source_url': catalog.get('source_url'),
            'records': [], 'blocked': []}
    seen = set()
    for record in catalog['records']:
        rid = record['record_id']
        if rid in seen:
            raise CuratorError('Repeated record identity; combine its PDF attachments in one manifest record: ' + rid)
        seen.add(rid)
        if record.get('status') not in ('verified-arxiv', 'verified-publication'):
            plan['blocked'].append({'record_id': rid, 'reason': record.get('reason', 'Unverified metadata')})
            continue
        desired = validate_data(record['data'], schema)
        existing, reason = match_existing(desired, items)
        if reason:
            plan['blocked'].append({'record_id': rid, 'reason': reason})
            continue
        accepted = set(approvals.get(rid, []))
        if accepted - APPROVABLE:
            raise CuratorError('Invalid approved field(s): ' + ','.join(sorted(accepted - APPROVABLE)))
        old = existing['data'] if existing else {}
        key = existing['key'] if existing else object_key(plan['server_id'] + ':paper:' + rid)
        patch, conflicts, omitted = {}, [], []
        effective_type = desired['itemType']
        if old.get('itemType') and old['itemType'] != desired['itemType'] and 'itemType' in accepted:
            old_fields = allowed_fields(schema, old['itemType'])
            new_fields = allowed_fields(schema, desired['itemType'])
            lost = [f for f in old_fields - new_fields if old.get(f) not in (None, '', [])]
            if lost:
                plan['blocked'].append({'record_id': rid,
                    'reason': 'Changing type would discard populated fields; preserve and inspect manually: ' + ', '.join(sorted(lost))})
                continue
        if old.get('itemType') and old['itemType'] != desired['itemType'] and 'itemType' not in accepted:
            effective_type = old['itemType']
        fields = allowed_fields(schema, effective_type)
        for field, value in desired.items():
            current = old.get(field)
            if current is not None and same_value(field, current, value):
                continue
            if field not in fields:
                omitted.append(field)
                continue
            empty = current in (None, '', [])
            date_refinement = (field == 'date' and bool(current) and
                               re.fullmatch(r'\d{4}(?:-\d{2})?', str(current)) is not None and
                               str(value).startswith(str(current) + '-'))
            if empty or field in accepted or date_refinement:
                patch[field] = copy.deepcopy(value)
            else:
                conflicts.append({'field': field, 'existing': current, 'official': value})
        effective = dict(old, **patch)
        paths, tags = classify(effective, catalog['source_name'])
        tags.append('身份:arXiv已核验' if record['status'] == 'verified-arxiv' else '身份:出版记录已核验')
        if not existing:
            tags.append('阅读状态:未读')
        if any(a['role'] == 'translation' for a in record['attachments']):
            tags.append('附件:中文译文')
        if conflicts or omitted:
            tags.append('元数据:需复核')
        attachments = []
        prior_artifacts = copy.deepcopy(history.get(rid, []))
        attachment_hashes = set()
        child_items = [i for i in items if i['data'].get('parentItem') == key
                       and i['data']['itemType'] == 'attachment']
        for attachment in record['attachments']:
            if attachment['sha256'] in attachment_hashes:
                raise CuratorError('Same PDF bytes occur twice in one record; resolve attachment roles instead of duplicating')
            attachment_hashes.add(attachment['sha256'])
            path = Path(attachment['path'])
            checked = checked_file(catalog['source_root'], str(path.relative_to(catalog['source_root'])))
            if digest(checked) != attachment['sha256']:
                raise CuratorError('Source PDF changed; re-scan before planning', 'SOURCE_CHANGED')
            candidates = []
            for child in child_items:
                stored = snapshot_attachment_path(child, db)
                if stored and digest(stored) == attachment['sha256']:
                    candidates.append(child['key'])
            if len(candidates) > 1:
                plan['blocked'].append({'record_id': rid, 'reason': 'Duplicate identical child PDFs; review before attaching'})
                attachments = None
                break
            try:
                a = preserve_archive_revision(copy.deepcopy(attachment), prior_artifacts)
            except CuratorError as e:
                if e.code != 'VERSION_CONFLICT':
                    raise
                plan['blocked'].append({'record_id': rid, 'reason': str(e)})
                attachments = None
                break
            a['key'] = candidates[0] if candidates else object_key(plan['server_id'] + ':attachment:' + key + ':' + a['sha256'])
            a['existing'] = bool(candidates)
            a['title'] = attachment_title(a)
            attachments.append(a)
            prior_artifacts.append(a)
        if attachments is None:
            continue
        note = provenance_note(dict(record, attachments=attachments), catalog.get('source_url'))
        plan['records'].append({
            'record_id': rid, 'key': key, 'create': existing is None,
            'expected_version': existing['version'] if existing else 0,
            'patch': patch, 'effective_type': effective_type, 'desired': desired,
            'approved_fields': sorted(accepted), 'conflicts': conflicts, 'omitted': omitted,
            'collection_paths': paths, 'tags': sorted(set(tags)), 'attachments': attachments,
            'note': {'key': object_key(plan['server_id'] + ':note:' + key + ':' +
                                      __import__('hashlib').sha256(note.encode()).hexdigest()), 'html': note},
        })
    plan['summary'] = {'ready': len(plan['records']), 'blocked': len(plan['blocked']),
                       'new_papers': sum(r['create'] for r in plan['records']),
                       'existing_papers': sum(not r['create'] for r in plan['records']),
                       'new_attachments': sum(not a['existing'] for r in plan['records'] for a in r['attachments']),
                       'records_with_conflicts': sum(bool(r['conflicts'] or r['omitted']) for r in plan['records'])}
    return plan


def collection_paths(collections):
    by_key = {c['key']: c['data'] for c in collections}
    result = {}
    def path(key, seen):
        if key in seen:
            raise CuratorError('Collection hierarchy contains a cycle')
        data = by_key[key]
        parent = data.get('parentCollection')
        if parent and parent not in by_key:
            raise CuratorError('Collection parent is missing')
        return (path(parent, seen | {key}) + '/' if parent else '') + data['name']
    for key in by_key:
        name = path(key, set())
        if name in result:
            raise CuratorError('Duplicate collection path: %s; review before filing' % name)
        result[name] = key
    return result


def chunks(values, size=50):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def journal_request(run, label, function):
    journal = load_json(run / 'journal.json') if (run / 'journal.json').exists() else []
    entry = {'action': label, 'status': 'intent',
             'started_at': dt.datetime.now(dt.timezone.utc).isoformat()}
    journal.append(entry)
    save_json(run / 'journal.json', journal)
    try:
        result = function()
    except Exception:
        entry['status'] = 'uncertain-or-failed; reconcile, never blindly retry'
        save_json(run / 'journal.json', journal)
        raise
    entry['status'] = 'completed'
    entry['completed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_json(run / 'journal.json', journal)
    return result


def assert_plan(plan, schema, source_audit):
    if plan.get('format') != 1 or not plan.get('server_id'):
        raise CuratorError('Invalid plan format')
    if plan['source_audit']['hashes'] != source_audit['hashes']:
        raise CuratorError('Installed Zotero code/schema changed; inspect and re-plan', 'SOURCE_CHANGED')
    keys = set()
    for r in plan['records']:
        if not re.fullmatch(r'[A-Z2-9]{8}', r['key']):
            raise CuratorError('Invalid parent key')
        if r['key'] in keys:
            raise CuratorError('Plan reuses a parent key')
        keys.add(r['key'])
        validate_data(dict(r['patch'], itemType=r['effective_type']), schema)
        if set(r.get('approved_fields', [])) - APPROVABLE:
            raise CuratorError('Invalid approval list')
        for path in r['collection_paths']:
            if (path != '文献库' and not path.startswith('文献库/')) or any(p in ('', '.', '..') for p in path.split('/')):
                raise CuratorError('Unsafe collection path')
        if not re.fullmatch(r'[A-Z2-9]{8}', r['note']['key']):
            raise CuratorError('Invalid provenance note key')
        for a in r['attachments']:
            if not re.fullmatch(r'[A-Z2-9]{8}', a['key']):
                raise CuratorError('Invalid attachment key')
            path = Path(a['path'])
            try:
                relative = path.relative_to(plan['source_root'])
            except ValueError:
                raise CuratorError('Attachment outside source root', 'UNSAFE_PATH')
            checked_file(plan['source_root'], str(relative))
            if path.stat().st_size > 256 * 1024 * 1024:
                raise CuratorError('Attachment exceeds 256 MiB safety limit')
            if digest(path) != a['sha256']:
                raise CuratorError('Source PDF changed; re-scan and re-plan', 'SOURCE_CHANGED')


def planned_patch(record, current, collection_keys):
    """Append to current tags/collections, never replace or remove the user's entries."""
    patch = copy.deepcopy(record['patch'])
    tags = copy.deepcopy(current.get('tags', []))
    known = {t['tag'] for t in tags}
    tags.extend({'tag': name, 'type': 0} for name in record['tags'] if name not in known)
    collections = list(dict.fromkeys(current.get('collections', []) + collection_keys))
    if tags != current.get('tags', []):
        patch['tags'] = tags
    if collections != current.get('collections', []):
        patch['collections'] = collections
    return {k: v for k, v in patch.items() if not same_value(k, current.get(k), v)}


def apply_plan(plan, client, schema, source_audit, state=STATE):
    assert_plan(plan, schema, source_audit)
    client.probe()
    if client.server_id != plan['server_id']:
        raise CuratorError('Plan belongs to another Zotero database', 'SERVER_CHANGED')
    if not client.key:
        raise CuratorError('Local authorization is required before applying', 'AUTH_REQUIRED')
    state = private_dir(state)
    lock_path = state / 'apply.lock'
    with lock_path.open('a') as lock:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CuratorError('Another curator apply is running', 'APPLY_BUSY')
        run = private_dir(state / 'runs' / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]))
        save_json(run / 'plan.json', plan)
        with snapshot(plan['database'], run / 'database-backup'):
            pass
        # All existing parent versions are checked before the first library mutation.
        current_items = {}
        for batch in chunks([r['key'] for r in plan['records']]):
            current_items.update({i['key']: i for i in client.get('items?itemKey=' + ','.join(batch))})
        for r in plan['records']:
            current = current_items.get(r['key'])
            if not r['create'] and not current:
                raise CuratorError('An existing paper disappeared; re-plan', 'STALE_VERSION')
            if current:
                old = current['data']
                if old.get('deleted'):
                    raise CuratorError('Planned item is now in trash; re-plan', 'STALE_VERSION')
                known, intended = identifiers(old), identifiers(r['desired'])
                if any(known[k] and intended[k] and known[k] != intended[k] for k in known):
                    raise CuratorError('Paper identity changed since planning; do not attach PDFs', 'IDENTITY_MISMATCH')
                changes_left = any(not same_value(k, old.get(k), v) for k, v in r['patch'].items())
                if current['version'] != r['expected_version'] and changes_left:
                    raise CuratorError('Paper changed since planning; re-plan', 'STALE_VERSION')
        collection_map = collection_paths(client.get('collections'))
        wanted = sorted({p for r in plan['records'] for path in r['collection_paths']
                         for p in ['/'.join(path.split('/')[:i]) for i in range(1, len(path.split('/')) + 1)]},
                        key=lambda s: (s.count('/'), s))
        for level in sorted({p.count('/') for p in wanted}):
            pending = []
            for path in wanted:
                if path.count('/') != level or path in collection_map:
                    continue
                parent, _, name = path.rpartition('/')
                key = object_key(plan['server_id'] + ':collection:' + path)
                # If a deterministic key collided with a renamed/unrelated collection, stop.
                if key in collection_map.values():
                    raise CuratorError('Collection key collision/rename; re-plan instead of overwriting')
                pending.append({'key': key, 'version': 0, 'name': name,
                                'parentCollection': collection_map[parent] if parent else False})
                collection_map[path] = key
            for batch in chunks(pending):
                journal_request(run, 'create-collections:' + ','.join(x['key'] for x in batch),
                                lambda batch=batch: client.write_batch('collections', batch))
        pending = []
        for r in plan['records']:
            current = current_items.get(r['key'])
            old = current['data'] if current else {}
            patch = planned_patch(r, old, [collection_map[p] for p in r['collection_paths']])
            if patch:
                version = current['version'] if current else 0
                pending.append(dict(patch, key=r['key'], version=version))
        for batch in chunks(pending):
            journal_request(run, 'write-papers:' + ','.join(x['key'] for x in batch),
                            lambda batch=batch: client.write_batch('items', batch))
        # Stored file attachments are created first, then native upload phases are used.
        # Existing files are verified, not overwritten. Incomplete previous uploads resume.
        for r in plan['records']:
            for a in r['attachments']:
                current = client.maybe_get('items/' + a['key'])
                if current:
                    data = current['data']
                    if data.get('deleted') or data.get('itemType') != 'attachment' or data.get('parentItem') != r['key']:
                        raise CuratorError('Attachment key collision or parent mismatch')
                    try:
                        actual = client.file_hash(a['key'], plan['database'])
                    except CuratorError as e:
                        if e.code not in ('HTTP_404', 'FILE_MISSING'):
                            raise
                        actual = None
                    if actual == a['sha256']:
                        continue
                    if actual is not None:
                        raise CuratorError('Existing attachment differs; never overwrite it', 'FILE_CONFLICT')
                else:
                    attachment = {'key': a['key'], 'version': 0, 'itemType': 'attachment',
                                  'parentItem': r['key'], 'linkMode': 'imported_file',
                                  'title': a['title'], 'contentType': 'application/pdf',
                                  'filename': a['filename']}
                    journal_request(run, 'create-attachment:' + a['key'],
                                    lambda attachment=attachment: client.write_batch('items', [attachment]))
                journal_request(run, 'upload-attachment:' + a['key'],
                                lambda a=a: client.upload(a['key'], a['path']))
                if client.file_hash(a['key'], plan['database']) != a['sha256']:
                    raise CuratorError('Stored PDF checksum mismatch', 'VERIFY_FAILED')
            note = r['note']
            old_note = client.maybe_get('items/' + note['key'])
            if old_note:
                if old_note['data'].get('parentItem') != r['key'] or old_note['data'].get('itemType') != 'note':
                    raise CuratorError('Provenance note key collision')
                # Preserve manual edits to provenance notes; never overwrite existing notes.
            else:
                entry = {'key': note['key'], 'version': 0, 'itemType': 'note',
                         'parentItem': r['key'], 'note': note['html'],
                         'tags': [{'tag': '系统:来源与版本记录'}]}
                journal_request(run, 'create-provenance-note:' + note['key'],
                                lambda entry=entry: client.write_batch('items', [entry]))
        result = verify_plan(plan, client)
        result['run_directory'] = str(run)
        save_json(run / 'verification.json', result)
        return result


def verify_plan(plan, client):
    client.probe()
    if client.server_id != plan['server_id']:
        raise CuratorError('Wrong Zotero database', 'SERVER_CHANGED')
    collections = collection_paths(client.get('collections'))
    records = []
    for r in plan['records']:
        actual = client.get('items/' + r['key'])
        data = actual['data']
        errors = []
        for field, value in r['patch'].items():
            if not same_value(field, data.get(field), value):
                errors.append('Field mismatch: ' + field)
        if data.get('deleted'):
            errors.append('Item is in trash')
        for path in r['collection_paths']:
            if collections.get(path) not in data.get('collections', []):
                errors.append('Missing collection: ' + path)
        actual_tags = {tag['tag'] for tag in data.get('tags', [])}
        for tag in r['tags']:
            if tag not in actual_tags:
                errors.append('Missing tag: ' + tag)
        for a in r['attachments']:
            try:
                child = client.get('items/' + a['key'])['data']
                if child.get('parentItem') != r['key']:
                    errors.append('Attachment parent mismatch: ' + a['key'])
                if client.file_hash(a['key'], plan['database']) != a['sha256']:
                    errors.append('PDF checksum mismatch: ' + a['key'])
            except (CuratorError, OSError) as e:
                errors.append('Attachment verification failed: ' + getattr(e, 'code', type(e).__name__))
        # "Write verified" is not the same as "all desired metadata has been accepted".
        gaps = [field for field, value in r['desired'].items() if not same_value(field, data.get(field), value)]
        records.append({'record_id': r['record_id'], 'key': r['key'], 'write_verified': not errors,
                        'metadata_complete_for_selected_edition': not gaps,
                        'metadata_gaps': gaps, 'errors': errors})
    return {'ok': all(r['write_verified'] for r in records) and not plan['blocked'],
            'verified_papers': sum(r['write_verified'] for r in records),
            'verified_pdfs': sum(len(r['attachments']) for r, v in zip(plan['records'], records) if v['write_verified']),
            'records': records, 'blocked': plan['blocked'],
            'note': 'Verified source metadata does not establish translation version identity. Unverified publication details are left empty.'}
