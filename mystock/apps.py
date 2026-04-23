"""
apps.py
━━━━━━━
StatReloader पर ready() दो बार call होती है।
_launched flag से double-start रुकता है।
"""

import sys
from django.apps import AppConfig


class MyStockConfig(AppConfig):
    name               = "mystock"
    default_auto_field = "django.db.models.BigAutoField"

    # Class-level flag — दोनों ready() calls में share होगा
    _launched = False

    def ready(self):
        skip_commands = {"migrate", "makemigrations", "collectstatic",
                         "test", "shell", "createsuperuser", "check"}
        if any(cmd in sys.argv for cmd in skip_commands):
            return

        # FIX: StatReloader पर ready() दो बार call होती है।
        # RUN_MAIN env var से पता चलता है कि यह असली child process है।
        import os
        if os.environ.get("RUN_MAIN") != "true":
            # यह reloader का parent process है — skip करो
            return

        if MyStockConfig._launched:
            return

        MyStockConfig._launched = True
        print("[Apps] ready() — fetcher launch हो रहा है...")
        self._launch_fetcher()

    @staticmethod
    def _launch_fetcher():
        try:
            from .background_fetcher import start_background_fetcher
            start_background_fetcher()
        except Exception as e:
            import traceback
            print(f"[Apps] ❌ ERROR: {type(e).__name__}: {e}")
            traceback.print_exc()
