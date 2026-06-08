from django.db import models
from django.utils import timezone

class OptionChain(models.Model):
    # In par Index zaroori hai kyunki hum inpar Filter lagayenge
    Time = models.DateTimeField(db_index=True)
    Expiry_Date = models.DateField(db_index=True, null=True, blank=True)
    Symbol = models.CharField(max_length=50, db_index=True)
    Lot_size = models.IntegerField(default=1)
    Strike_Price = models.FloatField(db_index=True)

    # In par Index ki zaroorat nahi hai (Data sirf display ke liye hai)
    CE_Delta = models.FloatField(null=True, blank=True)
    CE_RANGE = models.FloatField(null=True, blank=True)
    CE_IV = models.FloatField(null=True, blank=True)
    CE_COI_percent = models.FloatField(null=True, blank=True)
    CE_COI = models.FloatField(null=True, blank=True)
    CE_OI_percent = models.FloatField(null=True, blank=True)
    CE_OI = models.FloatField(null=True, blank=True)
    CE_Volume_percent = models.FloatField(null=True, blank=True)
    CE_Volume = models.FloatField(null=True, blank=True)
    CE_CLTP = models.FloatField(null=True, blank=True)
    CE_LTP = models.FloatField(null=True, blank=True)
    Reversl_Ce = models.FloatField(null=True, blank=True)

    Reversl_Pe = models.FloatField(null=True, blank=True)
    PE_LTP = models.FloatField(null=True, blank=True)
    PE_CLTP = models.FloatField(null=True, blank=True)
    PE_Volume = models.FloatField(null=True, blank=True)
    PE_Volume_percent = models.FloatField(null=True, blank=True)
    PE_OI = models.FloatField(null=True, blank=True)
    PE_OI_percent = models.FloatField(null=True, blank=True)
    PE_COI = models.FloatField(null=True, blank=True)
    PE_COI_percent = models.FloatField(null=True, blank=True)
    PE_IV = models.FloatField(null=True, blank=True)
    PE_RANGE = models.FloatField(null=True, blank=True)
    PE_Delta = models.FloatField(null=True, blank=True)
    
    Spot_Price = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"{self.Symbol} | {self.Strike_Price} | {self.Time}"

    class Meta:
        ordering = ['-Time']
        indexes = [
            models.Index(fields=['Symbol', 'Time'], name='idx_oc_symbol_time'),
            models.Index(fields=['Symbol', 'Strike_Price', 'Time'], name='idx_oc_symbol_strike_time'),
            # ⚡ OPTIMIZATION: For bulk queries - use Time field for date-based filtering
            models.Index(fields=['Symbol', 'Strike_Price'], name='idx_oc_symbol_strike'),
        ]


class SupportResistance(models.Model):
    # auto_now_add=True की जगह इसे सामान्य DateTimeField रखना बेहतर है 
    # ताकि आप मैन्युअल रूप से मार्केट का टाइम डाल सकें जैसा आपने async_live.py में किया है।
    Time = models.DateTimeField(null=True, blank=True, db_index=True) 
    Symbol = models.CharField(max_length=50, db_index=True)
    Spot_Price = models.FloatField(null=True, blank=True)
    Expiry_Date = models.DateField(null=True, blank=True)

    # CE Resistance (Top 2)
    Strike_Price_Ce1 = models.FloatField(null=True, blank=True)
    Reversl_Ce = models.FloatField(null=True, blank=True)
    week_Ce_1 = models.FloatField(null=True, blank=True)
    Stop_Loss_Ce1 = models.FloatField(null=True, blank=True)

    Strike_Price_Ce2 = models.FloatField(null=True, blank=True)
    Reversl_Ce_2 = models.FloatField(null=True, blank=True)
    week_Ce_2 = models.FloatField(null=True, blank=True)
    Stop_Loss_Ce2 = models.FloatField(null=True, blank=True)
    s_t_b_ce = models.CharField(max_length=20, null=True, blank=True) # TextField की जगह CharField भी चलेगा

    # PE Support (Top 2)
    Strike_Price_Pe1 = models.FloatField(null=True, blank=True)
    Reversl_Pe = models.FloatField(null=True, blank=True)
    week_Pe_1 = models.FloatField(null=True, blank=True)
    Stop_Loss_Pe1 = models.FloatField(null=True, blank=True)

    Strike_Price_Pe2 = models.FloatField(null=True, blank=True)
    Reversl_Pe_2 = models.FloatField(null=True, blank=True)
    week_Pe_2 = models.FloatField(null=True, blank=True)
    Stop_Loss_Pe2 = models.FloatField(null=True, blank=True)
    s_t_b_pe = models.CharField(max_length=20, null=True, blank=True)
    
    Bearish_Risk = models.IntegerField(default=0)
    Bullish_Risk = models.IntegerField(default=0)

    # --- New 4 Distance Columns ---
    dist_ce_1 = models.FloatField(default=0.0, verbose_name="Dist CE1 %")
    dist_ce_2 = models.FloatField(default=0.0, verbose_name="Dist CE2 %")
    
    dist_pe_1 = models.FloatField(default=0.0, verbose_name="Dist PE1 %")
    dist_pe_2 = models.FloatField(default=0.0, verbose_name="Dist PE2 %")


    class Meta:
        db_table = "Support_Resistance"
        indexes = [
            models.Index(fields=['Symbol', '-Time'], name='idx_sr_symbol_time'),
            # ⚡ OPTIMIZATION: For daily queries - use Time field directly
            models.Index(fields=['Symbol', 'Time'], name='idx_sr_symbol_time_asc'),
        ]

    def __str__(self):
        return f"{self.Symbol} | {self.Time}"
    
