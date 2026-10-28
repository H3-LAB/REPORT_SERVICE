"""REPORT SERVICE: reports a partir do Grafana ou de bases de dados (via agentes), enviados por e-mail e Telegram."""

import colorsys
import json
import logging
import os
import re
from datetime import datetime

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, url_for
from psycopg.types.json import Jsonb
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix

import db
from agents import bp as agent_api
from auth import access, can_see_team, current_access, current_user, init_auth, username
from delivery import CHANNEL_KINDS, recipients_text
from periods import FREQUENCIES, PERIODS, WEEKDAYS, describe_schedule, next_run
from runner import AGENT_KINDS, APP_TIMEZONE, Log, generate
from sources import SourceError, open_source

APP_VERSION = os.getenv('APP_VERSION', 'dev')
TEAM_NAME = re.compile(r'[A-Z0-9_-]{1,40}')
EMAIL = re.compile(r'[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+')
TELEGRAM_CHAT = re.compile(r'-?\d{1,20}|@[A-Za-z0-9_]{5,64}')
REPORT_KINDS = {'grafana': 'Painéis do Grafana (PDF)', 'sql': 'Consultas SQL (Excel/CSV, no agente)',
                'script': 'Script (Python/shell, no agente)'}
SQL_OUTPUTS = {'xlsx': 'Excel (.xlsx)', 'xlsx_zip': 'ZIP com o Excel lá dentro',
               'csv_zip': 'ZIP com um CSV por consulta (sem Excel)'}
STATUS_CLASS = {'sucesso': 'pill-ok', 'parcial': 'pill-warn', 'erro': 'pill-error', 'pendente': 'pill-muted',
                'a executar': 'pill-muted'}


def accent_theme(accent):
    """``{from, to, hsl}`` para o ``<style>`` da página, ou ``None`` (azul do app.css)."""
    if not accent:
        return None
    rgb = [int(accent[i:i + 2], 16) for i in (1, 3, 5)]
    hue, light, sat = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    return {'from': accent, 'to': '#' + ''.join(f'{int(c * .85):02x}' for c in rgb),
            'hsl': f'{hue * 360:.1f} {sat * 100:.1f}% {light * 100:.1f}%'}


def split_list(text):
    return [item for item in re.split(r'[\s,;]+', text or '') if item]


def visible_teams_sql():
    """``(cláusula, parâmetros)`` que limita os reports às equipas do utilizador (o admin vê todas)."""
    acc = current_access()
    if acc['admin']:
        return 'TRUE', []
    return 'r.team = ANY(%s)', [acc['teams']]


def get_report(report_id):
    report = db.one('SELECT * FROM reports WHERE id = %s', (report_id,))
    if not report or not can_see_team(report['team']):
        abort(404)
    return report


