"""PostgreSQL: ligação, migrações, segredos cifrados, configurações e auditoria."""

import os
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MIGRATIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrations')
# Advisory lock: o web e o scheduler arrancam ao mesmo tempo e só um aplica as migrações
MIGRATION_LOCK = 724551

_pool = None


def pool():
    global _pool
    if _pool is None:
        _pool = ConnectionPool(os.environ['DATABASE_URL'], min_size=1, max_size=int(os.getenv('DB_POOL_SIZE', '5')),
                               kwargs={'row_factory': dict_row}, open=True)
    return _pool


@contextmanager
def connect():
    """Uma transação: commit no fim, rollback se houver exceção."""
    with pool().connection() as conn:
        yield conn


def query(sql, params=None):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql, params=None):
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql, params=None):
    with connect() as conn:
        return conn.execute(sql, params)


def migrate():
    """Aplica por ordem os ``migrations/NNN_*.sql`` ainda não aplicados."""
    with connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK,))
        conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
                     '(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
        done = {row['version'] for row in conn.execute('SELECT version FROM schema_migrations')}
        for name in sorted(os.listdir(MIGRATIONS)):
            if name.endswith('.sql') and name not in done:
                with open(os.path.join(MIGRATIONS, name)) as f:
                    conn.execute(f.read())
                conn.execute('INSERT INTO schema_migrations (version) VALUES (%s)', (name,))


# ---------- Segredos ----------

def _fernet():
    return Fernet(os.environ['ENCRYPTION_KEY'])


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode() if value else None


def decrypt(value):
    if not value:
        return ''
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError('Não foi possível decifrar um segredo: a ENCRYPTION_KEY mudou?') from exc


# ---------- Configurações ----------

SETTING_DEFAULTS = {'accent': '', 'retention_days': 90}


def settings():
    saved = {row['key']: row['value'] for row in query('SELECT key, value FROM settings')}
    return {key: saved.get(key, value) for key, value in SETTING_DEFAULTS.items()}


def save_settings(values):
    with connect() as conn:
        for key, value in values.items():
            conn.execute('INSERT INTO settings (key, value) VALUES (%s, %s) '
                         'ON CONFLICT (key) DO UPDATE SET value = excluded.value', (key, Jsonb(value)))


# ---------- Auditoria ----------

def audit(username, action, target='', detail=''):
    execute('INSERT INTO audit (username, action, target, detail) VALUES (%s, %s, %s, %s)',
            (username, action, target, detail))
