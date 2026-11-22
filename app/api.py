from flask import Blueprint, current_app, jsonify, request

from app.archive.certificate import issue
from app.archive.run import archive_run
from app.errors import InputError
from app.monitor.inspection import STAGES, Inspection
from app.monitor.review import from_results
from app.monitor.source import sample

api = Blueprint('api', __name__, url_prefix='/api')


def archive():
    return current_app.extensions['monitor_archive']


@api.get('/samples/<name>/<stage>')
def preview(name, stage):
    if stage not in STAGES:
        raise InputError('STAGE_UNKNOWN', '找不到监测工序', 404)
    return jsonify(getattr(Inspection(sample(name)), stage))


@api.post('/runs')
def create_run():
    packet, reused = archive_run(archive(), request.get_json(silent=True))
    review = from_results(packet['results'], packet['manifest']['parameters']['stack'],
                          packet['manifest']['parameters']['tolerance'])
    return jsonify(run_id=packet['manifest']['run_id'], reused=reused, manifest=packet['manifest'],
                   totals=packet['results']['hours']['totals'], deviations=packet['results']['reports']['deviations'],
                   review=review['counts'])


@api.get('/runs')
def runs():
    return jsonify(runs=archive().history())


@api.get('/runs/<run_id>/<stage>')
def snapshot(run_id, stage):
    if stage not in STAGES:
        raise InputError('STAGE_UNKNOWN', '找不到监测工序', 404)
    packet = archive().require(run_id)
    if stage not in packet['results']:
        parameters = packet['manifest']['parameters']
        return jsonify(from_results(packet['results'], parameters['stack'], parameters.get('tolerance')))
    return jsonify(packet['results'][stage])


@api.get('/review')
def review_cases():
    stack = request.args.get('stack')
    cases = archive().review(stack)
    return jsonify(cases=cases, counts={
        'open': sum(case['status'] == 'OPEN' for case in cases),
        'closed': sum(case['status'] == 'CLOSED' for case in cases)})


@api.get('/export/<run_id>')
def export(run_id):
    response = jsonify(issue(archive().require(run_id)))
    response.headers['Content-Disposition'] = f'attachment; filename="monitor-{run_id}.json"'
    return response
