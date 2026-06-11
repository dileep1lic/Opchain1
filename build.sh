#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

# makemigrations यहाँ नहीं — local पर करें और commit करें
python manage.py migrate --no-input

# 🟢 नया कोड: डेटाबेस कैश टेबल बनाने के लिए 
python manage.py createcachetable

# SyncControl records ensure करो (loop toggles के लिए)
python manage.py shell -c "
from mystock.models import SyncControl
for name in ['nifty_loop','others_loop','bot_loop']:
    c,_ = SyncControl.objects.get_or_create(name=name, defaults={'is_active':True})
print('SyncControl records ready.')
"
# pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate