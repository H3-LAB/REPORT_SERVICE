"""Importa os reports dos scripts antigos (pasta GRAFANA_REPORTS) para a base de dados.

    python legacy_import.py /caminho/GRAFANA_REPORTS [--team OPERADOR] [--dry-run]

Por cada pasta (tsa, pki, signstash, …) e cada dashboard que o seu ``dashboards_report.py`` processa,
cria um report com os painéis, as variáveis, o tema, o período de cada painel, o logo e os destinatários.
Cria também a fonte Grafana (sem token: o admin introduz-o) e o canal SMTP. Os reports ficam inativos e
sem agendamento, para não enviarem em duplicado com o cron antigo; corre de novo sem duplicar.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

from psycopg.types.json import Jsonb

import db

PLACEHOLDER_PERIOD = {'{DATE-1}': 'previous_day', '{WEEK-DATE-1}': 'last_7_days', '{MONTH-DATE-1}': 'previous_month'}
RENDER_URL = re.compile(r'^(?P<base>.+?)/render/d-solo/(?P<uid>[^/?]+)/(?P<slug>[^?]+)\?(?P<query>.+)$')


def parse_template(path):
    """Painéis do ``*_urls_template.txt``: base, uid, slug e, por linha, painel, período, tema, tamanho e variáveis."""
    panels = []
    with open(path) as f:
        for line in f:
            match = RENDER_URL.match(line.strip())
            if not match:
                continue
            query = parse_qs(match['query'])
            period = next((p for marker, p in PLACEHOLDER_PERIOD.items() if marker in match['query']), 'previous_day')
            panels.append({
                'base': match['base'], 'uid': match['uid'], 'slug': match['slug'],
                'panel_id': int(query['panelId'][0]), 'period': period,
                'theme': query.get('theme', ['light'])[0], 'org_id': int(query.get('orgId', ['1'])[0]),
                'width': int(query.get('width', ['1900'])[0]), 'height': int(query.get('height', ['1100'])[0]),
                'timezone': query.get('tz', ['Europe/Lisbon'])[0],
                'variables': {k[4:]: v[0] for k, v in query.items() if k.startswith('var-')},
            })
    return panels


def script_info(folder):
    """Dashboards processados e logo do ``dashboards_report.py`` da pasta."""
    with open(os.path.join(folder, 'dashboards_report.py')) as f:
        code = f.read()
    names = re.search(r'for dashboard_name in \[([^\]]+)\]', code)
    logo = re.search(r'logo_path = os\.path\.join\(script_directory, "([^"]+)"\)', code)
    return re.findall(r'"([^"]+)"', names.group(1)) if names else [], logo.group(1) if logo else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('path')
    parser.add_argument('--team', default='OPERADOR')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    db.migrate()
    created = []
    seen = {}  # nome → pasta: pastas com o mesmo assunto (signstash, signstash_tests) ficam com a pasta no nome

    for group in sorted(os.listdir(args.path)):
        folder = os.path.join(args.path, group)
        if not os.path.isfile(os.path.join(folder, 'dashboards_report.py')):
            continue
        dashboards, logo_file = script_info(folder)
        with open(os.path.join(folder, 'email', 'email_configs.json')) as f:
            mail = json.load(f)
        with open(os.path.join(folder, 'email', 'email_recipients.txt')) as f:
            to = [line.strip() for line in f if '@' in line]

        for dashboard in dashboards:
            template = os.path.join(folder, 'dashboards', dashboard, f'{dashboard}_urls_template.txt')
            if not os.path.isfile(template):
                print(f'! {group}/{dashboard}: sem template, ignorado')
                continue
            panels = parse_template(template)
            if not panels:
                print(f'! {group}/{dashboard}: template sem URLs de render, ignorado')
                continue
            first = panels[0]
            name = mail['email_subject'] if len(dashboards) == 1 else f"{mail['email_subject']} · {dashboard}"
            if seen.setdefault(name, group) != group:
                name = f'{name} ({group})'
                seen[name] = group
            # Período do report: o mais comum; os painéis com outro levam o seu
            periods = [p['period'] for p in panels]
            period = max(set(periods), key=periods.count)
            print(f"- {name}: {len(panels)} painéis de {first['uid']}, período {period}, "
                  f"{len(to)} destinatários, logo {logo_file or '-'}")
            if args.dry_run:
                continue

            with db.connect() as conn:
                if conn.execute('SELECT 1 FROM reports WHERE name = %s', (name,)).fetchone():
                    print('  já existe, ignorado')
                    continue
                source = conn.execute('SELECT id FROM data_sources WHERE base_url = %s', (first['base'],)).fetchone()
                if not source:
                    # O mesmo servidor pode ter vários Grafanas (caminhos diferentes)
                    url = urlparse(first['base'])
                    source_name = f'Grafana {url.hostname}{url.path}'.rstrip('/')
                    source = conn.execute(
                        'INSERT INTO data_sources (name, base_url, org_id, timezone, verify_tls) '
                        'VALUES (%s, %s, %s, %s, false) RETURNING id',
                        (source_name, first['base'], first['org_id'], first['timezone'])).fetchone()
                    print(f'  fonte criada: {source_name} (falta o token)')
                channel_name = f"SMTP {mail['smtp_server']}"
                channel = conn.execute('SELECT id FROM channels WHERE name = %s', (channel_name,)).fetchone()
                if not channel:
                    channel = conn.execute(
                        "INSERT INTO channels (name, kind, config) VALUES (%s, 'smtp', %s) RETURNING id",
                        (channel_name, Jsonb({'host': mail['smtp_server'], 'port': int(mail['smtp_port']),
                                              'starttls': True, 'username': '', 'sender': mail['sender_email']}))
                    ).fetchone()
                    print(f'  canal criado: {channel_name}')
                logo_id = None
                logo_path = os.path.join(folder, logo_file) if logo_file else None
                if logo_path and logo_path.lower().endswith(('.png', '.jpg', '.jpeg')) and os.path.isfile(logo_path):
                    with open(logo_path, 'rb') as f:
                        data = f.read()
                    # O mesmo logo repete-se em várias pastas: um só na biblioteca
                    existing = conn.execute('SELECT id FROM logos WHERE md5(data) = %s',
                                            (hashlib.md5(data).hexdigest(),)).fetchone()
                    logo_id = existing['id'] if existing else conn.execute(
                        'INSERT INTO logos (name, mime, data) VALUES (%s, %s, %s) '
                        'ON CONFLICT (name) DO UPDATE SET name = logos.name RETURNING id',
                        (os.path.splitext(os.path.basename(logo_path))[0],
                         'image/png' if logo_path.lower().endswith('.png') else 'image/jpeg', data)).fetchone()['id']
                report_id = conn.execute(
                    '''INSERT INTO reports (name, team, source_id, dashboard_uid, dashboard_slug, dashboard_title,
                                            variables, period, theme, per_page, orientation, output, panel_width,
                                            panel_height, logo_id, title, subject, created_by, updated_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 2, 'portrait', 'pdf', %s, %s, %s, %s, %s,
                               'importação', 'importação') RETURNING id''',
                    # Título provisório a partir do slug; o editor guarda o título real do Grafana
                    (name, args.team, source['id'], first['uid'], first['slug'],
                     first['slug'].replace('-', ' ').capitalize(),
                     Jsonb(first['variables']), period, first['theme'], first['width'], first['height'], logo_id,
                     name, mail['email_subject'])).fetchone()['id']
                for position, panel in enumerate(panels):
                    conn.execute('INSERT INTO report_panels (report_id, position, panel_id, title, period) '
                                 'VALUES (%s, %s, %s, %s, %s)',
                                 (report_id, position, panel['panel_id'], f"Painel {panel['panel_id']}",
                                  None if panel['period'] == period else panel['period']))
                if to:
                    conn.execute('INSERT INTO report_recipients (report_id, channel_id, target) VALUES (%s, %s, %s)',
                                 (report_id, channel['id'], Jsonb({'to': to, 'cc': []})))
                conn.execute("INSERT INTO audit (username, action, target, detail) VALUES ('importação', "
                             "'report importado', %s, %s)", (f'#{report_id} {name}', f'{group}/{dashboard}'))
            created.append(name)
    print(f'{len(created)} reports importados (inativos, sem agendamento).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
