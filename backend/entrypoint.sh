#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py createcachetable --no-color 2>/dev/null || true
python manage.py collectstatic --noinput
exec gunicorn --bind 0.0.0.0:8000 --timeout 300 -k uvicorn.workers.UvicornWorker config.asgi:application
