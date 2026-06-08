#!/bin/bash

echo "============================================"
echo "   🚀 Starting Upstox Update Environment"
echo "============================================"
echo ""

# आपका असली प्रोजेक्ट पाथ
PROJECT_DIR="/media/dileep/SSD2/Optchain"

# पहली नई टर्मिनल विंडो में Django सर्वर स्टार्ट करें
gnome-terminal --title="Django Server" -- bash -c "cd $PROJECT_DIR && source myvenv/bin/activate && python3 manage.py runserver; exec bash"

# दूसरी नई टर्मिनल विंडो में Sync/Async इंजन स्टार्ट करें
gnome-terminal --title="Upstox Sync" -- bash -c "cd $PROJECT_DIR && source myvenv/bin/activate && python3 manage.py run_sync_async; exec bash"

echo "--------------------------------------------"
echo "🟡 Both processes are running in separate terminal windows."
echo "🟡 To stop: Close the respective terminal windows."
echo "--------------------------------------------"