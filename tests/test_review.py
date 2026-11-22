import json

from app.archive.review import case_id
from app.monitor.inspection import Inspection
from app.monitor.source import sample


def _files():
    return sample('dirty')


def _with_modes(files):
    return files | {'modes': files['modes'] + '\nK-04,2026-11-14T00:40Z,2026-11-14T00:44Z,STARTUP'}


def _with_report(files, value):
    return files | {'reports': files['reports'].replace('29.75', value)}


def test_dispositions_name_current_action_and_close_condition(dirty):
    rows = dirty.review['rows']
    deviation, unreference = rows
    assert (deviation['action'], deviation['reported_mg_nm3'], deviation['difference_mg_nm3']) == (
        'CHASE_REPORT', '29.75', '0.75')
    assert 'R-801' in deviation['chase'] and '容差 0.50' in deviation['chase']
    assert deviation['close_when']
    # 有效分钟跌到门槛以下：不能拿旧报送当现值，也不能当作差异已核清
    assert (unreference['action'], unreference['code']) == ('CHASE_EVIDENCE', 'NO_REFERENCE')
    assert (unreference['reported_mg_nm3'], unreference['superseded_reported_mg_nm3']) == (None, '31.00')
    assert '补 1 个有效分钟' in unreference['chase']


def test_preview_and_archive_expose_review(client, dirty):
    preview = client.get('/api/samples/dirty/review')
    assert preview.status_code == 200
    assert preview.json['counts'] == {'chase_report': 1, 'chase_evidence': 1, 'confirm_report': 0,
                                      'close': 0, 'cases': 1}
    from app.monitor.source import sample as load_sample
    assert Inspection(load_sample('dirty')).review == dirty.review


def test_only_deviation_hour_opens_a_case(client, dirty):
    run_id = client.post('/api/runs', json={'sample': 'dirty'}).json['run_id']
    stage = client.get(f'/api/runs/{run_id}/review').json
    assert stage == dirty.review
    cases = client.get('/api/review').json
    assert cases['counts'] == {'open': 1, 'closed': 0}
    assert [case['hour'] for case in cases['cases']] == ['2026-11-14T00:00:00+00:00']
    assert cases['cases'][0]['case_id'] == case_id('K-04', '2026-11-14T00:00:00+00:00')
    assert [event['kind'] for event in cases['cases'][0]['events']] == ['OPEN']


def test_retrying_same_recalculation_adds_no_review_record(client):
    body = {'files': _files()}
    first = client.post('/api/runs', json=body).json
    second = client.post('/api/runs', json=body).json
    assert (first['reused'], second['reused']) == (False, True)
    assert first['run_id'] == second['run_id']
    cases = client.get('/api/review').json['cases']
    assert [len(case['events']) for case in cases] == [1]


def test_late_mode_downgrades_chase_to_evidence_on_same_case(client):
    files = _files()
    client.post('/api/runs', json={'files': files})
    second = client.post('/api/runs', json={'files': _with_modes(files)}).json
    assert second['totals']['valid'] == 87
    assert second['review']['chase_report'] == 0 and second['review']['chase_evidence'] == 2
    case, = client.get('/api/review').json['cases']
    assert (case['status'], case['current']['action']) == ('OPEN', 'CHASE_EVIDENCE')
    assert [(event['kind'], event['action'], event['superseded_reported_mg_nm3']) for event in case['events']] == [
        ('OPEN', 'CHASE_REPORT', None), ('UPDATE', 'CHASE_EVIDENCE', '29.75')]
    # 旧报送值仍可在证据单中找到
    original = client.get('/api/runs/' + case['events'][0]['run_id'] + '/reports').json
    assert original['rows'][0]['reported_mg_nm3'] == '29.75'


def test_corrected_report_closes_and_old_evidence_stays_verifiable(client):
    files = _files()
    opened = client.post('/api/runs', json={'files': files}).json['run_id']
    closed = client.post('/api/runs', json={'files': _with_report(files, '30.50')}).json['run_id']
    case, = client.get('/api/review').json['cases']
    assert (case['status'], case['status_label']) == ('CLOSED', '已结案')
    assert case['closed_run'] == closed
    assert [(event['kind'], event['action']) for event in case['events']] == [
        ('OPEN', 'CHASE_REPORT'), ('CLOSE', 'CLOSE')]
    old = client.get(f'/api/export/{opened}')
    assert old.status_code == 200
    payload = {key: old.json[key] for key in ('manifest', 'originals', 'results')}
    from hashlib import sha256
    from app.archive.manifest import canonical
    assert old.json['sha256'] == sha256(canonical(payload).encode()).hexdigest()
    assert old.json['results']['reports']['rows'][0]['reported_mg_nm3'] == '29.75'
    new = client.get(f'/api/export/{closed}')
    assert new.json['results']['reports']['rows'][0]['code'] == 'MATCH'


def test_reopening_after_closure_is_a_new_event(client):
    files = _files()
    client.post('/api/runs', json={'files': files})
    client.post('/api/runs', json={'files': _with_report(files, '30.50')})
    assert client.get('/api/review').json['cases'][0]['status'] == 'CLOSED'
    client.post('/api/runs', json={'files': _with_report(files, '29.00')})
    case, = client.get('/api/review').json['cases']
    assert (case['status'], [event['kind'] for event in case['events']]) == (
        'OPEN', ['OPEN', 'CLOSE', 'REOPEN'])
    assert case['current']['difference_mg_nm3'] == '1.50'


def test_replaying_older_run_does_not_rewind_case_state(client):
    files = _files()
    first = client.post('/api/runs', json={'files': files}).json['run_id']
    client.post('/api/runs', json={'files': _with_report(files, '30.50')})
    again = client.post('/api/runs', json={'files': files}).json
    assert (again['run_id'], again['reused']) == (first, True)
    case, = client.get('/api/review').json['cases']
    assert (case['status'], [event['kind'] for event in case['events']]) == ('CLOSED', ['OPEN', 'CLOSE'])


def test_review_page_shows_ticket_and_close_link(client):
    run_id = client.post('/api/runs', json={'sample': 'dirty'}).json['run_id']
    page = client.get('/monitor/review?run=' + run_id).get_data(as_text=True)
    assert '超差复核跟进' in page
    assert 'R-801' in page and '结案条件' in page
    assert f'/api/export/{run_id}' in page
    assert '补证重算' in page


def test_review_stage_is_json_safe(dirty):
    json.dumps(dirty.review)
