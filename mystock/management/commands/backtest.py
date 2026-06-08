# ─────────────────────────────────────────────────────────────────────
# फाइल: mystock/management/commands/backtest.py
#
# चलाने का तरीका:
#   python manage.py backtest --symbol NIFTY --date 2024-01-15
#   python manage.py backtest --symbol BANKNIFTY --date 2024-01-15 --interval 1
#   python manage.py backtest --symbol NIFTY --date 2024-01-15 --show-all
# ─────────────────────────────────────────────────────────────────────

import re
from datetime import datetime, time as dt_time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Min, Max

from mystock.models import OptionChain, LiveSRData


class Command(BaseCommand):
    help = 'Trade Logic का Backtest करें OptionChain DB data से'

    def add_arguments(self, parser):
        parser.add_argument('--symbol',   default='NIFTY',      help='Symbol (NIFTY/BANKNIFTY)')
        parser.add_argument('--date',     required=True,         help='Date YYYY-MM-DD')
        parser.add_argument('--interval', default=1, type=int,   help='हर N मिनट पर tick simulate करें')
        parser.add_argument('--show-all', action='store_true',   help='हर tick की detail दिखाएं')
        parser.add_argument('--target-pts', default=None, type=float, help='Fixed target points (default: dynamic)')
        parser.add_argument('--sl-pts',     default=None, type=float, help='Fixed SL points (default: dynamic)')
        parser.add_argument('--buffer',     default=2.0,  type=float, help='Reversal buffer (default: 2.0)')

    def handle(self, *args, **options):
        symbol      = options['symbol'].upper()
        date_str    = options['date']
        interval    = options['interval']
        show_all    = options['show_all']
        TARGET_PTS  = options['target_pts'] or 50
        SL_PTS      = options['sl_pts']     or 50
        BUFFER      = options['buffer']

        try:
            selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            self.stderr.write(f"❌ Date format गलत है: {date_str} (सही: YYYY-MM-DD)")
            return

        step = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50

        day_start = timezone.make_aware(datetime.combine(selected_date, dt_time.min))
        day_end   = timezone.make_aware(datetime.combine(selected_date, dt_time.max))

        self.stdout.write("\n" + "═"*65)
        self.stdout.write(f"  📊 BACKTEST: {symbol}  |  Date: {date_str}  |  Buffer: {BUFFER}")
        self.stdout.write("═"*65)

        # ──────────────────────────────────────────────
        # 1. SR Timeline निकालो (tick-by-tick, हर minute)
        # ──────────────────────────────────────────────
        sr_rows = list(
            LiveSRData.objects
            .filter(Symbol__iexact=symbol, Time__gte=day_start, Time__lte=day_end)
            .order_by('Time')
        )
        if not sr_rows:
            self.stderr.write(f"❌ {date_str} को {symbol} का कोई LiveSRData नहीं मिला।")
            return

        self.stdout.write(f"  ✅ SR Rows मिले: {len(sr_rows)}")

        # ──────────────────────────────────────────────
        # 2. OptionChain ticks निकालो (Spot + Reversal)
        # ──────────────────────────────────────────────
        # सिर्फ unique times (हर interval मिनट)
        all_oc = list(
            OptionChain.objects
            .filter(Symbol__iexact=symbol, Time__gte=day_start, Time__lte=day_end)
            .order_by('Time')
            .values('Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe')
        )

        if not all_oc:
            self.stderr.write(f"❌ {date_str} को {symbol} का कोई OptionChain data नहीं मिला।")
            return

        # Spot times के unique list (interval के हिसाब से filter)
        spot_by_time = {}
        for row in all_oc:
            t = row['Time']
            # Interval filter: सिर्फ हर N मिनट का पहला tick
            if t.minute % interval == 0:
                if t not in spot_by_time:
                    spot_by_time[t] = {'spot': float(row['Spot_Price'] or 0), 'strikes': {}}
                s = float(row['Strike_Price'] or 0)
                if s > 0:
                    spot_by_time[t]['strikes'][s] = {
                        'ce': float(row['Reversl_Ce'] or 0),
                        'pe': float(row['Reversl_Pe'] or 0),
                    }

        sorted_times = sorted(spot_by_time.keys())
        self.stdout.write(f"  ✅ Ticks मिले: {len(sorted_times)}  (interval={interval}m)\n")

        # ──────────────────────────────────────────────
        # 3. SR को time के हिसाब से resolve करने का helper
        # ──────────────────────────────────────────────
        def get_sr_at_time(tick_time):
            """उस tick_time पर जो SR row active था वो return करो"""
            active = None
            for sr in sr_rows:
                if sr.Time <= tick_time:
                    active = sr
                else:
                    break
            return active

        def calc_eff_strikes(sr):
            """trade_logic.py का exact same 25+ condition logic"""
            if not sr:
                return 0, 0

            res_status = str(sr.resistance_status or "").upper()
            sup_status = str(sr.supprt_status or "").upper()

            res_base = float(sr.resistance_strike or 0)
            sup_base = float(sr.supprt_strike or 0)

            m_res = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
            m_sup = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
            res_target = float(m_res.group(1)) if m_res else res_base
            sup_target = float(m_sup.group(1)) if m_sup else sup_base

            # Resistance
            if   "WTB" in res_status and "WTB" in sup_status: eff_res = res_target + step
            elif "WTB" in res_status and "WTT" in sup_status: eff_res = res_base
            elif "WTB" in res_status and "STRONG" in sup_status: eff_res = res_base
            elif "WTB" in res_status and "SHIFTED WTB" in sup_status: eff_res = res_base + step
            elif "WTB" in res_status and "SHIFTED WTT" in sup_status: eff_res = res_base - step
            elif "WTT" in res_status and "WTB" in sup_status: eff_res = res_target
            elif "WTT" in res_status and "WTT" in sup_status: eff_res = res_target + step
            elif "WTT" in res_status and "STRONG" in sup_status: eff_res = res_target
            elif "WTT" in res_status and "SHIFTED WTB" in sup_status: eff_res = res_target + step
            elif "WTT" in res_status and "SHIFTED WTT" in sup_status: eff_res = res_base
            elif "STRONG" in res_status and "WTB" in sup_status: eff_res = res_base
            elif "STRONG" in res_status and "WTT" in sup_status: eff_res = res_base + step
            elif "STRONG" in res_status and "STRONG" in sup_status: eff_res = res_base + step
            elif "STRONG" in res_status and "SHIFTED WTB" in sup_status: eff_res = res_base + step
            elif "STRONG" in res_status and "SHIFTED WTT" in sup_status: eff_res = res_base
            elif "SHIFTED WTB" in res_status and "WTB" in sup_status: eff_res = res_base
            elif "SHIFTED WTB" in res_status and "WTT" in sup_status: eff_res = res_base + step
            elif "SHIFTED WTB" in res_status and "STRONG" in sup_status: eff_res = res_base + step
            elif "SHIFTED WTB" in res_status and "SHIFTED WTB" in sup_status: eff_res = res_base + step
            elif "SHIFTED WTB" in res_status and "SHIFTED WTT" in sup_status: eff_res = res_base
            elif "SHIFTED WTT" in res_status and "WTB" in sup_status: eff_res = res_target - step
            elif "SHIFTED WTT" in res_status and "WTT" in sup_status: eff_res = res_target
            elif "SHIFTED WTT" in res_status and "STRONG" in sup_status: eff_res = res_target - step
            elif "SHIFTED WTT" in res_status and "SHIFTED WTB" in sup_status: eff_res = res_target - step
            elif "SHIFTED WTT" in res_status and "SHIFTED WTT" in sup_status: eff_res = res_base
            else: eff_res = res_base + step

            # Support
            if   "WTB" in sup_status and "WTB" in res_status: eff_sup = sup_target - step
            elif "WTB" in sup_status and "WTT" in res_status: eff_sup = sup_target
            elif "WTB" in sup_status and "STRONG" in res_status: eff_sup = sup_target + step
            elif "WTB" in sup_status and "SHIFTED WTB" in res_status: eff_sup = sup_target - step
            elif "WTB" in sup_status and "SHIFTED WTT" in res_status: eff_sup = sup_base
            elif "WTT" in sup_status and "WTB" in res_status: eff_sup = sup_base
            elif "WTT" in sup_status and "WTT" in res_status: eff_sup = sup_base + step
            elif "WTT" in sup_status and "STRONG" in res_status: eff_sup = sup_base
            elif "WTT" in sup_status and "SHIFTED WTB" in res_status: eff_sup = sup_base + step
            elif "WTT" in sup_status and "SHIFTED WTT" in res_status: eff_sup = sup_base - step
            elif "STRONG" in sup_status and "WTB" in res_status: eff_sup = sup_base - step
            elif "STRONG" in sup_status and "WTT" in res_status: eff_sup = sup_base
            elif "STRONG" in sup_status and "STRONG" in res_status: eff_sup = sup_base - step
            elif "STRONG" in sup_status and "SHIFTED WTB" in res_status: eff_sup = sup_base
            elif "STRONG" in sup_status and "SHIFTED WTT" in res_status: eff_sup = sup_base - step
            elif "SHIFTED WTB" in sup_status and "WTB" in res_status: eff_sup = sup_target
            elif "SHIFTED WTB" in sup_status and "WTT" in res_status: eff_sup = sup_target + step
            elif "SHIFTED WTB" in sup_status and "STRONG" in res_status: eff_sup = sup_target + step
            elif "SHIFTED WTB" in sup_status and "SHIFTED WTB" in res_status: eff_sup = sup_base
            elif "SHIFTED WTB" in sup_status and "SHIFTED WTT" in res_status: eff_sup = sup_target + step
            elif "SHIFTED WTT" in sup_status and "WTB" in res_status: eff_sup = sup_base - step
            elif "SHIFTED WTT" in sup_status and "WTT" in res_status: eff_sup = sup_base
            elif "SHIFTED WTT" in sup_status and "STRONG" in res_status: eff_sup = sup_base - step
            elif "SHIFTED WTT" in sup_status and "SHIFTED WTB" in res_status: eff_sup = sup_base
            elif "SHIFTED WTT" in sup_status and "SHIFTED WTT" in res_status: eff_sup = sup_base - step
            else: eff_sup = sup_base - step

            return eff_res, eff_sup

        def get_rev_val_at_time(tick_time, strike, side, period=10):
            """उस tick पर reversal value — DB से last 10 rows का average"""
            target_col = 'Reversl_Ce' if side == 'CE' else 'Reversl_Pe'
            rows = (
                OptionChain.objects
                .filter(
                    Symbol__iexact=symbol,
                    Time__lte=tick_time,
                    Time__gte=day_start,
                    Strike_Price=strike
                )
                .order_by('-Time')
                .values(target_col)[:period]
            )
            vals = [float(r[target_col]) for r in rows if r[target_col] and float(r[target_col]) > 0]
            return round(sum(vals) / len(vals), 2) if vals else None

        # ──────────────────────────────────────────────
        # 4. SIMULATION LOOP
        # ──────────────────────────────────────────────
        trades    = []   # {'type', 'entry_time', 'entry_spot', 'entry_strike', 'trigger', 'exit_time', 'exit_spot', 'result', 'pnl'}
        open_trade = None
        warnings   = []
        tolerance  = 20.0

        for tick_time in sorted_times:
            tick_data = spot_by_time[tick_time]
            spot      = tick_data['spot']
            if spot <= 0:
                continue

            sr = get_sr_at_time(tick_time)
            if not sr:
                continue

            eff_res, eff_sup = calc_eff_strikes(sr)
            if not eff_res or not eff_sup:
                continue

            r_level = get_rev_val_at_time(tick_time, eff_res, 'CE')
            s_level = get_rev_val_at_time(tick_time, eff_sup, 'PE')

            # ── Show-all tick detail ──
            if show_all:
                t_ist_str = timezone.localtime(tick_time).strftime('%H:%M')
                tr_info = f"[OPEN: {open_trade['type']}@{open_trade['entry_spot']}]" if open_trade else ""
                self.stdout.write(
                    f"  {t_ist_str} IST | Spot={spot:.0f} | R={eff_res:.0f}({r_level or '—'}) "
                    f"S={eff_sup:.0f}({s_level or '—'}) | SR: {str(sr.resistance_status)[:12]} / {str(sr.supprt_status)[:12]} {tr_info}"
                )

            # ── EXIT LOGIC ──
            if open_trade:
                entry  = open_trade['entry_spot']
                ttype  = open_trade['type']
                target = open_trade.get('target')
                sl     = open_trade.get('sl')

                if ttype == 'PUT':
                    if not target: target = entry - TARGET_PTS
                    if not sl:     sl     = entry + SL_PTS
                    if sl <= entry:
                        sl = entry + SL_PTS
                        warnings.append(f"⚠️  {tick_time.strftime('%H:%M')} PUT SL safety override: SL था {sl:.0f} (entry से छोटा)")
                    hit_target = spot <= (target + BUFFER)
                    hit_sl     = spot >= (sl - BUFFER)

                elif ttype == 'CALL':
                    if not target: target = entry + TARGET_PTS
                    if not sl:     sl     = entry - SL_PTS
                    if sl >= entry:
                        sl = entry - SL_PTS
                        warnings.append(f"⚠️  {tick_time.strftime('%H:%M')} CALL SL safety override: SL था {sl:.0f} (entry से बड़ा)")
                    hit_target = spot >= (target - BUFFER)
                    hit_sl     = spot <= (sl + BUFFER)

                if hit_target or hit_sl:
                    pnl = (spot - entry) if ttype == 'CALL' else (entry - spot)
                    open_trade.update({
                        'exit_time': tick_time, 'exit_spot': spot,
                        'result': 'TARGET' if hit_target else 'SL',
                        'pnl': round(pnl, 2),
                    })
                    trades.append(open_trade)
                    emoji = "✅" if hit_target else "❌"
                    self.stdout.write(
                        f"  {emoji} CLOSED  | {ttype:4s} | {timezone.localtime(open_trade['entry_time']).strftime('%H:%M')} IST → "
                        f"{timezone.localtime(tick_time).strftime('%H:%M')} IST | Entry={entry:.0f} Exit={spot:.0f} | "
                        f"Result={open_trade['result']:6s} | PNL={pnl:+.0f}"
                    )
                    open_trade = None
                    continue

            if open_trade:
                continue   # trade open है, नई entry मत लो

            # ── ENTRY LOGIC ──
            # SL Pause check (trades list में देखो)
            last_put_sl  = next((t for t in reversed(trades) if t['type'] == 'PUT'  and t['result'] == 'SL'), None)
            last_call_sl = next((t for t in reversed(trades) if t['type'] == 'CALL' and t['result'] == 'SL'), None)

            r_paused = last_put_sl  and float(last_put_sl.get('entry_strike', 0))  == eff_res
            s_paused = last_call_sl and float(last_call_sl.get('entry_strike', 0)) == eff_sup

            # Repeat shift check
            if not r_paused and r_level:
                r_traded = any(
                    abs(t['trigger'] - r_level) <= tolerance
                    for t in trades if t['type'] == 'PUT'
                )
                if r_traded:
                    eff_res  = eff_res + step
                    r_level  = get_rev_val_at_time(tick_time, eff_res, 'CE')
                    if show_all:
                        self.stdout.write(f"    ↳ R REPEAT SHIFT → {eff_res}")

            if not s_paused and s_level:
                s_traded = any(
                    abs(t['trigger'] - s_level) <= tolerance
                    for t in trades if t['type'] == 'CALL'
                )
                if s_traded:
                    eff_sup  = eff_sup - step
                    s_level  = get_rev_val_at_time(tick_time, eff_sup, 'PE')
                    if show_all:
                        self.stdout.write(f"    ↳ S REPEAT SHIFT → {eff_sup}")

            # R Entry (PUT)
            if not r_paused and r_level and spot >= r_level:
                r_target_val = get_rev_val_at_time(tick_time, eff_res - step, 'CE')
                r_sl_val     = get_rev_val_at_time(tick_time, eff_res + step, 'CE')
                open_trade = {
                    'type': 'PUT', 'entry_time': tick_time, 'entry_spot': spot,
                    'entry_strike': eff_res, 'trigger': r_level,
                    'target': r_target_val, 'sl': r_sl_val,
                }
                self.stdout.write(
                    f"\n  PUT ENTRY | {timezone.localtime(tick_time).strftime('%H:%M')} IST | Spot={spot:.0f} "
                    f"| R={eff_res:.0f} Trigger={r_level:.0f} "
                    f"| Target={r_target_val or 'N/A'} SL={r_sl_val or 'N/A'}"
                )

            # S Entry (CALL)
            elif not s_paused and s_level and spot <= s_level:
                s_target_val = get_rev_val_at_time(tick_time, eff_sup + step, 'PE')
                s_sl_val     = get_rev_val_at_time(tick_time, eff_sup - step, 'PE')
                open_trade = {
                    'type': 'CALL', 'entry_time': tick_time, 'entry_spot': spot,
                    'entry_strike': eff_sup, 'trigger': s_level,
                    'target': s_target_val, 'sl': s_sl_val,
                }
                self.stdout.write(
                    f"\n  CALL ENTRY | {timezone.localtime(tick_time).strftime('%H:%M')} IST | Spot={spot:.0f} "
                    f"| S={eff_sup:.0f} Trigger={s_level:.0f} "
                    f"| Target={s_target_val or 'N/A'} SL={s_sl_val or 'N/A'}"
                )

        # अगर दिन के अंत में trade open रह गई
        if open_trade:
            last_spot = spot_by_time[sorted_times[-1]]['spot']
            pnl = (last_spot - open_trade['entry_spot']) if open_trade['type'] == 'CALL' else (open_trade['entry_spot'] - last_spot)
            open_trade.update({
                'exit_time': sorted_times[-1], 'exit_spot': last_spot,
                'result': 'OPEN (EOD)', 'pnl': round(pnl, 2),
            })
            trades.append(open_trade)
            self.stdout.write(f"\n  ⏰ EOD CLOSE  | {open_trade['type']} | PNL={pnl:+.0f}")

        # ──────────────────────────────────────────────
        # 5. SUMMARY
        # ──────────────────────────────────────────────
        self.stdout.write("\n" + "═"*65)
        self.stdout.write(f"  📋 SUMMARY — {symbol} {date_str}")
        self.stdout.write("─"*65)

        if not trades:
            self.stdout.write("  ⚪ कोई trade trigger नहीं हुई।")
            self.stdout.write("  → संभावित कारण: reversal level data नहीं मिला, या spot कभी level तक नहीं पहुँचा।")
        else:
            wins    = [t for t in trades if t['result'] == 'TARGET']
            losses  = [t for t in trades if t['result'] == 'SL']
            eod     = [t for t in trades if 'EOD' in t.get('result','')]
            net_pnl = sum(t['pnl'] for t in trades)
            win_rate = (len(wins) / len(trades) * 100) if trades else 0

            self.stdout.write(f"  कुल Trades   : {len(trades)}")
            self.stdout.write(f"  ✅ TARGET    : {len(wins)}")
            self.stdout.write(f"  ❌ SL        : {len(losses)}")
            self.stdout.write(f"  ⏰ EOD Open  : {len(eod)}")
            self.stdout.write(f"  Win Rate     : {win_rate:.1f}%")
            self.stdout.write(f"  Net PNL      : {net_pnl:+.2f} pts")
            self.stdout.write("─"*65)
            self.stdout.write("  Trade Details:")
            for t in trades:
                emoji = "✅" if t['result'] == 'TARGET' else ("❌" if t['result'] == 'SL' else "⏰")
                self.stdout.write(
                    f"  {emoji} {t['type']:4s} | In={timezone.localtime(t['entry_time']).strftime('%H:%M')} "
                    f"Out={t['exit_time'].strftime('%H:%M') if t.get('exit_time') else '—'} | "
                    f"E={t['entry_spot']:.0f} X={t.get('exit_spot',0):.0f} | "
                    f"PNL={t['pnl']:+.0f} | Strike={t['entry_strike']:.0f}"
                )

        # Warnings
        if warnings:
            self.stdout.write("\n  ⚠️  Warnings:")
            for w in warnings:
                self.stdout.write(f"  {w}")

        # Data Quality Check
        self.stdout.write("\n  🔍 Data Quality Check:")
        total_ticks  = len(sorted_times)
        no_rev_ticks = 0
        for t in sorted_times[:20]:   # पहले 20 ticks check करो
            sr = get_sr_at_time(t)
            if sr:
                er, es = calc_eff_strikes(sr)
                rv = get_rev_val_at_time(t, er, 'CE')
                if not rv:
                    no_rev_ticks += 1
        if no_rev_ticks > 10:
            self.stdout.write(f"  ⚠️  पहले 20 ticks में {no_rev_ticks}/20 पर reversal value नहीं मिली — OptionChain data sparse हो सकता है।")
        else:
            self.stdout.write(f"  ✅ Reversal data quality ठीक है।")

        self.stdout.write(f"  Total ticks simulated: {total_ticks}")
        self.stdout.write("═"*65 + "\n")
