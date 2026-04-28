import asyncio
import logging
import time as t_time
import aiohttp
import os
import sys
from datetime import datetime, time as dt_time
from django.core.management.base import BaseCommand
from django.utils import timezone
from asgiref.sync import sync_to_async
from datetime import timedelta
from django.core.cache import cache
from .async_live import (
    save_sr_async_wrapper,
    # get_smart_expiry,
    calculate_data_async_optimized,
    # save_live_sr_data_async_wrapper,
    save_live_sr_async,
    save_temp_async_wrapper,
    update_instrument_store_bulk,
    get_instrument_from_db,
    run_live_paper_trading
)
from .symbol import symbols as all_symbols
from mystock.models import OptionChain, SyncControl, SupportResistance, InstrumentStore, TempOptionChain, LiveSRData
from django.db import close_old_connections

# async wrapper बना लें ताकि लूप ब्लॉक ना हो
set_cache_async = sync_to_async(cache.set)

# Logging setup
log_dir = os.path.join(os.getcwd(), 'logs')
if not os.path.exists(log_dir): os.makedirs(log_dir)
log_file_path = os.path.join(log_dir, "stock_sync.log")

for handler in logging.root.handlers[:]: logging.root.removeHandler(handler)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(log_file_path, encoding='utf-8'), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)



bulk_create_async = sync_to_async(OptionChain.objects.bulk_create)
get_control_async = sync_to_async(SyncControl.objects.get_or_create)

