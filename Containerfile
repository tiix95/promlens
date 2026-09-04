FROM debian:trixie-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3-aiohttp \
        python3-fastapi \
        python3-uvicorn \
        python3-yaml \
        python3-pydantic \
        python3-uvloop \
        python3-websockets \
        python3-httptools \
        python3-passlib \
        python3-bcrypt \
        python3-itsdangerous \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r promlens && useradd -r -g promlens -d /app -s /sbin/nologin promlens

WORKDIR /app

COPY src/main.py src/prometheus_query.py src/auth.py src/notifier.py src/local_alerts.py ./
COPY src/static/ ./static/

RUN chown -R promlens:promlens /app && mkdir /data && chown promlens:promlens /data

ENV CONFIG_FILE=/data/promlens.yaml
ENV TOPOLOGY_FILE=/data/topology.yaml
ENV LAYOUT_FILE=/data/layout.json
ENV BIND_HOST=0.0.0.0
ENV BIND_PORT=8000

VOLUME ["/data"]

EXPOSE 8000

USER promlens

CMD python3 -m uvicorn main:app --host ${BIND_HOST} --port ${BIND_PORT}
