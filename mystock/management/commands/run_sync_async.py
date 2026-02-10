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
from .async_live import (
    save_sr_async_wrapper,
    get_smart_expiry,
    calculate_data_async_optimized,
    load_master_contract
)
from .symbol import symbols as all_symbols
from mystock.models import OptionChain, SyncControl

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
    is_trading_hours = lambda self: dt_time(9, 15) <= datetime.now().time() <= dt_time(19, 30)

    def handle(self, *args, **options):
        logger.info('🚀 Starting High-Speed Async Engine...') 
        try:
            asyncio.run(self.main_loop())
        except KeyboardInterrupt:
            logger.warning('Stopped by user.')

    # 1. शुरुआत में एक बार लोड करें
    load_master_contract()
    async def main_loop(self):
        other_symbols = [s for s in all_symbols if s != "NIFTY"]
        
        logger.info('⏳ Fetching Smart Expiries...')
        
        # --- NIFTY Expiry Fetch ---
        # get_smart_expiry लिस्ट देता है, हमें पहली डेट [0] चाहिए (Current Week)
        nifty_list = await sync_to_async(get_smart_expiry)("NIFTY")
        if nifty_list:
            nifty_expiry = nifty_list[0] # Current Expiry
        else:
            logger.error("❌ NIFTY Expiry not found!")
            return

        # --- STOCKS Expiry Fetch ---
        # किसी भी एक स्टॉक का नाम भेजें, यह DB में 'STOCK_MONTHLY' चेक करेगा
        stock_list = await sync_to_async(get_smart_expiry)(other_symbols[0])
        if stock_list:
            common_expiry = stock_list[0] # Current Monthly Expiry
        else:
            logger.error("❌ Stock Expiry not found!")
            return

        logger.info(f"✅ NIFTY Expiry: {nifty_expiry} | Stocks Expiry: {common_expiry}")

        async with aiohttp.ClientSession() as session:
            await asyncio.gather(
                # NIFTY loop में डायनामिक expiry भेजें
                self.nifty_loop(session, nifty_expiry, self.FIXED_SYMOL),
                # Others loop में डायनामिक common_expiry भेजें
                self.others_sr_loop(session, other_symbols, common_expiry)
            )
    

    async def nifty_loop(self, session, expiry, fixes_sym):
        """NIFTY Loop - No Changes"""
        while True:
            ctrl, _ = await get_control_async(name="nifty_loop")
            if not ctrl.is_active:
                print(f"⏸️  { fixes_sym} Loop Paused.") 
                await asyncio.sleep(10); continue

            if self.is_trading_hours():
                try:
                    df = await calculate_data_async_optimized(session, fixes_sym, expiry)
                    if df is not None and not df.empty:
                        entries = [OptionChain(
                            Time=row.get('Time'),
                            Symbol=row.get('Symbol'),
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
                        ) for _, row in df.iterrows()]
                        await bulk_create_async(entries)
                        print(f"⚡ [NIFTY] Processed expiry {expiry} - {len(entries)} entries.")
                except Exception as e:
                    logger.error(f"NIFTY Loop Error: {e}")
            else:
                print("⏸️  NIFTY Loop Outside Trading Hours.")
            await asyncio.sleep(5)

    async def others_sr_loop(self, session, symbols, expiry):
        """Modified Loop: Process 10 symbols, wait 2s, repeat."""
        
        # Helper function - removed semaphore since batching controls load
        async def process_one(sym):
            try:
                df = await calculate_data_async_optimized(session, sym, expiry)
                if df is not None and not df.empty:
                    await save_sr_async_wrapper(df, sym)
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
                            await asyncio.sleep(1)
                    # --- BATCHING LOGIC END ---
                    
                    duration = t_time.time() - start_time
                    logger.info(f"🚀 Cycle Completed: expiry:{expiry} | {total_success}/{len(symbols)} symbols in {duration:.2f}s")
                except Exception as e:
                    print(f"Others Loop Error: {e}") 
                # Full cycle sleep (can adjust this if needed)
                await asyncio.sleep(120)
            else:
                print("⏸️  Others Loop Outside Trading Hours.")
                await asyncio.sleep(5) 
            