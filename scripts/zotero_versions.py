"""Version-aware artifact provenance. A metadata version is NOT a PDF version."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.parse
import urllib.request

from zotero_core import CuratorError, digest, load_json, private_dir, save_json


def arxiv_version(value):
    match = re.search(r'(v\d+)(?:\.pdf)?(?:[?#].*)?$', str(value or ''), re.I)
    if match and int(match.group(1)[1:]) < 1:
        raise CuratorError('arXiv versions start at v1')
    return match.group(1).lower() if match else None


def edition_source(aid, version=None, evidence=None):
    return {'platform': 'arxiv', 'work_id': aid, 'version': version,
            'url': 'https://arxiv.org/abs/' + aid + (version or ''),
            'evidence': evidence or ('declared-by-import' if version else 'unknown'),
            'version_verified_by_download': False}


def attachment_title(attachment):
    source = attachment.get('source_edition') or {}
    version = source.get('version')
    platform = source.get('platform')
    edition = ('arXiv ' if platform == 'arxiv' else (platform + ' ' if platform else '')) + str(version) if version else '原文版本未知'
    role = attachment['role']
    title = {'translation': '中文译文（AI 翻译）', 'original': '原文', 'supplemental': '补充材料'}.get(role, role)
    title += ' · ' + edition
    if role == 'translation' and attachment.get('translation_revision'):
        title += ' · 译文 ' + attachment['translation_revision']
    if attachment.get('archive_revision'):
        title += ' · 归档 ' + attachment['archive_revision']
    return title


def validate_artifact_versions(record):
    """Reject conflicting work identities; never infer a version from a filename."""
    for attachment in record['attachments']:
        source = attachment.get('source_edition')
        if source is None:
            attachment['source_edition'] = edition_source(record.get('arxiv_id')) if record.get('arxiv_id') else {
                'platform': 'unknown', 'version': None, 'evidence': 'unknown'}
            source = attachment['source_edition']
        if source.get('platform') == 'arxiv':
            if source.get('work_id') != record.get('arxiv_id'):
                raise CuratorError('Attachment belongs to a different arXiv work', 'IDENTITY_MISMATCH')
            if source.get('version') and not re.fullmatch(r'v[1-9]\d*', source['version']):
                raise CuratorError('Invalid arXiv source version')
        revision = attachment.get('translation_revision')
        if revision and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}', revision):
            raise CuratorError('Invalid translation revision label')
    return record


def attach_receipt(manifest, receipt_path, state=None):
    """Pair a locally recorded download with a translation without claiming its accuracy."""
    receipt = load_json(receipt_path)
    if receipt.get('format') != 1 or receipt.get('producer') != 'zotero-library/pinned-arxiv-download':
        raise CuratorError('Unrecognized download receipt')
    source = copy.deepcopy(receipt['source_edition'])
    if (not re.fullmatch(r'[a-f0-9]{64}', receipt.get('sha256', ''))
            or source.get('platform') != 'arxiv'
            or not re.fullmatch(r'v[1-9]\d*', str(source.get('version', '')))):
        raise CuratorError('Invalid pinned download receipt fields')
    saved = Path(state) / 'download-receipts' / (receipt['sha256'] + '.json') if state else None
    trusted = bool(saved and saved.exists() and load_json(saved) == receipt)
    if not trusted:
        source['version_verified_by_download'] = False
        source['evidence'] = 'external-receipt-declaration; origin not independently verified'
    for record in manifest['records']:
        if source.get('work_id') != record.get('arxiv_id'):
            raise CuratorError('Download receipt belongs to a different paper', 'IDENTITY_MISMATCH')
        requested = record.get('metadata_version')
        if requested and requested != source['version']:
            raise CuratorError('Requested version contradicts download receipt', 'IDENTITY_MISMATCH')
        record['metadata_version'] = source['version']
        for a in record['attachments']:
            if a['role'] == 'original':
                if digest(a['path']) != receipt['sha256']:
                    raise CuratorError('Original PDF bytes do not match download receipt', 'SOURCE_CHANGED')
                a['source_edition'] = copy.deepcopy(source)
                a['download_receipt_sha256'] = digest(receipt_path)
            elif a['role'] == 'translation':
                # This pairing is declared by the caller/translation task, not proven by PDF similarity.
                a['source_edition'] = dict(source, evidence='translation-task-paired-to-download')
                a['derived_from_sha256'] = receipt['sha256']
                a['translation_content_verified'] = False
                a['download_receipt_sha256'] = digest(receipt_path)
    return manifest


def fetch_original(versioned_id, destination, state):
    """Explicit, pinned public download; no replacement of old files, no TeX execution."""
    from zotero_catalog import arxiv_id, fetch_public, parse_arxiv
    aid, version = arxiv_id(versioned_id), arxiv_version(versioned_id)
    if not aid or not version:
        raise CuratorError('Specify an exact arXiv version such as 2509.19297v2; do not download a floating latest URL')
    query_id = aid + version
    xml = fetch_public('https://export.arxiv.org/api/query?' + urllib.parse.urlencode({'id_list': query_id}))
    metadata = parse_arxiv(xml).get(query_id)
    if not metadata or metadata.get('version') != version:
        raise CuratorError('arXiv did not return the requested version', 'VERSION_UNCONFIRMED')
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise CuratorError('Download destination already exists; old versions are never overwritten', 'FILE_CONFLICT')
    private_dir(destination.parent)
    url = 'https://arxiv.org/pdf/' + query_id
    request = urllib.request.Request(url, headers={'User-Agent': 'ZoteroLibraryCurator/1.0'})
    fd, temporary = tempfile.mkstemp(prefix='.zotero-download-', dir=str(destination.parent))
    try:
        sha = hashlib.sha256()
        total = 0
        with os.fdopen(fd, 'wb') as out, urllib.request.urlopen(request, timeout=90) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != 'https' or final.hostname not in ('arxiv.org', 'export.arxiv.org'):
                raise CuratorError('Unexpected redirect for pinned original PDF', 'UNSAFE_ORIGIN')
            final_id = final.path.rsplit('/', 1)[-1].removesuffix('.pdf')
            if final_id != query_id and not final.path.removesuffix('.pdf').endswith('/' + query_id):
                raise CuratorError('Download URL no longer identifies the requested version', 'VERSION_UNCONFIRMED')
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                if total == 0 and b'%PDF-' not in block[:1024]:
                    raise CuratorError('Pinned download did not return a PDF', 'INVALID_PDF')
                total += len(block)
                if total > 256 * 1024 * 1024:
                    raise CuratorError('Original PDF exceeds 256 MiB safety limit')
                sha.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if not total:
            raise CuratorError('Empty PDF download', 'INVALID_PDF')
        # A hard link atomically refuses replacement if another process created destination.
        os.link(temporary, destination)
        source = dict(edition_source(aid, version, 'pinned-official-download'),
                      version_verified_by_download=True, url=url)
        receipt = {'format': 1, 'producer': 'zotero-library/pinned-arxiv-download',
                   'source_edition': source, 'sha256': sha.hexdigest(), 'bytes': total,
                   'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'metadata_sha256': hashlib.sha256(xml).hexdigest(),
                   'title': metadata['title'], 'metadata_version_url': metadata['version_url']}
        path = Path(state) / 'download-receipts' / (receipt['sha256'] + '.json')
        save_json(path, receipt)
        save_json(str(destination) + '.source.json', receipt)
        return {'file': str(destination), 'receipt': str(path), 'source_edition': source,
                'sha256': receipt['sha256'], 'bytes': total}
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def artifact_history(library):
    """Read only our structured provenance notes; ignore ordinary user notes."""
    history = {}
    for item in (library or {}).get('items', []):
        data = item['data']
        if data.get('itemType') != 'note' or '系统:来源与版本记录' not in {t['tag'] for t in data.get('tags', [])}:
            continue
        match = re.search(r'<pre[^>]*>(.*?)</pre>', data.get('note', ''), re.S | re.I)
        if not match:
            continue
        try:
            payload = json.loads(html.unescape(match.group(1)))
        except ValueError:
            continue
        if not payload.get('record_id'):
            continue
        for attachment in payload.get('attachments', []):
            if isinstance(attachment, dict) and attachment.get('sha256'):
                history.setdefault(payload['record_id'], []).append(attachment)
    return history


def preserve_archive_revision(attachment, prior):
    """rN is our archive sequence, never an upstream scientific version number."""
    prior = [a for a in prior if a.get('role') == attachment.get('role')]
    previous = [a for a in prior if a.get('sha256') == attachment['sha256']]
    known = next((a for a in previous if a.get('archive_revision')), None)
    if known:
        attachment['archive_revision'] = known['archive_revision']
        old_source = known.get('source_edition', {})
        source = attachment.get('source_edition', {})
        if not source.get('version') and old_source.get('version'):
            attachment['source_edition'] = copy.deepcopy(old_source)
        elif (source.get('version') and old_source.get('version')
              and source.get('version') != old_source.get('version')):
            raise CuratorError('Identical PDF bytes were associated with different source versions; review provenance instead of relabeling', 'VERSION_CONFLICT')
    else:
        numbers = [int(a['archive_revision'][1:]) for a in prior
                   if re.fullmatch(r'r[1-9]\d*', a.get('archive_revision', ''))]
        attachment['archive_revision'] = 'r%d' % (max(numbers, default=0) + 1)
    return attachment


def check_updates(catalog, cache_dir, xml_files=(), library=None):
    """Check latest-known metadata. Never overwrite/download/retranslate artifacts."""
    from zotero_catalog import resolve_catalog, crossref_record
    floating = copy.deepcopy(catalog)
    for r in floating['records']:
        r['metadata_version'] = None
    latest = resolve_catalog(floating, cache_dir, xml_files)
    by_id = {r['record_id']: r for r in latest['records']}
    result = []
    history = artifact_history(library)
    for old in catalog['records']:
        current = by_id[old['record_id']]
        meta = current.get('official') if current.get('status') == 'verified-arxiv' else current.get('candidate')
        head = meta.get('version') if meta else None
        all_attachments = old['attachments'] + history.get(old['record_id'], [])
        by_hash = {}
        for artifact in all_attachments:
            key = artifact.get('sha256')
            previous = by_hash.get(key)
            if previous is None or (artifact.get('source_edition', {}).get('version') and
                                    not previous.get('source_edition', {}).get('version')):
                by_hash[key] = artifact
        attachments = list(by_hash.values())
        known = sorted({a.get('source_edition', {}).get('version') for a in attachments
                        if a.get('source_edition', {}).get('platform') == 'arxiv'
                        and a.get('source_edition', {}).get('version')}, key=lambda v: int(v[1:]))
        unknown = sum(not a.get('source_edition', {}).get('version') for a in attachments)
        newer = bool(head and known and int(head[1:]) > max(int(v[1:]) for v in known))
        selected = old.get('provenance', {}).get('edition', {}).get('version')
        entry = {'record_id': old['record_id'], 'latest_known_arxiv_version': head,
                 'selected_metadata_version': selected, 'known_source_versions': known,
                 'unknown_source_version_attachments': unknown,
                 'newer_than_known_attachments': newer,
                 'unknown_attachments_are_not_assumed_outdated': True,
                 'action': 'report-only; keep old PDFs, translations and annotations',
                 'evidence': current.get('provenance', {}).get('raw')}
        publication = old.get('publication')
        if publication and publication['data'].get('DOI'):
            candidate = crossref_record(publication['data']['DOI'])
            entry['publisher_check'] = {
                'doi': candidate['data']['DOI'],
                'upstream_declared_version': candidate['raw'].get('version'),
                'relations': candidate['raw'].get('relation', {}),
                'bibliographic_metadata_changed': candidate['data'] != publication['data'],
                'metadata_change_does_not_prove_new_pdf_version': True,
            }
        result.append(entry)
    return {'records': result, 'automatic_file_changes': 0,
            'note': 'Latest known metadata may come from cache/provided XML. Version history includes declared catalog sources and archived provenance, not a fresh verification of every stored PDF. Publisher timestamps and hashes alone do not establish a named edition.'}
