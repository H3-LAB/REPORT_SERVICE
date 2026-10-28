# REPORT SERVICE

Reports em PDF (ou ZIP de PNGs) a partir de painéis do Grafana, ou em Excel/CSV a partir de bases de dados (através de agentes noutras máquinas), enviados por e-mail (SMTP ou NS) e Telegram, com agendamento, histórico e auditoria. Mesmo layout e login Keycloak das outras apps.

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
- **Report**: equipa, categoria (a lista de reports agrupa-se em tabs por categoria), fonte, dashboard, painéis por ordem (cada painel pode ter um período próprio), variáveis, período, formato (PDF A4 ou ZIP), tema, agendamento (diário, semanal, mensal) e destinatários por canal (e-mails Para/Cc; Telegram `chat_id` e ID do tópico).
- **Execuções**: "Executar agora", agendadas e reenvios correm no scheduler; cada uma guarda o log (painéis e envios), o estado (`sucesso`, `parcial`, `erro`) e o ficheiro durante a retenção.
- Tokens e passwords das fontes e canais ficam cifrados na base de dados com `ENCRYPTION_KEY`; nunca voltam ao browser.

## Agentes (reports SQL e script)

Para reports que leem bases de dados a que a app não chega: um **agente** corre na máquina que chega (ex. zproxy15) e só faz pedidos HTTPS de saída para a app.

1. **Administração → Agentes**: criar o agente (ex. `zproxy15`) e copiar o token (só aparece uma vez).
2. Na máquina do agente, com `agent/docker-compose.yml` e o `.env` (ver `agent/.env.example`):
   ```
   REPORT_SERVICE_URL=https://<servidor>/report-service
   AGENT_TOKEN=rta_...
   AGENT_HOSTNAME=zproxy15
   ALLOW_SCRIPTS=false
   HTTPS_PROXY=            # se sai pelo proxy
   ```
   `docker compose up -d`. Em Agentes passa a "ligado".
3. **Administração → Ligações BD**: host, porta, base, utilizador e password (cifrada), e o agente que lá chega. Use utilizadores só de leitura; o agente abre as transações em `READ ONLY`.
4. **Novo report**, tipo:
   - **Consultas SQL**: uma folha do Excel (ou CSV) por consulta; as colunas da query são o cabeçalho (`AS "NOME"`). O período entra com `:from` e `:to` (exclusivo): `WHERE criado >= :from AND criado < :to`. Formato `.xlsx`, `.xlsx` num ZIP, ou ZIP de CSV (`;`, UTF-8).
   - **Script** (Python 3 ou bash): só corre em agentes com `ALLOW_SCRIPTS=true`. Escreve o ficheiro em `$REPORT_OUTPUT_DIR` (vários vão num ZIP); recebe `REPORT_FROM`, `REPORT_TO`, `REPORT_FILE_STEM` e, por ligação escolhida, `DB_<NOME>_HOST/_PORT/_NAME/_USER/_PASSWORD/_DSN`. Tem `psycopg`, `psycopg2`, `openpyxl` e `requests`. O ambiente do script não tem o token do agente.
5. "Guardar e testar no agente" gera sem enviar; a execução mostra o log do agente e o ficheiro.

O envio (mail/NS/Telegram), o agendamento, o reenvio e a retenção são os mesmos dos reports do Grafana. Uma execução que o agente não vai buscar em `AGENT_WAIT_MINUTES` (30) fica em erro.

Build do agente: `docker build --build-arg VERSION=<versão> -t ghcr.io/h3-lab/report-service-agent:<versão> agent/`

## Arranque

```
cp .env.example .env    # preencher POSTGRES_PASSWORD, SECRET_KEY, ENCRYPTION_KEY e Keycloak
docker compose up -d
```

Três contentores: `db` (PostgreSQL, volume `report-service-db`), `web` (gunicorn) e `scheduler`. Corra **um só** scheduler: é ele que envia os reports agendados. As migrações aplicam-se sozinhas ao arrancar.

**Não perca a `ENCRYPTION_KEY`**: sem ela os tokens e passwords guardados ficam ilegíveis e têm de ser introduzidos de novo.

Backup: `docker compose exec db pg_dump -U report_service report_service > report_service.sql` (inclui logos e ficheiros gerados).

## Importar os scripts antigos (GRAFANA_REPORTS)

```
docker compose run --rm -v /caminho/GRAFANA_REPORTS:/legacy:ro web python legacy_import.py /legacy --dry-run
docker compose run --rm -v /caminho/GRAFANA_REPORTS:/legacy:ro web python legacy_import.py /legacy
```

Cria um report por dashboard (categoria = nome da pasta, painéis, períodos de cada painel, variáveis, tema, logo, destinatários), as fontes Grafana (**sem token**: introduza-o em Fontes de dados) e o canal SMTP. Os reports ficam **inativos e sem agendamento**, para não enviarem em duplicado com o cron antigo. Correr de novo não duplica.

## Build

```
docker build --build-arg VERSION=<versão> -t ghcr.io/h3-lab/report-service:<versão> .
```

Na rede da Multicert: `--build-arg http_proxy=http://proxy.multicert.inet:8080 --build-arg https_proxy=http://proxy.multicert.inet:8080`.

## Testes

Com um PostgreSQL vazio (a base é limpa pelos testes) e o `openpyxl` (usado pelo agente):

```
docker run --rm -e DATABASE_URL=postgresql://... -v "$PWD":/w -w /w/src --user root ghcr.io/h3-lab/report-service:<versão> \
    sh -c 'pip install -q openpyxl && python -m unittest discover -s ../tests'
```
