import re
import traceback
from datetime import datetime, time as dt_time
from django.utils import timezone
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from .models import OptionChain, LiveSRData
from .trade_logic import get_master_levels
from .views import get_instrument_key, fetch_candle_data, parse_candles


# ============================================================
#  1. PAGE VIEW  —  सिर्फ HTML render करेगा
# ============================================================
@login_required
def reversal_chart_view(request):
    context = {
        'symbols': ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX'],
    }
    return render(request, 'mystock/reversal_chart.html', context)


# ============================================================
#  2. DATA API  —  Candles + Reversal Lines भेजेगा
# ============================================================
@login_required
def reversal_chart_data_api(request):
    """
    Frontend को एक JSON में भेजेगा:
      - candles     : पूरे दिन की 1-min OHLC candles
      - timeline    : सभी tick-times की sorted list
      - timeline_data : हर tick पर { sr, lines } का dict
    JS इसे slider से control करेगा और lines live update होंगी।
    """
    symbol   = request.GET.get('symbol', 'NIFTY').upper()
    date_str = request.GET.get('date')

    if not date_str:
        return JsonResponse({'error': 'Date is required'}, status=400)

    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        day_start = timezone.make_aware(datetime.combine(selected_date, dt_time(9, 15)))
        day_end   = timezone.make_aware(datetime.combine(selected_date, dt_time(15, 30)))

        # --------------------------------------------------
        # A. Option Chain — Spot Price + Reversal columns
        # --------------------------------------------------
        oc_qs = OptionChain.objects.filter(
            Symbol=symbol,
            Time__gte=day_start,
            Time__lte=day_end,
        ).values(
            'Time', 'Strike_Price', 'Spot_Price',
            'CE_OI', 'PE_OI', 'CE_OI_percent', 'PE_OI_percent',
            'CE_Volume', 'PE_Volume',
            'CE_COI', 'PE_COI',
            'Reversl_Ce', 'Reversl_Pe',
        ).order_by('Time', 'Strike_Price')

        if not oc_qs.exists():
            return JsonResponse(
                {'error': 'इस तारीख का कोई Option Chain डेटा नहीं मिला।'},
                status=404,
            )

        # Time → grouped dict
        grouped_oc = {}
        for row in oc_qs:
            t_str = timezone.localtime(row['Time']).strftime('%H:%M:%S')
            if t_str not in grouped_oc:
                grouped_oc[t_str] = {
                    'spot': float(row['Spot_Price']) if row['Spot_Price'] else 0.0,
                    'chain': [],
                }
            r = dict(row)
            r['Time'] = t_str          # datetime → string (JSON serializable)
            grouped_oc[t_str]['chain'].append(r)

        # --------------------------------------------------
        # B. SR Data
        # --------------------------------------------------
        sr_qs = LiveSRData.objects.filter(
            Symbol=symbol,
            Time__gte=day_start,
            Time__lte=day_end,
        ).values(
            'Time', 'resistance_strike', 'resistance_status',
            'supprt_strike', 'supprt_status',
        ).order_by('Time')

        sr_list = []
        for row in sr_qs:
            t_str = timezone.localtime(row['Time']).strftime('%H:%M:%S')
            r = dict(row)
            r['Time'] = t_str
            sr_list.append((t_str, r))

        # --------------------------------------------------
        # C. Timeline बनाएं (Forward-fill SR + Reversal Lines)
        # --------------------------------------------------
        sorted_times = sorted(grouped_oc.keys())
        timeline_data = {}
        last_sr = {}
        sr_idx  = 0

        for t_str in sorted_times:
            oc_tick = grouped_oc[t_str]

            # Forward-fill SR
            while sr_idx < len(sr_list) and sr_list[sr_idx][0] <= t_str:
                last_sr = sr_list[sr_idx][1]
                sr_idx += 1

            lines = _build_reversal_lines(symbol, last_sr, oc_tick['chain'])
            spot  = oc_tick['spot']

            timeline_data[t_str] = {
                'spot'  : spot,
                'sr'    : last_sr,
                'lines' : lines,
            }

        # --------------------------------------------------
        # D. Candle Data (Upstox / आपका source)
        # --------------------------------------------------
        candles = []
        instrument_key = get_instrument_key(symbol)
        if instrument_key:
            chart_res = fetch_candle_data(instrument_key, "minutes", "1", date_str, date_str)
            if chart_res and chart_res.get("success"):
                candles = parse_candles(chart_res["data"])

        return JsonResponse({
            'success'       : True,
            'symbol'        : symbol,
            'date'          : date_str,
            'timeline'      : sorted_times,
            'timeline_data' : timeline_data,
            'candles'       : candles,
        })

    except Exception:
        print(traceback.format_exc())
        return JsonResponse({'error': 'Server error — check logs'}, status=500)


