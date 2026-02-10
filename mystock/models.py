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

    class Meta:
        ordering = ['-Time']

    def __str__(self):
        return f"{self.Symbol} | {self.Strike_Price} | {self.Time}"


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
            models.Index(fields=['Symbol', '-Time']),
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