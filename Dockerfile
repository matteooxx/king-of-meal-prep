# Multi-stage so the runtime image doesn't carry build-essential / libxml2-dev
# (~250 MB worth of compiler toolchain that's only needed during pip install
# of lxml).

# ---- builder ----
FROM python:3.12-slim AS builder

RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      build-essential libxml2-dev libxslt1-dev \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
# Install into a venv-shaped prefix that we'll copy into the runtime image.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---- runtime ----
FROM python:3.12-slim

# tesseract for receipt OCR; ca-certificates for httpx TLS.
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-ita \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pull in the python packages built in the builder stage.
COPY --from=builder /install /usr/local
RUN python -m pip uninstall --yes pip setuptools wheel

RUN useradd -r -u 1000 -m -d /home/king king
RUN install -d -m 0700 -o king -g king /data/runtime

WORKDIR /app
COPY --chown=king:king *.py .
COPY --chown=king:king static/ static/
COPY --chown=king:king templates/ templates/
COPY --chown=king:king nutrition/ nutrition/
COPY --chown=king:king recipes/ recipes/
COPY --chown=king:king pantry/ pantry/
COPY --chown=king:king planner/ planner/
COPY --chown=king:king i18n/ i18n/
COPY --chown=king:king barcodes/ barcodes/
COPY --chown=king:king scripts/init-local-env.py scripts/init-local-env.py
COPY --chown=king:king scripts/restore-backup.py scripts/restore-backup.py

ENV APP_ENV_PATH=/data/runtime/app.env \
    ENV_MARKER_PATH=/data/runtime/.env-changed \
    DB_PATH=/data/runtime/data.db \
    DB_BACKUP_DIR=/data/runtime/backups \
    NUTRITION_DB=/data/datasets/nutrition.db \
    HOME=/home/king \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 5002

USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5002/health', timeout=3).read()"]

CMD ["sh", "-c", "umask 077; exec gunicorn --bind 0.0.0.0:5002 --workers 1 --threads 8 --worker-class gthread --timeout 120 --access-logfile - --error-logfile - app:app"]
