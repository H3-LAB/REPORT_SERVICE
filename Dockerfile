# REPORTING TOOL: web (gunicorn) e scheduler usam esta imagem; muda só o comando.
# Build:  docker build --build-arg VERSION=<versão> -t ghcr.io/h3-lab/reporting-tool:<versão> .
# Na rede da Multicert passar o proxy (não fica na imagem):
#   --build-arg http_proxy=http://proxy.multicert.inet:8080 --build-arg https_proxy=http://proxy.multicert.inet:8080
FROM python:3.12-slim

ARG VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${VERSION}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/
RUN useradd --system --uid 10001 reporting
USER reporting

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
# timeout 300: a pré-visualização espera pelo render dos painéis no Grafana
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "300", "--access-logfile", "-", "app:app"]
