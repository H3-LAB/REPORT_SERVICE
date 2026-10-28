-- Agentes: correm noutras máquinas (as que chegam às bases de dados) e vão buscar trabalho à app por HTTPS
CREATE TABLE agents (
    id            serial PRIMARY KEY,
    name          text NOT NULL UNIQUE,
    token_hash    text NOT NULL,            -- sha256 do token; o token só é mostrado ao criar
    hostname      text,
    version       text,
    allow_scripts boolean,                  -- o que o agente reporta (ALLOW_SCRIPTS no compose dele)
    last_seen_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Ligações a bases de dados, usadas por um agente; a password vai cifrada (ENCRYPTION_KEY)
CREATE TABLE db_connections (
    id         serial PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    agent_id   integer NOT NULL REFERENCES agents ON DELETE RESTRICT,
    kind       text NOT NULL DEFAULT 'postgresql' CHECK (kind IN ('postgresql')),
    host       text NOT NULL,
    port       integer NOT NULL DEFAULT 5432,
    database   text NOT NULL,
    username   text NOT NULL,
    password   text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Tipo do report: painéis do Grafana (na app), SQL para Excel/CSV ou script (no agente)
ALTER TABLE reports
    ADD COLUMN kind text NOT NULL DEFAULT 'grafana' CHECK (kind IN ('grafana', 'sql', 'script')),
    ADD COLUMN agent_id integer REFERENCES agents ON DELETE RESTRICT,
    ADD COLUMN script_lang text CHECK (script_lang IN ('python', 'shell')),
    ADD COLUMN script text NOT NULL DEFAULT '',
    ALTER COLUMN source_id DROP NOT NULL,
    ALTER COLUMN dashboard_uid DROP NOT NULL,
    DROP CONSTRAINT reports_output_check,
    ADD CONSTRAINT reports_output_check CHECK (output IN ('pdf', 'zip', 'xlsx', 'xlsx_zip', 'csv_zip'));

-- SQL: uma folha por consulta. Script: as ligações que o script recebe (sheet e query vazias)
CREATE TABLE report_queries (
    report_id     integer NOT NULL REFERENCES reports ON DELETE CASCADE,
    position      integer NOT NULL,
    connection_id integer NOT NULL REFERENCES db_connections ON DELETE RESTRICT,
    sheet         text NOT NULL DEFAULT '',
    query         text NOT NULL DEFAULT '',
    PRIMARY KEY (report_id, position)
);

-- Execuções de teste (geram sem enviar) e o agente que executou
ALTER TABLE runs
    ADD COLUMN agent_id integer REFERENCES agents ON DELETE SET NULL,
    DROP CONSTRAINT runs_trigger_check,
    ADD CONSTRAINT runs_trigger_check CHECK (trigger IN ('agendado', 'manual', 'reenvio', 'teste'));
