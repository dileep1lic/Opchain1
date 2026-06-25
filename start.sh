#!/usr/bin/env bash

# Exit on error
set -o errexit

echo "Starting background sync engine..."
python manage.py run_sync_async &

echo "Starting Daphne ASGI server on port $PORT..."
exec daphne myproject.asgi:application --port $PORT --bind 0.0.0.0