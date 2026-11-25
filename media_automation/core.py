from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path


class ReviewRequired(Exception):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.db = sqlite3.connect(self.root / 'jobs.sqlite3', timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
            request TEXT NOT NULL, release TEXT, approval TEXT, sab_id TEXT,
            report TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            state TEXT NOT NULL, at REAL NOT NULL, detail TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS imports (
            job_id TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
            sha256 TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
            state TEXT NOT NULL, PRIMARY KEY(job_id,dst), UNIQUE(dst));
          CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL, acknowledged REAL,
            receipt TEXT,
            UNIQUE(job_id,kind));
        ''')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(outbox)')}
        for column,kind in [('receipt','TEXT'),('superseded_at','REAL'),('superseded_reason','TEXT')]:
            if column not in columns:self.db.execute('ALTER TABLE outbox ADD COLUMN '+column+' '+kind)
        self.db.commit()
        os.chmod(self.root / 'jobs.sqlite3', 0o600)

    def create(self, request: dict) -> dict:
        validate_request(request)
        identity = json.dumps([request['kind'], request['provider_id'],
                               request.get('season'), request.get('episodes', [])],
                              separators=(',', ':'))
        job_id = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO jobs(id,identity,state,request,updated) VALUES(?,?,?,?,?)',
                            (job_id, identity, 'DRAFT', json.dumps(request), time.time()))
        return self.decode(self.db.execute('SELECT * FROM jobs WHERE identity=?', (identity,)).fetchone())

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError('Unknown job')
        d = dict(row)
        for k in ['request', 'release', 'report']:
            if d[k] is not None:
                d[k] = json.loads(d[k])
        return d

    def get(self, job_id):
        return self.decode(self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def jobs(self):
        return [self.decode(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY updated')]

    def change(self, job_id, state, detail='', **fields):
        allowed = {'release', 'approval', 'sab_id', 'report', 'error', 'attempts', 'retry_at', 'request'}
        assert set(fields) <= allowed
        values = {k: json.dumps(v) if k in ['release', 'report', 'request'] else v for k, v in fields.items()}
        values.update(state=state, updated=time.time())
        with self.db:
            self.db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',
                            (*values.values(), job_id))
            self.db.execute('INSERT INTO events(job_id,state,at,detail) VALUES(?,?,?,?)',
                            (job_id, state, time.time(), detail))

    def notify(self, job_id, kind, payload):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outbox(id,job_id,kind,payload,created,acknowledged) VALUES(?,?,?,?,?,NULL)',
                            (str(uuid.uuid4()), job_id, kind, json.dumps(payload), time.time()))

    def outbox(self):
        return [dict(r) | {'payload': json.loads(r['payload'])} for r in
                self.db.execute('SELECT * FROM outbox WHERE acknowledged IS NULL AND superseded_at IS NULL ORDER BY created')]

    def ack(self, event_id, receipt):
        if not isinstance(receipt,str) or not receipt.strip() or len(receipt)>500:
            raise ValueError('Actual delivery receipt required before acknowledgment')
        with self.db:
            row=self.db.execute('SELECT * FROM outbox WHERE id=?',(event_id,)).fetchone()
            if row is None:raise KeyError('Unknown notification')
            if row['acknowledged'] is not None:
                if row['receipt']!=receipt:raise ValueError('Notification already acknowledged with a different receipt')
            else:
                self.db.execute('UPDATE outbox SET acknowledged=?,receipt=? WHERE id=?', (time.time(),receipt,event_id))
            if row['kind']=='READY':
                job=self.get(row['job_id'])
                if job['state'] in ['READY','NOTIFIED']:
                    now=time.time()
                    if job['state']=='READY':
                        report=job.get('report') or {}
                        report['notification']={'ready_event_id':event_id,'accepted_send_receipt':receipt,'acknowledged_at':now}
                        self.db.execute('UPDATE jobs SET state=?,report=?,updated=? WHERE id=?',('NOTIFIED',json.dumps(report),now,job['id']))
                        self.db.execute('INSERT INTO events(job_id,state,at,detail) VALUES(?,?,?,?)',(job['id'],'NOTIFIED',now,'READY message accepted; delivery receipt persisted'))
                    self.db.execute('UPDATE outbox SET superseded_at=?,superseded_reason=? WHERE job_id=? AND kind IN (?,?) AND acknowledged IS NULL AND superseded_at IS NULL',
                                    (now,'Resolved by delivered READY event '+event_id,job['id'],'NEEDS_REVIEW','WAITING_SPACE'))

    @contextlib.contextmanager
    def lock(self):
        with (self.root / 'worker.lock').open('a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def safe_component(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 180:
        raise ValueError('Invalid title')
    if any(ord(c) < 32 for c in value) or value in ('.', '..') or '/' in value or '\\' in value:
        raise ValueError('Unsafe title')
    return value.replace(':', ' -').strip()


def validate_request(r):
    if r.get('kind') not in ['movie', 'season']:
        raise ValueError('kind must be movie or season')
    safe_component(r.get('title', ''))
    if not re.fullmatch(r'(tmdb|imdb):[A-Za-z0-9]+', str(r.get('provider_id', ''))):
        raise ValueError('Canonical provider ID required')
    if not isinstance(r.get('year'), int) or not 1900 <= r['year'] <= 2100:
        raise ValueError('First-air/release year required')
    if r['kind'] == 'season':
        eps = r.get('episodes', [])
        if not isinstance(r.get('season'), int) or not 0 <= r['season'] <= 100:
            raise ValueError('Invalid season')
        if not eps or any(type(e) is not int or e < 1 or e > 999 for e in eps) or sorted(set(eps)) != eps:
            raise ValueError('Unique sorted episode numbers required')
        if not r.get('metadata_reference') or not isinstance(r.get('episode_durations'), dict):
            raise ValueError('Episode listing and durations required')
        if set(map(str, eps)) != set(r['episode_durations']):
            raise ValueError('Every requested episode needs metadata duration')
    combined_episode_end(r)


def combined_episode_end(request):
    """Installation-bound multi-episode approvals are not portable."""
    if any(request.get(k) is not None for k in ("combined_episode_range", "combined_episode_staging")):
        raise ReviewRequired("Combined episode requires installation-specific review not included in this baseline")
    return None


def checked_file(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ReviewRequired('Unsafe relative path')
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ReviewRequired('Symlinks are not accepted')
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise ReviewRequired('File is outside completed downloads')
    return current


def sha256(path: Path):
    before = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as f:
        while b := f.read(8*1024*1024):
            h.update(b)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ReviewRequired('Source changed while being verified')
    return h.hexdigest()


def reserve_fits(free_bytes: int, release_bytes: int, reserved_bytes=0, floor_bytes=20*1024**3):
    return release_bytes > 0 and free_bytes - reserved_bytes - 3*release_bytes >= floor_bytes
