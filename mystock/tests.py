from django.test import TestCase

# import pandas as pd
# import ast

# # 1. आपकी अपलोड की गई फाइल को पढ़ें
# df = pd.read_csv('option_chain_data.csv')

# # 2. 'data' कॉलम के अंदर छुपे हुए टेक्स्ट (स्ट्रिंग) को असली डिक्शनरी में बदलें
# parsed_data = []
# for val in df['data'].dropna():
#     try:
#         # ast.literal_eval सुरक्षित रूप से स्ट्रिंग को डिक्शनरी में बदल देता है
#         parsed_data.append(ast.literal_eval(val))
#     except Exception as e:
#         print(f"Error parsing row: {e}")

# # 3. नेस्टेड डेटा (JSON) को एकदम फ्लैट (अलग-अलग कॉलम्स) DataFrame में बदलें
# flattened_df = pd.json_normalize(parsed_data)

# # 4. नए और साफ़ डेटा को नई CSV फाइल में सेव कर दें
# flattened_df.to_csv('clean_option_chain.csv', index=False)

# print(f"✅ डेटा सफलतापूर्वक साफ हो गया है!")
# print(f"कुल Rows: {len(flattened_df)} | कुल Columns: {len(flattened_df.columns)}")
import os
import sys
import django

# प्रोजेक्ट के रूट फोल्डर को सिस्टम पाथ में जोड़ें
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

# Django की सेटिंग्स लोड करें (यहाँ 'myproject.settings' आपके प्रोजेक्ट के सेटिंग्स का नाम है)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings') 
django.setup()

# ----------------- अपडेटेड LTP फिक्स स्क्रिप्ट -----------------
from mystock.models import PaperTrade, OptionChain
from django.db import transaction

def fix_ltp_expiry_issue():
    trades = PaperTrade.objects.filter(entry_strike__isnull=False)
    count = 0

    print("🚀 LTP सही एक्सपायरी के साथ अपडेट हो रहा है... कृपया प्रतीक्षा करें।")

    with transaction.atomic():
        for t in trades:
            updated = False
            
            # 1. Entry LTP (यहाँ 'Expiry_Date' से सॉर्ट कर रहे हैं)
            if t.entry_time:
                entry_oc = OptionChain.objects.filter(
                    Symbol=t.symbol,
                    Strike_Price=t.entry_strike,
                    Time__date=t.trade_date,
                    Time__lte=t.entry_time
                ).order_by('-Time', 'Expiry_Date').first() 
                
                if entry_oc:
                    t.entry_ltp = entry_oc.CE_LTP if t.trade_type == 'CALL' else entry_oc.PE_LTP
                    if not t.lot_size:
                        t.lot_size = entry_oc.Lot_size
                    updated = True

            # 2. Exit LTP (यहाँ भी 'Expiry_Date' जोड़ा गया है)
            if t.exit_time:
                exit_oc = OptionChain.objects.filter(
                    Symbol=t.symbol,
                    Strike_Price=t.entry_strike,
                    Time__date=t.trade_date,
                    Time__lte=t.exit_time
                ).order_by('-Time', 'Expiry_Date').first()
                
                if exit_oc:
                    t.exit_ltp = exit_oc.CE_LTP if t.trade_type == 'CALL' else exit_oc.PE_LTP
                    updated = True

            # 3. सही PnL कैलकुलेट करना
            if getattr(t, 'entry_ltp', None) is not None and getattr(t, 'exit_ltp', None) is not None:
                new_pnl = round(t.exit_ltp - t.entry_ltp, 2)
                t.pnl = new_pnl
                
                if t.lot_size:
                    t.pnl_rupees = round(new_pnl * t.lot_size, 2)
                
                updated = True

            if updated:
                t.save()
                count += 1
                print(f"✅ Trade {t.id} Fixed -> Entry: {t.entry_ltp} | Exit: {t.exit_ltp} | PnL(₹): {t.pnl_rupees}")

    print(f"🎉 कुल {count} ट्रेड्स को सही एक्सपायरी के साथ ठीक कर दिया गया है!")

# स्क्रिप्ट को चलाएं
fix_ltp_expiry_issue()