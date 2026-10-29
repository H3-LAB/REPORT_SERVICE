-- Como o túnel SSH chega ao servidor: proxy HTTP (CONNECT) e/ou bastion (ProxyJump), ambos opcionais
ALTER TABLE db_connections
    ADD COLUMN ssh_proxy            text,                    -- host:porta
    ADD COLUMN ssh_jump_host        text,
    ADD COLUMN ssh_jump_port        integer NOT NULL DEFAULT 22,
    ADD COLUMN ssh_jump_user        text,
    ADD COLUMN ssh_jump_key         text,                    -- cifrada; vazia = a chave do túnel
    ADD COLUMN ssh_jump_known_hosts text;