def validate_report(form):
    """Valores do formulário do report. Devolve ``(report, painéis, destinatários, consultas, erros)``."""
    acc = current_access()
    errors = []
    try:
        variables = json.loads(form.get('variables') or '{}')
        panels = json.loads(form.get('panels') or '[]')
        recipients = json.loads(form.get('recipients') or '[]')
        queries = json.loads(form.get('queries') or '[]')
        assert isinstance(variables, dict) and isinstance(panels, list) and isinstance(recipients, list) \
            and isinstance(queries, list)
    except (ValueError, AssertionError):
        return None, [], [], [], ['O formulário chegou incompleto. Recarregue a página.']

    def number(name, low, high, default):
        try:
            value = int(form.get(name) or default)
            if low <= value <= high:
                return value
        except ValueError:
            pass
        errors.append(f'Valor inválido em {name}.')
        return default

    kind = form.get('kind') if form.get('kind') in REPORT_KINDS else 'grafana'
    report = {
        'kind': kind,
        'name': (form.get('name') or '').strip()[:120],
        'team': (form.get('team') or '').strip().upper(),
        'category': ' '.join((form.get('category') or '').split())[:40],
        'source_id': number('source_id', 0, 2**31, 0) or None,
        'dashboard_uid': (form.get('dashboard_uid') or '').strip(),
        'dashboard_slug': re.sub(r'[^a-z0-9_-]', '', (form.get('dashboard_slug') or '_').lower()) or '_',
        'dashboard_title': (form.get('dashboard_title') or '').strip()[:200],
        'variables': {str(k): str(v)[:200] for k, v in variables.items() if re.fullmatch(r'[A-Za-z0-9_]{1,64}', str(k))},
        'period': form.get('period'),
        'theme': form.get('theme'),
        'per_page': number('per_page', 1, 2, 2),
        'orientation': form.get('orientation'),
        'output': form.get('output'),
        'panel_width': number('panel_width', 400, 4000, 1900),
        'panel_height': number('panel_height', 300, 4000, 1100),
        'logo_id': number('logo_id', 0, 2**31, 0) or None,
        'title': (form.get('title') or '').strip()[:200],
        'subject': (form.get('subject') or '').strip()[:200],
        'message': (form.get('message') or '').strip()[:2000],
        'active': form.get('active') == '1',
        'agent_id': None,
        'script_lang': None,
        'script': '',
    }
    freq = form.get('schedule_freq') or ''
    report['schedule'] = None
    if freq:
        at = form.get('schedule_time') or ''
        if freq not in FREQUENCIES or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', at):
            errors.append('Agendamento inválido.')
        else:
            report['schedule'] = {'freq': freq, 'time': at, 'weekday': number('schedule_weekday', 0, 6, 0),
                                  'day': number('schedule_day', 1, 28, 1)}
    if not report['name']:
        errors.append('O nome é obrigatório.')
    if not TEAM_NAME.fullmatch(report['team']) or not (acc['admin'] or report['team'] in acc['teams']):
        errors.append('Escolha uma das suas equipas.')
    if report['period'] not in PERIODS:
        errors.append('Escolha o período.')
    if report['active'] and not report['schedule']:
        errors.append('Um report ativo precisa de agendamento.')
    clean_panels, clean_queries = [], []
    if kind == 'grafana':
        clean_panels = validate_grafana(report, panels, errors)
    else:
        clean_queries = validate_agent_report(report, form, queries, errors)

    channels = {row['id']: row for row in db.query('SELECT id, kind, name FROM channels')}
    clean_recipients = []
    for item in recipients[:50]:
        channel = channels.get(int(item.get('channel_id') or 0))
        if not channel:
            errors.append('Destinatário sem canal válido.')
            continue
        if channel['kind'] == 'telegram':
            chat, topic = str(item.get('chat_id') or '').strip(), str(item.get('topic_id') or '').strip()
            if not TELEGRAM_CHAT.fullmatch(chat) or (topic and not topic.isdigit()):
                errors.append(f"{channel['name']}: chat_id inválido (número, -100… ou @canal) ou tópico não numérico.")
                continue
            target = {'chat_id': chat, 'topic_id': topic}
        else:
            to, cc = split_list(item.get('to')), split_list(item.get('cc'))
            bad = [address for address in to + cc if not EMAIL.fullmatch(address)]
            if bad or not to:
                errors.append(f"{channel['name']}: e-mails inválidos ou em falta ({', '.join(bad) or 'Para vazio'}).")
                continue
            target = {'to': to, 'cc': cc}
        clean_recipients.append({'channel_id': channel['id'], 'target': target})
    return report, clean_panels, clean_recipients, clean_queries, errors


