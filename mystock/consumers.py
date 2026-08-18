# consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer

class OptionChainConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group_name = "live_options_group"
        # क्लाइंट को ग्रुप में जोड़ें
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        # क्लाइंट को ग्रुप से हटाएँ
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    # यह फंक्शन तब कॉल होगा जब run_sync_async सिग्नल भेजेगा
    async def send_data_update(self, event):
        # फ्रंटएंड को JSON मेसेज भेजें
        await self.send(text_data=json.dumps({
            "symbol":     event.get("symbol"),
            "spot_price": event.get("spot_price"),
            "data_time":  event.get("data_time"),
            "message":    event.get("message", "UPDATE_NOW"),
            "r_strike":   event.get("r_strike"),   # 🔴 R Strike (None या float)
            "s_strike":   event.get("s_strike"),   # 🔴 S Strike (None या float)
        }))