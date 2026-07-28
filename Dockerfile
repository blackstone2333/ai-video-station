FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system sixv \
    && useradd --system --gid sixv --home-dir /app sixv \
    && mkdir -p /data \
    && chown -R sixv:sixv /app /data

COPY --chown=sixv:sixv sixv ./sixv
COPY --chown=sixv:sixv static ./static
COPY --chown=sixv:sixv main.py ./main.py

USER sixv
EXPOSE 16666

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:16666/health', timeout=3)" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:16666", "--workers", "1", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "sixv.app:create_app()"]
