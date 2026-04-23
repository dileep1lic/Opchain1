"""
WSGI config for myproject.
"""
import os
import signal

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')

# ── FIX: Broken pipe को silently ignore करो ──────────────────────────────────
# जब browser tab close हो या client disconnect करे तो Django error log में
# "Broken pipe" spam होता है। यह SIG_IGN से रुक जाएगा।
try:
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except AttributeError:
    pass  # Windows पर SIGPIPE नहीं होता — ignore

application = get_wsgi_application()
