FROM python:3.13-slim

# Accept Coolify build args safely
ARG BUILD_DATE
ARG GIT_COMMIT
ARG SECRET_KEY
ARG ENCRYPTION_KEY
ARG EMAIL_BACKEND
ARG ALLOWED_HOSTS
ARG DEFAULT_FROM_EMAIL
ARG RESEND_API_KEY
ARG USE_RESEND
ARG Q_WORKERS
ARG GUNICORN_WORKERS
ARG GUNICORN_THREADS
ARG GUNICORN_TIMEOUT
ARG GATEWAY_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=pyrunner.settings \
    PORT=8000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p \
    /app/data/environments \
    /app/data/workdir \
    /opt/elitex/storage/jobs \
    /app/staticfiles

# Temporary dummy keys for collectstatic only
ENV SECRET_KEY="build-temp-key" \
    ENCRYPTION_KEY="build-temp-key"

RUN python manage.py collectstatic --noinput

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --create-home appuser \
    && chown -R appuser:appuser /app \
    && chown -R appuser:appuser /opt/elitex/storage

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=40s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')"

ENTRYPOINT ["/entrypoint.sh"]
