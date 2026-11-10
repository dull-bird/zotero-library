"""Source-verified catalog construction. No LLM calls and no PDF uploads."""
from __future__ import annotations

import csv
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from zotero_core import CuratorError, digest, load_json, save_json
from zotero_versions import arxiv_version, edition_source, validate_artifact_versions

ATOM = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
ARXIV = re.compile(r'(?<!\d)(\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(v\d+)?(?!\d)', re.I)
DOI = re.compile(r'10\.\d{4,9}/[^\s<>"\[\]]+', re.I)


def clean_text(text):
    return ' '.join((text or '').split())


def arxiv_id(text):
    match = ARXIV.search(str(text or ''))
    return match.group(1).lower() if match else None


def doi_id(text):
    match = DOI.search(urllib.parse.unquote(str(text or '')))
    return match.group(0).rstrip('.,;').lower() if match else None


def normalized_title(title):
    # Repository indexes often append a method acronym; it is not the formal title.
    title = re.sub(r'\s*\([A-Z][A-Z0-9-]{1,15}\)\s*$', '', title or '')
    return ''.join(c for c in unicodedata.normalize('NFKC', title).casefold() if c.isalnum())


def identifiers(data):
    # Only designated fields; do NOT scan abstracts/notes, which cite other papers.
    aid = arxiv_id(data.get('archiveID')) or arxiv_id(data.get('url'))
    if not aid:
        match = re.search(r'(?im)^arxiv\s*:\s*(.+)$', data.get('extra', ''))
        aid = arxiv_id(match.group(1)) if match else None
    doi = doi_id(data.get('DOI'))
    return {'arxiv': aid, 'doi': doi}


def title_matches(hint, official):
    a, b = normalized_title(hint), normalized_title(official)
    return bool(a and b and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.95))


def checked_file(root, relative):
    root = Path(root).resolve()
    given = root / relative
    # Reject both final symlinks and symlinked ancestors, even if they resolve inside root.
    try:
        parts = given.absolute().relative_to(root).parts
    except ValueError:
        raise CuratorError('Attachment is outside source root', 'UNSAFE_PATH')
    node = root
    for part in parts:
        node = node / part
        if part == '..' or node.is_symlink():
            raise CuratorError('Attachment traversal/symlink rejected', 'UNSAFE_PATH')
    path = given.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise CuratorError('Attachment is outside source root', 'UNSAFE_PATH')
    if not path.is_file() or path.suffix.lower() != '.pdf':
        raise CuratorError('Expected an existing PDF attachment: %s' % relative, 'PDF_MISSING')
    with path.open('rb') as f:
        if b'%PDF-' not in f.read(1024):
            raise CuratorError('Not a PDF: %s' % relative, 'INVALID_PDF')
    return path


def scan_repository(root, overrides=None, source_url=None):
    root = Path(root).expanduser().resolve()
    overrides = overrides or {}
    records = []
    by_identity = {}
    seen_paths = set()
    with (root / 'index.tsv').open(encoding='utf-8', newline='') as f:
        for row in csv.reader(f, delimiter='\t'):
            if not row:
                continue
            if len(row) != 4:
                raise CuratorError('index.tsv must have ID, title, translation date, relative PDF path')
            aid, title, translated, relative = row
            override = overrides.get(relative, {})
            raw_id = override.get('arxiv_id') or aid
            version = override.get('source_version') or arxiv_version(raw_id)
            aid = arxiv_id(raw_id)
            path = checked_file(root, relative)
            if str(path) in seen_paths:
                raise CuratorError('Duplicate PDF path in index: %s' % relative)
            seen_paths.add(str(path))
            record_id = 'arxiv:' + aid if aid else 'file:' + digest(path)
            record = {
                'record_id': record_id, 'arxiv_id': aid, 'metadata_version': version, 'title_hint': title,
                'identity_title': override.get('identity_title'),
                'identity_evidence': override.get('evidence'),
                'translation_date': translated, 'source_path': relative, 'source_paths': [relative],
                'attachments': [{'path': str(path), 'role': 'translation', 'language': 'zh-CN',
                    'ai_translated': True, 'sha256': digest(path), 'filename': path.name,
                    'source_edition': edition_source(aid, version, 'index-declared' if version else 'unknown') if aid else
                        {'platform': 'unknown', 'version': None, 'evidence': 'unknown'},
                    'translation_revision': override.get('translation_revision'), 'translation_date': translated}],
            }
            validate_artifact_versions(record)
            if record_id in by_identity:
                previous = by_identity[record_id]
                previous['attachments'].extend(record['attachments'])
                previous['source_paths'].append(relative)
                old_version = previous.get('metadata_version')
                if not old_version or not version:
                    previous['metadata_version'] = None
                elif int(version[1:]) > int(old_version[1:]):
                    previous['metadata_version'] = version
                    previous['title_hint'] = title
                    previous['identity_title'] = record.get('identity_title')
                    previous['identity_evidence'] = record.get('identity_evidence')
            else:
                by_identity[record_id] = record
                records.append(record)
    return {'format': 1, 'source_root': str(root), 'source_url': source_url,
            'source_name': root.name, 'records': records}


