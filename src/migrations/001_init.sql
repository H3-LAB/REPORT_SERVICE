-- Esquema inicial do REPORTING TOOL

CREATE TABLE settings (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);

-- Fontes de dados (para já só Grafana). O token vai cifrado (Fernet, ENCRYPTION_KEY).
CREATE TABLE data_sources (
    id         serial PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    kind       text NOT NULL DEFAULT 'grafana' CHECK (kind IN ('grafana')),
    base_url   text NOT NULL,
    org_id     integer NOT NULL DEFAULT 1,
    token      text,
    verify_tls boolean NOT NULL DEFAULT true,
    timezone   text NOT NULL DEFAULT 'Europe/Lisbon',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Canais de envio: config visível em jsonb, segredos (password, token do bot) cifrados em secret.
CREATE TABLE channels (
    id         serial PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    kind       text NOT NULL CHECK (kind IN ('smtp', 'ns', 'telegram')),
    config     jsonb NOT NULL DEFAULT '{}',
    secret     text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE logos (
    id         serial PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    mime       text NOT NULL,
    data       bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reports (
    id              serial PRIMARY KEY,
    name            text NOT NULL,
    team            text NOT NULL,
    source_id       integer NOT NULL REFERENCES data_sources ON DELETE RESTRICT,
    dashboard_uid   text NOT NULL,
    dashboard_slug  text NOT NULL DEFAULT '_',
    dashboard_title text NOT NULL DEFAULT '',
    variables       jsonb NOT NULL DEFAULT '{}',
    period          text NOT NULL,
    theme           text NOT NULL DEFAULT 'light' CHECK (theme IN ('light', 'dark')),
    per_page        integer NOT NULL DEFAULT 2 CHECK (per_page IN (1, 2)),
    orientation     text NOT NULL DEFAULT 'portrait' CHECK (orientation IN ('portrait', 'landscape')),
    output          text NOT NULL DEFAULT 'pdf' CHECK (output IN ('pdf', 'zip')),
    panel_width     integer NOT NULL DEFAULT 1900,
    panel_height    integer NOT NULL DEFAULT 1100,
    logo_id         integer REFERENCES logos ON DELETE SET NULL,
    title           text NOT NULL DEFAULT '',
    subject         text NOT NULL DEFAULT '',
    message         text NOT NULL DEFAULT '',
    schedule        jsonb,
    active          boolean NOT NULL DEFAULT false,
    next_run_at     timestamptz,
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reports_team ON reports (team);
CREATE INDEX reports_due ON reports (next_run_at) WHERE active;

CREATE TABLE report_panels (
    report_id integer NOT NULL REFERENCES reports ON DELETE CASCADE,
    position  integer NOT NULL,
    panel_id  integer NOT NULL,
    title     text NOT NULL DEFAULT '',
    -- Período só deste painel (ex. diário, semanal e mensal no mesmo PDF); NULL usa o do report
    period    text,
    PRIMARY KEY (report_id, position)
);

-- target: smtp/ns {"to": [...], "cc": [...]}; telegram {"chat_id": "...", "topic_id": "..."}
CREATE TABLE report_recipients (
    id         serial PRIMARY KEY,
    report_id  integer NOT NULL REFERENCES reports ON DELETE CASCADE,
    channel_id integer NOT NULL REFERENCES channels ON DELETE RESTRICT,
    target     jsonb NOT NULL
);

CREATE TABLE runs (
    id            serial PRIMARY KEY,
    report_id     integer NOT NULL REFERENCES reports ON DELETE CASCADE,
    trigger       text NOT NULL CHECK (trigger IN ('agendado', 'manual', 'reenvio')),
    status        text NOT NULL DEFAULT 'pendente'
                  CHECK (status IN ('pendente', 'a executar', 'sucesso', 'parcial', 'erro')),
    requested_by  text,
    parent_run_id integer REFERENCES runs ON DELETE SET NULL,
    period_from   timestamptz,
    period_to     timestamptz,
    log           jsonb NOT NULL DEFAULT '[]',
    file_name     text,
    file_mime     text,
    file          bytea,
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz
);
CREATE INDEX runs_pending ON runs (created_at) WHERE status = 'pendente';
CREATE INDEX runs_report ON runs (report_id, created_at DESC);

CREATE TABLE audit (
    id       serial PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    username text,
    action   text NOT NULL,
    target   text,
    detail   text
);
CREATE INDEX audit_at ON audit (at DESC);
