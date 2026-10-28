# REPORTING TOOL

Reports em PDF (ou ZIP de PNGs) a partir de painéis de fontes de dados (para já, Grafana com o plugin de render), enviados por e-mail (SMTP ou NS) e Telegram, com agendamento, histórico e auditoria. Mesmo layout e login Keycloak das outras apps.

## Perfis (Keycloak)

Grupos debaixo de `KEYCLOAK_GROUP_ROOT` (por defeito `/REPORTING_CORP`), lidos do claim `groups` (mapper "Group Membership", "Full group path"):

| Grupo | Acesso |
|---|---|
| `/REPORTING_CORP/ADMIN` | Fontes de dados, canais, logos, configurações, auditoria e os reports de todas as equipas |
| `/REPORTING_CORP/OPERADOR` | Reports da equipa `OPERADOR` |
| `/REPORTING_CORP/<EQUIPA>` (opcional) | Reports da equipa `<EQUIPA>` (ex. `/REPORTING_CORP/PKI`) |

Quem não está em nenhum vê "Sem acesso" e sai da sessão; o próximo login lê os grupos de novo.

## Como funciona

- **Fontes de dados** (admin): URL base do Grafana e token de service account (papel Viewer). O editor lê os dashboards, painéis e variáveis pela API do Grafana.
- **Canais** (admin): SMTP, NS (SOAP `MailRequest`, com endpoint, ClientID, TemplateID, SignMessage…) e Telegram (bot). Cada canal tem "Enviar teste".
- **Logos** (admin): biblioteca de PNG/JPG; cada report escolhe o seu.
- **Report**: equipa, fonte, dashboard, painéis por ordem (cada painel pode ter um período próprio), variáveis, período, formato (PDF A4 ou ZIP), tema, agendamento (diário, semanal, mensal) e destinatários por canal (e-mails Para/Cc; Telegram `chat_id` e ID do tópico).
- **Execuções**: "Executar agora", agendadas e reenvios correm no scheduler; cada uma guarda o log (painéis e envios), o estado (`sucesso`, `parcial`, `erro`) e o ficheiro durante a retenção.
- Tokens e passwords das fontes e canais ficam cifrados na base de dados com `ENCRYPTION_KEY`; nunca voltam ao browser.

## Arranque

```
cp .env.example .env    # preencher POSTGRES_PASSWORD, SECRET_KEY, ENCRYPTION_KEY e Keycloak
docker compose up -d
```

Três contentores: `db` (PostgreSQL, volume `reporting-db`), `web` (gunicorn) e `scheduler`. Corra **um só** scheduler: é ele que envia os reports agendados. As migrações aplicam-se sozinhas ao arrancar.

**Não perca a `ENCRYPTION_KEY`**: sem ela os tokens e passwords guardados ficam ilegíveis e têm de ser introduzidos de novo.

Backup: `docker compose exec db pg_dump -U reporting reporting > reporting.sql` (inclui logos e ficheiros gerados).

## Importar os scripts antigos (GRAFANA_REPORTS)

```
docker compose run --rm -v /caminho/GRAFANA_REPORTS:/legacy:ro web python legacy_import.py /legacy --dry-run
docker compose run --rm -v /caminho/GRAFANA_REPORTS:/legacy:ro web python legacy_import.py /legacy
```

Cria um report por dashboard (painéis, períodos de cada painel, variáveis, tema, logo, destinatários), as fontes Grafana (**sem token**: introduza-o em Fontes de dados) e o canal SMTP. Os reports ficam **inativos e sem agendamento**, para não enviarem em duplicado com o cron antigo. Correr de novo não duplica.

## Build

```
docker build --build-arg VERSION=<versão> -t ghcr.io/h3-lab/reporting-tool:<versão> .
```

Na rede da Multicert: `--build-arg http_proxy=http://proxy.multicert.inet:8080 --build-arg https_proxy=http://proxy.multicert.inet:8080`.

## Testes

Com um PostgreSQL vazio (a base é limpa pelos testes):

```
docker run --rm -e DATABASE_URL=postgresql://... -v "$PWD":/w -w /w/src --user root ghcr.io/h3-lab/reporting-tool:<versão> \
    python -m unittest discover -s ../tests
```
