"""复核事项（按烟道 + UTC 小时）的跨归档跟进。

事项只在报送差异（超差、冲突报送）出现时立案；之后每次不同内容的复算
只推进同一事项的「当前该追什么」，不会另开新单。归档编号由原件内容
决定，重试同一次复算复用编号且不再写入事项轨迹，事件键 (事项, 归档编号)
也唯一，因此不会新增复核记录。
"""

import json
from datetime import datetime
from hashlib import sha256

from app.archive.manifest import canonical
from app.monitor.clock import local as local_time
from app.monitor.review import STATUS_LABELS, from_results

OPEN, CLOSED = 'OPEN', 'CLOSED'
EVENT_LABELS = {'OPEN': '立案', 'UPDATE': '推进', 'CLOSE': '结案', 'REOPEN': '重开'}

CURRENT_FIELDS = ('action', 'action_label', 'code', 'status', 'valid', 'threshold', 'missing', 'exempt',
                  'computed_mg_nm3', 'reported_mg_nm3', 'superseded_reported_mg_nm3', 'difference_mg_nm3',
                  'tickets', 'chase', 'close_when', 'local', 'hour')
FINGERPRINT_FIELDS = ('action', 'code', 'status', 'valid', 'threshold', 'missing', 'exempt',
                      'computed_mg_nm3', 'reported_mg_nm3', 'superseded_reported_mg_nm3',
                      'difference_mg_nm3', 'tickets')


def case_id(stack, hour):
    return sha256(f'case\x1f{stack}\x1f{hour}'.encode('utf-8')).hexdigest()[:16]


def _event_id(identifier, run_id):
    return sha256(f'event\x1f{identifier}\x1f{run_id}'.encode('utf-8')).hexdigest()[:16]


def _current(row):
    return {key: row.get(key) for key in CURRENT_FIELDS}


def _fingerprint(row):
    return sha256(canonical({key: row.get(key) for key in FINGERPRINT_FIELDS}).encode('utf-8')).hexdigest()


def _instant(text):
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return datetime.fromisoformat('1970-01-01T00:00:00+00:00')


def _display_time(text):
    return None if not text else local_time(_instant(text))


def _event_payload(row, kind, run_id, at):
    return _current(row) | {'kind': kind, 'kind_label': EVENT_LABELS[kind], 'run_id': run_id,
                            'created_utc': at, 'at_local': _display_time(at)}


def record(db, packet):
    """在归档事务内推进复核事项；返回本轮写入的事件数。"""
    manifest = packet['manifest']
    parameters = manifest.get('parameters') or {}
    review = from_results(packet['results'], parameters.get('stack'), parameters.get('tolerance'))
    run_id, at = manifest['run_id'], manifest.get('created_utc')
    written = 0
    for row in review['rows']:
        identifier = case_id(row['stack'], row['hour'])
        fingerprint = _fingerprint(row)
        existing = db.execute(
            'SELECT status, fingerprint FROM review_cases WHERE id = ?', (identifier,)).fetchone()
        if existing is None:
            if not row['case']:
                continue
            kind, status = 'OPEN', OPEN
            opened_run, opened_utc, closed_run, closed_utc = run_id, at, None, None
        else:
            old_status, old_fingerprint = existing
            if row['action'] == 'NONE' or fingerprint == old_fingerprint:
                continue
            opened_run, opened_utc = db.execute(
                'SELECT opened_run, opened_utc FROM review_cases WHERE id = ?', (identifier,)).fetchone()
            if row['action'] == 'CLOSE':
                kind, status = 'CLOSE', CLOSED
                closed_run, closed_utc = run_id, at
            elif old_status == CLOSED:
                kind, status = 'REOPEN', OPEN
                closed_run, closed_utc = None, None
            else:
                kind, status = 'UPDATE', OPEN
                closed_run, closed_utc = None, None
        payload = _event_payload(row, kind, run_id, at)
        inserted = db.execute(
            'INSERT INTO review_events (id, case_id, run_id, created_utc, kind, payload) '
            'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(case_id, run_id) DO NOTHING',
            (_event_id(identifier, run_id), identifier, run_id, at, kind, canonical(payload))).rowcount
        if not inserted:
            continue
        written += 1
        current = canonical(_current(row))
        if existing is None:
            db.execute(
                'INSERT INTO review_cases (id, stack, hour, status, fingerprint, opened_run, opened_utc, '
                'closed_run, closed_utc, current, updated_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(stack, hour) DO NOTHING',
                (identifier, row['stack'], row['hour'], status, fingerprint, opened_run, opened_utc,
                 closed_run, closed_utc, current, at))
        else:
            db.execute(
                'UPDATE review_cases SET status = ?, fingerprint = ?, closed_run = ?, closed_utc = ?, '
                'current = ?, updated_utc = ? WHERE id = ?',
                (status, fingerprint, closed_run, closed_utc, current, at, identifier))
    return written


def _case_row(row):
    item = {'case_id': row[0], 'stack': row[1], 'hour': row[2], 'status': row[3], 'opened_run': row[4],
            'opened_utc': row[5], 'closed_run': row[6], 'closed_utc': row[7],
            'current': json.loads(row[8]), 'updated_utc': row[9], 'events': []}
    item['status_label'] = STATUS_LABELS.get(item['status'], item['status'])
    item['local'] = local_time(_instant(item['hour']))
    item['opened_local'] = _display_time(item['opened_utc'])
    item['closed_local'] = _display_time(item['closed_utc'])
    return item


def load(db, stack=None):
    query = ('SELECT id, stack, hour, status, opened_run, opened_utc, closed_run, closed_utc, current, updated_utc '
             'FROM review_cases')
    parameters = ()
    if stack is not None:
        query += ' WHERE stack = ?'
        parameters = (stack,)
    query += ' ORDER BY hour, opened_utc'
    cases = {row[0]: _case_row(row) for row in db.execute(query, parameters)}
    if cases:
        marks = ','.join('?' for _ in cases)
        for row in db.execute(
                f'SELECT id, case_id, run_id, created_utc, kind, payload FROM review_events '
                f'WHERE case_id IN ({marks}) ORDER BY created_utc, rowid', tuple(cases)):
            event = json.loads(row[5]) | {'event_id': row[0], 'run_id': row[2], 'created_utc': row[3],
                                          'kind': row[4], 'kind_label': EVENT_LABELS.get(row[4], row[4])}
            event['at_local'] = _display_time(row[3])
            cases[row[1]]['events'].append(event)
    return list(cases.values())
