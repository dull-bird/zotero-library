"""Offline tests only. Never use the user's Zotero database, keys or network."""
import contextlib
import copy
import hashlib
import http.server
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from zotero_core import (CuratorError, LocalClient, date_from_storage, digest, object_key, save_json, snapshot)
from zotero_catalog import (arxiv_id, checked_file, classify, doi_id, identifiers,
                            normalized_title, parse_arxiv, resolve_catalog, scan_repository, select_publication)
from zotero_workflow import (apply_plan, build_plan, collection_paths, creator_names,
                             validate_data, verify_plan)
from zotero_versions import (arxiv_version, edition_source, attachment_title, attach_receipt,
                             validate_artifact_versions, preserve_archive_revision, check_updates, fetch_original)
from zotero_library import parser, make_manifest

SCHEMA = {'itemTypes': [
    {'itemType': kind, 'fields': [{'field': f} for f in fields],
     'creatorTypes': [{'creatorType': 'author'}]} for kind, fields in [
        ('preprint', ['title', 'abstractNote', 'date', 'repository', 'archiveID', 'url', 'libraryCatalog', 'extra']),
        ('report', ['title', 'abstractNote', 'date', 'url', 'libraryCatalog', 'extra']),
        ('journalArticle', ['title', 'abstractNote', 'date', 'url', 'DOI', 'publicationTitle', 'extra'])]]}
