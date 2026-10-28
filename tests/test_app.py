"""Testes: períodos, perfis, PDF, NS, formulários, permissões por equipa, execuções, agentes e importação.

Precisa de um PostgreSQL vazio em DATABASE_URL (a base é limpa no início) e do openpyxl (o agente usa-o):
    docker run --rm --network host -e DATABASE_URL=postgresql://... -v "$PWD":/w -w /w/src --user root <imagem> \
        sh -c 'pip install -q openpyxl && python -m unittest discover -s ../tests'
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from PIL import Image

os.environ.update({
    'SECRET_KEY': 'test', 'ENCRYPTION_KEY': Fernet.generate_key().decode(), 'AUTH_DISABLED': '0',
    'KEYCLOAK_URL': 'http://keycloak.invalid', 'KEYCLOAK_REALM': 'test', 'KEYCLOAK_CLIENT_ID': 'REPORT_SERVICE',
    'KEYCLOAK_CLIENT_SECRET': 'x', 'SESSION_COOKIE_SECURE': '0',
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent'))

import db  # noqa: E402

with db.connect() as conn:
    conn.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')

import app as reporting  # noqa: E402
import delivery  # noqa: E402
import runner  # noqa: E402
from periods import next_run, period_range  # noqa: E402
from render import build_pdf  # noqa: E402

LISBON = ZoneInfo('Europe/Lisbon')
ADMIN = {'username': 'ana', 'name': 'Ana', 'email': 'ana@x', 'groups': ['/REPORTING_CORP/ADMIN']}
OPERATOR = {'username': 'rui', 'name': 'Rui', 'email': 'rui@x', 'groups': ['/REPORTING_CORP/OPERADOR']}
PKI = {'username': 'eva', 'name': 'Eva', 'email': 'eva@x', 'groups': ['/REPORTING_CORP/PKI']}
OUTSIDER = {'username': 'zé', 'name': 'Zé', 'email': 'ze@x', 'groups': ['/OUTRA_APP/ADMIN']}


def png(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (190, 110), color).save(out, 'PNG')
    return out.getvalue()


class FakeGrafana:
    """Substitui o GrafanaSource: o painel 99 falha sempre."""
    rendered = []

    def __init__(self, *args, **kwargs):
        pass

    def test(self):
        return 'Grafana 11, token aceite'

    def dashboards(self, search=''):
        return [{'uid': 'abc', 'title': 'TSA Overview', 'folder': 'Reports', 'slug': 'tsa-overview'}]

    def dashboard(self, uid):
        return {'uid': uid, 'title': 'TSA Overview', 'slug': 'tsa-overview', 'variables': [],
                'panels': [{'id': 1, 'title': 'Pedidos', 'type': 'timeseries'}]}

    def render(self, uid, slug, panel_id, start, end, variables=None, theme='light', width=1900, height=1100):
        from sources import SourceError
        FakeGrafana.rendered.append((panel_id, start, end, dict(variables or {})))
        if panel_id == 99:
            raise SourceError('O Grafana respondeu HTTP 500.')
        return png()


class PeriodTest(unittest.TestCase):
    def test_ranges(self):
        now = datetime(2026, 10, 28, 9, 30, tzinfo=LISBON)
        start, end = period_range('previous_month', now)
        self.assertEqual((start.date().isoformat(), end.date().isoformat()), ('2026-09-01', '2026-09-30'))
        self.assertEqual((end.hour, end.minute), (23, 59))
        start, end = period_range('last_7_days', now)
        self.assertEqual((start.day, end.day), (21, 27))
        start, end = period_range('previous_week', now)  # 28/10/2026 é quarta
        self.assertEqual((start.strftime('%a %d'), end.strftime('%a %d')), ('Mon 19', 'Sun 25'))
        start, _ = period_range('month_to_date', datetime(2026, 11, 1, 8, tzinfo=LISBON))
        self.assertEqual(start.date().isoformat(), '2026-10-01')

    def test_range_across_dst_change_has_right_offsets(self):
        # A hora muda a 25/10/2026: o início do dia 24 ainda é +01:00, o fim do dia 25 já é +00:00
        start, end = period_range('previous_day', datetime(2026, 10, 26, 9, tzinfo=LISBON))
        self.assertEqual(end.utcoffset().total_seconds(), 0)
        start, _ = period_range('last_7_days', datetime(2026, 10, 26, 9, tzinfo=LISBON))
        self.assertEqual(start.utcoffset().total_seconds(), 3600)

    def test_next_run(self):
        after = datetime(2026, 10, 28, 9, 0, tzinfo=LISBON)  # quarta
        self.assertEqual(next_run({'freq': 'daily', 'time': '08:00'}, after).day, 29)
        self.assertEqual(next_run({'freq': 'daily', 'time': '10:00'}, after).day, 28)
        self.assertEqual(next_run({'freq': 'weekly', 'time': '08:00', 'weekday': 0}, after).day, 2)
        self.assertEqual(next_run({'freq': 'monthly', 'time': '07:00', 'day': 1}, after).date().isoformat(),
                         '2026-11-01')
        self.assertIsNone(next_run(None, after))


class UnitTest(unittest.TestCase):
    def test_pdf_with_missing_panel(self):
        pdf = build_pdf('TSA', '01/09/2026 a 30/09/2026',
                        [('A', png(), None, ''), ('B', None, 'HTTP 500', 'Dia anterior: 27/10/2026')], logo=png('blue'))
        self.assertTrue(pdf.startswith(b'%PDF'))

    def test_file_name_keeps_letters_without_accents(self):
        from render import file_stem
        self.assertEqual(file_stem('TSA diário & PKI', datetime(2026, 10, 27)), 'TSA_DIARIO_PKI_2026-10-27')

    def test_ns_request_escapes_values(self):
        xml = delivery.ns_request({'client_id': 'PSS', 'sender': 'a@x', 'system_name': 's', 'client_name': 'c',
                                   'template_id': 't', 'sign_message': True}, 'b@x', 'R&D <report>', '', 'r.pdf',
                                  'application/pdf', b'%PDF')
        self.assertIn('R&amp;D &lt;report&gt;', xml)
        self.assertIn('<mes:SignMessage>true</mes:SignMessage>', xml)
        self.assertIn('JVBERg==', xml)

    def test_telegram_network_error_hides_token(self):
        import requests
        channel = {'kind': 'telegram', 'config': {'api_url': 'https://api.telegram.org'},
                   'secret': db.encrypt('123:SEGREDO')}
        error = requests.ConnectionError('Max retries with url: /bot123:SEGREDO/sendDocument')
        with mock.patch('delivery.requests.post', side_effect=error):
            with self.assertRaises(delivery.DeliveryError) as ctx:
                delivery.send(channel, {'chat_id': '1'}, 's', '', 'f.pdf', 'application/pdf', b'x')
        self.assertNotIn('SEGREDO', str(ctx.exception))


@mock.patch('sources.KINDS', {'grafana': ('Grafana', FakeGrafana)})
class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = reporting.app

    def setUp(self):
        self.client = self.app.test_client()

    def login(self, user):
        with self.client.session_transaction() as session:
            session['user'] = user

    def make_source_and_channel(self):
        self.login(ADMIN)
        self.client.post('/admin/fontes/nova', data={'name': 'Grafana teste', 'kind': 'grafana', 'org_id': '1',
                                                    'base_url': 'http://grafana.invalid/g', 'token': 'glsa_SEGREDO',
                                                    'timezone': 'Europe/Lisbon', 'verify_tls': '1'})
        self.client.post('/admin/canais/novo', data={'name': 'Mail teste', 'kind': 'smtp', 'host': 'mail.invalid',
                                                    'port': '25', 'sender': 'pss@x.com', 'secret': 'PASSWORD1'})
        source = db.one("SELECT * FROM data_sources WHERE name = 'Grafana teste'")
        channel = db.one("SELECT * FROM channels WHERE name = 'Mail teste'")
        return source, channel

    def report_form(self, source, channel, **changes):
        form = {'name': 'TSA mensal', 'team': 'OPERADOR', 'source_id': source['id'], 'dashboard_uid': 'abc',
                'dashboard_slug': 'tsa-overview', 'dashboard_title': 'TSA Overview', 'period': 'previous_month',
                'theme': 'light', 'per_page': '2', 'orientation': 'portrait', 'output': 'pdf',
                'panel_width': '1900', 'panel_height': '1100', 'schedule_freq': 'monthly', 'schedule_time': '07:00',
                'schedule_day': '1', 'active': '1', 'variables': json.dumps({'client': 'bcp'}),
                'panels': json.dumps([{'id': 1, 'title': 'Pedidos'}, {'id': 99, 'title': 'Erros', 'period': 'previous_day'}]),
                'recipients': json.dumps([{'channel_id': channel['id'], 'to': 'a@multicert.com; b@multicert.com', 'cc': ''}])}
        form.update(changes)
        return form

    def test_outsider_is_forbidden_and_logged_out(self):
        self.login(OUTSIDER)
        response = self.client.get('/')
        self.assertEqual(response.status_code, 403)
        self.assertIn('/REPORTING_CORP', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/fontes/1/dashboards').status_code, 401)

    def test_operator_cannot_reach_admin(self):
        self.login(OPERATOR)
        self.assertEqual(self.client.get('/admin/fontes').status_code, 302)
        self.assertEqual(self.client.post('/admin/configuracoes', data={}).status_code, 403)

    def test_secrets_are_encrypted_and_not_shown(self):
        source, channel = self.make_source_and_channel()
        self.assertNotIn('SEGREDO', source['token'])
        self.assertEqual(db.decrypt(source['token']), 'glsa_SEGREDO')
        self.assertNotIn('PASSWORD1', channel['secret'] or '')
        page = self.client.get(f"/admin/fontes/{source['id']}").get_data(as_text=True)
        self.assertNotIn('glsa_SEGREDO', page)
        self.assertNotIn('SEGREDO', json.dumps([dict(r) for r in db.query('SELECT * FROM audit')], default=str))
        # Guardar sem token mantém o anterior
        self.client.post(f"/admin/fontes/{source['id']}", data={'name': 'Grafana teste', 'kind': 'grafana',
                                                               'org_id': '1', 'base_url': 'http://grafana.invalid/g',
                                                               'timezone': 'Europe/Lisbon'})
        self.assertEqual(db.decrypt(db.one('SELECT token FROM data_sources WHERE id = %s', (source['id'],))['token']),
                         'glsa_SEGREDO')

    def test_report_lifecycle(self):
        source, channel = self.make_source_and_channel()
        self.login(OPERATOR)
        response = self.client.post('/reports/novo', data=self.report_form(source, channel))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:500])
        report = db.one("SELECT * FROM reports WHERE name = 'TSA mensal'")
        self.assertEqual((report['team'], report['variables'], report['created_by']), ('OPERADOR', {'client': 'bcp'}, 'rui'))
        self.assertEqual(report['next_run_at'].astimezone(LISBON).strftime('%d %H:%M'), '01 07:00')
        recipients = db.query('SELECT target FROM report_recipients WHERE report_id = %s', (report['id'],))
        self.assertEqual(recipients[0]['target']['to'], ['a@multicert.com', 'b@multicert.com'])

        # Pré-visualização: PDF mesmo com o painel 99 em falta; o painel 99 usa o seu período
        FakeGrafana.rendered.clear()
        preview = self.client.post(f"/reports/{report['id']}/pre-visualizar")
        self.assertEqual(preview.mimetype, 'application/pdf')
        (_, start1, _, variables), (_, start99, _, _) = sorted(FakeGrafana.rendered, key=lambda r: r[0])
        self.assertEqual(start1.day, 1)
        self.assertNotEqual(start99.day, 1) if datetime.now(LISBON).day != 2 else None
        self.assertEqual(variables, {'client': 'bcp'})

        # Executar agora → scheduler executa → parcial (painel em falta), e-mail enviado
        self.client.post(f"/reports/{report['id']}/executar")
        sent = []
        with mock.patch('runner.send', side_effect=lambda *a: sent.append(a) or 'enviado'):
            run = runner.claim_next()
            self.assertEqual(runner.execute(run), 'parcial')
        self.assertEqual(sent[0][4], f"TSA_MENSAL_{run_row(run['id'])['period_from'].astimezone(LISBON):%Y-%m-%d}.pdf")
        download = self.client.get(f"/execucoes/{run['id']}/ficheiro")
        self.assertTrue(download.data.startswith(b'%PDF'))

        # Reenvio usa o mesmo ficheiro, sem novo render
        FakeGrafana.rendered.clear()
        self.client.post(f"/execucoes/{run['id']}/reenviar")
        with mock.patch('runner.send', return_value='enviado'):
            self.assertEqual(runner.execute(runner.claim_next()), 'sucesso')
        self.assertEqual(FakeGrafana.rendered, [])

        # Falha de envio: erro, com o motivo no log
        self.client.post(f"/reports/{report['id']}/executar")
        with mock.patch('runner.send', side_effect=delivery.DeliveryError('SMTP recusou')):
            run = runner.claim_next()
            self.assertEqual(runner.execute(run), 'erro')
        self.assertIn('SMTP recusou', json.dumps(run_row(run['id'])['log']))

    def test_teams_only_see_their_reports(self):
        source, channel = self.make_source_and_channel()
        self.client.post('/reports/novo', data=self.report_form(source, channel, name='Só PKI', team='PKI'))
        pki_report = db.one("SELECT id FROM reports WHERE name = 'Só PKI'")
        self.login(OPERATOR)
        self.assertNotIn('Só PKI', self.client.get('/').get_data(as_text=True))
        self.assertEqual(self.client.get(f"/reports/{pki_report['id']}").status_code, 404)
        self.assertEqual(self.client.post(f"/reports/{pki_report['id']}/executar").status_code, 404)
        # Um operador não cria reports para outra equipa
        response = self.client.post('/reports/novo', data=self.report_form(source, channel, name='X', team='PKI'))
        self.assertEqual(response.status_code, 400)
        self.login(PKI)
        self.assertIn('Só PKI', self.client.get('/').get_data(as_text=True))

    def test_reports_are_grouped_by_category(self):
        source, channel = self.make_source_and_channel()
        self.login(OPERATOR)
        for name, category in (('A', ' TSA '), ('B', 'PKI'), ('C', '')):
            self.client.post('/reports/novo', data=self.report_form(source, channel, name=f'Report {name}',
                                                                    category=category))
        self.assertEqual(db.one("SELECT category FROM reports WHERE name = 'Report A'")['category'], 'TSA')
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Sem categoria', page)
        self.assertTrue(all(f'Report {n}' in page for n in 'ABC'))
        page = self.client.get('/?categoria=TSA').get_data(as_text=True)
        self.assertIn('Report A', page)
        self.assertNotIn('Report B', page)
        page = self.client.get('/?categoria=TSA&categoria=').get_data(as_text=True)
        self.assertTrue('Report A' in page and 'Report C' in page)
        self.assertNotIn('Report B', page)

    def test_agent_sql_report(self):
        import agent
        import openpyxl
        import psycopg
        source, channel = self.make_source_and_channel()
        page = self.client.post('/admin/agentes', data={'name': 'zproxy15'}).get_data(as_text=True)
        token = re.search(r'rta_[\w-]+', page)[0]
        self.assertNotIn(token, json.dumps(db.query('SELECT * FROM agents'), default=str))
        info = psycopg.conninfo.conninfo_to_dict(os.environ['DATABASE_URL'])
        agent_id = db.one("SELECT id FROM agents WHERE name = 'zproxy15'")['id']
        self.client.post('/admin/ligacoes/nova', data={
            'name': 'onlinestore', 'agent_id': agent_id, 'host': info['host'], 'port': info.get('port', 5432),
            'database': info['dbname'], 'username': info['user'], 'password': info['password']})
        connection = db.one("SELECT * FROM db_connections WHERE name = 'onlinestore'")
        self.assertNotEqual(connection['password'], info['password'])
        yesterday = datetime.now(LISBON).replace(hour=12) - timedelta(days=1)
        db.execute('CREATE TABLE orders (id int, created timestamptz, note text)')
        db.execute("INSERT INTO orders VALUES (1, %s, 'a%%b'), (2, %s, 'a%%b'), (3, %s, 'x')",
                   (yesterday, yesterday + timedelta(days=1), yesterday))
        query = "SELECT id AS \"ID\", note FROM orders WHERE created >= :from AND created < :to AND note LIKE '%a%'"
        form = self.report_form(source, channel, kind='sql', name='Onlinestore', output='xlsx', period='previous_day',
                                queries=json.dumps([{'connection_id': connection['id'], 'sheet': 'ORDERS', 'query': query}]))
        response = self.client.post('/reports/novo', data=form)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:800])
        report = db.one("SELECT * FROM reports WHERE name = 'Onlinestore'")
        self.assertEqual((report['kind'], report['agent_id'], report['source_id']), ('sql', agent_id, None))

        # O scheduler não gera reports de agente; o agente vai buscá-los com o token
        self.client.post(f"/reports/{report['id']}/executar")
        self.assertIsNone(runner.claim_next())
        api = self.app.test_client()
        self.assertEqual(api.post('/api/agente/trabalho', headers={'Authorization': 'Bearer rta_errado'}).status_code, 401)
        auth = {'Authorization': f'Bearer {token}'}
        job = api.post('/api/agente/trabalho', json={'hostname': 'zproxy15', 'allow_scripts': False}, headers=auth).get_json()
        self.assertEqual(job['queries'][0]['connection']['password'], info['password'])
        self.assertEqual(api.post('/api/agente/trabalho', json={}, headers=auth).status_code, 204)

        ok, name, data, log = agent.execute(job)
        self.assertTrue(ok, log)
        self.assertEqual(list(openpyxl.load_workbook(io.BytesIO(data))['ORDERS'].values), [('ID', 'note'), (1, 'a%b')])
        api.post(f"/api/agente/execucoes/{job['run_id']}", headers=auth, data={
            'ok': '1', 'log': json.dumps(log), 'file': (io.BytesIO(data), name)})
        run = runner.claim_next()
        with mock.patch('runner.send', return_value='enviado') as send:
            self.assertEqual(runner.execute(run), 'sucesso')
        self.assertEqual(send.call_args[0][4], name)
        text = json.dumps(run_row(run['id'])['log'], ensure_ascii=False)
        self.assertIn('Agente zproxy15', text)
        self.assertIn('Folha \\"ORDERS\\": 1 linhas', text)

        # Agente desligado: a execução não fica pendente para sempre
        stale = db.one("INSERT INTO runs (report_id, trigger, created_at) VALUES (%s, 'manual', now() - interval '2 hours') "
                       'RETURNING id', (report['id'],))
        runner.housekeeping()
        self.assertEqual(run_row(stale['id'])['status'], 'erro')

    def test_agent_scripts(self):
        import agent
        job = {'run_id': 1, 'kind': 'script', 'report': 'R', 'file_stem': 'R_2026', 'timezone': 'Europe/Lisbon',
               'period': {'from': '2026-10-27T00:00:00+00:00', 'to': '2026-10-28T00:00:00+00:00'},
               'connections': [{'name': 'online-store', 'host': 'h', 'port': 5432, 'database': 'd', 'username': 'u',
                                'password': 'p'}],
               'script': {'lang': 'python', 'code': (
                   "import os\nassert 'AGENT_TOKEN' not in os.environ\nprint('olá', os.environ['DB_ONLINE_STORE_USER'])\n"
                   "open(os.path.join(os.environ['REPORT_OUTPUT_DIR'], 'r.csv'), 'w').write(os.environ['REPORT_FROM'])\n")}}
        ok, _, _, log = agent.execute(job)
        self.assertFalse(ok)
        self.assertIn('ALLOW_SCRIPTS', json.dumps(log))
        with mock.patch('agent.ALLOW_SCRIPTS', True), mock.patch.dict(os.environ, {'AGENT_TOKEN': 'rta_segredo'}):
            ok, name, data, log = agent.execute(job)
        self.assertTrue(ok, log)
        self.assertEqual((name, data), ('r.csv', b'2026-10-27T00:00:00+00:00'))
        self.assertIn('olá u', json.dumps(log, ensure_ascii=False))

    def test_invalid_report_is_rejected(self):
        source, channel = self.make_source_and_channel()
        self.login(OPERATOR)
        page = self.client.post('/reports/novo', data=self.report_form(
            source, channel, panels='[]', period='sempre', schedule_freq='', active='1',
            recipients=json.dumps([{'channel_id': channel['id'], 'to': 'não-é-email'}]))).get_data(as_text=True)
        for text in ('pelo menos um painel', 'Escolha o período', 'precisa de agendamento', 'e-mails inválidos'):
            self.assertIn(text, page)

    def test_scheduler_enqueues_due_reports_once(self):
        source, channel = self.make_source_and_channel()
        self.client.post('/reports/novo', data=self.report_form(source, channel, name='Diário'))
        report = db.one("SELECT id FROM reports WHERE name = 'Diário'")
        db.execute("UPDATE reports SET next_run_at = now() - interval '1 minute' WHERE id = %s", (report['id'],))
        self.assertEqual(runner.enqueue_due(), 1)
        self.assertEqual(runner.enqueue_due(), 0)
        self.assertGreater(db.one('SELECT next_run_at FROM reports WHERE id = %s', (report['id'],))['next_run_at'],
                           datetime.now(LISBON))

    def test_channel_in_use_cannot_be_deleted(self):
        source, channel = self.make_source_and_channel()
        self.client.post('/reports/novo', data=self.report_form(source, channel, name='Usa canal'))
        page = self.client.post(f"/admin/canais/{channel['id']}/apagar", follow_redirects=True).get_data(as_text=True)
        self.assertIn('há reports que usam', page)

    def test_logo_upload_validates_image(self):
        self.login(ADMIN)
        self.client.post('/admin/logos', data={'name': 'Falso', 'file': (io.BytesIO(b'<svg/>'), 'x.svg')},
                         content_type='multipart/form-data')
        self.assertIsNone(db.one("SELECT 1 FROM logos WHERE name = 'Falso'"))
        self.client.post('/admin/logos', data={'name': 'Vermelho', 'file': (io.BytesIO(png()), 'x.png')},
                         content_type='multipart/form-data')
        self.assertEqual(db.one("SELECT mime FROM logos WHERE name = 'Vermelho'")['mime'], 'image/png')


def run_row(run_id):
    return db.one('SELECT * FROM runs WHERE id = %s', (run_id,))


class ImportTest(unittest.TestCase):
    def test_legacy_folder(self):
        import legacy_import
        root = tempfile.mkdtemp()
        folder = os.path.join(root, 'tsa')
        os.makedirs(os.path.join(folder, 'dashboards', 'tsa_frontends'))
        os.makedirs(os.path.join(folder, 'email'))
        os.makedirs(os.path.join(folder, 'logos'))
        with open(os.path.join(folder, 'dashboards_report.py'), 'w') as f:
            f.write('logo_path = os.path.join(script_directory, "logos/logo.png")\n'
                    'for dashboard_name in ["tsa_frontends"]:\n')
        with open(os.path.join(folder, 'logos', 'logo.png'), 'wb') as f:
            f.write(png('blue'))
        with open(os.path.join(folder, 'email', 'email_configs.json'), 'w') as f:
            json.dump({'smtp_server': 'mail01', 'smtp_port': 25, 'sender_email': 's@x.com',
                       'email_subject': 'TSA - SERVICE REPORT'}, f)
        with open(os.path.join(folder, 'email', 'email_recipients.txt'), 'w') as f:
            f.write('a@x.com\nb@x.com\n')
        base = 'http://monit02:4001/grafana_reports/render/d-solo/cf4/tsa-overview?orgId=1'
        with open(os.path.join(folder, 'dashboards', 'tsa_frontends', 'tsa_frontends_urls_template.txt'), 'w') as f:
            f.write(f'{base}&from={{MONTH-DATE-1}}&to={{MONTH-DATE-2}}&theme=light&panelId=1&width=1900&height=1100'
                    f'&tz=Europe%2FLisbon&var-client=bcp\n'
                    f'{base}&from={{DATE-1}}&to={{DATE-2}}&theme=light&panelId=2&width=1900&height=1100\n'
                    f'{base}&from={{MONTH-DATE-1}}&to={{MONTH-DATE-2}}&theme=light&panelId=3&width=1900&height=1100\n')
        for _ in range(2):  # a segunda vez não duplica
            with mock.patch('sys.argv', ['legacy_import.py', root]):
                legacy_import.main()
        reports = db.query("SELECT * FROM reports WHERE name = 'TSA - SERVICE REPORT'")
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual((report['period'], report['variables'], report['active'], report['category']),
                         ('previous_month', {'client': 'bcp'}, False, 'TSA'))
        panels = db.query('SELECT panel_id, period FROM report_panels WHERE report_id = %s ORDER BY position', (report['id'],))
        self.assertEqual([(p['panel_id'], p['period']) for p in panels], [(1, None), (2, 'previous_day'), (3, None)])
        source = db.one('SELECT * FROM data_sources WHERE id = %s', (report['source_id'],))
        self.assertEqual((source['base_url'], source['token']), ('http://monit02:4001/grafana_reports', None))
        self.assertIsNotNone(report['logo_id'])


if __name__ == '__main__':
    unittest.main()
