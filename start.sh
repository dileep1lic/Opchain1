#!/bin/bash

# अगर कोई कमांड फेल हो तो तुरंत रुक जाए
set -o errexit

# 1. डेटाबेस माइग्रेशन (Database Update)
# हर डिप्लॉयमेंट पर डेटाबेस को अपडेट रखना अच्छी आदत है
echo "Applying database migrations..."
python manage.py migrate

# 2. बैकग्राउंड वर्कर शुरू करें (Background Worker)
# '&' का मतलब है इसे पीछे (Background) में चलाओ और तुरंत अगली लाइन पर बढ़ जाओ।
# हम यहाँ 'run_sync_async' का उपयोग कर रहे हैं जैसा आपने बताया।
echo "Starting Background Worker (run_sync_async)..."
python manage.py run_sync_async &

# 3. मुख्य वेब सर्वर शुरू करें (Main Web Server)
# यह सबसे अंत में होना चाहिए और इसके पीछे '&' नहीं लगाना है।
# यह Foreground में चलेगा ताकि Render को पता चले कि वेबसाइट लाइव है।
echo "Starting Gunicorn Server..."
gunicorn myproject.wsgi