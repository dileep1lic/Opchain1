"""
ASGI config for myproject project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

# import os

# from django.core.asgi import get_asgi_application

# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')

# application = get_asgi_application()

import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import mystock.routing

# ध्यान दें: 'myproject' की जगह अपने मेन प्रोजेक्ट फोल्डर का नाम लिखें 
# (शायद आपके प्रोजेक्ट का नाम 'Opchain' या 'myproject' है)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')

application = ProtocolTypeRouter({
    # HTTP रिक्वेस्ट्स को नार्मल Django हैंडल करेगा
    "http": get_asgi_application(),
    
    # WebSocket रिक्वेस्ट्स को Channels हैंडल करेगा
    "websocket": AuthMiddlewareStack(
        URLRouter(
            mystock.routing.websocket_urlpatterns
        )
    ),
})
