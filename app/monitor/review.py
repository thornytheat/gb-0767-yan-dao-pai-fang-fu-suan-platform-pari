"""报送差异派复核与逐小时跟进判定。

一次复算只能说明「这一刻」该追什么：超差要催报送更正；有效分钟跌到门槛以下时，
原来的报送值已不可比，不能再拿旧值当现值去催，也不能当作差异已核清而销项，
只能先补分钟证据。跨多次复算的生命周期由 app.archive.review 汇总。
"""

from decimal import Decimal, InvalidOperation

from app.numbers import display

CHASE_REPORT = 'CHASE_REPORT'
CHASE_EVIDENCE = 'CHASE_EVIDENCE'
CONFIRM_REPORT = 'CONFIRM_REPORT'
CLOSE = 'CLOSE'
CLOSED = 'CLOSED'
NONE = 'NONE'

# 只有报送差异（含冲突报送）才派复核事项。
OPENING = frozenset({'DEVIATION', 'REPORT_CONFLICT'})

ACTION_LABELS = {
    CHASE_REPORT: '催报更正',
    CHASE_EVIDENCE: '补证重算',
    CONFIRM_REPORT: '核对报送',
    CLOSE: '可结案',
    CLOSED: '已结案',
    NONE: '无需跟进',
}

STATUS_LABELS = {'OPEN': '跟进中', 'CLOSED': '已结案'}


def _tolerance_text(value):
    if value is None:
        return None
    try:
        return display(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return str(value)


def _tickets(report):
    return [row['fields']['ticket'] for row in report.get('reports') or []]


def _report_for(hour, reports):
    return reports.get(hour['hour']) or {
        'code': 'NO_REPORT', 'reported_mg_nm3': None, 'difference_mg_nm3': None, 'reports': []}


def _comparable(hour, report, limit):
    """小时可比值与报送值都在时，给出催报判定。"""
    code = report.get('code', 'NO_REPORT')
    computed, reported = hour.get('mg_nm3'), report.get('reported_mg_nm3')
    difference = report.get('difference_mg_nm3')
    tickets = _tickets(report)
    joined = '、'.join(tickets) if tickets else '未找到'
    if code == 'DEVIATION':
        detail = (f'报送单 {joined} 更正 {hour["local"]} 报送值：报送 {reported}，'
                  f'复算 {computed}，差 {difference}' + (f'（容差 {limit}）' if limit else ''))
        return CHASE_REPORT, detail, ('收到更正报送后复算一致（差额在容差内）' if limit else '收到更正报送后复算一致')
    if code == 'REPORT_CONFLICT':
        return CHASE_REPORT, f'核对 {hour["local"]} 冲突报送单 {joined}，取一致值后重算', '报送值不再冲突且复算一致'
    if code == 'NO_REPORT':
        return CHASE_REPORT, f'追 {hour["local"]} 报送小时单，复算值 {computed}', '收到报送后复算一致'
    return CLOSE, f'复算 {computed} 与报送 {reported} 差 {difference}，在容差内', '本次复算已一致，随本次归档结案'


def dispositions(hours, report_rows, stack, tolerance=None):
    """按小时给出本轮复算后「当前该追什么」。"""
    reports = {row['hour']: row for row in report_rows}
    limit = _tolerance_text(tolerance)
    rows = []
    for hour in hours:
        report = _report_for(hour, reports)
        code = report.get('code', 'NO_REPORT')
        tickets = _tickets(report)
        joined = '、'.join(tickets) if tickets else '未找到'
        computed = hour.get('mg_nm3')
        reported = report.get('reported_mg_nm3')
        difference = report.get('difference_mg_nm3')
        threshold = hour.get('threshold')
        valid = hour.get('valid') or 0
        missing = hour.get('missing') or 0
        status = hour.get('status')
        gap = max((threshold or 0) - valid, 0)
        local = hour.get('local') or hour.get('hour')
        current_reported, superseded, current_difference = reported, None, difference

        if status == 'EXEMPT' and reported is not None:
            action = CONFIRM_REPORT
            chase = f'{local} 整小时工况免计，无有效浓度；核对报送单 {joined}（{reported}）是否撤回或作废'
            close_when = '报送单撤回或确认作废后结案'
            current_reported, superseded, current_difference = None, reported, None
        elif status == 'INSUFFICIENT' or (status == 'EXEMPT' and reported is None):
            action = CHASE_EVIDENCE
            chase = (f'{local} 有效分钟 {valid}/{threshold}，补 {gap} 个有效分钟后再核对报送'
                     + (f'；原报送 {reported} 已不作现值' if reported is not None else ''))
            close_when = f'补足 {gap} 个有效分钟并复算与报送一致'
            current_reported, superseded, current_difference = (None, reported, None) if reported is not None else (
                None, None, None)
        elif status == 'VALID' and code in ('DEVIATION', 'REPORT_CONFLICT', 'NO_REPORT', 'MATCH'):
            action, chase, close_when = _comparable(hour, report, limit)
        else:
            action, chase, close_when = NONE, '无超差或不可比小时，无需复核对', None

        rows.append({
            'stack': stack,
            'hour': hour['hour'],
            'local': local,
            'code': code,
            'status': status,
            'action': action,
            'action_label': ACTION_LABELS[action],
            'case': code in OPENING,
            'valid': valid,
            'threshold': threshold,
            'missing': missing,
            'exempt': hour.get('exempt') or 0,
            'computed_mg_nm3': computed,
            'reported_mg_nm3': current_reported,
            'superseded_reported_mg_nm3': superseded,
            'difference_mg_nm3': current_difference,
            'tickets': tickets,
            'chase': chase,
            'close_when': close_when,
        })
    return rows


def payload(hours, report_rows, stack, tolerance=None):
    rows = dispositions(hours, report_rows, stack, tolerance)
    return {
        'stack': stack,
        'rows': rows,
        'counts': {
            'chase_report': sum(row['action'] == CHASE_REPORT for row in rows),
            'chase_evidence': sum(row['action'] == CHASE_EVIDENCE for row in rows),
            'confirm_report': sum(row['action'] == CONFIRM_REPORT for row in rows),
            'close': sum(row['action'] == CLOSE for row in rows),
            'cases': sum(row['case'] for row in rows),
        },
    }


def from_results(results, stack, tolerance=None):
    """兼容早前留档：若归档结果里没有 review 工序，按小时与报送重算。"""
    review = results.get('review')
    if isinstance(review, dict) and 'rows' in review:
        return review
    return payload(results['hours']['rows'], results['reports']['rows'], stack, tolerance)
