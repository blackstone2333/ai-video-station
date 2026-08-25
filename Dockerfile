FROM python:3.11.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system ainas \
    && useradd --system --gid ainas --home-dir /app ainas \
    && mkdir -p /data \
    && chown -R ainas:ainas /app /data

COPY --chown=ainas:ainas ainas ./ainas
COPY --chown=ainas:ainas static ./static
COPY --chown=ainas:ainas main.py ./main.py

USER ainas
EXPOSE 16666

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:16666/health', timeout=3)" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:16666", "--workers", "1", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "ainas.app:create_app()"]
