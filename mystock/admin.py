from django.contrib import admin
from .models import OptionChain, SupportResistance, SyncControl, TempOptionChain, InstrumentStore

# 1. OptionChain Model
@admin.register(OptionChain)
class OptionChainAdmin(admin.ModelAdmin):
    # ये वो कॉलम्स हैं जो लिस्ट में बाहर दिखेंगे
    list_display = ('Symbol', 'Strike_Price', 'Time', 'CE_OI', 'PE_OI', 'CE_OI_percent')
    
    # साइड में फ़िल्टर लगाने के लिए
    list_filter = ('Symbol', 'Time')
    
    # सर्च बार (Symbol या Strike Price से ढूँढने के लिए)
    search_fields = ('Symbol', 'Strike_Price')

# 2. SupportResistance Model (इसे भी रजिस्टर कर लें)
@admin.register(SupportResistance)
class SupportResistanceAdmin(admin.ModelAdmin):
    list_display = ('Symbol', 'Time', 'Spot_Price', 'Bearish_Risk', 'Bullish_Risk')
    list_filter = ('Symbol', 'Time')

# 3. Control Loops (Optional)
@admin.register(SyncControl)
class SyncControlAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')

# 4. Temp Data (Optional)
@admin.register(TempOptionChain)
class TempOptionChainAdmin(admin.ModelAdmin):
    list_display = ('Symbol', 'Strike_Price', 'Time')

# 5. Instrument Store (Optional)
@admin.register(InstrumentStore)
class InstrumentStoreAdmin(admin.ModelAdmin):
    # यहाँ हमने बिल्कुल वही नाम लिखे हैं जो models.py में हैं
    list_display = ('symbol', 'instrument_key', 'lot_size', 'expiry_dates', 'last_updated')
    
    # आप चाहें तो इसमें भी सर्च और फ़िल्टर लगा सकते हैं
    list_filter = ('symbol',)
    search_fields = ('symbol', 'instrument_key')