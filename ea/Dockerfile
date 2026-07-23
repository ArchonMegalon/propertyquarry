FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl docker.io docker-compose espeak-ng ffmpeg && \
    curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-29.3.0.tgz -o /tmp/docker.tgz && \
    tar -xzf /tmp/docker.tgz -C /tmp && \
    mv /tmp/docker/docker /usr/local/bin/docker && \
    chmod +x /usr/local/bin/docker && \
    rm -rf /var/lib/apt/lists/* && \
    rm -rf /tmp/docker /tmp/docker.tgz && \
    adduser --system --uid 10001 --group ea

WORKDIR /app

RUN mkdir -p /app/scripts
COPY scripts/willhaben_property_packet.py /app/scripts/willhaben_property_packet.py

COPY . /tmp/src

RUN set -eu; \
    if [ -f /tmp/src/ea/requirements.txt ] && [ -d /tmp/src/ea/app ]; then \
      APP_SRC=/tmp/src/ea; \
    elif [ -f /tmp/src/requirements.txt ] && [ -d /tmp/src/app ]; then \
      APP_SRC=/tmp/src; \
    else \
      echo "application source not found in build context" >&2; \
      exit 1; \
    fi; \
    cp "$APP_SRC/requirements.txt" /app/requirements.txt; \
    if [ -f "$APP_SRC/requirements-openvoice.txt" ]; then cp "$APP_SRC/requirements-openvoice.txt" /app/requirements-openvoice.txt; fi; \
    cp -r "$APP_SRC/app" /app/; \
    cp -r "$APP_SRC/schema" /app/schema; \
    cp "$APP_SRC/docker-entrypoint.sh" /usr/local/bin/docker-entrypoint.sh; \
    if [ -f "$APP_SRC/LTDs.md" ]; then \
      cp "$APP_SRC/LTDs.md" /app/LTDs.md; \
    elif [ -f /tmp/src/LTDs.md ]; then \
      cp /tmp/src/LTDs.md /app/LTDs.md; \
    fi; \
    mkdir -p /app/scripts; \
    if [ -d /tmp/src/scripts ]; then \
      for script in /tmp/src/scripts/*; do \
        [ -f "$script" ] || continue; \
        cp "$script" /app/scripts/; \
      done; \
    elif [ -d "$APP_SRC/scripts" ]; then \
      for script in "$APP_SRC"/scripts/*; do \
        [ -f "$script" ] || continue; \
        cp "$script" /app/scripts/; \
      done; \
    fi; \
    mkdir -p /data/artifacts /data/public_property_tours /data/provider-ledger /home/ea; \
    chown -R ea:ea /app /data /home/ea /usr/local/bin/docker-entrypoint.sh; \
    rm -rf /tmp/src

RUN pip install --no-cache-dir -r requirements.txt

RUN chmod +x /usr/local/bin/docker-entrypoint.sh && \
    find /app/scripts -maxdepth 1 -type f -name '*.sh' -exec chmod +x {} \;

USER ea
HEALTHCHECK --interval=30s --timeout=15s --start-period=30s --retries=5 \
  CMD ["/bin/sh", "-ec", "role=${EA_ROLE:-api}; case \"$role\" in worker|scheduler) exec python -m app.scheduler_healthcheck ;; openvoice) port=${OPENVOICE_PORT:-8093}; curl -fsS --connect-timeout 2 --max-time 10 http://127.0.0.1:${port}/health/live >/dev/null ;; *) curl -fsS --connect-timeout 2 --max-time 10 http://127.0.0.1:8090/health/live >/dev/null ;; esac"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.runner"]