def validate_grafana(report, panels, errors):
    """Fonte, dashboard, formato e painéis de um report do Grafana. Devolve os painéis."""
    if not report['source_id'] or not db.one('SELECT 1 FROM data_sources WHERE id = %s', (report['source_id'],)):
        errors.append('Escolha uma fonte de dados.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', report['dashboard_uid']):
        errors.append('Escolha um dashboard.')
    if report['theme'] not in ('light', 'dark') or report['orientation'] not in ('portrait', 'landscape') \
            or report['output'] not in ('pdf', 'zip'):
        errors.append('Escolha o tema, a orientação e o formato.')
    if report['logo_id'] and not db.one('SELECT 1 FROM logos WHERE id = %s', (report['logo_id'],)):
        errors.append('O logo escolhido já não existe.')
    clean = []
    for panel in panels[:50]:
        try:
            period = panel.get('period') or None
            if period and period not in PERIODS:
                raise ValueError
            clean.append({'panel_id': int(panel['id']), 'title': str(panel.get('title') or '')[:200],
                          'period': period})
        except (KeyError, TypeError, ValueError):
            errors.append('Painel inválido.')
    if not clean:
        errors.append('Escolha pelo menos um painel.')
    return clean


def validate_agent_report(report, form, queries, errors):
    """Consultas SQL ou script, e o agente onde correm. Devolve as linhas de ``report_queries``."""
    report.update(source_id=None, dashboard_uid=None, dashboard_slug='_', dashboard_title='', variables={},
                  logo_id=None, theme='light', per_page=2, orientation='portrait')
    connections = {row['id']: row for row in db.query('SELECT id, name, agent_id FROM db_connections')}
    clean = []
    for item in queries[:20]:
        connection = connections.get(int(item.get('connection_id') or 0)) if isinstance(item, dict) else None
        if not connection:
            errors.append('Escolha uma ligação válida em cada consulta.')
            continue
        sheet = re.sub(r'[\\/*?:\[\]]', '', str(item.get('sheet') or '')).strip()[:31]  # limites do Excel
        query = str(item.get('query') or '').strip()
        if report['kind'] == 'sql' and (not sheet or not query or len(query) > 100_000):
            errors.append('Cada consulta precisa de nome da folha e da query.')
            continue
        clean.append({'connection_id': connection['id'], 'agent_id': connection['agent_id'],
                      'sheet': sheet if report['kind'] == 'sql' else '', 'query': query if report['kind'] == 'sql' else ''})
    agents = {q['agent_id'] for q in clean}
    if report['kind'] == 'sql':
        if report['output'] not in SQL_OUTPUTS:
            errors.append('Escolha o formato do ficheiro.')
        if not clean:
            errors.append('Junte pelo menos uma consulta.')
        sheets = [q['sheet'].casefold() for q in clean]
        if len(set(sheets)) != len(sheets):
            errors.append('Os nomes das folhas têm de ser diferentes.')
    else:
        report.update(output='zip', script_lang=form.get('script_lang'), script=(form.get('script') or '').strip())
        if report['script_lang'] not in ('python', 'shell') or not report['script'] \
                or len(report['script']) > 200_000:
            errors.append('Escolha a linguagem e escreva o script.')
        try:
            agents.add(int(form.get('agent_id') or 0) or None) if not clean else None
        except ValueError:
            pass
    agents.discard(None)
    if len(agents) != 1:
        errors.append('Escolha o agente.' if not agents else
                      'As ligações de um report têm de ser do mesmo agente (o report corre numa só máquina).')
    elif not db.one('SELECT 1 FROM agents WHERE id = %s', (next(iter(agents)),)):
        errors.append('O agente escolhido já não existe.')
    else:
        report['agent_id'] = next(iter(agents))
    return [{k: q[k] for k in ('connection_id', 'sheet', 'query')} for q in clean]