class Command(BaseCommand):
    help = 'High-Speed Async Engine with Smart Expiry'
    
    # FIXED variables हटा दें, अब हम डायनामिक लाएंगे
    FIXED_SYMOL = "NIFTY" 
    # Trading hours: 9:15 AM to 3:30 PM
    is_trading_hours = lambda self: dt_time(9, 15) <= datetime.now().time() <= dt_time(15, 30)
    # Bot trading hours: 9:20 AM to 2:45 PM (थोड़ा कम ताकि पेपर ट्रेडिंग के लिए समय रहे)
    is_trad_hours = lambda self: dt_time(9, 20) <= datetime.now().time() <= dt_time(14, 45)

    def handle(self, *args, **options):
        logger.info('🚀 Starting High-Speed Async Engine...') 
        try:
            asyncio.run(self.main_loop())
        except KeyboardInterrupt:
            logger.warning('Stopped by user.')

    # 1. शुरुआत में एक बार लोड करें
    # load_master_contract()
    async def main_loop(self):
        n_key, n_lot, n_expiries = None, 1, []
        other_symbols = [s for s in all_symbols if s != "NIFTY"]
        
        # --- 1. SMART UPDATE CHECK ---
        today = datetime.now().date()
        store_count = await sync_to_async(InstrumentStore.objects.count)()
        
        if store_count == 0 or datetime.now().weekday() == 2:
            last_entry = await sync_to_async(InstrumentStore.objects.first)()
            if not last_entry or last_entry.last_updated != today:
                logger.info("🔄 Refreshing Instrument Database...")
                await sync_to_async(update_instrument_store_bulk)()

        # --- 2. FETCH FROM DB ---
        n_key, n_lot, n_expiries = await get_instrument_from_db("NIFTY")
        
        if not n_key or not n_expiries:
            logger.error("❌ Critical: NIFTY data missing. Engine stopping.")
            return 


        nifty_expiry = n_expiries[0]

        # बाकी का लूप अब सीधे डेटाबेस (InstrumentStore) से डेटा उठाएगा
        other_symbols = [s for s in all_symbols if s != "NIFTY"]
        if not n_expiries:
            logger.error("❌ NIFTY Expiry not found! Make sure update_instrument_store_bulk is working.")
            return

        nifty_expiry = n_expiries[0]
        
        logger.info('⏳ Fetching Data from InstrumentStore...')
        
        # --- 2. NIFTY Data Fetch ---
        # get_instrument_from_db अब (key, lot, expiry_list) रिटर्न करता है
        n_key, n_lot, n_expiries = await get_instrument_from_db("NIFTY")
        
        if n_expiries and len(n_expiries) > 0:
            nifty_expiry = n_expiries[0] # Current Week Expiry
        else:
            logger.error("❌ NIFTY Expiry not found in Database!")
            # बैकअप के तौर पर पुराना फंक्शन चला सकते हैं अगर DB खाली हो
            return

        # --- 3. STOCKS Expiry Fetch ---
        # हम किसी भी एक स्टॉक (जैसे पहले स्टॉक) की एक्सपायरी लिस्ट उठा लेते हैं
        s_key, s_lot, s_expiries = await get_instrument_from_db(other_symbols[0])
        
        if s_expiries and len(s_expiries) > 0:
            # स्टॉक्स के लिए आमतौर पर मंथली एक्सपायरी [0] पर ही होती है
            common_expiry = s_expiries[0] 
        else:
            logger.error("❌ Stock Expiry not found in Database!")
            return

        logger.info(f"✅ NIFTY Expiry: {nifty_expiry} | Stocks Expiry: {common_expiry}")

        # --- 4. START ASYNC LOOPS ---
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(
                # NIFTY loop: डायनामिक एक्सपायरी के साथ
                self.nifty_loop(session, nifty_expiry, self.FIXED_SYMOL),
                # Others loop: सभी स्टॉक्स और उनकी कॉमन एक्सपायरी के साथ
                self.others_sr_loop(session, other_symbols, common_expiry)
            )

    async def nifty_loop(self, session, expiry, fixes_sym):
        """NIFTY Loop - Optimized Cleanup before Trading Hours"""
        # यह फ्लैग ट्रैक करेगा कि क्या आज की सफाई पूरी हो गई है
        # cleanup_done_today = None

        while True:
            await sync_to_async(close_old_connections)()
            try:
                ctrl, _ = await get_control_async(name="nifty_loop")
            except Exception as e:
                logger.error(f"DB Connection Error, retrying in 10s: {e}")
                await asyncio.sleep(10)
                continue 
    
            ctrl, _ = await get_control_async(name="nifty_loop")
            # current_now = timezone.now()
            # current_date = current_now.date()
            
            

            # 2. 📈 LIVE TRADING LOOP
            if not ctrl.is_active:
                print(f"⏸️  { fixes_sym} Loop Paused.") 
                await asyncio.sleep(10); continue

            if self.is_trading_hours():
           
                try:
                    df = await calculate_data_async_optimized(session, fixes_sym, expiry)
                    if df is not None and not df.empty:
                        # 🟢 पूरे डेटा का Totals कैलकुलेट करें
                        nifty_totals = {
                            'total_ce_oi': float(df['CE_OI'].sum() or 0),
                            'total_pe_oi': float(df['PE_OI'].sum() or 0),
                            'total_ce_coi': float(df['CE_COI'].sum() or 0),
                            'total_pe_coi': float(df['PE_COI'].sum() or 0),
                        }
                        # इसे 60 सेकंड के लिए कैश करें
                        await set_cache_async(f'live_nifty_totals_{fixes_sym}', nifty_totals, 60)
                        # 1. DataFrame को Strike_Price के क्रम में Sort करें
                        df = df.sort_values(by='Strike_Price').reset_index(drop=True)

                        # 2. Spot Price लें (मान कर चल रहे हैं कि एक Expiry/Symbol के लिए यह समान है)
                        spot_price = df['Spot_Price'].iloc[0]

                        # 3. ATM (At-The-Money) Strike का Index पता करें (जो Spot Price के सबसे करीब हो)
                        atm_index = (df['Strike_Price'] - spot_price).abs().idxmin()

                        # 4. 30 छोटी और 30 बड़ी स्ट्राइक की रेंज निकालें
                        # max(0, ...) और min(len(), ...) इसलिए ताकि index out of bounds का error न आए
                        start_index = max(0, atm_index - 30)
                        end_index = min(len(df), atm_index + 31) # +31 इसलिए ताकि ATM भी include रहे

                        # 5. DataFrame को फ़िल्टर करें
                        filtered_df = df.iloc[start_index:end_index]

                        # 🟢 नया कोड: DataFrame को Dictionary में बदलकर Cache में डालें
                        live_data_dict = filtered_df.to_dict('records')
                        # 10 सेकंड के लिए कैश में रखें (हर 5 सेकंड में ये रिफ्रेश हो ही जाएगा)
                        await set_cache_async(f'live_nifty_data_{fixes_sym}', live_data_dict, 60)
                        await set_cache_async(f'live_nifty_spot_{fixes_sym}', spot_price, 60)

                        entries = [OptionChain(
                            Time=row.get('Time'),
                            Symbol=row.get('Symbol'),
                            Lot_size=row.get('Lot_size'),
                            Expiry_Date=expiry,
                            Strike_Price=row.get('Strike_Price'),
                            Spot_Price=row.get('Spot_Price'),
                            # CE Data
                            CE_Delta=row.get('CE_Delta'),
                            CE_RANGE=row.get('CE_RANGE'),
                            CE_IV=row.get('CE_IV'),
                            CE_COI_percent=row.get('CE_COI_percent'),
                            CE_COI=row.get('CE_COI'),
                            CE_OI_percent=row.get('CE_OI_percent'),
                            CE_OI=row.get('CE_OI'),
                            CE_Volume_percent=row.get('CE_Volume_percent'),
                            CE_Volume=row.get('CE_Volume'),
                            CE_CLTP=row.get('CE_CLTP'),
                            CE_LTP=row.get('CE_LTP'),
                            Reversl_Ce=row.get('Reversl_Ce'),

                            # PE Data
                            Reversl_Pe=row.get('Reversl_Pe'),
                            PE_LTP=row.get('PE_LTP'),
                            PE_CLTP=row.get('PE_CLTP'),
                            PE_Volume=row.get('PE_Volume'),
                            PE_Volume_percent=row.get('PE_Volume_percent'),
                            PE_OI=row.get('PE_OI'),
                            PE_OI_percent=row.get('PE_OI_percent'),
                            PE_COI=row.get('PE_COI'),
                            PE_COI_percent=row.get('PE_COI_percent'),
                            PE_IV=row.get('PE_IV'),
                            PE_RANGE=row.get('PE_RANGE'),
                            PE_Delta=row.get('PE_Delta'),
                        ) for _, row in filtered_df.iterrows()]
                        await bulk_create_async(entries)
                        print(f"⚡ [NIFTY] Processed expiry {expiry} - {len(entries)} entries.")
                        
                        # 🆕 NEW: सिर्फ NIFTY के लिए हमारी नई टेबल में डेटा सेव करें
                        await save_live_sr_async(df, fixes_sym)
                        # await sync_to_async(run_live_paper_trading)(df=df, symbol=fixes_sym) # लाइव पेपर ट्रेडिंग भी ट्रिगर करें
                        # bot को चलाने के लिए कंट्रोल चेक करें
                        if self.is_trad_hours():
                            ctrl, _ = await get_control_async(name="bot_loop")
                            if ctrl.is_active:
                                await sync_to_async(run_live_paper_trading)(df=df, symbol=fixes_sym) 
                            else:
                                # बॉट रुका हुआ है, लेकिन डेटाबेस में स्पॉट प्राइस और लेवल्स जा रहे हैं!
                                print("🤖 Bot Loop is Paused. Data will be saved but no trades will be executed.")
                                pass
                        else:
                            print("⏸️ Bot Loop Outside Trading Hours. No trades will be executed, but data will be saved.")
                    else:
                        print(f"⚠️ [NIFTY] No data returned for expiry {expiry}.")
                except Exception as e:
                    logger.error(f"NIFTY Loop Error: {e}")
            else:
                print("⏸️  NIFTY Loop Outside Trading Hours.")
            await asyncio.sleep(5)

    async def others_sr_loop(self, session, symbols, expiry):
        """Modified Loop: Process 10 symbols, wait 2s, repeat."""
        

        async def process_one(sym):
            try:
                df = await calculate_data_async_optimized(session, sym, expiry)
                if df is not None and not df.empty:
                    # 1. Save Support Resistance (Existing)
                    await save_sr_async_wrapper(df, sym)

                    # 2. Save FULL DATA to TempOptionChain (New)
                    await save_temp_async_wrapper(df, sym)

                    await save_live_sr_async(df, sym)
                    return True
            except Exception as e:
                logger.error(f"Error {sym}: {e}")
            return False

        while True:
            ctrl, _ = await get_control_async(name="others_loop")
            if not ctrl.is_active:
                print("⏸️  Others Loop Paused.")
                await asyncio.sleep(10); continue
            
            if self.is_trading_hours():
                try:
                    start_time = t_time.time()
                    logger.info("--- Batched Sync Started ---")
                    
                    total_success = 0
                    batch_size = 20
                    
                    # --- BATCHING LOGIC START ---
                    for i in range(0, len(symbols), batch_size):
                        batch_start_time = t_time.time() # ⏱️ सिर्फ इस बैच का टाइमर स्टार्ट
                        # 1. Create a batch of 10
                        batch = symbols[i : i + batch_size]
                        
                        # 2. Process this batch concurrently
                        tasks = [process_one(sym) for sym in batch]
                        results = await asyncio.gather(*tasks)
                        
                        # 3. Count success
                        total_success += sum(1 for r in results if r)
                        # टाइम कैलकुलेशन
                        current_time = t_time.time()
                        batch_duration = current_time - batch_start_time  # इस बैच का समय
                        total_symbols_processed = i + len(batch)
                        # --- YOUR PRINT STATEMENT HERE ---
                        print(
                            f"batch {i//batch_size + 1} completed | "
                            f"Batch Time: {batch_duration:.2f}s | "  # यहाँ सिर्फ इस बैच का टाइम आएगा
                            f"Success so far: {total_success}/{total_symbols_processed} symbols"
                        )

                        # 4. Wait 2 seconds before next batch (but skip sleep after last batch)
                        if i + batch_size < len(symbols):
                            await asyncio.sleep(0)
                    # --- BATCHING LOGIC END ---
                    
                    duration = t_time.time() - start_time
                    logger.info(f"🚀 Cycle Completed: expiry:{expiry} | {total_success}/{len(symbols)} symbols in {duration:.2f}s")
                except Exception as e:
                    print(f"Others Loop Error: {e}") 
                # Full cycle sleep (can adjust this if needed)
                await asyncio.sleep(180)
            else:
                print("⏸️  Others Loop Outside Trading Hours.")
                await asyncio.sleep(60) 
            