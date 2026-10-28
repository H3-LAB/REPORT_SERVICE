-- Assinaturas dos e-mails: HTML com as imagens embebidas (data:), escolhida em cada report
CREATE TABLE signatures (
    id         serial PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    html       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE reports ADD COLUMN signature_id integer REFERENCES signatures ON DELETE SET NULL;