def save_report(report_id, report, panels, recipients, queries=()):
    next_at = next_run(report['schedule'], datetime.now(APP_TIMEZONE)) if report['active'] else None
    columns = list(report) + ['next_run_at', 'updated_by']
    values = [Jsonb(v) if k in ('variables', 'schedule') and v is not None else v for k, v in report.items()]
    values += [next_at, username()]
    with db.connect() as conn:
        if report_id:
            conn.execute(f"UPDATE reports SET {', '.join(f'{c} = %s' for c in columns)}, updated_at = now() "
                         'WHERE id = %s', values + [report_id])
            conn.execute('DELETE FROM report_panels WHERE report_id = %s', (report_id,))
            conn.execute('DELETE FROM report_recipients WHERE report_id = %s', (report_id,))
            conn.execute('DELETE FROM report_queries WHERE report_id = %s', (report_id,))
        else:
            report_id = conn.execute(f"INSERT INTO reports ({', '.join(columns + ['created_by'])}) "
                                     f"VALUES ({', '.join(['%s'] * (len(columns) + 1))}) RETURNING id",
                                     values + [username()]).fetchone()['id']
        for position, panel in enumerate(panels):
            conn.execute('INSERT INTO report_panels (report_id, position, panel_id, title, period) '
                         'VALUES (%s, %s, %s, %s, %s)',
                         (report_id, position, panel['panel_id'], panel['title'], panel.get('period')))
        for position, query in enumerate(queries):
            conn.execute('INSERT INTO report_queries (report_id, position, connection_id, sheet, query) '
                         'VALUES (%s, %s, %s, %s, %s)',
                         (report_id, position, query['connection_id'], query['sheet'], query['query']))
        for item in recipients:
            conn.execute('INSERT INTO report_recipients (report_id, channel_id, target) VALUES (%s, %s, %s)',
                         (report_id, item['channel_id'], Jsonb(item['target'])))
    return report_id


