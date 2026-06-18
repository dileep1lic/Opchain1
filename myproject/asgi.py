"""
ASGI config for myproject project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""



import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import mystock.routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')

# ✅ Thread pool size बढ़ाएं — sync views के लिए
# Default = CPU cores. Polling APIs के लिए ज़्यादा threads चाहिए
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")  # development only

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(mystock.routing.websocket_urlpatterns)
    ),
})