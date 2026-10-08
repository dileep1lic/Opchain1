"""
Migration 0022: Multi-Symbol SyncControl Entries
BANKNIFTY, FINNIFTY, MIDCPNIFTY के loops DB में initialize करती है।
"""
from django.db import migrations


def add_symbol_loops(apps, schema_editor):
    SyncControl = apps.get_model('mystock', 'SyncControl')
    new_loops = ['banknifty_loop', 'finnifty_loop', 'midcpnifty_loop']
    for name in new_loops:
        SyncControl.objects.get_or_create(
            name=name,
            defaults={'is_active': True}
        )


def remove_symbol_loops(apps, schema_editor):
    SyncControl = apps.get_model('mystock', 'SyncControl')
    SyncControl.objects.filter(
        name__in=['banknifty_loop', 'finnifty_loop', 'midcpnifty_loop']
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('mystock', '0021_login_log'),
    ]

    operations = [
        migrations.RunPython(add_symbol_loops, reverse_code=remove_symbol_loops),
    ]