# ============================================================
#  3. HELPER — Reversal Lines बनाना
# ============================================================
def _build_reversal_lines(symbol, sr_dict, oc_list):
    """
    SR data + Option Chain से reversal price-lines बनाता है।
    Returns list of { price, color, width, label, style }
    """
    try:
        if not sr_dict or not oc_list:
            return []

        step = 100 if ('BANKNIFTY' in symbol or 'SENSEX' in symbol) else 50
        spot = float(oc_list[0].get('Spot_Price', 0)) if oc_list else 0

        # ── Resistance effective level ──────────────────────
        res_status = str(sr_dict.get('resistance_status', '')).upper()
        res_base   = float(sr_dict.get('resistance_strike') or 0)
        m_res      = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
        res_target = float(m_res.group(1)) if m_res else res_base

        if   'SHIFTED WTT' in res_status: eff_res = res_base + step
        elif 'SHIFTED WTB' in res_status: eff_res = res_base + step
        elif 'WTT'         in res_status: eff_res = res_target - step
        elif 'WTB'         in res_status: eff_res = res_target + step
        elif 'STRONG'      in res_status: eff_res = res_base + step
        else:                             eff_res = res_base + step

        # ── Support effective level ─────────────────────────
        sup_status = str(sr_dict.get('supprt_status', '')).upper()
        sup_base   = float(sr_dict.get('supprt_strike') or 0)
        m_sup      = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
        sup_target = float(m_sup.group(1)) if m_sup else sup_base

        if   'SHIFTED WTT' in sup_status: eff_sup = sup_base - step
        elif 'SHIFTED WTB' in sup_status: eff_sup = sup_base - step
        elif 'WTT'         in sup_status: eff_sup = sup_target - step
        elif 'WTB'         in sup_status: eff_sup = sup_target + step
        elif 'STRONG'      in sup_status: eff_sup = sup_base - step
        else:                             eff_sup = sup_base - step

        # Fallback अगर SR खाली हो
        if eff_res == 0: eff_res = spot + step
        if eff_sup == 0: eff_sup = spot - step
        if res_base == 0: res_base = spot
        if sup_base == 0: sup_base = spot

        global_low  = min(eff_sup, sup_base) - step
        global_high = max(eff_res, res_base) + step

        lines    = []
        seen_ce  = set()
        seen_pe  = set()

        # ── Pass 1: Master R / Master S (मोटी lines) ────────
        for row in oc_list:
            strike = float(row.get('Strike_Price', 0))
            if not (global_low <= strike <= global_high):
                continue

            if strike == eff_res and row.get('Reversl_Ce'):
                val = float(row['Reversl_Ce'])
                if val > 0:
                    lines.append({
                        'price' : val,
                        'color' : '#FF8C00',   # orange — Master R
                        'width' : 3,
                        'label' : f'MR {strike:.0f}',
                        'style' : 'solid',
                        'type'  : 'master_r',
                    })
                    seen_ce.add(val)

            if strike == eff_sup and row.get('Reversl_Pe'):
                val = float(row['Reversl_Pe'])
                if val > 0:
                    lines.append({
                        'price' : val,
                        'color' : '#00BFFF',   # sky-blue — Master S
                        'width' : 3,
                        'label' : f'MS {strike:.0f}',
                        'style' : 'solid',
                        'type'  : 'master_s',
                    })
                    seen_pe.add(val)

        # ── Pass 2: बाकी CE / PE lines (पतली, dashed) ──────
        for row in oc_list:
            strike = float(row.get('Strike_Price', 0))
            if not (global_low <= strike <= global_high):
                continue

            if strike != eff_res and row.get('Reversl_Ce'):
                val = float(row['Reversl_Ce'])
                if val > 0 and val >= spot and val not in seen_ce:
                    lines.append({
                        'price' : val,
                        'color' : '#F85149',   # red — CE reversal
                        'width' : 1,
                        'label' : f'CE {strike:.0f}',
                        'style' : 'dashed',
                        'type'  : 'ce',
                    })
                    seen_ce.add(val)

            if strike != eff_sup and row.get('Reversl_Pe'):
                val = float(row['Reversl_Pe'])
                if val > 0 and val < spot and val not in seen_pe:
                    lines.append({
                        'price' : val,
                        'color' : '#3FB950',   # green — PE reversal
                        'width' : 1,
                        'label' : f'PE {strike:.0f}',
                        'style' : 'dashed',
                        'type'  : 'pe',
                    })
                    seen_pe.add(val)

        lines.sort(key=lambda x: x['price'], reverse=True)
        return lines

    except Exception as e:
        print(f'_build_reversal_lines error: {e}')
        return []