class SyncControl(models.Model):
    name = models.CharField(max_length=50, unique=True) # "nifty_loop" या "others_loop"
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} - {'Running' if self.is_active else 'Stopped'}"
    
class ExpiryCache(models.Model):
    # Symbol: NIFTY, BANKNIFTY, RELIANCE, etc.
    symbol = models.CharField(max_length=50, unique=True, db_index=True)
    
    # Expiries: पूरी लिस्ट यहाँ सेव होगी (जैसे ['2024-02-15', '2024-02-22'])
    # Django का JSONField लिस्ट को अपने आप हैंडल कर लेता है
    expiries = models.JSONField(default=list)
    
    # Last Updated: कब डेटा अपडेट हुआ (ताकि हम पुराना डेटा चेक कर सकें)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.symbol} - {self.last_updated.date()}"

    # एक हेल्पर फंक्शन जो बताएगा कि डेटा ताज़ा है या नहीं
    def is_data_fresh(self):
        # अगर डेटा आज का है, तो True रिटर्न करेगा
        return self.last_updated.date() == timezone.now().date()
    
# models.py में जोड़ें

class TempOptionChain(models.Model):
    # यह टेबल केवल सर्च किए गए स्टॉक का डेटा रखेगी
    Time = models.DateTimeField(db_index=True)
    Expiry_Date = models.DateField(db_index=True, null=True, blank=True)
    Symbol = models.CharField(max_length=50, db_index=True)
    Lot_size = models.IntegerField(default=1)
    Strike_Price = models.FloatField(db_index=True)

    # बाकी सारे कॉलम्स OptionChain जैसे ही रहेंगे
    CE_Delta = models.FloatField(null=True, blank=True)
    CE_RANGE = models.FloatField(null=True, blank=True)
    CE_IV = models.FloatField(null=True, blank=True)
    CE_COI_percent = models.FloatField(null=True, blank=True)
    CE_COI = models.FloatField(null=True, blank=True)
    CE_OI_percent = models.FloatField(null=True, blank=True)
    CE_OI = models.FloatField(null=True, blank=True)
    CE_Volume_percent = models.FloatField(null=True, blank=True)
    CE_Volume = models.FloatField(null=True, blank=True)
    CE_CLTP = models.FloatField(null=True, blank=True)
    CE_LTP = models.FloatField(null=True, blank=True)
    Reversl_Ce = models.FloatField(null=True, blank=True)

    Reversl_Pe = models.FloatField(null=True, blank=True)
    PE_LTP = models.FloatField(null=True, blank=True)
    PE_CLTP = models.FloatField(null=True, blank=True)
    PE_Volume = models.FloatField(null=True, blank=True)
    PE_Volume_percent = models.FloatField(null=True, blank=True)
    PE_OI = models.FloatField(null=True, blank=True)
    PE_OI_percent = models.FloatField(null=True, blank=True)
    PE_COI = models.FloatField(null=True, blank=True)
    PE_COI_percent = models.FloatField(null=True, blank=True)
    PE_IV = models.FloatField(null=True, blank=True)
    PE_RANGE = models.FloatField(null=True, blank=True)
    PE_Delta = models.FloatField(null=True, blank=True)
    
    Spot_Price = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['Strike_Price'] # स्ट्राइक प्राइस के हिसाब से सॉर्टेड

