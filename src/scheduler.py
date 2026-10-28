"""Processo do scheduler: corre num contentor próprio (um só), nunca dentro dos workers do gunicorn.

Cria as execuções agendadas e executa as pendentes (agendadas, "Executar agora" e reenvios).
"""

import logging
import os
import time

import db
import runner

POLL_SECONDS = int(os.getenv('SCHEDULER_POLL_SECONDS', '15'))
HOUSEKEEPING_SECONDS = 3600
HEARTBEAT = '/tmp/scheduler.heartbeat'


def touch_heartbeat():
    """Sinal de vida para o healthcheck (healthcheck.py): só depois de um ciclo sem erros."""
    with open(HEARTBEAT, 'w'):
        pass


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    log = logging.getLogger('reporting.scheduler')
    db.migrate()
    log.info('Scheduler a correr (verifica a cada %ss)', POLL_SECONDS)
    last_housekeeping = 0
    while True:
        try:
            if time.monotonic() - last_housekeeping > HOUSEKEEPING_SECONDS:
                runner.housekeeping()
                last_housekeeping = time.monotonic()
            if runner.enqueue_due():
                log.info('Execuções agendadas criadas')
            while run := runner.claim_next():
                runner.execute(run)
                touch_heartbeat()
            touch_heartbeat()
        except Exception:
            # Base de dados em baixo, por exemplo: tenta outra vez na próxima volta
            log.exception('Erro no ciclo do scheduler')
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