def create_app():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
    # Atrás do nginx: esquema, host e prefixo vêm dos headers X-Forwarded-*
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)
    if os.getenv('AGENT_API_ONLY') == '1':
        # Porta dos agentes (ex. 10050 com TLS, sem nginx): só a API dos agentes. Antes do login, para não o expor
        @app.before_request
        def agent_api_only():
            if request.endpoint not in ('health', 'agent_api.claim', 'agent_api.result'):
                abort(404)
    init_auth(app)
    db.migrate()

    from admin import bp as admin_bp
    app.register_blueprint(admin_bp)
    app.register_blueprint(agent_api)

    @app.context_processor
    def inject_layout():
        settings = db.settings()
        user = current_user()
        return {'user': user, 'acc': access(user) if user else None, 'theme': accent_theme(settings['accent']),
                'app_version': APP_VERSION, 'status_class': STATUS_CLASS}

    @app.template_filter('dt')
    def format_datetime(value):
        return value.astimezone(APP_TIMEZONE).strftime('%d/%m/%Y %H:%M') if value else '-'

    @app.route('/health')
    def health():
        db.one('SELECT 1')
        return jsonify({'status': 'ok', 'version': APP_VERSION})

    # ---------- Reports ----------

    @app.route('/')
    def reports():
        where, params = visible_teams_sql()
        rows = db.query(f'''SELECT r.*, s.name AS source_name, a.name AS agent_name,
                              (SELECT row_to_json(x) FROM (SELECT id, status, created_at FROM runs
                                WHERE report_id = r.id ORDER BY id DESC LIMIT 1) x) AS last_run
                            FROM reports r LEFT JOIN data_sources s ON s.id = r.source_id
                            LEFT JOIN agents a ON a.id = r.agent_id
                            WHERE {where} ORDER BY r.name''', params)
        # Filtro por categorias (várias; nenhuma = todas); '' = sem categoria
        categories = sorted({r['category'] for r in rows if r['category']}, key=str.casefold)
        counts = {c: sum(r['category'] == c for r in rows) for c in categories + ['']}
        selected = [c for c in dict.fromkeys(request.args.getlist('categoria')) if counts.get(c)]
        if selected:
            rows = [r for r in rows if r['category'] in selected]
        return render_template('reports.html', page='reports', reports=rows, periods=PERIODS,
                               describe_schedule=describe_schedule, categories=categories, counts=counts,
                               total=sum(counts.values()), selected=selected)

    def form_context(report, panels, recipients, queries):
        acc = current_access()
        teams = acc['teams']
        if acc['admin']:
            teams = sorted(set(teams) | {row['team'] for row in db.query('SELECT DISTINCT team FROM reports')}
                           | {'OPERADOR'})
        where, params = visible_teams_sql()
        categories = [row['category'] for row in db.query(
            f"SELECT DISTINCT category FROM reports r WHERE {where} AND category <> '' ORDER BY category", params)]
        return {'page': 'reports', 'report': report, 'panels': panels, 'recipients': recipients, 'teams': teams,
                'categories': categories, 'queries': queries, 'kinds': REPORT_KINDS, 'sql_outputs': SQL_OUTPUTS,
                'agents': db.query('SELECT id, name, allow_scripts FROM agents ORDER BY name'),
                'connections': db.query('SELECT c.id, c.name, c.agent_id, a.name AS agent_name FROM db_connections c '
                                        'JOIN agents a ON a.id = c.agent_id ORDER BY c.name'),
                'sources': db.query('SELECT id, name, base_url FROM data_sources ORDER BY name'),
                'channels': db.query('SELECT id, name, kind FROM channels ORDER BY name'),
                'logos': db.query('SELECT id, name FROM logos ORDER BY name'),
                'periods': PERIODS, 'frequencies': FREQUENCIES, 'weekdays': WEEKDAYS, 'channel_kinds': CHANNEL_KINDS}

    @app.route('/reports/novo', methods=['GET', 'POST'])
    @app.route('/reports/<int:report_id>', methods=['GET', 'POST'])
    def report_form(report_id=None):
        existing = get_report(report_id) if report_id else None
        if request.method == 'POST':
            report, panels, recipients, queries, errors = validate_report(request.form)
            if not errors:
                report_id = save_report(report_id, report, panels, recipients, queries)
                db.audit(username(), 'report guardado', f'#{report_id} {report["name"]}',
                         f"equipa {report['team']}, {len(panels) or len(queries)} "
                         f"{'painéis' if report['kind'] == 'grafana' else 'consultas/ligações'}, "
                         f"{len(recipients)} destinatários")
                flash('Report guardado.')
                if request.form.get('then') == 'preview' and report['kind'] in AGENT_KINDS:
                    return test_run(report_id)
                if request.form.get('then') == 'preview':
                    return redirect(url_for('report_form', report_id=report_id, preview=1))
                return redirect(url_for('report_form', report_id=report_id))
            for error in errors:
                flash(error, 'error')
            # Volta a mostrar o que a pessoa escreveu
            report = {**(existing or {}), **(report or {}), 'id': report_id}
            return render_template('report_form.html', **form_context(
                report, [{'id': p['panel_id'], 'title': p['title'], 'period': p['period']} for p in panels],
                [{'channel_id': r['channel_id'], **r['target']} for r in recipients], queries)), 400
        if existing:
            panels = [{'id': p['panel_id'], 'title': p['title'], 'period': p['period']} for p in
                      db.query('SELECT panel_id, title, period FROM report_panels WHERE report_id = %s '
                               'ORDER BY position', (report_id,))]
            recipients = [{'channel_id': r['channel_id'], **r['target']} for r in
                          db.query('SELECT channel_id, target FROM report_recipients WHERE report_id = %s ORDER BY id',
                                   (report_id,))]
            queries = db.query('SELECT connection_id, sheet, query FROM report_queries WHERE report_id = %s '
                               'ORDER BY position', (report_id,))
            return render_template('report_form.html', **form_context(existing, panels, recipients, queries))
        acc = current_access()
        blank = {'team': (acc['teams'] or ['OPERADOR'])[0], 'category': request.args.get('categoria', ''), 'period': 'previous_day', 'theme': 'light', 'per_page': 2,
                 'orientation': 'portrait', 'output': 'pdf', 'panel_width': 1900, 'panel_height': 1100,
                 'variables': {}, 'schedule': None, 'active': False, 'kind': request.args.get('tipo', 'grafana'),
                 'script_lang': 'python', 'script': ''}
        return render_template('report_form.html', **form_context(blank, [], [], []))

    @app.post('/reports/<int:report_id>/duplicar')
    def report_duplicate(report_id):
        report = get_report(report_id)
        copy = {k: report[k] for k in ('kind', 'agent_id', 'script_lang', 'script', 'team', 'category', 'source_id', 'dashboard_uid', 'dashboard_slug', 'dashboard_title',
                                       'variables', 'period', 'theme', 'per_page', 'orientation', 'output',
                                       'panel_width', 'panel_height', 'logo_id', 'title', 'subject', 'message',
                                       'schedule')}
        copy.update(name=f"{report['name']} (cópia)", active=False)
        panels = db.query('SELECT panel_id, title, period FROM report_panels WHERE report_id = %s ORDER BY position',
                          (report_id,))
        recipients = db.query('SELECT channel_id, target FROM report_recipients WHERE report_id = %s', (report_id,))
        queries = db.query('SELECT connection_id, sheet, query FROM report_queries WHERE report_id = %s '
                           'ORDER BY position', (report_id,))
        new_id = save_report(None, copy, panels, recipients, queries)
        db.audit(username(), 'report duplicado', f'#{report_id} → #{new_id}')
        flash('Cópia criada (inativa).')
        return redirect(url_for('report_form', report_id=new_id))

    @app.post('/reports/<int:report_id>/apagar')
    def report_delete(report_id):
        report = get_report(report_id)
        db.execute('DELETE FROM reports WHERE id = %s', (report_id,))
        db.audit(username(), 'report apagado', f"#{report_id} {report['name']}")
        flash('Report apagado.')
        return redirect(url_for('reports'))

    @app.post('/reports/<int:report_id>/executar')
    def report_run(report_id):
        get_report(report_id)
        run = db.one("INSERT INTO runs (report_id, trigger, requested_by) VALUES (%s, 'manual', %s) RETURNING id",
                     (report_id, username()))
        db.audit(username(), 'execução pedida', f'#{report_id}', f"execução #{run['id']}")
        flash('Execução em fila: o scheduler gera e envia dentro de segundos.')
        return redirect(url_for('run_detail', run_id=run['id']))

    def test_run(report_id):
        """Reports de agente: gera no agente sem enviar; a execução mostra o log e o ficheiro."""
        run = db.one("INSERT INTO runs (report_id, trigger, requested_by) VALUES (%s, 'teste', %s) RETURNING id",
                     (report_id, username()))
        db.audit(username(), 'teste pedido', f'#{report_id}', f"execução #{run['id']}")
        flash('Teste em fila: o agente gera o ficheiro, sem enviar a ninguém.')
        return redirect(url_for('run_detail', run_id=run['id']))

    @app.post('/reports/<int:report_id>/testar')
    def report_test(report_id):
        get_report(report_id)
        return test_run(report_id)

    @app.post('/reports/<int:report_id>/pre-visualizar')
    def report_preview(report_id):
        """Gera o ficheiro agora, sem enviar nem guardar."""
        report = get_report(report_id)
        if report['kind'] in AGENT_KINDS:
            return jsonify({'error': 'Os reports de agente testam-se com "Testar no agente".', 'log': []}), 400
        log = Log()
        try:
            result = generate(report, log)
        except Exception as exc:
            return jsonify({'error': str(exc)[:300], 'log': log}), 502
        return Response(result['data'], mimetype=result['mime'], headers={
            'Content-Disposition': f"{'inline' if result['mime'] == 'application/pdf' else 'attachment'}; "
                                   f"filename={result['file_name']}"})

    # ---------- Execuções ----------

    @app.route('/execucoes')
    def runs():
        where, params = visible_teams_sql()
        report_id = request.args.get('report', type=int)
        if report_id:
            where += ' AND r.id = %s'
            params.append(report_id)
        rows = db.query(f'''SELECT u.id, u.trigger, u.status, u.requested_by, u.created_at, u.started_at,
                                   u.finished_at, u.file_name, u.file IS NOT NULL AS has_file, r.name AS report_name,
                                   r.id AS report_id, r.team
                            FROM runs u JOIN reports r ON r.id = u.report_id
                            WHERE {where} ORDER BY u.id DESC LIMIT 200''', params)
        return render_template('runs.html', page='runs', runs=rows, report_id=report_id)

    def get_run(run_id):
        run = db.one('SELECT u.*, r.name AS report_name, r.team, r.kind, a.name AS agent_name FROM runs u '
                     'JOIN reports r ON r.id = u.report_id LEFT JOIN agents a ON a.id = r.agent_id '
                     'WHERE u.id = %s', (run_id,))
        if not run or not can_see_team(run['team']):
            abort(404)
        return run

    @app.route('/execucoes/<int:run_id>')
    def run_detail(run_id):
        run = get_run(run_id)
        if request.accept_mimetypes.best_match(['application/json', 'text/html']) == 'application/json':
            return jsonify({'status': run['status'], 'log': run['log']})
        return render_template('run.html', page='runs', run=run)

    @app.route('/execucoes/<int:run_id>/ficheiro')
    def run_download(run_id):
        run = get_run(run_id)
        if run['file'] is None:
            abort(404)
        return Response(bytes(run['file']), mimetype=run['file_mime'],
                        headers={'Content-Disposition': f"attachment; filename={run['file_name']}"})

    @app.post('/execucoes/<int:run_id>/reenviar')
    def run_resend(run_id):
        run = get_run(run_id)
        if run['file'] is None:
            flash('O ficheiro desta execução já não existe.', 'error')
            return redirect(url_for('run_detail', run_id=run_id))
        new = db.one("INSERT INTO runs (report_id, trigger, requested_by, parent_run_id) "
                     "VALUES (%s, 'reenvio', %s, %s) RETURNING id", (run['report_id'], username(), run_id))
        db.audit(username(), 'reenvio pedido', f"#{run['report_id']}", f"execução #{run_id} → #{new['id']}")
        flash('Reenvio em fila.')
        return redirect(url_for('run_detail', run_id=new['id']))

    # ---------- API do editor ----------

    def source_client(source_id):
        row = db.one('SELECT * FROM data_sources WHERE id = %s', (source_id,))
        if not row:
            abort(404)
        return open_source(row)

    @app.route('/api/fontes/<int:source_id>/dashboards')
    def api_dashboards(source_id):
        try:
            return jsonify(source_client(source_id).dashboards(request.args.get('q', '')))
        except SourceError as exc:
            return jsonify({'error': str(exc)}), 502

    @app.route('/api/fontes/<int:source_id>/dashboards/<uid>')
    def api_dashboard(source_id, uid):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', uid):
            abort(404)
        try:
            return jsonify(source_client(source_id).dashboard(uid))
        except SourceError as exc:
            return jsonify({'error': str(exc)}), 502

    app.jinja_env.globals.update(recipients_text=recipients_text, channel_kinds=CHANNEL_KINDS)
    return app


app = create_app() if os.getenv('REPORTING_NO_APP') != '1' else None

# URL_PREFIX (ex. /reporting): a app responde só debaixo do prefixo
if app and os.getenv('URL_PREFIX'):
    prefix = '/' + os.environ['URL_PREFIX'].strip('/')
    app.wsgi_app = DispatcherMiddleware(lambda env, start: (start('302 Found', [('Location', prefix + '/')]), [b''])[1],
                                        {prefix: app.wsgi_app})