class InstrumentStore(models.Model):
    symbol = models.CharField(max_length=50, unique=True, db_index=True)
    instrument_key = models.CharField(max_length=100)
    lot_size = models.IntegerField(default=1)
    # हम यहाँ expiry_date को JSONField या String की तरह रख सकते हैं 
    # क्योंकि एक सिंबल की कई एक्सपायरी होती हैं
    expiry_dates = models.JSONField(default=list, blank=True) 
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.symbol} ({self.last_updated.date()})"



class LiveSRData(models.Model):
    Time = models.DateTimeField()
    Symbol = models.CharField(max_length=50)
    Expiry_Date = models.CharField(max_length=20, null=True, blank=True)
    Spot_Price = models.FloatField(default=0.0)

    # =======================================
    # CE (CALL) - RESISTANCE DATA
    # =======================================
    # CE OI
    ce_high_oi_strike = models.FloatField(null=True, blank=True)
    ce_oi_status = models.CharField(max_length=20, null=True, blank=True) # WTT/WTB/STRONG
    ce_2nd_high_oi_strike = models.FloatField(null=True, blank=True)
    
    # CE Volume
    ce_high_vol_strike = models.FloatField(null=True, blank=True)
    ce_vol_status = models.CharField(max_length=20, null=True, blank=True) # WTT/WTB/STRONG
    ce_2nd_high_vol_strike = models.FloatField(null=True, blank=True)
    resistance_strike = models.FloatField(null=True, blank=True)
    resistance_status = models.CharField(max_length=100, null=True, blank=True)

    # =======================================
    # PE (PUT) - SUPPORT DATA
    # =======================================
    # PE OI
    pe_high_oi_strike = models.FloatField(null=True, blank=True)
    pe_oi_status = models.CharField(max_length=20, null=True, blank=True) # WTT/WTB/STRONG
    pe_2nd_high_oi_strike = models.FloatField(null=True, blank=True)
    
    # PE Volume
    pe_high_vol_strike = models.FloatField(null=True, blank=True)
    pe_vol_status = models.CharField(max_length=20, null=True, blank=True) # WTT/WTB/STRONG
    pe_2nd_high_vol_strike = models.FloatField(null=True, blank=True)
    supprt_strike = models.FloatField(null=True, blank=True)
    supprt_status = models.CharField(max_length=100, null=True, blank=True)
    

    def __str__(self):
        return f"{self.Symbol} - {self.Time.strftime('%H:%M:%S')}"
    

    
class PaperTrade(models.Model):
    symbol = models.CharField(max_length=20, db_index=True)
    trade_date = models.DateField(default=timezone.now, db_index=True)
    trade_type = models.CharField(max_length=10)  # 'CALL' या 'PUT'

    # ── Entry Details ──
    entry_time = models.DateTimeField(default=timezone.now)
    entry_spot = models.FloatField()
    trigger_level = models.CharField(max_length=10)  # 'R' या 'S'
    trigger_price = models.FloatField()

    # ── Exit Details ──
    exit_time = models.DateTimeField(null=True, blank=True)
    exit_spot = models.FloatField(null=True, blank=True)
    result = models.CharField(max_length=20, default="OPEN", db_index=True)  # OPEN, TARGET, SL
    pnl = models.FloatField(default=0.0)
    entry_strike = models.FloatField(null=True, blank=True)
    is_replay = models.BooleanField(default=False)
    entry_ltp = models.FloatField(null=True, blank=True)
    exit_ltp = models.FloatField(null=True, blank=True)
    lot_size = models.IntegerField(null=True, blank=True)
    pnl_rupees = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            # ⚡ OPTIMIZATION: Frequently used filters in admin_status_api & get_master_levels
            models.Index(fields=['symbol', 'trade_date', 'result'], name='idx_pt_core'),
            models.Index(fields=['symbol', 'trade_date', 'trade_type', 'result'], name='idx_pt_type_result'),
            models.Index(fields=['trade_date', 'result'], name='idx_pt_date_result'),
            models.Index(fields=['symbol', 'result'], name='idx_pt_symbol_result'),
        ]

    def __str__(self):
        return f"{self.symbol} | {self.trade_type} | {self.result} | PNL: {self.pnl}"


