#!/usr/bin/env bash
set -e

echo "[VYUHA-ENTRYPOINT] Waiting for PostgreSQL database connection..."
# Parse host and port from DATABASE_URL or default to db:5432
until pg_isready -h db -p 5432 -U vyuha; do
  echo "[VYUHA-ENTRYPOINT] Database unreachable. Waiting 2 seconds..."
  sleep 2
done

echo "[VYUHA-ENTRYPOINT] Database connection established."

echo "[VYUHA-ENTRYPOINT] Executing Alembic schema migrations..."
alembic upgrade head

echo "[VYUHA-ENTRYPOINT] Booting FastAPI ASGI server via Uvicorn..."
exec uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 2 --loop uvloop