def parse_arxiv(xml):
    entries = {}
    for e in ET.fromstring(xml).findall('a:entry', ATOM):
        raw_id = e.findtext('a:id', default='', namespaces=ATOM)
        aid = arxiv_id(raw_id)
        if not aid:
            continue
        def text(name):
            return clean_text(e.findtext(name, default='', namespaces=ATOM))
        version = arxiv_version(raw_id)
        meta = {
            'arxiv_id': aid, 'version': version, 'version_url': raw_id.replace('http://', 'https://'),
            'title': text('a:title'), 'abstract': text('a:summary'),
            'authors': [clean_text(a.findtext('a:name', default='', namespaces=ATOM))
                        for a in e.findall('a:author', ATOM)],
            'submitted': text('a:published'), 'updated': text('a:updated'),
            'categories': [c.attrib['term'] for c in e.findall('a:category', ATOM)],
            'published_doi': text('x:doi'), 'journal_ref': text('x:journal_ref'),
            'comment': text('x:comment'),
        }
        if version:
            entries[aid + version] = meta
        existing = entries.get(aid)
        if not existing or int((version or 'v0')[1:]) >= int((existing.get('version') or 'v0')[1:]):
            entries[aid] = meta
    return entries


def fetch_public(url, max_bytes=10 * 1024 * 1024):
    req = urllib.request.Request(url, headers={'User-Agent': 'ZoteroLibraryCurator/1.0 (personal bibliography; no document uploads)'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as f:
                body = f.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise CuratorError('Metadata response exceeds size limit')
                return body
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == 2:
                raise CuratorError('Metadata source HTTP %s' % e.code, 'METADATA_UNAVAILABLE')
            retry = e.headers.get('Retry-After', '')
            time.sleep(min(int(retry), 30) if retry.isdigit() else 3 * (attempt + 1))
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise CuratorError('Metadata source is unavailable', 'METADATA_UNAVAILABLE')
            time.sleep(3 * (attempt + 1))
    raise CuratorError('Metadata source is unavailable')


def resolve_catalog(manifest, cache_dir, xml_files=()):
    """Official arXiv metadata only. Preserve journal-version candidates separately."""
    cache_dir = Path(cache_dir)
    entries, evidence = {}, {}
    for file in xml_files:
        raw = Path(file).read_bytes()
        parsed = parse_arxiv(raw)
        entries.update(parsed)
        evidence.update({aid: {'file': str(Path(file).resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}
                         for aid in parsed})
    wanted = sorted({r['arxiv_id'] + (r.get('metadata_version') or '')
                     for r in manifest['records'] if r.get('arxiv_id')})
    missing = []
    for aid in wanted:
        if aid in entries:
            continue
        cached = cache_dir / (aid.replace('/', '_') + '.xml')
        if cached.exists() and time.time() - cached.stat().st_mtime < 86400:
            parsed = parse_arxiv(cached.read_bytes())
            if aid in parsed:
                entries.update(parsed)
                evidence[aid] = {'file': str(cached.resolve()), 'sha256': digest(cached)}
                continue
        missing.append(aid)
    for offset in range(0, len(missing), 50):
        if offset:
            time.sleep(3.1)  # arXiv recommends at least 3 seconds between requests.
        batch = missing[offset:offset + 50]
        url = 'https://export.arxiv.org/api/query?' + urllib.parse.urlencode({
            'id_list': ','.join(batch), 'max_results': len(batch)})
        raw = fetch_public(url)
        parsed = parse_arxiv(raw)
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cached = cache_dir / ('batch-' + hashlib.sha256(raw).hexdigest()[:16] + '.xml')
        cached.write_bytes(raw)
        for aid in batch:
            if aid in parsed:
                alias = cache_dir / (aid.replace('/', '_') + '.xml')
                alias.write_bytes(raw)
                entries[aid] = parsed[aid]
                evidence[aid] = {'file': str(cached.resolve()), 'sha256': digest(cached)}
    result = dict(manifest)
    result['records'] = []
    for record in manifest['records']:
        r = __import__('copy').deepcopy(record)
        validate_artifact_versions(r)
        query_id = (r.get('arxiv_id') or '') + (r.get('metadata_version') or '')
        meta = entries.get(query_id)
        r['warnings'] = []
        if not meta:
            r.update({'status': 'needs-review', 'reason': 'No verified arXiv record; do not invent metadata'})
            result['records'].append(r)
            continue
        identity_title = r.get('identity_title') or r['title_hint']
        if not title_matches(identity_title, meta['title']):
            r.update({'status': 'needs-review', 'reason': 'Repository/PDF identity title does not match official record', 'candidate': meta})
            result['records'].append(r)
            continue
        if r.get('identity_title') and not r.get('identity_evidence'):
            r.update({'status': 'needs-review', 'reason': 'Title override requires independently inspected PDF evidence'})
            result['records'].append(r)
            continue
        if not meta['authors'] or not meta['submitted'] or not meta['abstract']:
            r.update({'status': 'needs-review', 'reason': 'Official metadata lacks essential fields'})
            result['records'].append(r)
            continue
        # Single-field names preserve spelling/order without guessing surname structure.
        data = {'itemType': 'preprint', 'title': meta['title'],
                'creators': [{'creatorType': 'author', 'name': name} for name in meta['authors']],
                'abstractNote': meta['abstract'], 'date': meta['submitted'][:10],
                'repository': 'arXiv', 'archiveID': meta['arxiv_id'],
                'url': 'https://arxiv.org/abs/' + meta['arxiv_id'],
                'libraryCatalog': 'arXiv', 'extra': 'arXiv: ' + meta['arxiv_id']}
        if meta['published_doi'] or meta['journal_ref'] or meta['comment']:
            r['warnings'].append('Publication/venue comments are recorded as evidence, not mixed into the preprint edition.')
        year_hint = r['source_path'].split('/')[0]
        if year_hint.isdigit() and year_hint != data['date'][:4]:
            r['warnings'].append('Folder year %s differs from official submission year %s.' % (year_hint, data['date'][:4]))
        if any(not a.get('source_edition', {}).get('version') for a in r['attachments']):
            r['warnings'].append('Attachment source version is unknown; latest official metadata does not prove PDF version identity.')
        r.update({'status': 'verified-arxiv', 'data': data, 'official': meta,
                  'provenance': {'kind': 'arxiv-atom', 'url': meta['version_url'],
                                 'edition': {'platform': 'arxiv', 'work_id': meta['arxiv_id'],
                                             'version': meta['version'], 'url': meta['version_url']},
                                 'raw': evidence[query_id],
                                 'fields': ['title', 'creators', 'abstractNote', 'date', 'repository', 'archiveID', 'url']}})
        result['records'].append(r)
    return result


def crossref_record(doi):
    """Separate publisher-edition candidate, never silently applied to a preprint."""
    normalized = doi_id(doi)
    if not normalized:
        raise CuratorError('Invalid DOI')
    url = 'https://api.crossref.org/works/' + urllib.parse.quote(normalized, safe='')
    raw = fetch_public(url)
    msg = json.loads(raw)['message']
    if doi_id(msg.get('DOI')) != normalized:
        raise CuratorError('Crossref DOI identity mismatch', 'IDENTITY_MISMATCH')
    kind = {'journal-article': 'journalArticle', 'proceedings-article': 'conferencePaper',
            'book': 'book', 'book-chapter': 'bookSection', 'report': 'report'}.get(msg.get('type'))
    if not kind:
        raise CuratorError('Unsupported Crossref type; inspect candidate manually')
    data = {'itemType': kind, 'title': clean_text(' '.join(msg.get('title', []))),
            'DOI': msg['DOI'], 'url': msg.get('URL', 'https://doi.org/' + normalized), 'creators': []}
    for author in msg.get('author', []):
        creator = {'creatorType': 'author'}
        creator.update({'name': author['name']} if author.get('name') else
                       {'firstName': author.get('given', ''), 'lastName': author.get('family', '')})
        data['creators'].append(creator)
    for source, target in [('volume', 'volume'), ('issue', 'issue'), ('page', 'pages'), ('publisher', 'publisher')]:
        if msg.get(source):
            data[target] = str(msg[source])
    container = clean_text(' '.join(msg.get('container-title', [])))
    if container and kind in ('journalArticle', 'conferencePaper', 'bookSection'):
        data[{'journalArticle': 'publicationTitle', 'conferencePaper': 'proceedingsTitle',
              'bookSection': 'bookTitle'}[kind]] = container
    dates = msg.get('published', {}).get('date-parts', [[]])[0]
    if dates:
        data['date'] = '-'.join(str(part) if i == 0 else '%02d' % part for i, part in enumerate(dates))
    return {'status': 'publisher-candidate', 'data': data,
            'provenance': {'url': url, 'sha256': hashlib.sha256(raw).hexdigest()}, 'raw': msg,
            'warning': 'Check title AND authors against the intended paper before selecting this publication edition.'}


def select_publication(catalog, record_id, candidate, schema):
    """Explicit edition selection after strict title AND ordered-author checks."""
    from zotero_workflow import creator_names, validate_data, allowed_fields
    if candidate.get('status') != 'publisher-candidate':
        raise CuratorError('Expected an independently fetched publisher candidate')
    result = __import__('copy').deepcopy(catalog)
    records = [r for r in result['records'] if r['record_id'] == record_id]
    if len(records) != 1 or records[0].get('status') != 'verified-arxiv':
        raise CuratorError('Select exactly one verified arXiv record')
    record = records[0]
    data = candidate['data']
    if not title_matches(record['data']['title'], data.get('title', '')):
        raise CuratorError('Publication and preprint titles do not match', 'IDENTITY_MISMATCH')
    original = creator_names(record['data'].get('creators', []))
    published = creator_names(data.get('creators', []))
    common = set(original) & set(published)
    if (not original or not published or original[0] != published[0]
            or len(common) < min(2, len(original), len(published))
            or len(common) / max(len(original), len(published)) < 0.5):
        raise CuratorError('Publication authors do not establish identity; inspect manually', 'IDENTITY_MISMATCH')
    official_doi = doi_id(record['official'].get('published_doi'))
    if official_doi and official_doi != doi_id(data.get('DOI')):
        raise CuratorError('Publisher DOI contradicts arXiv publication DOI', 'IDENTITY_MISMATCH')
    allowed = allowed_fields(schema, data['itemType'])
    selected = {k: v for k, v in data.items() if k in allowed}
    selected['extra'] = 'arXiv: ' + record['arxiv_id']
    validate_data(selected, schema)
    record.update({'status': 'verified-publication', 'data': selected,
                   'publication': candidate,
                   'provenance': dict(candidate['provenance'], kind='crossref',
                                      edition={'platform': 'publisher', 'identifier': selected.get('DOI'),
                                               'version': candidate.get('raw', {}).get('version'),
                                               'url': selected.get('url')},
                                      identity_check='title, first author, author overlap, DOI when provided by arXiv')})
    record['warnings'].append('Bibliographic metadata now describes the publication edition; attached translated PDF may describe the preprint edition.')
    return result


def classify(data, source_name, topics=None):
    title = data.get('title', '')
    rules = topics or [
        ('医疗AI', r'clinical|diagnostic|ambulatory|conversational.*health'),
        ('生理感知与rPPG', r'rppg|facephys|photoplethysm|contactless vitals|remote ppg'),
        ('机器人与路径规划', r'shift planner|coverage.*planning|trajectory optimization'),
        ('世界模型与视觉规划', r'jepa|world models|visual planning'),
        ('目标检测', r'object detection|detr'),
        ('三维视觉与重建', r'3d|geometry|geometric|stereo|depth|gaussian|splat|4d|bundle adjustment'),
    ]
    matched = [name for name, regex in rules if re.search(regex, title, re.I)]
    publication_type = {'preprint': '预印本', 'journalArticle': '期刊论文',
        'conferencePaper': '会议论文', 'book': '图书', 'bookSection': '书籍章节',
        'thesis': '学位论文', 'report': '技术报告', 'dataset': '数据集'}.get(data['itemType'], '其他')
    root = '文献库'
    paths = [root, root + '/02_文献类型/' + publication_type,
             root + '/04_来源/' + source_name]
    paths.extend(root + '/01_主题/' + topic for topic in (matched or ['待分类']))
    if re.match(r'^\d{4}', data.get('date', '')):
        paths.append(root + '/03_年份/' + data['date'][:4])
    tags = ['来源:' + source_name]
    tags.extend('主题:' + topic for topic in matched)
    if re.search(r'\bsurvey\b|systematic review', title, re.I):
        tags.append('研究类型:综述（题名规则）')
    elif re.search(r'\btoolbox\b|\bbenchmark\b|\bdataset\b', title, re.I):
        tags.append('研究类型:工具或基准（题名规则）')
    return paths, tags