class BotSettings(models.Model):
    trading_enabled = models.BooleanField(default=True)
    default_target = models.FloatField(default=50.0)
    default_sl = models.FloatField(default=50.0)
    reversal_buffer = models.FloatField(default=5.0)
    user_name = models.CharField(max_length=50, default="सर")

    class Meta:
        verbose_name = "Bot Setting"
        verbose_name_plural = "Bot Settings"

# Trade jnournal के लिए Enums (Choices) - models.py में जोड़ें
class TradeStatus(models.TextChoices):
    WTT         = 'wtt',         'WTT'
    WTB         = 'wtb',         'WTB'
    STRONG      = 'strong',      'STRONG'
    SHIFTED_WTB = 'shifted_wtb', 'SHIFTED WTB'
    SHIFTED_WTT = 'shifted_wtt', 'SHIFTED WTT'


class TradeType(models.TextChoices):
    CALL = 'call', 'CALL'
    PUT  = 'put',  'PUT'


class TradeLevel(models.TextChoices):
    # ── Support ──────────────────────────────────────────
    SUPPORT_STRIKE           = 'support_strike',           'Support Strike'
    SUPPORT_PLUS_STRIKE      = 'support_plus_strike',      'Support + Strike'
    SUPPORT_MINUS_STRIKE     = 'support_minus_strike',     'Support - Strike'
    SUPPORT2_STRIKE          = 'support2_strike',          '2Support Strike'
    SUPPORT2_PLUS_STRIKE     = 'support2_plus_strike',     '2Support + Strike'
    SUPPORT2_MINUS_STRIKE    = 'support2_minus_strike',    '2Support - Strike'
    # ── Resistance ───────────────────────────────────────
    RESISTANCE_STRIKE        = 'resistance_strike',        'Resistance Strike'
    RESISTANCE_PLUS_STRIKE   = 'resistance_plus_strike',   'Resistance + Strike'
    RESISTANCE_MINUS_STRIKE  = 'resistance_minus_strike',  'Resistance - Strike'
    RESISTANCE2_STRIKE       = 'resistance2_strike',       '2Resistance Strike'
    RESISTANCE2_PLUS_STRIKE  = 'resistance2_plus_strike',  '2Resistance + Strike'
    RESISTANCE2_MINUS_STRIKE = 'resistance2_minus_strike', '2Resistance - Strike'


class TradingJournal(models.Model):
    date = models.DateField(auto_now_add=True)

    # ── Resistance ────────────────────────────────────────────
    resistance_status  = models.CharField(
        max_length=20, choices=TradeStatus.choices,
        verbose_name="Resistance Status",
    )
    resistance_strike  = models.DecimalField(
        max_digits=12, decimal_places=2,
        verbose_name="Resistance Strike",
    )
    resistance_strike2 = models.DecimalField(
        max_digits=12, decimal_places=2,
        null=True, blank=True,
        verbose_name="2nd Resistance Strike",
    )

    # ── Support ───────────────────────────────────────────────
    support_status  = models.CharField(
        max_length=20, choices=TradeStatus.choices,
        verbose_name="Support Status",
    )
    support_strike  = models.DecimalField(
        max_digits=12, decimal_places=2,
        verbose_name="Support Strike",
    )
    support_strike2 = models.DecimalField(
        max_digits=12, decimal_places=2,
        null=True, blank=True,
        verbose_name="2nd Support Strike",
    )

    # ── Trade Info ────────────────────────────────────────────
    trade_type  = models.CharField(
        max_length=10, choices=TradeType.choices,
        verbose_name="Trade Type",
    )
    trade_level = models.CharField(
        max_length=30, choices=TradeLevel.choices,
        verbose_name="Trade Level",
    )

    notes = models.TextField(blank=True, verbose_name="Notes")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering            = ['-created_at']
        verbose_name        = "Trading Journal"
        verbose_name_plural = "Trading Journals"

    def __str__(self):
        return f"{self.date} | {self.trade_type.upper()} | {self.get_trade_level_display()}"


class VoiceCommand(models.Model):
    order      = models.PositiveIntegerField(default=0, db_index=True)   # क्रम नंबर
    text       = models.TextField(verbose_name="कमांड टेक्स्ट")          # बोला जाने वाला टेक्स्ट
    is_active  = models.BooleanField(default=True)                        # चालू/बंद
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering            = ['order']
        verbose_name        = "Voice Command"
        verbose_name_plural = "Voice Commands"

    def __str__(self):
        return f"#{self.order} — {self.text[:60]}"