"""Local-only Zotero 10 client. Original implementation; Python 3.9+ stdlib only."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

DEFAULT_APP = Path('/Applications/Zotero.app')
DEFAULT_DB = Path.home() / 'Zotero/zotero.sqlite'
STATE = Path.home() / '.local/share/zotero-library'
KEY_CHARS = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'


class CuratorError(Exception):
    def __init__(self, message, code='CURATOR_ERROR'):
        super().__init__(message)
        self.code = code


def private_dir(path):
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save_json(path, value):
    path = Path(path).expanduser()
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix='.curator-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json(path):
    return json.loads(Path(path).expanduser().read_text(encoding='utf-8'))


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def object_key(namespace):
    raw = hashlib.sha256(namespace.encode()).digest()
    return ''.join(KEY_CHARS[b % len(KEY_CHARS)] for b in raw[:8])


def date_from_storage(value):
    """Mirror Date.multipartToStr, excluding real SQL date-time strings."""
    sql_time = re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?', value)
    return value[11:] if re.match(r'^\d{4}-\d{2}-\d{2} ', value) and not sql_time else value


def inspect_source(app=DEFAULT_APP):
    app = Path(app).expanduser()
    with (app / 'Contents/Info.plist').open('rb') as f:
        version = plistlib.load(f)['CFBundleShortVersionString']
    with zipfile.ZipFile(app / 'Contents/Resources/app/omni.ja') as z:
        schema = json.loads(z.read('resource/schema/global/schema.json'))
        paths = [
            'chrome/content/zotero/xpcom/server/server_localAPI.js',
            'chrome/content/zotero/xpcom/server/server_connector.js',
            'resource/schema/global/schema.json',
            'chrome/content/zotero/xpcom/utilities/date.js',
            'chrome/content/zotero/xpcom/data/item.js',
        ]
        hashes = {n: hashlib.sha256(z.read(n)).hexdigest() for n in paths}
        native = z.read(paths[0]).decode()
    return {
        'version': version, 'schema_version': schema['version'],
        'hashes': hashes,
        'features': {s: s in native for s in [
            '/api/local/authorize', '/api/local/uploads/:uploadKey',
            'Zotero-Server-ID', 'If-None-Match', 'clientVersion',
        ]},
    }, schema


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalClient:
    """Never redirects or sends a local key outside the pinned loopback origin."""
    def __init__(self, base='http://127.0.0.1:23119', state=STATE, timeout=45):
        parsed = urllib.parse.urlsplit(base)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise CuratorError('Only an HTTP loopback Zotero origin is allowed', 'UNSAFE_ORIGIN')
        self.base = base.rstrip('/')
        self.state = Path(state)
        self.timeout = timeout
        self.server_id = None
        self.key = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        path = self.state / 'authorization.json'
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise CuratorError('authorization.json must be owner-only (chmod 600)', 'INSECURE_KEY_FILE')
            auth = load_json(path)
            if auth.get('origin') == self.base:
                self.key, self.server_id = auth.get('key'), auth.get('server_id')

    def request(self, method, path, value=None, headers=None, raw=None, timeout=None):
        if not path.startswith('/api/') or path.startswith('//'):
            raise CuratorError('Only /api/ paths are accepted', 'UNSAFE_PATH')
        h = {'Zotero-API-Version': '3'}
        if self.server_id:
            h['Zotero-Server-ID'] = self.server_id
        if method != 'GET' and self.key:
            h['Zotero-API-Key'] = self.key
        if value is not None:
            raw = json.dumps(value, ensure_ascii=False).encode()
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=raw, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as response:
                body, response_headers = response.read(), response.headers
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors='replace')[:2000]
            if e.code == 403 and 'Local API is not enabled' in text:
                raise CuratorError('Enable Zotero Settings > Advanced > Allow other applications on this computer to communicate with Zotero. This does not enable cloud sync.', 'API_DISABLED')
            if e.code == 401:
                raise CuratorError('Local write authorization is missing, expired, or revoked; run authorize and approve in Zotero.', 'AUTH_REQUIRED')
            if e.code in (412, 428):
                raise CuratorError('Zotero precondition failed; stop and re-plan, do not overwrite concurrently edited data.', 'STALE_VERSION')
            raise CuratorError('Zotero HTTP %s: %s' % (e.code, text), 'HTTP_%s' % e.code)
        except (OSError, urllib.error.URLError) as e:
            # Do not echo request URLs, auth, or bearer upload keys.
            raise CuratorError('Zotero request did not complete (%s). Writes are NOT automatically retried; re-plan to reconcile.' % type(e).__name__, 'REQUEST_UNCERTAIN')
        sid = response_headers.get('Zotero-Server-ID')
        if sid:
            if self.server_id and self.server_id != sid:
                raise CuratorError('Zotero database/server identity changed', 'SERVER_CHANGED')
            self.server_id = sid
        if not body:
            return None
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body.decode('utf-8')

    def probe(self):
        self.request('GET', '/api/')
        if not self.server_id:
            raise CuratorError('Zotero did not provide a server ID', 'SERVER_UNPINNED')
        return {'origin': self.base, 'server_id': self.server_id, 'write_key_present': bool(self.key)}

    def authorize(self):
        self.probe()
        result = self.request('POST', '/api/local/authorize',
                              {'appName': 'Zotero Library Curator (local skill)'}, timeout=300)
        if result.get('remember'):
            save_json(self.state / 'authorization.json', {
                'origin': self.base, 'server_id': self.server_id, 'key': result['key']})
            self.key = result['key']
        # Never print a key, including a single-use one.
        return {'authorized': True, 'persistent': bool(result.get('remember')),
                'message': 'Ready for local writes' if result.get('remember') else
                'One-time permission was not persisted. Batch work needs reusable permission, or a fresh authorization for each write.'}

    def get(self, path):
        return self.request('GET', '/api/users/0/' + path)

    def maybe_get(self, path):
        try:
            return self.get(path)
        except CuratorError as e:
            if e.code == 'HTTP_404':
                return None
            raise

    def write_batch(self, kind, objects):
        """Keyed, versioned writes; check per-object failures even on HTTP 200."""
        if not self.key:
            raise CuratorError('Run authorize first; never fall back to SQLite writes.', 'AUTH_REQUIRED')
        if not 1 <= len(objects) <= 50:
            raise CuratorError('Write batches must have 1..50 objects')
        for obj in objects:
            if not obj.get('key') or 'version' not in obj:
                raise CuratorError('Every object needs a stable key and expected version')
        result = self.request('POST', '/api/users/0/' + kind, objects)
        if result.get('failed'):
            raise CuratorError('Partial batch failure: %s; re-plan before proceeding' %
                               json.dumps(result['failed'], ensure_ascii=False), 'PARTIAL_FAILURE')
        successes = set(result.get('successful', {})) | set(result.get('unchanged', {}))
        if successes != {str(i) for i in range(len(objects))}:
            raise CuratorError('Incomplete batch response; reconcile before continuing', 'PARTIAL_FAILURE')
        return result

    def upload(self, item_key, path):
        """Native authorize -> raw bytes -> register. Never overwrite an existing file."""
        path = Path(path)
        params = urllib.parse.urlencode({
            'md5': digest(path, 'md5'), 'filename': path.name,
            'filesize': path.stat().st_size, 'mtime': int(path.stat().st_mtime * 1000),
            'contentType': 'application/pdf',
        }).encode()
        endpoint = '/api/users/0/items/%s/file' % item_key
        cond = {'Content-Type': 'application/x-www-form-urlencoded', 'If-None-Match': '*'}
        auth = self.request('POST', endpoint, raw=params, headers=cond)
        if auth.get('exists'):
            return
        url = urllib.parse.urlsplit(auth['url'])
        expected = urllib.parse.urlsplit(self.base)
        if (url.scheme, url.hostname, url.port) != (expected.scheme, expected.hostname, expected.port):
            raise CuratorError('Rejected upload URL outside pinned Zotero origin', 'UNSAFE_ORIGIN')
        if url.path != '/api/local/uploads/' + auth['uploadKey'] or url.query or url.fragment:
            raise CuratorError('Unexpected local upload URL', 'UNSAFE_PATH')
        with path.open('rb') as stream:
            self.request('POST', url.path, raw=stream, headers={
                'Content-Type': 'application/octet-stream',
                'Content-Length': str(path.stat().st_size)})
        self.request('POST', endpoint,
                     raw=urllib.parse.urlencode({'upload': auth['uploadKey']}).encode(), headers=cond)

    def file_hash(self, item_key, db=DEFAULT_DB):
        uri = self.get('items/%s/file/view/url' % item_key)
        if not uri:
            raise CuratorError('Attachment has no local file yet', 'FILE_MISSING')
        if not isinstance(uri, str):
            raise CuratorError('Unexpected attachment URL response')
        parsed = urllib.parse.urlsplit(uri.strip())
        if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
            raise CuratorError('Attachment is not a local file', 'UNSAFE_PATH')
        path = Path(urllib.parse.unquote(parsed.path)).resolve()
        storage = Path(db).expanduser().resolve().parent / 'storage'
        try:
            path.relative_to(storage)
        except ValueError:
            raise CuratorError('Refusing to read an attachment outside the data directory storage/', 'UNSAFE_PATH')
        if not path.is_file():
            raise CuratorError('Attachment has no local file yet', 'FILE_MISSING')
        return digest(path)


@contextlib.contextmanager
def snapshot(db=DEFAULT_DB, destination=None):
    """Stable DB+WAL copy; never connect for writes to the live database.

    Zotero may hold an exclusive connection, so SQLite's online backup can block.
    Copy both files, reject concurrent changes, then recover/check only the copy.
    """
    db = Path(db).expanduser().resolve()
    if not db.is_file():
        raise CuratorError('Zotero database was not found at %s' % db, 'DB_MISSING')
    temporary = tempfile.TemporaryDirectory(prefix='zotero-curator-') if destination is None else None
    out = private_dir(temporary.name if temporary else destination)
    if (out / 'zotero.sqlite').resolve() == db:
        if temporary:
            temporary.cleanup()
        raise CuratorError('Snapshot destination must not be the live database directory', 'UNSAFE_PATH')
    if not temporary and any(out.glob('zotero.sqlite*')):
        raise CuratorError('Snapshot destination already contains database files; choose a new directory', 'UNSAFE_PATH')
    try:
        for attempt in range(3):
            sources = [p for p in (db, Path(str(db) + '-wal')) if p.exists()]
            before = [(str(p), p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) for p in sources]
            for old in out.glob('zotero.sqlite*'):
                old.unlink()
            for source in sources:
                target = out / ('zotero.sqlite' + ('-wal' if source.name.endswith('-wal') else ''))
                shutil.copyfile(source, target)
                target.chmod(0o600)
            current = [p for p in (db, Path(str(db) + '-wal')) if p.exists()]
            after = [(str(p), p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) for p in current]
            if before == after:
                break
            time.sleep(0.15)
        else:
            raise CuratorError('Database changed during every snapshot; retry when Zotero is idle', 'DB_BUSY')
        connection = sqlite3.connect(str(out / 'zotero.sqlite'))
        try:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise CuratorError('Snapshot integrity check failed', 'DB_INVALID')
            connection.execute('PRAGMA query_only=ON')
            yield connection
        finally:
            connection.close()
    finally:
        if temporary:
            temporary.cleanup()


def read_library(db=DEFAULT_DB):
    """Read one personal library from a snapshot, including committed WAL changes."""
    with snapshot(db) as c:
        c.row_factory = sqlite3.Row
        library = c.execute("SELECT libraryID FROM libraries WHERE type='user'").fetchall()
        if len(library) != 1:
            raise CuratorError('Expected exactly one personal library', 'LIBRARY_AMBIGUOUS')
        libid = library[0][0]
        items = {}
        for r in c.execute('''SELECT i.itemID,i.key,i.clientVersion,t.typeName
            FROM items i JOIN itemTypes t USING(itemTypeID)
            WHERE i.libraryID=? AND i.itemID NOT IN(SELECT itemID FROM deletedItems)''', (libid,)):
            items[r['itemID']] = {'key': r['key'], 'version': r['clientVersion'], 'data': {
                'key': r['key'], 'version': r['clientVersion'], 'itemType': r['typeName'],
                'tags': [], 'collections': [], 'creators': []}}
        for r in c.execute('''SELECT d.itemID,f.fieldName,v.value FROM itemData d
            JOIN fields f USING(fieldID) JOIN itemDataValues v USING(valueID)'''):
            if r[0] in items:
                # Mirrors Zotero.Date.multipartToStr: database dates are not API strings.
                value = r[2]
                if r[1] == 'date':
                    value = date_from_storage(value)
                items[r[0]]['data'][r[1]] = value
        for r in c.execute('''SELECT ic.itemID,c.firstName,c.lastName,c.fieldMode,ct.creatorType
            FROM itemCreators ic JOIN creators c USING(creatorID)
            JOIN creatorTypes ct USING(creatorTypeID) ORDER BY ic.orderIndex'''):
            if r[0] in items:
                creator = {'creatorType': r[4]}
                creator.update({'name': r[2]} if r[3] else {'firstName': r[1], 'lastName': r[2]})
                items[r[0]]['data']['creators'].append(creator)
        for r in c.execute('SELECT it.itemID,t.name,it.type FROM itemTags it JOIN tags t USING(tagID)'):
            if r[0] in items:
                items[r[0]]['data']['tags'].append({'tag': r[1], 'type': r[2]})
        collections = []
        for r in c.execute('''SELECT c.key,c.collectionName,p.key,c.clientVersion
            FROM collections c LEFT JOIN collections p ON p.collectionID=c.parentCollectionID
            WHERE c.libraryID=?''', (libid,)):
            collections.append({'key': r[0], 'version': r[3], 'data': {
                'name': r[1], 'parentCollection': r[2] or False}})
        for r in c.execute('SELECT ci.itemID,c.key FROM collectionItems ci JOIN collections c USING(collectionID)'):
            if r[0] in items:
                items[r[0]]['data']['collections'].append(r[1])
        for r in c.execute('SELECT itemID,parentItemID,linkMode,path,storageHash FROM itemAttachments'):
            if r[0] in items:
                items[r[0]]['data'].update({'parentItem': items.get(r[1], {}).get('key'),
                    'linkMode': r[2], '_path': r[3], '_storageHash': r[4]})
        for r in c.execute('SELECT itemID,parentItemID,note FROM itemNotes'):
            if r[0] in items:
                items[r[0]]['data'].update({'parentItem': items.get(r[1], {}).get('key'), 'note': r[2]})
        sid = c.execute("SELECT value FROM settings WHERE setting='localAPI' AND key='serverID'").fetchone()
        return {'server_id': sid[0] if sid else None, 'items': list(items.values()), 'collections': collections}
