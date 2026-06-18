start_sh_content = """#!/usr/bin/env bash

# किसी भी एरर पर स्क्रिप्ट को रोकने के लिए (फोरग्राउंड कमांड्स के लिए)
set -o errexit

echo "🔄 बैकग्राउंड में लाइव डेटा सिंक इंजन (run_sync_async) शुरू कर रहे हैं..."
python manage.py run_sync_async &

echo "🚀 Daphne ASGI सर्वर को पोर्ट $PORT पर चालू कर रहे हैं..."
# exec का उपयोग करने से Daphne मुख्य प्रोसेस बन जाता है, जिससे सर्वर स्टेबल रहता है
exec daphne myproject.asgi:application --port $PORT --bind 0.0.0.0
"""

with open("start.sh", "w", encoding="utf-8") as f:
    f.write(start_sh_content)

print("File start.sh successfully created.")