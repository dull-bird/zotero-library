#!/usr/bin/env python3
"""Zotero Library Curator. Run --help; outputs one JSON object, never a secret."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import uuid

from zotero_core import (CuratorError, DEFAULT_APP, DEFAULT_DB, STATE, LocalClient,
                         digest, inspect_source, load_json, private_dir, read_library, save_json)
from zotero_catalog import (arxiv_id, checked_file, crossref_record, resolve_catalog,
                            scan_repository, select_publication)
from zotero_workflow import apply_plan, build_plan, verify_plan
from zotero_versions import (arxiv_version, edition_source, attach_receipt, fetch_original,
                             check_updates, validate_artifact_versions)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app', type=Path, default=DEFAULT_APP)
    p.add_argument('--db', type=Path, default=DEFAULT_DB)
    p.add_argument('--state', type=Path, default=STATE)
    p.add_argument('--origin', default='http://127.0.0.1:23119')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='Read installed code/schema, API availability and database counts')
    commands.add_parser('authorize', help='Ask the human for native local write authorization')
    audit = commands.add_parser('source-audit', help='Export installed-source hashes and actual item schema')
    audit.add_argument('--out-dir', type=Path, required=True)
    inventory = commands.add_parser('inventory', help='Read committed data from a stable DB+WAL snapshot')
    inventory.add_argument('--out', type=Path, required=True)
    scan = commands.add_parser('scan', help='Build a manifest from a local papers-cn style index.tsv')
    scan.add_argument('--repo', type=Path, required=True)
    scan.add_argument('--source-url')
    scan.add_argument('--overrides', type=Path)
    scan.add_argument('--out', type=Path, required=True)
    manifest = commands.add_parser('manifest', help='Build a single-paper manifest after a DeepOrbit translation')
    manifest.add_argument('--arxiv', required=True)
    manifest.add_argument('--title', required=True, help='Original title checked against the paper')
    manifest.add_argument('--source-root', type=Path, required=True)
    manifest.add_argument('--source-name', default='DeepOrbit')
    manifest.add_argument('--translation', type=Path)
    manifest.add_argument('--original', type=Path)
    manifest.add_argument('--translation-date', help='Actual translation date, not publication date')
    manifest.add_argument('--translation-revision', help='Translator-declared revision, distinct from arXiv vN and automatic archive rN')
    manifest.add_argument('--receipt', type=Path, help='Pinned original download receipt; source work/version/hash must agree')
    manifest.add_argument('--out', type=Path, required=True)
    download = commands.add_parser('fetch-original', help='Download one explicitly pinned original version and record a hash receipt')
    download.add_argument('--arxiv', required=True, help='Must include vN, e.g. 2509.19297v2')
    download.add_argument('--destination', type=Path, required=True)
    versions = commands.add_parser('versions', help='Check latest-known source versions; never replace files or retranslate')
    versions.add_argument('--catalog', type=Path, required=True)
    versions.add_argument('--xml', type=Path, action='append', default=[])
    versions.add_argument('--fresh', action='store_true', help='Use a new metadata cache instead of the daily cache')
    versions.add_argument('--out', type=Path, required=True)
    resolve = commands.add_parser('resolve', help='Fetch/cross-check official arXiv metadata')
    resolve.add_argument('--manifest', type=Path, required=True)
    resolve.add_argument('--xml', type=Path, action='append', default=[])
    resolve.add_argument('--out', type=Path, required=True)
    doi = commands.add_parser('doi', help='Fetch a separate Crossref publication-edition candidate; no writes')
    doi.add_argument('doi')
    doi.add_argument('--out', type=Path, required=True)
    edition = commands.add_parser('select-edition', help='Select an independently verified publisher edition; no library writes')
    edition.add_argument('--catalog', type=Path, required=True)
    edition.add_argument('--record-id', required=True)
    edition.add_argument('--candidate', type=Path, required=True)
    edition.add_argument('--out', type=Path, required=True)
    plan = commands.add_parser('plan', help='Match existing papers, preserve conflicts, plan additive changes')
    plan.add_argument('--catalog', type=Path, required=True)
    plan.add_argument('--approvals', type=Path, help='Explicit per-record field approvals; never use blanket approval')
    plan.add_argument('--out', type=Path, required=True)
    for name in ('apply', 'verify'):
        command = commands.add_parser(name)
        command.add_argument('--plan', type=Path, required=True)
    sync = commands.add_parser('sync', help='Scan/resolve/plan; --apply performs safe routine writes and verifies')
    source = sync.add_mutually_exclusive_group(required=True)
    source.add_argument('--repo', type=Path)
    source.add_argument('--manifest', type=Path)
    sync.add_argument('--source-url')
    sync.add_argument('--overrides', type=Path)
    sync.add_argument('--approvals', type=Path)
    sync.add_argument('--xml', type=Path, action='append', default=[])
    sync.add_argument('--out-dir', type=Path)
    sync.add_argument('--apply', action='store_true')
    return p


def make_manifest(args):
    aid = arxiv_id(args.arxiv)
    if not aid:
        raise CuratorError('An exact arXiv ID is required')
    root_spelling = args.source_root.expanduser().absolute()
    root = root_spelling.resolve()
    version = arxiv_version(args.arxiv)
    attachments = []
    for path, role, language in [(args.original, 'original', 'en'),
                                  (args.translation, 'translation', 'zh-CN')]:
        if path is None:
            continue
        path = path.expanduser().absolute()
        try:
            relative = path.relative_to(root_spelling)
        except ValueError:
            try:
                relative = path.relative_to(root)
            except ValueError:
                raise CuratorError('PDF must be inside --source-root', 'UNSAFE_PATH')
        path = checked_file(root, str(relative))
        attachments.append({'path': str(path), 'filename': path.name, 'sha256': digest(path),
                            'role': role, 'language': language, 'ai_translated': role == 'translation',
                            'source_edition': edition_source(aid, version),
                            'translation_revision': args.translation_revision if role == 'translation' else None,
                            'translation_date': args.translation_date if role == 'translation' else None})
    if not attachments:
        raise CuratorError('Provide --translation and/or --original')
    manifest = {'format': 1, 'source_root': str(root), 'source_name': args.source_name, 'source_url': None,
            'records': [{'record_id': 'arxiv:' + aid, 'arxiv_id': aid, 'metadata_version': version, 'title_hint': args.title,
                         'translation_date': args.translation_date,
                         'source_path': str(Path(attachments[-1]['path']).relative_to(root)),
                         'attachments': attachments}]}
    if args.receipt:
        manifest = attach_receipt(manifest, args.receipt, args.state)
    validate_artifact_versions(manifest['records'][0])
    return manifest


def main(argv=None):
    args = parser().parse_args(argv)
    client = LocalClient(args.origin, args.state)
    if args.command == 'doctor':
        audit, schema = inspect_source(args.app)
        library = read_library(args.db)
        try:
            api = client.probe()
        except CuratorError as e:
            api = {'available': False, 'code': e.code, 'message': str(e)}
        return {'source': audit, 'api': api, 'counts': {
            'papers': sum(i['data']['itemType'] not in ('note', 'attachment', 'annotation') for i in library['items']),
            'attachments': sum(i['data']['itemType'] == 'attachment' for i in library['items']),
            'notes': sum(i['data']['itemType'] == 'note' for i in library['items']),
            'collections': len(library['collections'])},
            'supported_item_types': [t['itemType'] for t in schema['itemTypes']]}
    if args.command == 'authorize':
        return client.authorize()
    if args.command == 'source-audit':
        audit, schema = inspect_source(args.app)
        save_json(args.out_dir / 'source-audit.json', audit)
        save_json(args.out_dir / 'schema.json', schema)
        return {'audit': audit, 'files': [str(args.out_dir / n) for n in ('source-audit.json', 'schema.json')]}
    if args.command == 'inventory':
        library = read_library(args.db)
        save_json(args.out, library)
        return {'items': len(library['items']), 'collections': len(library['collections']), 'file': str(args.out)}
    if args.command == 'scan':
        manifest = scan_repository(args.repo, load_json(args.overrides) if args.overrides else None, args.source_url)
        save_json(args.out, manifest)
        return {'records': len(manifest['records']), 'file': str(args.out)}
    if args.command == 'manifest':
        manifest = make_manifest(args)
        save_json(args.out, manifest)
        return {'records': 1, 'pdfs': len(manifest['records'][0]['attachments']), 'file': str(args.out)}
    if args.command == 'fetch-original':
        return fetch_original(args.arxiv, args.destination, args.state)
    if args.command == 'versions':
        if args.fresh and args.xml:
            raise CuratorError('--fresh and supplied XML cannot both describe a live source check')
        cache = args.state / ('version-checks/' + uuid.uuid4().hex if args.fresh else 'metadata-cache')
        report = check_updates(load_json(args.catalog), cache, args.xml, read_library(args.db))
        save_json(args.out, report)
        return {'file': str(args.out), 'newer_known_sources': sum(r['newer_than_known_attachments'] for r in report['records']),
                'unknown_source_version_attachments': sum(r['unknown_source_version_attachments'] for r in report['records']),
                'automatic_file_changes': 0}
    if args.command == 'resolve':
        catalog = resolve_catalog(load_json(args.manifest), args.state / 'metadata-cache', args.xml)
        save_json(args.out, catalog)
        return {'verified': sum(r['status'] == 'verified-arxiv' for r in catalog['records']),
                'needs_review': [r['record_id'] for r in catalog['records'] if r['status'] != 'verified-arxiv'],
                'file': str(args.out)}
    if args.command == 'doi':
        candidate = crossref_record(args.doi)
        save_json(args.out, candidate)
        return {'candidate': candidate['data'], 'warning': candidate['warning'], 'file': str(args.out)}
    if args.command == 'select-edition':
        _, schema = inspect_source(args.app)
        catalog = select_publication(load_json(args.catalog), args.record_id, load_json(args.candidate), schema)
        save_json(args.out, catalog)
        return {'record_id': args.record_id, 'file': str(args.out),
                'message': 'Publisher edition selected; re-plan to review changes before applying.'}
    if args.command == 'plan':
        audit, schema = inspect_source(args.app)
        plan = build_plan(load_json(args.catalog), read_library(args.db), schema, audit, args.db,
                          load_json(args.approvals) if args.approvals else None)
        save_json(args.out, plan)
        return {'summary': plan['summary'], 'file': str(args.out), 'blocked': plan['blocked'],
                'conflicts': [{'record_id': r['record_id'], 'fields': r['conflicts'], 'omitted': r['omitted']}
                              for r in plan['records'] if r['conflicts'] or r['omitted']]}
    if args.command == 'apply':
        audit, schema = inspect_source(args.app)
        return apply_plan(load_json(args.plan), client, schema, audit, args.state)
    if args.command == 'verify':
        return verify_plan(load_json(args.plan), client)
    if args.command == 'sync':
        out = args.out_dir or args.state / 'preparations' / (
            dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
        private_dir(out)
        manifest = load_json(args.manifest) if args.manifest else scan_repository(
            args.repo, load_json(args.overrides) if args.overrides else None, args.source_url)
        save_json(out / 'manifest.json', manifest)
        catalog = resolve_catalog(manifest, args.state / 'metadata-cache', args.xml)
        save_json(out / 'catalog.json', catalog)
        audit, schema = inspect_source(args.app)
        plan = build_plan(catalog, read_library(args.db), schema, audit, args.db,
                          load_json(args.approvals) if args.approvals else None)
        save_json(out / 'plan.json', plan)
        result = {'preparation_directory': str(out), 'summary': plan['summary'], 'blocked': plan['blocked']}
        if args.apply:
            result['verification'] = apply_plan(plan, client, schema, audit, args.state)
        return result
    raise CuratorError('Unknown command')


if __name__ == '__main__':
    try:
        data = main()
        print(json.dumps({'ok': True, 'data': data}, ensure_ascii=False, indent=2))
        # Verification reports failure to the shell as well as in JSON.
        if isinstance(data, dict) and (data.get('ok') is False or
                data.get('verification', {}).get('ok') is False):
            sys.exit(2)
    except CuratorError as e:
        print(json.dumps({'ok': False, 'error': {'code': e.code, 'message': str(e)}}, ensure_ascii=False))
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(json.dumps({'ok': False, 'error': {'code': 'INPUT_OR_IO_ERROR',
              'message': str(e)}}, ensure_ascii=False))
        sys.exit(1)
