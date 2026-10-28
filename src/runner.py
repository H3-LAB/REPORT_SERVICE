"""Gerar um report (render dos painéis + PDF/ZIP) e executar as execuções pendentes.

Os reports SQL e script são gerados por um agente (``agents.py``); o ficheiro volta para a execução e o envio
segue por aqui, como nos do Grafana.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

import db
from delivery import DeliveryError, recipients_text, send
from periods import PERIODS, next_run, period_range
from render import build_pdf, build_zip, file_stem
from sources import SourceError, open_source

logger = logging.getLogger('reporting.runner')

# Cada painel abre um browser no renderer do Grafana: poucos de cada vez
RENDER_CONCURRENCY = int(os.getenv('RENDER_CONCURRENCY', '2'))
APP_TIMEZONE = ZoneInfo(os.getenv('APP_TIMEZONE', 'Europe/Lisbon'))
# Uma execução "a executar" há mais tempo que isto ficou presa (scheduler reiniciado a meio)
STALE_MINUTES = 60
# Uma execução de agente "pendente" há mais tempo que isto: o agente está desligado
AGENT_WAIT_MINUTES = int(os.getenv('AGENT_WAIT_MINUTES', '30'))
AGENT_KINDS = ('sql', 'script')


class Log(list):
    """Linhas ``{time, level, message}`` mostradas na execução."""

    def add(self, message, level='info'):
        self.append({'time': datetime.now(APP_TIMEZONE).strftime('%H:%M:%S'), 'level': level, 'message': message})


def report_panels(report_id):
    return db.query('SELECT panel_id, title, period FROM report_panels WHERE report_id = %s ORDER BY position',
                    (report_id,))


def report_recipients(report_id):
    return db.query('SELECT r.target, c.* FROM report_recipients r JOIN channels c ON c.id = r.channel_id '
                    'WHERE r.report_id = %s ORDER BY r.id', (report_id,))


def generate(report, log):
    """Renderiza os painéis e monta o ficheiro. Devolve ``{file_name, mime, data, start, end, failed}``."""
    source_row = db.one('SELECT * FROM data_sources WHERE id = %s', (report['source_id'],))
    source = open_source(source_row)
    now = datetime.now(ZoneInfo(source_row['timezone']))
    start, end = period_range(report['period'], now)
    period_text = f'{start:%d/%m/%Y}' if start.date() == end.date() else f'{start:%d/%m/%Y} a {end:%d/%m/%Y}'
    panels = report_panels(report['id'])
    if not panels:
        raise RuntimeError('O report não tem painéis.')
    log.add(f"Período: {period_text} · {len(panels)} painéis de {report['dashboard_title'] or report['dashboard_uid']}")

    def fetch(panel):
        # Um painel com período próprio leva a legenda com as datas, para não se confundir com o do report
        own = panel['period'] and panel['period'] != report['period']
        panel_start, panel_end = period_range(panel['period'], now) if own else (start, end)
        caption = f"{PERIODS[panel['period']]}: {panel_start:%d/%m/%Y} a {panel_end:%d/%m/%Y}" if own else ''
        try:
            png = source.render(report['dashboard_uid'], report['dashboard_slug'], panel['panel_id'], panel_start,
                                panel_end, report['variables'], report['theme'], report['panel_width'],
                                report['panel_height'])
            return panel['title'], png, None, caption
        except SourceError as exc:
            return panel['title'], None, str(exc), caption

    with ThreadPoolExecutor(max_workers=RENDER_CONCURRENCY) as pool:
        results = list(pool.map(fetch, panels))
    failed = 0
    for title, png, error, _ in results:
        if png:
            log.add(f'Painel "{title}" renderizado ({len(png) // 1024} KB)')
        else:
            failed += 1
            log.add(f'Painel "{title}" falhou: {error}', 'warn')
    if failed == len(results):
        raise RuntimeError('Nenhum painel foi renderizado.')

    stem = file_stem(report['name'], start)
    if report['output'] == 'zip':
        data, mime, file_name = build_zip(stem, results), 'application/zip', stem + '.zip'
    else:
        logo = db.one('SELECT data FROM logos WHERE id = %s', (report['logo_id'],)) if report['logo_id'] else None
        data = build_pdf(report['title'] or report['name'], period_text, results, logo and bytes(logo['data']),
                         report['per_page'], report['orientation'])
        mime, file_name = 'application/pdf', stem + '.pdf'
    log.add(f'Gerado {file_name} ({len(data) // 1024} KB)', 'ok')
    return {'file_name': file_name, 'mime': mime, 'data': data, 'start': start, 'end': end, 'failed': failed}


def execute(run):
    """Executa uma linha de ``runs`` já marcada "a executar": gera (ou reutiliza, num reenvio) e envia."""
    log = Log(run.get('log') or [])  # num report de agente, o log do agente vem primeiro
    report = db.one('SELECT * FROM reports WHERE id = %s', (run['report_id'],))
    status, file_saved = 'erro', False
    try:
        if run.get('has_file'):
            # Gerado pelo agente
            done = db.one('SELECT file_name, file_mime, file, period_from, period_to FROM runs WHERE id = %s',
                          (run['id'],))
            result = {'file_name': done['file_name'], 'mime': done['file_mime'], 'data': bytes(done['file']),
                      'start': done['period_from'], 'end': done['period_to'], 'failed': 0}
        elif run['parent_run_id']:
            parent = db.one('SELECT file_name, file_mime, file, period_from, period_to FROM runs WHERE id = %s',
                            (run['parent_run_id'],))
            if not parent or parent['file'] is None:
                raise RuntimeError('O ficheiro da execução original já não existe (retenção).')
            result = {'file_name': parent['file_name'], 'mime': parent['file_mime'], 'data': bytes(parent['file']),
                      'start': parent['period_from'], 'end': parent['period_to'], 'failed': 0}
            log.add(f"Reenvio do ficheiro da execução #{run['parent_run_id']}: {parent['file_name']}")
        elif report['kind'] in AGENT_KINDS:
            raise RuntimeError('Este report é gerado por um agente.')
        else:
            result = generate(report, log)
        db.execute('UPDATE runs SET file_name = %s, file_mime = %s, file = %s, period_from = %s, period_to = %s '
                   'WHERE id = %s', (result['file_name'], result['mime'], result['data'], result['start'],
                                     result['end'], run['id']))
        file_saved = True

        recipients = [] if run.get('trigger') == 'teste' else report_recipients(report['id'])
        sent = failed_sends = 0
        subject = report['subject'] or report['title'] or report['name']
        for channel in recipients:
            try:
                detail = send(channel, channel['target'], subject, report['message'], result['file_name'],
                              result['mime'], result['data'])
                log.add(f"{channel['name']}: {detail}", 'ok')
                sent += 1
            except DeliveryError as exc:
                log.add(f"{channel['name']} ({recipients_text(channel['kind'], channel['target'])}): {exc}", 'error')
                failed_sends += 1
        if run.get('trigger') == 'teste':
            log.add('Teste: não foi enviado a ninguém.', 'ok')
        elif not recipients:
            log.add('Sem destinatários: o ficheiro fica só no histórico.', 'warn')

        if result['failed'] == 0 and failed_sends == 0:
            status = 'sucesso'
        elif recipients and sent == 0:
            status = 'erro'
        else:
            status = 'parcial'
    except Exception as exc:
        logger.exception('Execução %s falhou', run['id'])
        log.add(f'Erro: {exc}'[:500], 'error')
    finally:
        if not file_saved:
            status = 'erro'
        db.execute('UPDATE runs SET status = %s, log = %s, finished_at = now() WHERE id = %s',
                   (status, Jsonb(log), run['id']))
        logger.info('Execução %s do report %s: %s', run['id'], report['id'], status)
    return status


# ---------- Scheduler ----------

def enqueue_due():
    """Cria uma execução para cada report ativo cuja hora chegou, e calcula a próxima."""
    with db.connect() as conn:
        due = conn.execute('SELECT id, schedule FROM reports WHERE active AND next_run_at <= now() '
                           'FOR UPDATE SKIP LOCKED').fetchall()
        for report in due:
            conn.execute("INSERT INTO runs (report_id, trigger) VALUES (%s, 'agendado')", (report['id'],))
            conn.execute('UPDATE reports SET next_run_at = %s WHERE id = %s',
                         (next_run(report['schedule'], datetime.now(APP_TIMEZONE)), report['id']))
    return len(due)


def claim_next():
    """A execução pendente mais antiga, já marcada "a executar" (SKIP LOCKED: seguro com vários schedulers).

    As dos reports de agente só depois de o agente devolver o ficheiro (ou num reenvio)."""
    with db.connect() as conn:
        return conn.execute(
            "UPDATE runs SET status = 'a executar', started_at = coalesce(started_at, now()) WHERE id = ("
            "SELECT u.id FROM runs u JOIN reports r ON r.id = u.report_id WHERE u.status = 'pendente' "
            "AND (r.kind = 'grafana' OR u.file IS NOT NULL OR u.parent_run_id IS NOT NULL) ORDER BY u.created_at "
            "LIMIT 1 FOR UPDATE OF u SKIP LOCKED) "
            "RETURNING id, report_id, parent_run_id, trigger, log, file IS NOT NULL AS has_file").fetchone()


def housekeeping():
    """Fecha execuções presas e apaga os ficheiros mais antigos que a retenção (o registo fica)."""
    db.execute(f"UPDATE runs SET status = 'erro', finished_at = now(), log = log || %s "
               f"WHERE status = 'a executar' AND started_at < now() - interval '{STALE_MINUTES} minutes'",
               (Jsonb([{'time': '', 'level': 'error', 'message': 'Interrompida (o scheduler reiniciou).'}]),))
    db.execute(f"UPDATE runs u SET status = 'erro', finished_at = now(), log = log || %s FROM reports r "
               f"WHERE r.id = u.report_id AND r.kind IN ('sql', 'script') AND u.status = 'pendente' "
               f"AND u.file IS NULL AND u.parent_run_id IS NULL "
               f"AND u.created_at < now() - interval '{AGENT_WAIT_MINUTES} minutes'",
               (Jsonb([{'time': '', 'level': 'error',
                        'message': f'O agente não a foi buscar em {AGENT_WAIT_MINUTES} minutos: está ligado?'}]),))
    days = int(db.settings()['retention_days'])
    db.execute('UPDATE runs SET file = NULL WHERE file IS NOT NULL AND created_at < now() - make_interval(days => %s)',
               (days,))
