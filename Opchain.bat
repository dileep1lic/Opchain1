@echo off
title 🟦 Upstox Auto Runner

echo ============================================
echo   🚀 Starting Upstox Update Environment
echo ============================================
echo.

:: डायरेक्टरी बदलें (Change Directory)
cd /d "C:\Users\dilee\Desktop\Opchain"

:: Virtual Environment को एक्टिवेट करें
call myenv\Scripts\activate.bat
echo ✅ Virtual environment activated.

:: Django सर्वर को एक नई विंडो में स्टार्ट करें
start "Django Server" cmd /k "python manage.py runserver"

:: Sync/Async कमांड को दूसरी नई विंडो में स्टार्ट करें
start "Upstox Sync" cmd /k "python manage.py run_sync_async"

echo --------------------------------------------
echo 🟡 Both processes are running in separate windows.
echo 🟡 To stop: Close the respective command prompt windows.
echo --------------------------------------------
pause