# Agente do REPORT SERVICE: corre na máquina que chega às bases de dados.
# Build:  docker build --build-arg VERSION=<versão> -t ghcr.io/h3-lab/report-service-agent:<versão> agent/
FROM python:3.12-slim

ARG VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_VERSION=${VERSION} \
    TZ=Europe/Lisbon

WORKDIR /agent
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .
RUN useradd --system --uid 10002 agent
USER agent

CMD ["python", "agent.py"]
