-- Categoria livre do report: a lista de reports agrupa-se em tabs por categoria ('' = sem categoria)
ALTER TABLE reports ADD COLUMN category text NOT NULL DEFAULT '';
CREATE INDEX reports_category ON reports (category);
