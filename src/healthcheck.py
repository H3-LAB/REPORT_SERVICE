"""Healthcheck da imagem, igual para os três contentores: vê o comando do processo 1.

- scheduler: o ciclo marcou o sinal de vida há pouco (fica parado se a base de dados estiver em baixo);
- gunicorn (web ou agent-api): GET /health na porta do --bind, em HTTPS se tiver --certfile.
"""

import os
import ssl
import sys
import time
import urllib.request

HEARTBEAT = '/tmp/scheduler.heartbeat'
# Uma execução longa (muitos painéis) não marca o sinal de vida enquanto corre
HEARTBEAT_MAX_SECONDS = 600


def main():
    with open('/proc/1/cmdline', 'rb') as f:
        args = [a.decode() for a in f.read().split(b'\0') if a]
    if any(a.endswith('scheduler.py') for a in args):
        return 0 if time.time() - os.path.getmtime(HEARTBEAT) < HEARTBEAT_MAX_SECONDS else 1
    bind = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ('--bind', '-b')), None) or \
        next((a.split('=', 1)[1] for a in args if a.startswith('--bind=')), '0.0.0.0:8000')
    tls = any(a == '--certfile' or a.startswith('--certfile=') for a in args)
    url = f"{'https' if tls else 'http'}://127.0.0.1:{bind.rsplit(':', 1)[1]}/health"
    urllib.request.urlopen(url, context=ssl._create_unverified_context() if tls else None, timeout=4)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(exc)
        sys.exit(1)
