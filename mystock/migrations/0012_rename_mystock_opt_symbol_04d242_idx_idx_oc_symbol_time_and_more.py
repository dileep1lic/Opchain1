# Generated migration for performance optimization
# Adds strategic indexes for query optimization

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mystock', '0011_alter_tradingjournal_resistance_status_and_more'),
    ]

    operations = [
        # ⚡ OPTIMIZATION: OptionChain - Add symbol+strike index for faster lookups
        migrations.AddIndex(
            model_name='optionchain',
            index=models.Index(
                fields=['Symbol', 'Strike_Price'],
                name='idx_oc_symbol_strike',
            ),
        ),
        
        # ⚡ OPTIMIZATION: LiveSRData - Add ascending time index for efficient ordering
        migrations.AddIndex(
            model_name='livesrdata',
            index=models.Index(
                fields=['Symbol', 'Time'],
                name='idx_sr_symbol_time_asc',
            ),
        ),
        
        # ⚡ OPTIMIZATION: PaperTrade - Add comprehensive indexes
        migrations.AddIndex(
            model_name='papertrade',
            index=models.Index(
                fields=['symbol', 'trade_date', 'trade_type', 'result'],
                name='idx_pt_type_result',
            ),
        ),
    ]
