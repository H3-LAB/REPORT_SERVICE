-- Túnel SSH opcional por ligação: o agente abre-o só durante a execução (a chave vai cifrada, ENCRYPTION_KEY)
ALTER TABLE db_connections
    ADD COLUMN ssh_host        text,
    ADD COLUMN ssh_port        integer NOT NULL DEFAULT 22,
    ADD COLUMN ssh_user        text,
    ADD COLUMN ssh_key         text,
    ADD COLUMN ssh_known_hosts text;
