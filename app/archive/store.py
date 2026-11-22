import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from app.archive.manifest import canonical
from app.archive.review import load, record
from app.errors import InputError


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(
                'CREATE TABLE IF NOT EXISTS monitoring (id TEXT PRIMARY KEY, packet TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS review_cases ('
                ' id TEXT PRIMARY KEY, stack TEXT NOT NULL, hour TEXT NOT NULL, status TEXT NOT NULL,'
                ' fingerprint TEXT NOT NULL, opened_run TEXT NOT NULL, opened_utc TEXT, closed_run TEXT,'
                ' closed_utc TEXT, current TEXT NOT NULL, updated_utc TEXT);'
                'CREATE UNIQUE INDEX IF NOT EXISTS review_cases_hour ON review_cases(stack, hour);'
                'CREATE TABLE IF NOT EXISTS review_events ('
                ' id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES review_cases(id),'
                ' run_id TEXT NOT NULL, created_utc TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,'
                ' UNIQUE(case_id, run_id));'
                'CREATE INDEX IF NOT EXISTS review_events_case ON review_events(case_id);')

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            yield db

    def lookup(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT packet FROM monitoring WHERE id = ?', (run_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def require(self, run_id):
        packet = self.lookup(run_id)
        if packet is None:
            raise InputError('RUN_UNKNOWN', '找不到监测留档', 404)
        return packet

    def commit(self, run_id, packet):
        with self.connection() as db:
            cursor = db.execute('INSERT INTO monitoring VALUES (?, ?) ON CONFLICT(id) DO NOTHING',
                                (run_id, canonical(packet)))
            reused = cursor.rowcount == 0
            record(db, packet)
            stored = db.execute('SELECT packet FROM monitoring WHERE id = ?', (run_id,)).fetchone()[0]
        return json.loads(stored), reused

    def history(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT json_extract(packet, '$.manifest') FROM monitoring ORDER BY rowid DESC LIMIT 100")]

    def review(self, stack=None):
        with self.connection() as db:
            return load(db, stack)
