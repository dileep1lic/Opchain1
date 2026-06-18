web: daphne -b 0.0.0.0 -p $PORT --access-log - --proxy-headers myproject.asgi:application
worker: python manage.py run_sync_async
