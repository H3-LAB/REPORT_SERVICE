"""Login Keycloak (OIDC) e perfis pelos grupos de ``KEYCLOAK_GROUP_ROOT`` (por defeito ``/REPORTING_CORP``).

- ``<raiz>/ADMIN``: fontes, canais, logos, configurações, auditoria e os reports de todas as equipas.
- ``<raiz>/OPERADOR``: reports da equipa ``OPERADOR``.
- Outro subgrupo (``<raiz>/PKI``): equipa ``PKI``; quem lá está vê e edita os reports dessa equipa.
"""

import os
from functools import wraps
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

bp = Blueprint('auth', __name__)
oauth = OAuth()

# A API dos agentes autentica-se com o token do agente, não com o Keycloak
PUBLIC_ENDPOINTS = {'health', 'static', 'auth.login', 'auth.callback', 'agent_api.claim', 'agent_api.result',
                    'signature_image'}


def _normalize(group):
    # Com "Full group path" o Keycloak envia "/REPORTING_CORP/ADMIN": compara sem barras e sem maiúsculas
    return str(group).strip('/').casefold()


def group_root():
    return os.getenv('KEYCLOAK_GROUP_ROOT', '/REPORTING_CORP')


def access(user):
    """``{'admin': bool, 'teams': [...]}`` ou ``None`` se o utilizador não pertence a nenhum grupo da app."""
    if current_app.config['AUTH_DISABLED']:
        return {'admin': True, 'teams': ['OPERADOR']}
    root = _normalize(group_root())
    admin = False
    teams = []
    for group in (user or {}).get('groups', []):
        path = _normalize(group)
        if not path.startswith(root + '/'):
            continue
        name = path[len(root) + 1:].split('/', 1)[0].upper()
        if name == 'ADMIN':
            admin = True
        elif name and name not in teams:
            teams.append(name)
    if not admin and not teams:
        return None
    return {'admin': admin, 'teams': teams}


def current_user():
    return session.get('user')


def current_access():
    return access(current_user())


def username():
    return (current_user() or {}).get('username', 'local')


def can_see_team(team):
    acc = current_access()
    return bool(acc and (acc['admin'] or team in acc['teams']))


def wants_json():
    return (request.method != 'GET' or request.path.split('/')[1:2] == ['api']
            or request.accept_mimetypes.best_match(['application/json', 'text/html']) == 'application/json')


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not (current_access() or {}).get('admin'):
            if wants_json():
                return jsonify({'error': 'Só um administrador pode fazer isto.'}), 403
            return redirect(url_for('reports'))
        return view(*args, **kwargs)
    return wrapper


def init_auth(app):
    disabled = os.getenv('AUTH_DISABLED', '0') == '1'
    secret = os.getenv('SECRET_KEY')
    if not secret and not disabled:
        # Com uma chave conhecida qualquer pessoa forja uma sessão de admin
        raise RuntimeError('SECRET_KEY em falta no env.')
    app.config.update(
        AUTH_DISABLED=disabled,
        SECRET_KEY=secret or os.urandom(32),
        SESSION_COOKIE_NAME='reporting_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', '0' if disabled else '1') == '1',
    )
    if not disabled:
        missing = [name for name in ('KEYCLOAK_URL', 'KEYCLOAK_REALM', 'KEYCLOAK_CLIENT_ID', 'KEYCLOAK_CLIENT_SECRET')
                   if not os.getenv(name)]
        if missing:
            raise RuntimeError('Configuração Keycloak em falta: ' + ', '.join(missing))
        issuer = f"{os.environ['KEYCLOAK_URL'].rstrip('/')}/realms/{os.environ['KEYCLOAK_REALM']}"
        app.config['KEYCLOAK_ISSUER'] = issuer
        oauth.init_app(app)
        oauth.register('keycloak', client_id=os.environ['KEYCLOAK_CLIENT_ID'],
                       client_secret=os.environ['KEYCLOAK_CLIENT_SECRET'],
                       server_metadata_url=f'{issuer}/.well-known/openid-configuration',
                       client_kwargs={'scope': 'openid profile email', 'code_challenge_method': 'S256',
                                      'default_timeout': 10})
    app.register_blueprint(bp)

    @app.before_request
    def require_login():
        if app.config['AUTH_DISABLED']:
            session.setdefault('user', {'username': 'local', 'name': 'Utilizador local', 'email': 'local@localhost',
                                        'groups': []})
            return None
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        user = current_user()
        if not user:
            if wants_json():
                return jsonify({'error': 'Sessão expirada. Recarregue a página.'}), 401
            session['next'] = request.full_path if request.query_string else request.path
            return redirect(url_for('auth.login'))
        if not access(user):
            # Sai da sessão: depois de o admin do Keycloak dar acesso, o próximo login lê os grupos de novo
            session.pop('user', None)
            if wants_json():
                return jsonify({'error': 'Sem permissão.'}), 403
            return render_template('forbidden.html', denied=user, root=group_root()), 403
        return None


@bp.route('/login')
def login():
    if current_app.config['AUTH_DISABLED'] or current_user():
        return redirect(url_for('reports'))
    return oauth.keycloak.authorize_redirect(url_for('auth.callback', _external=True))


@bp.route('/auth/callback')
def callback():
    try:
        token = oauth.keycloak.authorize_access_token()
    except Exception as exc:
        current_app.logger.warning('Login Keycloak falhou: %s', exc)
        return render_template('forbidden.html', error='Não foi possível iniciar sessão no Keycloak.'), 401
    claims = token.get('userinfo') or {}
    if 'groups' not in claims:
        return render_template('forbidden.html', error='O Keycloak não enviou os grupos. No client, adicione o mapper '
                               '"Group Membership" com o claim "groups" (Full group path).'), 401
    next_url = session.pop('next', None)
    session.clear()
    session.permanent = True
    session['user'] = {'username': claims.get('preferred_username', ''), 'email': claims.get('email', ''),
                       'name': claims.get('name') or claims.get('preferred_username', ''),
                       'groups': claims.get('groups', [])}
    session['id_token'] = token.get('id_token')
    # Só caminhos desta app: um "next" externo seria um open redirect
    if not (next_url and next_url.startswith('/') and not next_url.startswith('//')):
        next_url = None
    return redirect(request.script_root + next_url if next_url else url_for('reports'))


@bp.route('/logout')
def logout():
    id_token = session.get('id_token')
    session.clear()
    if current_app.config['AUTH_DISABLED']:
        return redirect(url_for('reports'))
    params = {'post_logout_redirect_uri': url_for('reports', _external=True),
              'client_id': os.environ['KEYCLOAK_CLIENT_ID']}
    if id_token:
        params['id_token_hint'] = id_token
    return redirect(f"{current_app.config['KEYCLOAK_ISSUER']}/protocol/openid-connect/logout?" + urlencode(params))
