#!/bin/bash
set -e

echo "=========================================="
echo "  PyRunner - Starting up..."
echo "=========================================="

PORT="${PORT:-8000}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-4}"
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
Q_WORKERS="${Q_WORKERS:-2}"

# Validate required environment variables
if [ -z "$SECRET_KEY" ]; then
    echo ""
    echo "ERROR: SECRET_KEY is required but not set."
    exit 1
fi

if [ -z "$ENCRYPTION_KEY" ]; then
    echo ""
    echo "ERROR: ENCRYPTION_KEY is required but not set."
    exit 1
fi

# Ensure storage directory exists (important for bind mount)
mkdir -p /opt/elitex/storage/jobs

echo "[*] Applying database migrations..."
python manage.py migrate --noinput

echo "[*] Starting services..."

# Start django-q cluster (if enabled)
if [ "$Q_WORKERS" -gt 0 ]; then
    echo "    - Starting django-q worker..."
    python manage.py qcluster &
    QCLUSTER_PID=$!
fi

# Graceful shutdown handler
cleanup() {
    echo ""
    echo "[*] Shutting down..."

    if [ -n "$QCLUSTER_PID" ]; then
        echo "    - Stopping worker..."
        kill -TERM "$QCLUSTER_PID" 2>/dev/null || true
        wait "$QCLUSTER_PID" 2>/dev/null || true
    fi

    exit 0
}

trap cleanup SIGTERM SIGINT

echo "    - Starting web server on port ${PORT}..."
echo ""
echo "=========================================="
echo "  PyRunner is ready!"
echo "=========================================="
echo ""

exec gunicorn pyrunner.wsgi:application \
    --bind 0.0.0.0:${PORT} \
    --workers ${GUNICORN_WORKERS} \
    --threads ${GUNICORN_THREADS} \
    --timeout ${GUNICORN_TIMEOUT} \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile -
