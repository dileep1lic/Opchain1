from django.db import migrations


def init_bot_controls(apps, schema_editor):
    SyncControl = apps.get_model('mystock', 'SyncControl')
    for name in ['nifty_bot', 'banknifty_bot', 'finnifty_bot', 'midcpnifty_bot']:
        SyncControl.objects.get_or_create(name=name, defaults={'is_active': True})


class Migration(migrations.Migration):
    dependencies = [
        ('mystock', '0022_init_multisymbol_synccontrol'),
    ]
    operations = [
        migrations.RunPython(init_bot_controls, migrations.RunPython.noop),
    ]
