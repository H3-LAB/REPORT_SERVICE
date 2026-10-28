"""Administração: fontes de dados, agentes, ligações a bases de dados, canais, logos, assinaturas, configurações e
auditoria
(só ``<raiz>/ADMIN``)."""

import io
import os
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from flask import Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, url_for
from PIL import Image, UnidentifiedImageError
from psycopg.types.json import Jsonb

import db
from agents import new_token
from auth import admin_required, username
from delivery import CHANNEL_FIELDS, CHANNEL_KINDS, CHANNEL_SECRET, EMAIL_FONT, DeliveryError, send
from sources import KINDS, SourceError, open_source

bp = Blueprint('admin', __name__, url_prefix='/admin')

ACCENT_PALETTE = (
    ('Azul (NPROD)', '#2563eb'), ('Esmeralda (DEV)', '#059669'), ('Rosa (PROD)', '#be123c'),
    ('Roxo (INET)', '#9333ea'), ('Âmbar (CORP)', '#b45309'), ('Índigo', '#4f46e5'),
    ('Verde-azulado', '#0d9488'), ('Ardósia', '#475569'),
)
HEX_COLOR = re.compile(r'#[0-9a-f]{6}')
URL = re.compile(r'https?://\S+')
LOGO_MAX_BYTES = 1024 * 1024
AGENT_NAME = re.compile(r'[A-Za-z0-9_.-]{2,60}')
# Cabe no MAX_CONTENT_LENGTH (2 MB) do formulário, com as imagens em base64
SIGNATURE_MAX_CHARS = 1_800_000


def _delete(table, row_id, label, back, feminine=False):
    """Apaga, ou explica que está em uso (FK RESTRICT)."""
    this, deleted = ('esta', 'apagada') if feminine else ('este', 'apagado')
    try:
        row = db.one(f'DELETE FROM {table} WHERE id = %s RETURNING name', (row_id,))
    except psycopg.errors.ForeignKeyViolation:
        flash(f'Não é possível apagar: há reports que usam {this} {label}.', 'error')
        return redirect(url_for(back))
    if row:
        db.audit(username(), f'{label} {deleted}', row['name'])
        flash(f'{label.capitalize()} {deleted}.')
    return redirect(url_for(back))


# ---------- Fontes de dados ----------

@bp.route('/fontes')
@admin_required
def sources():
    rows = db.query('''SELECT s.*, (SELECT count(*) FROM reports WHERE source_id = s.id) AS reports
                       FROM data_sources s ORDER BY name''')
    return render_template('admin/sources.html', page='sources', sources=rows, kinds=KINDS)


