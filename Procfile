web: gunicorn myproject.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --preload --log-file -
worker: python manage.py run_sync_async
