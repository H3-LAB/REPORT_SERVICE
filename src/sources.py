"""Fontes de dados. Cada tipo sabe listar dashboards/painéis e renderizar um painel em PNG.

Uma fonte nova (outro tipo) implementa os mesmos quatro métodos e entra em ``KINDS``.
"""

import requests

import db


class SourceError(Exception):
    pass


class GrafanaSource:
    """Grafana com o plugin de render (``/render/d-solo``), autenticado por token de service account."""

    def __init__(self, base_url, token, org_id=1, verify_tls=True, timezone='Europe/Lisbon'):
        self.base_url = base_url.rstrip('/')
        self.org_id = org_id
        self.verify = verify_tls
        self.timezone = timezone
        self.session = requests.Session()
        if token:
            self.session.headers['Authorization'] = f'Bearer {token}'

    def _get(self, path, timeout=15, **params):
        try:
            response = self.session.get(self.base_url + path, params=params, verify=self.verify, timeout=timeout)
        except requests.RequestException as exc:
            raise SourceError(f'Sem ligação ao Grafana: {type(exc).__name__}') from exc
        if response.status_code in (401, 403):
            raise SourceError(f'O Grafana recusou o token (HTTP {response.status_code}).')
        if response.status_code != 200:
            raise SourceError(f'O Grafana respondeu HTTP {response.status_code} em {path}.')
        return response

    def test(self):
        """Valida URL e token; devolve uma descrição curta."""
        health = self._get('/api/health').json()
        self._get('/api/search', limit=1)
        return f"Grafana {health.get('version', '?')}, token aceite"

    def dashboards(self, search=''):
        rows = self._get('/api/search', type='dash-db', query=search, limit=100).json()
        return [{'uid': row['uid'], 'title': row['title'], 'folder': row.get('folderTitle') or 'General',
                 'slug': row.get('url', '').rstrip('/').rsplit('/', 1)[-1] or '_'} for row in rows]

    def dashboard(self, uid):
        """Painéis (incluindo os de linhas recolhidas) e variáveis do dashboard."""
        data = self._get(f'/api/dashboards/uid/{uid}').json()
        panels = []

        def collect(items):
            for panel in items:
                if panel.get('type') == 'row':
                    collect(panel.get('panels', []))
                elif 'id' in panel:
                    panels.append({'id': panel['id'], 'title': panel.get('title') or f"Painel {panel['id']}",
                                   'type': panel.get('type', '')})

        collect(data['dashboard'].get('panels', []))
        variables = []
        for var in data['dashboard'].get('templating', {}).get('list', []):
            current = var.get('current', {}).get('value', '')
            variables.append({'name': var['name'], 'label': var.get('label') or var['name'],
                              'current': current if isinstance(current, str) else ','.join(current),
                              'options': [o.get('value') for o in var.get('options', []) if isinstance(o.get('value'), str)]})
        return {'uid': uid, 'title': data['dashboard'].get('title', ''),
                'slug': data.get('meta', {}).get('slug') or '_', 'panels': panels, 'variables': variables}

    def render(self, uid, slug, panel_id, start, end, variables=None, theme='light', width=1900, height=1100):
        """PNG do painel no período ``start``–``end`` (datetimes)."""
        params = {'orgId': self.org_id, 'panelId': panel_id, 'from': int(start.timestamp() * 1000),
                  'to': int(end.timestamp() * 1000), 'theme': theme, 'width': width, 'height': height,
                  'tz': self.timezone}
        for name, value in (variables or {}).items():
            params[f'var-{name}'] = value
        # O render demora: cada painel abre um browser no renderer
        response = self._get(f'/render/d-solo/{uid}/{slug or "_"}', timeout=120, **params)
        if not response.headers.get('Content-Type', '').startswith('image/png'):
            raise SourceError('O Grafana não devolveu uma imagem (o plugin de render está instalado?).')
        return response.content


KINDS = {'grafana': ('Grafana', GrafanaSource)}


def open_source(row):
    """Cliente da fonte a partir da linha de ``data_sources``."""
    _, cls = KINDS[row['kind']]
    return cls(row['base_url'], db.decrypt(row['token']), org_id=row['org_id'], verify_tls=row['verify_tls'],
               timezone=row['timezone'])