@bp.route('/fontes/nova', methods=['GET', 'POST'])
@bp.route('/fontes/<int:source_id>', methods=['GET', 'POST'])
@admin_required
def source_form(source_id=None):
    existing = db.one('SELECT * FROM data_sources WHERE id = %s', (source_id,)) if source_id else None
    if source_id and not existing:
        abort(404)
    if request.method == 'GET':
        return render_template('admin/source_form.html', page='sources', source=existing or
                               {'kind': 'grafana', 'org_id': 1, 'verify_tls': True, 'timezone': 'Europe/Lisbon'},
                               kinds=KINDS)
    form = request.form
    values = {'name': form.get('name', '').strip()[:100], 'kind': form.get('kind', 'grafana'),
              'base_url': form.get('base_url', '').strip().rstrip('/'), 'verify_tls': form.get('verify_tls') == '1',
              'timezone': form.get('timezone', '').strip()}
    errors = []
    try:
        values['org_id'] = int(form.get('org_id') or 1)
    except ValueError:
        errors.append('O Org ID é um número.')
    try:
        ZoneInfo(values['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        errors.append('Fuso horário inválido (ex. Europe/Lisbon).')
    if not values['name']:
        errors.append('O nome é obrigatório.')
    if values['kind'] not in KINDS:
        errors.append('Tipo de fonte inválido.')
    if not URL.fullmatch(values['base_url']):
        errors.append('O URL base começa por http:// ou https://.')
    token = form.get('token', '').strip()
    if token:
        values['token'] = db.encrypt(token)
    elif not existing:
        errors.append('O token de service account é obrigatório.')
    if errors:
        for error in errors:
            flash(error, 'error')
        return render_template('admin/source_form.html', page='sources', source={**(existing or {}), **values},
                               kinds=KINDS), 400
    columns = list(values)
    try:
        if existing:
            db.execute(f"UPDATE data_sources SET {', '.join(f'{c} = %s' for c in columns)}, updated_at = now() "
                       'WHERE id = %s', list(values.values()) + [source_id])
        else:
            source_id = db.one(f"INSERT INTO data_sources ({', '.join(columns)}) "
                               f"VALUES ({', '.join(['%s'] * len(columns))}) RETURNING id", list(values.values()))['id']
    except psycopg.errors.UniqueViolation:
        flash('Já existe uma fonte com esse nome.', 'error')
        return render_template('admin/source_form.html', page='sources', source={**(existing or {}), **values},
                               kinds=KINDS), 400
    # O token não entra na auditoria: só se foi alterado
    db.audit(username(), 'fonte guardada', values['name'], values['base_url'] + (' · token alterado' if token else ''))
    flash('Fonte guardada.')
    return redirect(url_for('admin.source_form', source_id=source_id))


@bp.post('/fontes/<int:source_id>/testar')
@admin_required
def source_test(source_id):
    row = db.one('SELECT * FROM data_sources WHERE id = %s', (source_id,))
    if not row:
        abort(404)
    try:
        return jsonify({'ok': True, 'message': open_source(row).test()})
    except SourceError as exc:
        return jsonify({'ok': False, 'message': str(exc)})


@bp.post('/fontes/<int:source_id>/apagar')
@admin_required
def source_delete(source_id):
    return _delete('data_sources', source_id, 'fonte', 'admin.sources')


# ---------- Agentes ----------

def _agents_page(new_agent_token=None, status=200):
    rows = db.query("""SELECT a.*, a.last_seen_at > now() - interval '2 minutes' AS online,
                              (SELECT count(*) FROM db_connections WHERE agent_id = a.id) AS connections,
                              (SELECT count(*) FROM reports WHERE agent_id = a.id) AS reports
                       FROM agents a ORDER BY name""")
    # O .env mostrado ao criar um agente: a porta dos agentes (agent-api), não o URL da interface
    agent_url = os.getenv('AGENT_API_URL', '').rstrip('/') or request.url_root.rstrip('/')
    return render_template('admin/agents.html', page='agents', agents=rows, new_token=new_agent_token,
                           agent_url=agent_url, agent_host=urlparse(agent_url).hostname,
                           agent_ip=os.getenv('AGENT_API_IP', '')), status


@bp.route('/agentes', methods=['GET', 'POST'])
@admin_required
def agents():
    if request.method == 'GET':
        return _agents_page()
    name = request.form.get('name', '').strip()[:60]
    if not AGENT_NAME.fullmatch(name):
        flash('Nome do agente: letras, números, ponto, hífen ou _ (ex. zproxy15).', 'error')
        return _agents_page(status=400)
    token, digest = new_token()
    try:
        db.execute('INSERT INTO agents (name, token_hash) VALUES (%s, %s)', (name, digest))
    except psycopg.errors.UniqueViolation:
        flash('Já existe um agente com esse nome.', 'error')
        return _agents_page(status=400)
    db.audit(username(), 'agente criado', name)
    # O token só aparece nesta resposta (não vai para a sessão)
    return _agents_page({'agent': name, 'token': token})


@bp.post('/agentes/<int:agent_id>/nome')
@admin_required
def agent_rename(agent_id):
    # O token não muda: o agente continua a funcionar com o nome novo
    old = db.one('SELECT name FROM agents WHERE id = %s', (agent_id,))
    if not old:
        abort(404)
    name = request.form.get('name', '').strip()[:60]
    if not AGENT_NAME.fullmatch(name):
        flash('Nome do agente: letras, números, ponto, hífen ou _ (ex. zproxy15).', 'error')
        return redirect(url_for('admin.agents'))
    try:
        db.execute('UPDATE agents SET name = %s WHERE id = %s', (name, agent_id))
    except psycopg.errors.UniqueViolation:
        flash('Já existe um agente com esse nome.', 'error')
        return redirect(url_for('admin.agents'))
    db.audit(username(), 'agente renomeado', old['name'], f'para {name}')
    flash('Nome do agente alterado.')
    return redirect(url_for('admin.agents'))


@bp.post('/agentes/<int:agent_id>/token')
@admin_required
def agent_token(agent_id):
    token, digest = new_token()
    row = db.one('UPDATE agents SET token_hash = %s WHERE id = %s RETURNING name', (digest, agent_id))
    if not row:
        abort(404)
    db.audit(username(), 'token do agente renovado', row['name'])
    return _agents_page({'agent': row['name'], 'token': token})


@bp.post('/agentes/<int:agent_id>/apagar')
@admin_required
def agent_delete(agent_id):
    try:
        row = db.one('DELETE FROM agents WHERE id = %s RETURNING name', (agent_id,))
    except psycopg.errors.ForeignKeyViolation:
        flash('Não é possível apagar: há ligações ou reports que usam este agente.', 'error')
        return redirect(url_for('admin.agents'))
    if row:
        db.audit(username(), 'agente apagado', row['name'])
        flash('Agente apagado.')
    return redirect(url_for('admin.agents'))


# ---------- Ligações a bases de dados ----------

@bp.route('/ligacoes')
@admin_required
def connections():
    rows = db.query("""SELECT c.*, a.name AS agent_name,
                              (SELECT count(DISTINCT report_id) FROM report_queries WHERE connection_id = c.id) AS reports
                       FROM db_connections c JOIN agents a ON a.id = c.agent_id ORDER BY c.name""")
    return render_template('admin/connections.html', page='connections', connections=rows)


@bp.route('/ligacoes/nova', methods=['GET', 'POST'])
@bp.route('/ligacoes/<int:connection_id>', methods=['GET', 'POST'])
@admin_required
def connection_form(connection_id=None):
    existing = db.one('SELECT * FROM db_connections WHERE id = %s', (connection_id,)) if connection_id else None
    if connection_id and not existing:
        abort(404)
    context = {'page': 'connections', 'agents': db.query('SELECT id, name FROM agents ORDER BY name')}
    if request.method == 'GET':
        return render_template('admin/connection_form.html', connection=existing or {'port': 5432}, **context)
    form = request.form
    values = {'name': form.get('name', '').strip()[:100], 'host': form.get('host', '').strip()[:200],
              'database': form.get('database', '').strip()[:100], 'username': form.get('username', '').strip()[:100]}
    errors = [] if all(values.values()) else ['Nome, host, base de dados e utilizador são obrigatórios.']
    try:
        values['agent_id'] = int(form.get('agent_id') or 0)
        values['port'] = int(form.get('port') or 5432)
        assert 1 <= values['port'] <= 65535 and any(a['id'] == values['agent_id'] for a in context['agents'])
    except (ValueError, AssertionError):
        errors.append('Escolha o agente e uma porta válida.')
    if existing and values.get('agent_id') != existing['agent_id'] and db.one(
            'SELECT 1 FROM report_queries WHERE connection_id = %s', (connection_id,)):
        errors.append('Há reports que usam esta ligação: o agente não pode mudar (os reports correm num só agente).')
    password = form.get('password', '')
    if password:
        values['password'] = db.encrypt(password)
    connection = {**(existing or {}), **values}
    if errors:
        for error in errors:
            flash(error, 'error')
        return render_template('admin/connection_form.html', connection=connection, **context), 400
    columns = list(values)
    try:
        if existing:
            db.execute(f"UPDATE db_connections SET {', '.join(f'{c} = %s' for c in columns)}, updated_at = now() "
                       'WHERE id = %s', list(values.values()) + [connection_id])
        else:
            connection_id = db.one(f"INSERT INTO db_connections ({', '.join(columns)}) "
                                   f"VALUES ({', '.join(['%s'] * len(columns))}) RETURNING id",
                                   list(values.values()))['id']
    except psycopg.errors.UniqueViolation:
        flash('Já existe uma ligação com esse nome.', 'error')
        return render_template('admin/connection_form.html', connection=connection, **context), 400
    db.audit(username(), 'ligação guardada', values['name'],
             f"{values['username']}@{values['host']}:{values['port']}/{values['database']}"
             + (' · password alterada' if password else ''))
    flash('Ligação guardada.')
    return redirect(url_for('admin.connection_form', connection_id=connection_id))


@bp.post('/ligacoes/<int:connection_id>/apagar')
@admin_required
def connection_delete(connection_id):
    return _delete('db_connections', connection_id, 'ligação', 'admin.connections', feminine=True)


# ---------- Canais ----------

@bp.route('/canais')
@admin_required
def channels():
    rows = db.query('''SELECT c.*, (SELECT count(DISTINCT report_id) FROM report_recipients WHERE channel_id = c.id)
                       AS reports FROM channels c ORDER BY name''')
    return render_template('admin/channels.html', page='channels', channels=rows, kinds=CHANNEL_KINDS)


@bp.route('/canais/novo', methods=['GET', 'POST'])
@bp.route('/canais/<int:channel_id>', methods=['GET', 'POST'])
@admin_required
def channel_form(channel_id=None):
    existing = db.one('SELECT * FROM channels WHERE id = %s', (channel_id,)) if channel_id else None
    if channel_id and not existing:
        abort(404)
    context = {'page': 'channels', 'kinds': CHANNEL_KINDS, 'fields': CHANNEL_FIELDS, 'secrets': CHANNEL_SECRET,
               'signatures': db.query('SELECT id, name FROM signatures ORDER BY name')}
    if request.method == 'GET':
        blank = {'kind': request.args.get('tipo', 'smtp'), 'config': {
            'port': 25, 'api_url': 'https://api.telegram.org', 'sign_message': True, 'verify_tls': True}}
        return render_template('admin/channel_form.html', channel=existing or blank, **context)
    form = request.form
    kind = existing['kind'] if existing else form.get('kind')
    name = form.get('name', '').strip()[:100]
    errors = [] if name else ['O nome é obrigatório.']
    if kind not in CHANNEL_KINDS:
        errors.append('Tipo de canal inválido.')
    config = {}
    for field, label, input_type, required in CHANNEL_FIELDS.get(kind, []):
        if input_type == 'checkbox':
            config[field] = form.get(field) == '1'
            continue
        value = form.get(field, '').strip()
        if input_type == 'number' and value:
            value = int(value) if value.isdigit() else None
        if required and not value:
            errors.append(f'{label}: obrigatório.')
        if input_type == 'url' and value and not URL.fullmatch(value):
            errors.append(f'{label}: começa por http:// ou https://.')
        config[field] = value
    secret = form.get('secret', '').strip()
    if kind == 'telegram' and not secret and not existing:
        errors.append('O token do bot é obrigatório.')
    channel = {**(existing or {}), 'kind': kind, 'name': name, 'config': config}
    if errors:
        for error in errors:
            flash(error, 'error')
        return render_template('admin/channel_form.html', channel=channel, **context), 400
    try:
        if existing:
            db.execute('UPDATE channels SET name = %s, config = %s, secret = COALESCE(%s, secret), updated_at = now() '
                       'WHERE id = %s', (name, Jsonb(config), db.encrypt(secret), channel_id))
        else:
            channel_id = db.one('INSERT INTO channels (name, kind, config, secret) VALUES (%s, %s, %s, %s) RETURNING id',
                                (name, kind, Jsonb(config), db.encrypt(secret)))['id']
    except psycopg.errors.UniqueViolation:
        flash('Já existe um canal com esse nome.', 'error')
        return render_template('admin/channel_form.html', channel=channel, **context), 400
    db.audit(username(), 'canal guardado', name, CHANNEL_KINDS[kind] + (' · segredo alterado' if secret else ''))
    flash('Canal guardado.')
    return redirect(url_for('admin.channel_form', channel_id=channel_id))


@bp.post('/canais/<int:channel_id>/testar')
@admin_required
def channel_test(channel_id):
    """Envia uma mensagem de teste com um anexo pequeno para o destino indicado."""
    channel = db.one('SELECT * FROM channels WHERE id = %s', (channel_id,))
    if not channel:
        abort(404)
    data = request.get_json(silent=True) or {}
    if channel['kind'] == 'telegram':
        target = {'chat_id': str(data.get('chat_id', '')).strip(), 'topic_id': str(data.get('topic_id', '')).strip()}
        if not target['chat_id']:
            return jsonify({'ok': False, 'message': 'Indique o chat_id.'})
    else:
        target = {'to': [a for a in re.split(r'[\s,;]+', data.get('to', '')) if a], 'cc': []}
        if not target['to']:
            return jsonify({'ok': False, 'message': 'Indique um e-mail.'})
    signature = db.one('SELECT id, html FROM signatures WHERE id = %s', (int(data.get('signature_id') or 0),))
    try:
        detail = send(channel, target, 'REPORT SERVICE: teste do canal',
                      f"Teste do canal \"{channel['name']}\" pedido por {username()}.",
                      'teste.txt', 'text/plain', 'Teste do REPORT SERVICE.\n'.encode(), signature)
    except DeliveryError as exc:
        return jsonify({'ok': False, 'message': str(exc)})
    db.audit(username(), 'canal testado', channel['name'], detail)
    return jsonify({'ok': True, 'message': detail})


@bp.post('/canais/<int:channel_id>/apagar')
@admin_required
def channel_delete(channel_id):
    return _delete('channels', channel_id, 'canal', 'admin.channels')


# ---------- Logos ----------

@bp.route('/logos', methods=['GET', 'POST'])
@admin_required
def logos():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()[:100]
        upload = request.files.get('file')
        data = upload.read(LOGO_MAX_BYTES + 1) if upload else b''
        try:
            image = Image.open(io.BytesIO(data))
            image.verify()
            mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg'}.get(image.format)
        except (UnidentifiedImageError, OSError):
            mime = None
        if not name or not data or len(data) > LOGO_MAX_BYTES or not mime:
            flash('Indique um nome e uma imagem PNG ou JPG até 1 MB.', 'error')
        else:
            try:
                db.execute('INSERT INTO logos (name, mime, data) VALUES (%s, %s, %s)', (name, mime, data))
                db.audit(username(), 'logo carregado', name)
                flash('Logo carregado.')
            except psycopg.errors.UniqueViolation:
                flash('Já existe um logo com esse nome.', 'error')
        return redirect(url_for('admin.logos'))
    rows = db.query('''SELECT l.id, l.name, l.mime, length(l.data) AS size, l.created_at,
                              (SELECT count(*) FROM reports WHERE logo_id = l.id) AS reports
                       FROM logos l ORDER BY name''')
    return render_template('admin/logos.html', page='logos', logos=rows)


@bp.route('/logos/<int:logo_id>/imagem')
@admin_required
def logo_image(logo_id):
    row = db.one('SELECT mime, data FROM logos WHERE id = %s', (logo_id,))
    if not row:
        abort(404)
    return Response(bytes(row['data']), mimetype=row['mime'])


@bp.post('/logos/<int:logo_id>/apagar')
@admin_required
def logo_delete(logo_id):
    # Os reports que o usavam ficam sem logo (ON DELETE SET NULL)
    return _delete('logos', logo_id, 'logo', 'admin.logos')


# ---------- Assinaturas ----------

@bp.route('/assinaturas')
@admin_required
def signatures():
    rows = db.query('SELECT s.*, (SELECT count(*) FROM reports WHERE signature_id = s.id) AS reports '
                    'FROM signatures s ORDER BY name')
    return render_template('admin/signatures.html', page='signatures', signatures=rows, email_font=EMAIL_FONT)


@bp.route('/assinaturas/nova', methods=['GET', 'POST'])
@bp.route('/assinaturas/<int:signature_id>', methods=['GET', 'POST'])
@admin_required
def signature_form(signature_id=None):
    existing = db.one('SELECT * FROM signatures WHERE id = %s', (signature_id,)) if signature_id else None
    if signature_id and not existing:
        abort(404)
    if request.method == 'GET':
        return render_template('admin/signature_form.html', page='signatures', signature=existing or {'html': ''})
    name = request.form.get('name', '').strip()[:100]
    html = request.form.get('html', '').strip()
    signature = {**(existing or {}), 'name': name, 'html': html}
    error = None
    if not name or not html:
        error = 'Indique o nome e a assinatura.'
    elif len(html) > SIGNATURE_MAX_CHARS:
        error = 'A assinatura é demasiado grande: use imagens mais pequenas (até ~1 MB no total).'
    if not error:
        try:
            if existing:
                db.execute('UPDATE signatures SET name = %s, html = %s, updated_at = now() WHERE id = %s',
                           (name, html, signature_id))
            else:
                signature_id = db.one('INSERT INTO signatures (name, html) VALUES (%s, %s) RETURNING id',
                                      (name, html))['id']
            db.audit(username(), 'assinatura guardada', name)
            flash('Assinatura guardada.')
            return redirect(url_for('admin.signature_form', signature_id=signature_id))
        except psycopg.errors.UniqueViolation:
            error = 'Já existe uma assinatura com esse nome.'
    flash(error, 'error')
    return render_template('admin/signature_form.html', page='signatures', signature=signature), 400


@bp.post('/assinaturas/<int:signature_id>/apagar')
@admin_required
def signature_delete(signature_id):
    # Os reports que a usavam ficam sem assinatura (ON DELETE SET NULL)
    return _delete('signatures', signature_id, 'assinatura', 'admin.signatures', feminine=True)


# ---------- Configurações e auditoria ----------

@bp.route('/configuracoes', methods=['GET', 'POST'])
@admin_required
def settings():
    if request.method == 'POST':
        accent = '' if request.form.get('reset_accent') else request.form.get('accent', '').strip().lower()
        try:
            retention = int(request.form.get('retention_days', ''))
        except ValueError:
            retention = 0
        if accent and not HEX_COLOR.fullmatch(accent):
            flash('A cor tem de estar no formato #rrggbb.', 'error')
        elif not 1 <= retention <= 3650:
            flash('A retenção é um número de dias entre 1 e 3650.', 'error')
        else:
            db.save_settings({'accent': accent, 'retention_days': retention})
            db.audit(username(), 'configurações guardadas', '', f'cor {accent or "azul"}, retenção {retention} dias')
            flash('Configurações guardadas.')
        return redirect(url_for('admin.settings'))
    return render_template('admin/settings.html', page='settings', settings=db.settings(), palette=ACCENT_PALETTE)


@bp.route('/auditoria')
@admin_required
def audit():
    search = request.args.get('q', '').strip()
    where, params = '', []
    if search:
        where = 'WHERE username ILIKE %s OR action ILIKE %s OR target ILIKE %s OR detail ILIKE %s'
        params = [f'%{search}%'] * 4
    rows = db.query(f'SELECT * FROM audit {where} ORDER BY id DESC LIMIT 500', params)
    return render_template('admin/audit.html', page='audit', rows=rows, search=search)
