"""API dos agentes: vão buscar trabalho (reports SQL e script) e devolvem o ficheiro e o log.

O agente corre noutra máquina (a que chega às bases de dados) e só faz pedidos de saída para aqui, com o token
que o admin criou. O envio do ficheiro aos destinatários fica no scheduler, como nos reports do Grafana.
"""

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, request
from psycopg.types.json import Jsonb
from werkzeug.utils import secure_filename

import db
from periods import period_range
from render import file_stem
from runner import APP_TIMEZONE, AGENT_KINDS

bp = Blueprint('agent_api', __name__, url_prefix='/api/agente')

MAX_FILE_BYTES = int(os.getenv('AGENT_MAX_FILE_MB', '50')) * 1024 * 1024
OUTPUT_MIME = {'.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
               '.zip': 'application/zip', '.csv': 'text/csv', '.pdf': 'application/pdf', '.txt': 'text/plain'}


def new_token():
    """``(token, hash)``: o token só é mostrado ao admin; na base fica o hash."""
    token = 'rta_' + secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current_agent():
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        abort(401)
    agent = db.one('SELECT * FROM agents WHERE token_hash = %s', (token_hash(header[7:].strip()),))
    if not agent:
        abort(401)
    return agent


def connection_payload(row):
    return {'name': row['name'], 'kind': row['kind'], 'host': row['host'], 'port': row['port'],
            'database': row['database'], 'username': row['username'], 'password': db.decrypt(row['password'])}


def job_payload(run, report):
    queries = db.query('SELECT q.sheet, q.query, c.* FROM report_queries q JOIN db_connections c '
                       'ON c.id = q.connection_id WHERE q.report_id = %s ORDER BY q.position', (report['id'],))
    job = {'run_id': run['id'], 'kind': report['kind'], 'report': report['name'], 'output': report['output'],
           'file_stem': file_stem(report['name'], run['period_from']), 'timezone': str(APP_TIMEZONE),
           # :to é exclusivo (início do dia/semana/mês seguinte): use "data >= :from AND data < :to"
           'period': {'from': run['period_from'].isoformat(),
                      'to': (run['period_to'] + timedelta(microseconds=1)).isoformat()}}
    if report['kind'] == 'sql':
        job['queries'] = [{'sheet': q['sheet'], 'query': q['query'], 'connection': connection_payload(q)}
                          for q in queries]
    else:
        job['script'] = {'lang': report['script_lang'], 'code': report['script']}
        job['connections'] = [connection_payload(q) for q in queries]
    return job


@bp.before_request
def larger_uploads():
    request.max_content_length = MAX_FILE_BYTES + 1024 * 1024


@bp.post('/trabalho')
def claim():
    """Próxima execução para este agente (200 com o job) ou 204 se não há nada."""
    agent = current_agent()
    info = request.get_json(silent=True) or {}
    with db.connect() as conn:
        conn.execute('UPDATE agents SET last_seen_at = now(), hostname = %s, version = %s, allow_scripts = %s '
                     'WHERE id = %s', (str(info.get('hostname') or '')[:200], str(info.get('version') or '')[:40],
                                       bool(info.get('allow_scripts')), agent['id']))
        run = conn.execute(
            "SELECT u.id, u.report_id FROM runs u JOIN reports r ON r.id = u.report_id "
            "WHERE u.status = 'pendente' AND r.agent_id = %s AND r.kind = ANY(%s) AND u.file IS NULL "
            "AND u.parent_run_id IS NULL ORDER BY u.created_at LIMIT 1 FOR UPDATE OF u SKIP LOCKED",
            (agent['id'], list(AGENT_KINDS))).fetchone()
        if not run:
            return '', 204
        report = conn.execute('SELECT * FROM reports WHERE id = %s', (run['report_id'],)).fetchone()
        start, end = period_range(report['period'], datetime.now(APP_TIMEZONE))
        run = conn.execute("UPDATE runs SET status = 'a executar', started_at = now(), agent_id = %s, "
                           "period_from = %s, period_to = %s WHERE id = %s RETURNING *",
                           (agent['id'], start, end, run['id'])).fetchone()
    return jsonify(job_payload(run, report))


@bp.post('/execucoes/<int:run_id>')
def result(run_id):
    """Resultado do agente: ``ok``, ``log`` (JSON) e ``file``. Com ficheiro, a execução volta a "pendente" para
    o scheduler enviar; sem ficheiro, fica em erro."""
    agent = current_agent()
    run = db.one("SELECT id FROM runs WHERE id = %s AND agent_id = %s AND status = 'a executar'",
                 (run_id, agent['id']))
    if not run:
        abort(409)
    try:
        log = [{'time': str(line.get('time', ''))[:8], 'level': str(line.get('level', 'info'))[:5],
                'message': str(line.get('message', ''))[:2000]} for line in json.loads(request.form.get('log') or '[]')]
    except (ValueError, AttributeError):
        log = []
    log = [{'time': datetime.now(APP_TIMEZONE).strftime('%H:%M:%S'), 'level': 'info',
            'message': f"Agente {agent['name']} ({agent['hostname'] or '?'})"}] + log[:500]
    upload = request.files.get('file')
    data = upload.read(MAX_FILE_BYTES + 1) if upload else b''
    name = secure_filename(upload.filename) if upload else ''
    extension = os.path.splitext(name)[1].lower()
    if request.form.get('ok') == '1' and data and len(data) <= MAX_FILE_BYTES and extension in OUTPUT_MIME:
        db.execute("UPDATE runs SET status = 'pendente', file = %s, file_name = %s, file_mime = %s, log = %s "
                   "WHERE id = %s", (data, name, OUTPUT_MIME[extension], Jsonb(log), run_id))
    else:
        if request.form.get('ok') == '1':
            log.append({'time': '', 'level': 'error', 'message': 'O agente não devolveu um ficheiro válido '
                        f"(.xlsx, .zip, .csv, .pdf ou .txt até {MAX_FILE_BYTES // 1024 // 1024} MB)."})
        db.execute("UPDATE runs SET status = 'erro', finished_at = now(), log = %s WHERE id = %s",
                   (Jsonb(log), run_id))
    return jsonify({'ok': True})
