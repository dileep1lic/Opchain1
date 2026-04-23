#!/usr/bin/env bash
# Render Build Script
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

# Note: makemigrations यहाँ नहीं — migrations हमेशा local पर बनाएं और commit करें
python manage.py migrate --no-input
