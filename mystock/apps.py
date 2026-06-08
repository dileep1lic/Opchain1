"""
apps.py — Background fetcher हटाया (Render पर run_sync_async अलग process में चलता है)
"""
from django.apps import AppConfig


class MyStockConfig(AppConfig):
    name               = "mystock"
    default_auto_field = "django.db.models.BigAutoField"
