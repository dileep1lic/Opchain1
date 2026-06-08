from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # as_websocket() की जगह as_asgi() का इस्तेमाल करें
    re_path(r'ws/options/$', consumers.OptionChainConsumer.as_asgi()),
]