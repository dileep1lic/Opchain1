"""
Migration 0007 — SyncControl initial records बनाता है।

यह migration ensure करती है कि nifty_loop, others_loop, bot_loop
DB में हमेशा exist करें। पहले toggle_sync में .get() crash देता था
अगर record नहीं था। अब get_or_create fix है — यह migration extra safety है।
"""
from django.db import migrations


def create_sync_controls(apps, schema_editor):
    SyncControl = apps.get_model('mystock', 'SyncControl')
    for name in ['nifty_loop', 'others_loop', 'bot_loop']:
        SyncControl.objects.get_or_create(
            name=name,
            defaults={'is_active': True}
        )


def reverse_sync_controls(apps, schema_editor):
    pass  # rollback पर कुछ नहीं हटाएं


class Migration(migrations.Migration):

    dependencies = [
        ('mystock', '0006_performance_indexes'),
    ]

    operations = [
        migrations.RunPython(create_sync_controls, reverse_sync_controls),
    ]
