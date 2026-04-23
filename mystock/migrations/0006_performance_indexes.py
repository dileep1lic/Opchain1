# Generated manually — Performance optimization migration
# Adds missing indexes on PaperTrade and fixes OptionChain duplicate Meta

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mystock', '0005_botsettings'),
    ]

    operations = [
        # PaperTrade — trade_date और result पर index (admin_status_api query fast होगी)
        migrations.AddIndex(
            model_name='papertrade',
            index=models.Index(fields=['trade_date', 'result'], name='pt_date_result_idx'),
        ),
        migrations.AddIndex(
            model_name='papertrade',
            index=models.Index(fields=['symbol', 'result'], name='pt_symbol_result_idx'),
        ),
    ]