AUDIT = {'hashes': {'schema': 'fixture'}, 'version': '10.0-test'}
SID = 'offline-test-server'
TITLE = 'Example Geometry: A Verified Reconstruction Method'
XML = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2412.10001v2</id><title>Example Geometry: A Verified Reconstruction Method</title>
<published>2024-12-14T06:32:20Z</published><updated>2025-04-24T01:16:48Z</updated>
<summary>An official abstract, not an AI summary.</summary><author><name>Given Family</name></author>
<author><name>Mar\xc3\xada de la Cruz</name></author><category term="cs.CV"/>
<arxiv:comment>Accepted at a conference; pages not provided</arxiv:comment></entry></feed>'''


class FakeClient:
    """Contract-level mock: version checks, files, collections; no real Zotero."""
    def __init__(self, root):
        self.server_id = SID
        self.key = 'FAKE-NOT-A-REAL-KEY'
        self.root = root
        self.items = {}
        self.collections = {}
        self.writes = []
        self.fail_upload = False

    def probe(self):
        return {'server_id': self.server_id}

    def get(self, path):
        url = urllib.parse.urlsplit(path)
        if url.path == 'collections':
            return copy.deepcopy(list(self.collections.values()))
        if url.path == 'items':
            keys = urllib.parse.parse_qs(url.query).get('itemKey', [''])[0].split(',')
            return copy.deepcopy([self.items[k] for k in keys if k in self.items])
        if url.path.startswith('items/'):
            key = url.path.split('/')[1]
            if key in self.items:
                return copy.deepcopy(self.items[key])
        raise CuratorError('not found', 'HTTP_404')

    def maybe_get(self, path):
        try:
            return self.get(path)
        except CuratorError as e:
            if e.code == 'HTTP_404':
                return None
            raise

    def write_batch(self, kind, objects):
        target = self.items if kind == 'items' else self.collections
        for obj in objects:
            old = target.get(obj['key'])
            if old and old['version'] != obj['version']:
                raise CuratorError('concurrent edit', 'STALE_VERSION')
            if not old and obj['version'] != 0:
                raise CuratorError('missing object', 'STALE_VERSION')
            data = dict(old['data'] if old else {}, **copy.deepcopy(obj))
            version = old['version'] + 1 if old else 1
            data['version'] = version
            target[obj['key']] = {'key': obj['key'], 'version': version, 'data': data}
        self.writes.append((kind, copy.deepcopy(objects)))
        return {'successful': {str(i): target[obj['key']] for i, obj in enumerate(objects)}}

    def upload(self, key, path):
        if self.fail_upload:
            raise CuratorError('simulated timeout', 'REQUEST_UNCERTAIN')
        self.writes.append(('upload', key))
        dest = self.root / 'storage' / key
        dest.mkdir(parents=True, exist_ok=True)
        file = dest / Path(path).name
        file.write_bytes(Path(path).read_bytes())
        self.items[key]['data']['_file'] = str(file)

    def file_hash(self, key, db):
        value = self.items.get(key, {}).get('data', {}).get('_file')
        if value and Path(value).is_file():
            return digest(value)
        raise CuratorError('missing file', 'HTTP_404')


class CatalogFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / '2026').mkdir()
        self.pdf = self.root / '2026/paper_CN.pdf'
        self.pdf.write_bytes(b'%PDF-1.7\nnot rendered in this test\n%%EOF')
        (self.root / 'index.tsv').write_text('2412.10001\t' + TITLE + '\t2026-04-25\t2026/paper_CN.pdf\n')
        self.xml = self.root / 'official.xml'
        self.xml.write_bytes(XML)

    def tearDown(self):
        self.tmp.cleanup()

    def catalog(self):
        return resolve_catalog(scan_repository(self.root), self.root / 'cache', [self.xml])

class CatalogTests(CatalogFixture, unittest.TestCase):
    def test_identifier_versions_and_old_style(self):
        self.assertEqual(arxiv_id('https://arxiv.org/pdf/2412.10001v3.pdf'), '2412.10001')
        self.assertEqual(arxiv_id('arXiv:hep-th/9901001v2'), 'hep-th/9901001')
        self.assertEqual(doi_id('https://doi.org/10.1000/ABC.'), '10.1000/abc')

    def test_dont_extract_cited_identifiers_from_abstract(self):
        self.assertEqual(identifiers({'abstractNote': 'cites 2412.10001'}), {'arxiv': None, 'doi': None})

    def test_official_date_not_translation_or_folder_date(self):
        r = self.catalog()['records'][0]
        self.assertEqual(r['data']['date'], '2024-12-14')
        self.assertEqual(r['translation_date'], '2026-04-25')
        self.assertTrue(any('Folder year' in s for s in r['warnings']))

    def test_full_author_names_order_preserved_without_surname_guess(self):
        r = self.catalog()['records'][0]
        self.assertEqual(r['data']['creators'], [
            {'creatorType': 'author', 'name': 'Given Family'},
            {'creatorType': 'author', 'name': 'María de la Cruz'}])

    def test_preprint_venue_not_invented_from_comment(self):
        r = self.catalog()['records'][0]
        self.assertNotIn('publicationTitle', r['data'])
        self.assertNotIn('pages', r['data'])
        self.assertEqual(r['data']['itemType'], 'preprint')

    def test_unknown_translation_version_is_explicit(self):
        self.assertTrue(any('unknown' in s for s in self.catalog()['records'][0]['warnings']))

    def test_title_mismatch_blocks(self):
        m = scan_repository(self.root)
        m['records'][0]['title_hint'] = 'A Completely Different Paper'
        c = resolve_catalog(m, self.root / 'cache', [self.xml])
        self.assertEqual(c['records'][0]['status'], 'needs-review')

    def test_alias_override_needs_pdf_evidence(self):
        m = scan_repository(self.root)
        m['records'][0]['identity_title'] = TITLE
        c = resolve_catalog(m, self.root / 'cache', [self.xml])
        self.assertEqual(c['records'][0]['status'], 'needs-review')

    def test_missing_id_does_not_trigger_title_guess(self):
        m = scan_repository(self.root)
        m['records'][0]['arxiv_id'] = None
        m['records'][0]['attachments'][0]['source_edition'] = {'platform': 'unknown', 'version': None}
        c = resolve_catalog(m, self.root / 'cache', [self.xml])
        self.assertEqual(c['records'][0]['status'], 'needs-review')

    def test_path_traversal_rejected(self):
        with self.assertRaises(CuratorError):
            checked_file(self.root, '../outside.pdf')

    def test_symlink_rejected_even_within_root(self):
        (self.root / 'alias.pdf').symlink_to(self.pdf)
        with self.assertRaises(CuratorError):
            checked_file(self.root, 'alias.pdf')

    def test_not_pdf_rejected(self):
        self.pdf.write_text('not a PDF')
        with self.assertRaises(CuratorError):
            scan_repository(self.root)

    def test_title_acronym_not_part_of_formal_title(self):
        self.assertEqual(normalized_title('End-to-End Object Detection with Transformers (DETR)'),
                         normalized_title('End-to-End Object Detection with Transformers'))

    def test_top_level_collection_is_an_aggregate_not_an_empty_folder(self):
        paths, _ = classify(self.catalog()['records'][0]['data'], 'papers-cn')
        self.assertIn('文献库', paths)

    def test_multiple_collection_dimensions(self):
        paths, tags = classify({'itemType': 'preprint', 'title': TITLE, 'date': '2024-12-14'}, 'papers-cn')
        self.assertIn('文献库/03_年份/2024', paths)
        self.assertIn('文献库/02_文献类型/预印本', paths)
        self.assertIn('文献库/01_主题/三维视觉与重建', paths)

    def test_schema_rejects_venue_field_on_preprint(self):
        with self.assertRaises(CuratorError):
            validate_data({'itemType': 'preprint', 'publicationTitle': 'invented'}, SCHEMA)

    def test_schema_rejects_wrong_creator_type(self):
        with self.assertRaises(CuratorError):
            validate_data({'itemType': 'preprint', 'creators': [{'creatorType': 'invented', 'name': 'A'}]}, SCHEMA)

    def test_source_manifest_hash_detects_changed_pdf(self):
        c = self.catalog()
        self.pdf.write_bytes(b'%PDF-1.7\nchanged')
        with self.assertRaises(CuratorError):
            build_plan(c, {'server_id': SID, 'items': [], 'collections': []}, SCHEMA, AUDIT)


class WorkflowTests(CatalogFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db = self.root / 'db/zotero.sqlite'
        self.db.parent.mkdir()
        with sqlite3.connect(str(self.db)) as c:
            c.execute('CREATE TABLE fixture(value TEXT)')
        self.client = FakeClient(self.db.parent)
        self.state = self.root / 'state'

    def plan(self, items=None, approvals=None):
        return build_plan(self.catalog(), {'server_id': SID, 'items': items or [], 'collections': []},
                          SCHEMA, AUDIT, self.db, approvals)

    def existing(self, **changes):
        d = {'key': 'ABCD2345', 'version': 7, 'itemType': 'report', 'title': TITLE,
             'url': 'https://arxiv.org/abs/2412.10001', 'creators': [], 'tags': [], 'collections': []}
        d.update(changes)
        return {'key': d['key'], 'version': 7, 'data': d}

    def test_strong_id_updates_existing_not_duplicate(self):
        plan = self.plan([self.existing()])
        self.assertEqual(plan['summary']['existing_papers'], 1)
        self.assertEqual(plan['records'][0]['key'], 'ABCD2345')

    def test_nonempty_metadata_preserved_and_conflict_reported(self):
        plan = self.plan([self.existing(title='Human custom title')])
        r = plan['records'][0]
        self.assertNotIn('title', r['patch'])
        self.assertTrue(any(c['field'] == 'title' for c in r['conflicts']))

    def test_empty_authors_filled_and_date_precision_refined(self):
        r = self.plan([self.existing(date='2024')])['records'][0]
        self.assertEqual(len(r['patch']['creators']), 2)
        self.assertEqual(r['patch']['date'], '2024-12-14')

    def test_explicit_approval_allows_type_correction(self):
        r = self.plan([self.existing()], {'arxiv:2412.10001': ['itemType']})['records'][0]
        self.assertEqual(r['patch']['itemType'], 'preprint')
        self.assertEqual(r['patch']['repository'], 'arXiv')

    def test_type_approval_cannot_silently_drop_populated_fields(self):
        schema = copy.deepcopy(SCHEMA)
        schema['itemTypes'][1]['fields'].append({'field': 'institution'})
        old = self.existing(institution='Human curated institution')
        plan = build_plan(self.catalog(), {'server_id': SID, 'items': [old], 'collections': []},
                          schema, AUDIT, self.db, {'arxiv:2412.10001': ['itemType']})
        self.assertEqual(plan['summary']['blocked'], 1)

    def test_same_pdf_bytes_cannot_be_counted_as_two_attachments(self):
        catalog = self.catalog()
        catalog['records'][0]['attachments'].append(copy.deepcopy(catalog['records'][0]['attachments'][0]))
        with self.assertRaises(CuratorError):
            build_plan(catalog, {'server_id': SID, 'items': [], 'collections': []}, SCHEMA, AUDIT, self.db)

    def test_unapproved_type_change_not_applied(self):
        r = self.plan([self.existing()])['records'][0]
        self.assertNotIn('itemType', r['patch'])
        self.assertIn('repository', r['omitted'])

    def test_repeated_identifier_candidates_block(self):
        a, b = self.existing(), self.existing(key='BCDE3456')
        plan = self.plan([a, b])
        self.assertEqual(plan['summary']['ready'], 0)
        self.assertEqual(plan['summary']['blocked'], 1)

    def test_title_only_duplicate_blocks_not_imports(self):
        plan = self.plan([self.existing(url='')])
        self.assertEqual(plan['summary']['blocked'], 1)

    def test_conflicting_strong_identifiers_block(self):
        catalog = self.catalog()
        catalog['records'][0]['data']['DOI'] = '10.1000/new'
        old = self.existing(DOI='10.1000/different')
        schema = copy.deepcopy(SCHEMA)
        schema['itemTypes'][0]['fields'].append({'field': 'DOI'})
        plan = build_plan(catalog, {'server_id': SID, 'items': [old], 'collections': []}, schema, AUDIT, self.db)
        self.assertEqual(plan['summary']['blocked'], 1)

    def test_full_apply_and_verify(self):
        plan = self.plan()
        result = apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        self.assertTrue(result['ok'])
        self.assertEqual(result['verified_papers'], 1)
        self.assertEqual(result['verified_pdfs'], 1)
        self.assertTrue(result['records'][0]['metadata_complete_for_selected_edition'])
        self.assertTrue((Path(result['run_directory']) / 'database-backup/zotero.sqlite').exists())

    def test_reapply_is_noop_no_duplicate_items_or_pdfs(self):
        plan = self.plan()
        apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        count, writes = len(self.client.items), len(self.client.writes)
        apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        self.assertEqual(len(self.client.items), count)
        self.assertEqual(len(self.client.writes), writes)

    def test_interrupted_upload_resumes_without_duplicate_attachment(self):
        plan = self.plan()
        self.client.fail_upload = True
        with self.assertRaises(CuratorError):
            apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        count = len(self.client.items)
        self.client.fail_upload = False
        result = apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        self.assertTrue(result['ok'])
        # Only the provenance note is newly created on successful completion.
        self.assertEqual(len(self.client.items), count + 1)

    def test_concurrent_edit_blocks_before_collection_writes(self):
        existing = self.existing()
        plan = self.plan([existing])
        edited = copy.deepcopy(existing)
        edited['version'] += 1
        edited['data']['title'] = 'Changed after plan'
        self.client.items[edited['key']] = edited
        with self.assertRaises(CuratorError):
            apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        self.assertEqual(self.client.writes, [])

    def test_changed_identity_rejected_even_if_other_planned_fields_match(self):
        plan = self.plan()
        r = plan['records'][0]
        data = dict(r['patch'], url='https://arxiv.org/abs/9999.12345', archiveID='9999.12345')
        self.client.items[r['key']] = {'key': r['key'], 'version': 1, 'data': data}
        with self.assertRaises(CuratorError) as error:
            apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        self.assertEqual(error.exception.code, 'IDENTITY_MISMATCH')
        self.assertEqual(self.client.writes, [])

    def test_server_change_rejected(self):
        plan = self.plan()
        self.client.server_id = 'another-database'
        with self.assertRaises(CuratorError):
            apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        self.assertEqual(self.client.writes, [])

    def test_source_update_rejected(self):
        plan = self.plan()
        with self.assertRaises(CuratorError):
            apply_plan(plan, self.client, SCHEMA, {'hashes': {'schema': 'changed'}}, self.state)
        self.assertEqual(self.client.writes, [])

    def test_changed_source_pdf_rejected_before_writes(self):
        plan = self.plan()
        self.pdf.write_bytes(b'%PDF-1.7 different')
        with self.assertRaises(CuratorError):
            apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        self.assertEqual(self.client.writes, [])

    def test_new_translation_bytes_archive_r2_and_keep_old_pdf(self):
        first_plan = self.plan()
        apply_plan(first_plan, self.client, SCHEMA, AUDIT, self.state)
        old_key = first_plan['records'][0]['attachments'][0]['key']
        old_hash = self.client.file_hash(old_key, self.db)
        self.pdf.write_bytes(b'%PDF-1.7 a revised translation')
        library = {'server_id': SID, 'items': list(self.client.items.values()),
                   'collections': list(self.client.collections.values())}
        second_plan = build_plan(self.catalog(), library, SCHEMA, AUDIT, self.db)
        a = second_plan['records'][0]['attachments'][0]
        self.assertEqual(a['archive_revision'], 'r2')
        self.assertEqual(second_plan['summary']['new_papers'], 0)
        apply_plan(second_plan, self.client, SCHEMA, AUDIT, self.state)
        self.assertEqual(self.client.file_hash(old_key, self.db), old_hash)
        self.assertEqual(sum(i['data']['itemType'] == 'attachment' for i in self.client.items.values()), 2)

    def test_existing_pdf_not_overwritten(self):
        plan = self.plan()
        apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)
        key = plan['records'][0]['attachments'][0]['key']
        Path(self.client.items[key]['data']['_file']).write_bytes(b'%PDF-1.7 user changed file')
        with self.assertRaises(CuratorError):
            apply_plan(plan, self.client, SCHEMA, AUDIT, self.state)

    def test_collection_cycle_rejected(self):
        with self.assertRaises(CuratorError):
            collection_paths([{'key': 'A', 'data': {'name': 'a', 'parentCollection': 'B'}},
                              {'key': 'B', 'data': {'name': 'b', 'parentCollection': 'A'}}])

    def test_duplicate_collection_path_rejected(self):
        with self.assertRaises(CuratorError):
            collection_paths([{'key': 'A', 'data': {'name': 'same'}}, {'key': 'B', 'data': {'name': 'same'}}])

    def test_snapshot_must_not_write_live_directory(self):
        before = digest(self.db)
        with self.assertRaises(CuratorError):
            with snapshot(self.db, self.db.parent):
                pass
        self.assertEqual(before, digest(self.db))

    def test_existing_backup_not_overwritten(self):
        out = self.root / 'previous-backup'
        out.mkdir()
        existing = out / 'zotero.sqlite'
        existing.write_text('previous backup must survive')
        with self.assertRaises(CuratorError):
            with snapshot(self.db, out):
                pass
        self.assertEqual(existing.read_text(), 'previous backup must survive')

    def test_wal_is_included_in_snapshot(self):
        c = sqlite3.connect(str(self.db))
        c.execute('PRAGMA journal_mode=WAL')
        c.execute("INSERT INTO fixture VALUES('committed-in-WAL')")
        c.commit()
        with snapshot(self.db) as read:
            self.assertEqual(read.execute('SELECT value FROM fixture').fetchone()[0], 'committed-in-WAL')
        c.close()


class EditionTests(CatalogFixture, unittest.TestCase):
    def candidate(self):
        return {'status': 'publisher-candidate', 'data': {
            'itemType': 'journalArticle', 'title': TITLE, 'date': '2025-06-01',
            'DOI': '10.1000/example', 'publicationTitle': 'Verified Journal',
            'creators': [{'creatorType': 'author', 'firstName': 'Given', 'lastName': 'Family'},
                         {'creatorType': 'author', 'name': 'María de la Cruz'}]},
            'provenance': {'url': 'https://api.crossref.org/works/10.1000%2Fexample', 'sha256': 'test'}}

    def test_verified_publication_can_be_selected_without_mixing_dates(self):
        selected = select_publication(self.catalog(), 'arxiv:2412.10001', self.candidate(), SCHEMA)
        record = selected['records'][0]
        self.assertEqual(record['status'], 'verified-publication')
        self.assertEqual(record['data']['date'], '2025-06-01')
        self.assertEqual(record['official']['submitted'], '2024-12-14T06:32:20Z')
        self.assertEqual(record['data']['extra'], 'arXiv: 2412.10001')

    def test_publication_wrong_authors_rejected(self):
        candidate = self.candidate()
        candidate['data']['creators'][0]['firstName'] = 'Different'
        with self.assertRaises(CuratorError):
            select_publication(self.catalog(), 'arxiv:2412.10001', candidate, SCHEMA)

    def test_publication_wrong_title_rejected(self):
        candidate = self.candidate()
        candidate['data']['title'] = 'A different paper'
        with self.assertRaises(CuratorError):
            select_publication(self.catalog(), 'arxiv:2412.10001', candidate, SCHEMA)

    def test_publication_doi_contradiction_rejected(self):
        catalog = self.catalog()
        catalog['records'][0]['official']['published_doi'] = '10.1000/another'
        with self.assertRaises(CuratorError):
            select_publication(catalog, 'arxiv:2412.10001', self.candidate(), SCHEMA)


class VersionTests(CatalogFixture, unittest.TestCase):
    def test_arxiv_version_parsed_separately_from_work_id(self):
        self.assertEqual(arxiv_id('https://arxiv.org/pdf/2412.10001v2.pdf'), '2412.10001')
        self.assertEqual(arxiv_version('https://arxiv.org/pdf/2412.10001v2.pdf'), 'v2')
        self.assertIsNone(arxiv_version('2412.10001'))

    def test_arxiv_v0_is_invalid_not_floating_latest(self):
        with self.assertRaises(CuratorError):
            arxiv_version('2412.10001v0')

    def test_metadata_and_artifact_versions_are_distinct(self):
        record = self.catalog()['records'][0]
        self.assertEqual(record['provenance']['edition']['version'], 'v2')
        self.assertIsNone(record['attachments'][0]['source_edition']['version'])

    def test_pinned_metadata_query_does_not_fall_back_to_latest(self):
        manifest = scan_repository(self.root)
        manifest['records'][0]['metadata_version'] = 'v1'
        with mock.patch('zotero_catalog.fetch_public', return_value=XML):
            catalog = resolve_catalog(manifest, self.root / 'cache', [self.xml])
        self.assertEqual(catalog['records'][0]['status'], 'needs-review')

    def test_explicit_cli_version_is_preserved(self):
        args = parser().parse_args(['manifest', '--arxiv', '2412.10001v2', '--title', TITLE,
            '--source-root', str(self.root), '--translation', str(self.pdf),
            '--translation-revision', 't1', '--out', str(self.root / 'm.json')])
        record = make_manifest(args)['records'][0]
        self.assertEqual(record['metadata_version'], 'v2')
        self.assertEqual(record['attachments'][0]['source_edition']['version'], 'v2')
        self.assertEqual(record['attachments'][0]['translation_revision'], 't1')
        self.assertFalse(record['attachments'][0]['source_edition']['version_verified_by_download'])

    def test_multiple_versions_in_index_stay_one_paper(self):
        second = self.root / '2026/paper_v2_CN.pdf'
        second.write_bytes(b'%PDF-1.7\nsecond version')
        (self.root / 'index.tsv').write_text('2412.10001v1\t' + TITLE + '\t2026-04-25\t2026/paper_CN.pdf\n' +
            '2412.10001v2\t' + TITLE + '\t2026-04-26\t2026/paper_v2_CN.pdf\n')
        manifest = scan_repository(self.root)
        self.assertEqual(len(manifest['records']), 1)
        self.assertEqual(len(manifest['records'][0]['attachments']), 2)
        self.assertEqual(manifest['records'][0]['metadata_version'], 'v2')

    def test_filename_version_is_not_treated_as_source_version(self):
        fake = self.root / '2026/paper_v99_CN.pdf'
        self.pdf.rename(fake)
        (self.root / 'index.tsv').write_text('2412.10001\t' + TITLE + '\t2026-04-25\t2026/paper_v99_CN.pdf\n')
        record = scan_repository(self.root)['records'][0]
        self.assertIsNone(record['attachments'][0]['source_edition']['version'])

    def test_archive_revision_auto_increments_but_same_bytes_reuse_it(self):
        first = preserve_archive_revision({'role': 'translation', 'sha256': 'one'}, [])
        second = preserve_archive_revision({'role': 'translation', 'sha256': 'two'}, [first])
        repeated = preserve_archive_revision({'role': 'translation', 'sha256': 'one'}, [first, second])
        self.assertEqual(first['archive_revision'], 'r1')
        self.assertEqual(second['archive_revision'], 'r2')
        self.assertEqual(repeated['archive_revision'], 'r1')

    def test_archive_history_preserves_known_source_version_on_reimport(self):
        previous = {'role': 'translation', 'sha256': 'one', 'archive_revision': 'r1',
                    'source_edition': edition_source('2412.10001', 'v2')}
        current = {'role': 'translation', 'sha256': 'one', 'source_edition': edition_source('2412.10001')}
        result = preserve_archive_revision(current, [previous])
        self.assertEqual(result['source_edition']['version'], 'v2')

    def test_identical_bytes_do_not_get_relabeled_to_different_source_version(self):
        previous = {'role': 'original', 'sha256': 'one', 'archive_revision': 'r1',
                    'source_edition': edition_source('2412.10001', 'v1')}
        current = {'role': 'original', 'sha256': 'one', 'source_edition': edition_source('2412.10001', 'v2')}
        with self.assertRaises(CuratorError):
            preserve_archive_revision(current, [previous])

    def test_update_report_never_mutates_catalog_or_pdf(self):
        catalog = self.catalog()
        catalog['records'][0]['attachments'][0]['source_edition'] = edition_source('2412.10001', 'v2')
        latest = self.root / 'latest.xml'
        latest.write_bytes(XML.replace(b'2412.10001v2', b'2412.10001v3'))
        before = copy.deepcopy(catalog)
        original_hash = digest(self.pdf)
        report = check_updates(catalog, self.root / 'cache', [latest])
        self.assertTrue(report['records'][0]['newer_than_known_attachments'])
        self.assertEqual(report['records'][0]['latest_known_arxiv_version'], 'v3')
        self.assertEqual(report['automatic_file_changes'], 0)
        self.assertEqual(catalog, before)
        self.assertEqual(digest(self.pdf), original_hash)

    def test_unknown_version_not_assumed_outdated(self):
        report = check_updates(self.catalog(), self.root / 'cache', [self.xml])
        self.assertFalse(report['records'][0]['newer_than_known_attachments'])
        self.assertEqual(report['records'][0]['unknown_source_version_attachments'], 1)

    def test_wrong_work_attachment_version_rejected(self):
        record = self.catalog()['records'][0]
        record['attachments'][0]['source_edition'] = edition_source('2412.99999', 'v2')
        with self.assertRaises(CuratorError):
            validate_artifact_versions(record)

    def test_version_title_labels_are_explicit(self):
        a = {'role': 'translation', 'source_edition': edition_source('2412.10001', 'v2'),
             'translation_revision': 't3', 'archive_revision': 'r4'}
        title = attachment_title(a)
        self.assertIn('arXiv v2', title)
        self.assertIn('译文 t3', title)
        self.assertIn('归档 r4', title)

    def test_floating_original_download_is_rejected_without_network(self):
        with mock.patch('zotero_catalog.fetch_public') as fetch:
            with self.assertRaises(CuratorError):
                fetch_original('2412.10001', self.root / 'original.pdf', self.root / 'state')
            fetch.assert_not_called()

    def test_pinned_original_download_has_hash_receipt(self):
        stream = io.BytesIO(b'%PDF-1.7\nexact source version\n%%EOF')
        stream.geturl = lambda: 'https://arxiv.org/pdf/2412.10001v2'
        with mock.patch('zotero_catalog.fetch_public', return_value=XML), \
             mock.patch('zotero_versions.urllib.request.urlopen', return_value=stream):
            result = fetch_original('2412.10001v2', self.root / 'original.pdf', self.root / 'state')
        self.assertEqual(digest(result['file']), result['sha256'])
        receipt = json.loads(Path(result['receipt']).read_text())
        self.assertTrue(receipt['source_edition']['version_verified_by_download'])
        self.assertEqual(receipt['source_edition']['version'], 'v2')

    def test_pinned_download_rejects_wrong_final_version(self):
        stream = io.BytesIO(b'%PDF-wrong version')
        stream.geturl = lambda: 'https://arxiv.org/pdf/2412.10001v3'
        with mock.patch('zotero_catalog.fetch_public', return_value=XML), \
             mock.patch('zotero_versions.urllib.request.urlopen', return_value=stream):
            with self.assertRaises(CuratorError):
                fetch_original('2412.10001v2', self.root / 'original.pdf', self.root / 'state')
        self.assertFalse((self.root / 'original.pdf').exists())

    def test_download_receipt_binds_original_hash_and_translation_source(self):
        original = self.root / 'original.pdf'
        original.write_bytes(b'%PDF-original')
        receipt = {'format': 1, 'producer': 'zotero-library/pinned-arxiv-download',
                   'source_edition': dict(edition_source('2412.10001', 'v2'), version_verified_by_download=True),
                   'sha256': digest(original)}
        file = self.root / 'receipt.json'
        save_json(file, receipt)
        manifest = scan_repository(self.root)
        manifest['records'][0]['attachments'].append({'path': str(original), 'role': 'original',
                                                     'sha256': digest(original)})
        result = attach_receipt(manifest, file)
        self.assertEqual(result['records'][0]['metadata_version'], 'v2')
        translated = result['records'][0]['attachments'][0]
        self.assertEqual(translated['derived_from_sha256'], digest(original))
        self.assertFalse(translated['translation_content_verified'])

    def test_external_receipt_does_not_automatically_become_download_verified(self):
        receipt = {'format': 1, 'producer': 'zotero-library/pinned-arxiv-download',
                   'source_edition': dict(edition_source('2412.10001', 'v2'), version_verified_by_download=True),
                   'sha256': digest(self.pdf)}
        file = self.root / 'untrusted-receipt.json'
        save_json(file, receipt)
        manifest = attach_receipt(scan_repository(self.root), file, self.root / 'state')
        self.assertFalse(manifest['records'][0]['attachments'][0]['source_edition']['version_verified_by_download'])

    def test_receipt_hash_cannot_escape_private_receipt_directory(self):
        receipt = {'format': 1, 'producer': 'zotero-library/pinned-arxiv-download',
                   'source_edition': edition_source('2412.10001', 'v2'), 'sha256': '../../authorization'}
        file = self.root / 'bad-receipt.json'
        save_json(file, receipt)
        with self.assertRaises(CuratorError):
            attach_receipt(scan_repository(self.root), file, self.root / 'state')

    def test_other_platform_version_is_a_declared_label_not_a_fake_arxiv_number(self):
        record = self.catalog()['records'][0]
        record['attachments'][0]['source_edition'] = {'platform': 'publisher', 'version': 'accepted-manuscript',
            'identifier': '10.1000/example', 'evidence': 'publisher label'}
        validate_artifact_versions(record)
        title = attachment_title(record['attachments'][0])
        self.assertIn('publisher accepted-manuscript', title)
        self.assertNotIn('arXiv', title)


class ClientTests(unittest.TestCase):
    def test_multipart_database_date_decoded_like_zotero(self):
        self.assertEqual(date_from_storage('2026-00-00 2026'), '2026')
        self.assertEqual(date_from_storage('2026-08-18 August 18, 2026'), 'August 18, 2026')
        self.assertEqual(date_from_storage('2026-08-18'), '2026-08-18')
        self.assertEqual(date_from_storage('2026-08-18 12:30:00'), '2026-08-18 12:30:00')

    def test_remote_origins_rejected(self):
        for origin in ['https://example.com', 'http://0.0.0.0:23119', 'http://user:pass@localhost:23119',
                       'http://localhost:23119/other']:
            with self.assertRaises(CuratorError):
                LocalClient(origin)

    def test_secret_file_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'authorization.json'
            save_json(p, {'key': 'TEST', 'origin': 'http://127.0.0.1:23119', 'server_id': SID})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            p.chmod(0o644)
            with self.assertRaises(CuratorError):
                LocalClient(state=Path(d))

    def test_missing_attachment_file_is_recoverable_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            c = LocalClient(state=Path(d))
            c.get = mock.Mock(return_value=False)
            with self.assertRaises(CuratorError) as error:
                c.file_hash('ABCD2345', Path(d) / 'zotero.sqlite')
            self.assertEqual(error.exception.code, 'FILE_MISSING')
            c.get.return_value = (Path(d) / 'storage/ABCD2345/missing.pdf').as_uri()
            with self.assertRaises(CuratorError) as error:
                c.file_hash('ABCD2345', Path(d) / 'zotero.sqlite')
            self.assertEqual(error.exception.code, 'FILE_MISSING')

    def test_http200_partial_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            c = LocalClient(state=Path(d))
            c.key = 'fake'
            c.request = mock.Mock(return_value={'failed': {'0': {'code': 400}}, 'successful': {}})
            with self.assertRaises(CuratorError) as error:
                c.write_batch('items', [{'key': 'ABCD2345', 'version': 0}])
            self.assertEqual(error.exception.code, 'PARTIAL_FAILURE')

    def test_http200_missing_result_is_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            c = LocalClient(state=Path(d))
            c.key = 'fake'
            c.request = mock.Mock(return_value={'failed': {}, 'successful': {}})
            with self.assertRaises(CuratorError):
                c.write_batch('items', [{'key': 'ABCD2345', 'version': 0}])

    def test_batch_requires_expected_version(self):
        with tempfile.TemporaryDirectory() as d:
            c = LocalClient(state=Path(d))
            c.key = 'fake'
            with self.assertRaises(CuratorError):
                c.write_batch('items', [{'key': 'ABCD2345'}])

    def test_upload_does_not_accept_off_origin_url(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file.pdf'
            path.write_bytes(b'%PDF-test')
            c = LocalClient(state=Path(d))
            c.request = mock.Mock(return_value={'url': 'http://example.com/api/local/uploads/X', 'uploadKey': 'X'})
            with self.assertRaises(CuratorError):
                c.upload('ABCD2345', path)
            self.assertEqual(c.request.call_count, 1)

    def test_stream_upload_matches_native_three_phase_contract(self):
        received = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, body=None):
                self.send_response(200)
                self.send_header('Zotero-Server-ID', SID)
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())
            def do_GET(self):
                self.respond({})
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                received.append((self.path, dict(self.headers), body))
                if self.path.endswith('/file') and b'md5=' in body:
                    self.respond({'url': 'http://127.0.0.1:%s/api/local/uploads/TESTUPLOAD' % self.server.server_port,
                                  'uploadKey': 'TESTUPLOAD'})
                else:
                    self.respond()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'file.pdf'
                path.write_bytes(b'%PDF-binary\x00\xff\n')
                c = LocalClient('http://127.0.0.1:%s' % server.server_port, state=Path(d))
                c.probe()
                c.key = 'TEST-ONLY'
                c.upload('ABCD2345', path)
                self.assertEqual(len(received), 3)
                self.assertEqual(received[1][2], path.read_bytes())
                self.assertEqual(received[0][1]['If-None-Match'], '*')
                self.assertEqual(received[2][1]['If-None-Match'], '*')
                self.assertEqual(received[0][1]['Zotero-Server-Id'], SID)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
