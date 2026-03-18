from django.shortcuts import render
from django.http import JsonResponse
from django.db.models.functions import TruncSecond
from django.db.models import Min
from .models import OptionChain
import datetime

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
UTC = datetime.timezone.utc

STRIKES_EACH_SIDE = 15   # ← Yahan badlo agar aur strikes chahiye


def to_ist_str(dt):
    return dt.astimezone(IST).strftime('%Y-%m-%d %H:%M:%S')


def ist_str_to_utc(ts_str):
    naive = datetime.datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
    return naive.replace(tzinfo=IST).astimezone(UTC)


def market_replay_view(request):
    symbols      = list(OptionChain.objects.values_list('Symbol', flat=True).distinct().order_by('Symbol'))
    expiry_dates = OptionChain.objects.values_list('Expiry_Date', flat=True).distinct().order_by('Expiry_Date')
    expiry_list  = [d.strftime('%Y-%m-%d') for d in expiry_dates if d]
    return render(request, 'mystock/market_replay.html', {
        'symbols': symbols, 'expiry_dates': expiry_list,
    })


# ─────────────────────────────────────────────────────────────
#  1. TIMESTAMPS
# ─────────────────────────────────────────────────────────────
def get_replay_timestamps(request):
    symbol      = request.GET.get('symbol', '').strip()
    expiry_date = request.GET.get('expiry_date', '').strip()
    replay_date = request.GET.get('replay_date', '').strip()

    if not (symbol and replay_date):
        return JsonResponse({'error': 'symbol aur replay_date zaroori hain'}, status=400)

    try:
        date_obj = datetime.date.fromisoformat(replay_date)
    except ValueError:
        return JsonResponse({'error': 'replay_date format galat'}, status=400)

    qs = OptionChain.objects.filter(Symbol=symbol, Time__date=date_obj)
    if expiry_date:
        qs = qs.filter(Expiry_Date=expiry_date)

    ts_qs = (
        qs.annotate(ts_sec=TruncSecond('Time'))
          .values('ts_sec')
          .distinct()
          .order_by('ts_sec')
          .values_list('ts_sec', flat=True)
    )

    ts_list = [t.astimezone(IST).strftime('%Y-%m-%d %H:%M:%S') for t in ts_qs]
    return JsonResponse({'timestamps': ts_list, 'total_ticks': len(ts_list)})


# ─────────────────────────────────────────────────────────────
#  2. SINGLE TICK  — Spot ke aaspaas 15+15 strikes
# ─────────────────────────────────────────────────────────────
def get_replay_tick(request):
    symbol      = request.GET.get('symbol', '').strip()
    expiry_date = request.GET.get('expiry_date', '').strip()
    timestamp   = request.GET.get('timestamp', '').strip()

    if not (symbol and timestamp):
        return JsonResponse({'error': 'symbol aur timestamp zaroori hain'}, status=400)

    try:
        utc_dt     = ist_str_to_utc(timestamp)
        utc_dt_end = utc_dt + datetime.timedelta(seconds=1)
    except ValueError:
        return JsonResponse({'error': 'timestamp format galat'}, status=400)

    qs = OptionChain.objects.filter(Symbol=symbol, Time__gte=utc_dt, Time__lt=utc_dt_end)
    if expiry_date:
        qs = qs.filter(Expiry_Date=expiry_date)
    qs = qs.order_by('Strike_Price')

    def fmt(val):
        if val is None:
            return ''
        return round(val, 2) if isinstance(val, float) else val

    # ── Step 1: Duplicate strikes hatao, spot_price lo ──
    all_objs     = []
    spot_price   = None
    seen_strikes = set()

    for obj in qs:
        sp = round(obj.Strike_Price, 1) if obj.Strike_Price else None
        if sp in seen_strikes:
            continue
        seen_strikes.add(sp)
        if spot_price is None and obj.Spot_Price:
            spot_price = obj.Spot_Price
        all_objs.append(obj)

    # ── Step 2: Spot ke nearest strike dhundo ──
    # Phir usse 15 neeche aur 15 upar ki strikes rakhlo
    if spot_price and all_objs:
        all_strikes = sorted([round(o.Strike_Price, 1) for o in all_objs])
        nearest_idx = min(range(len(all_strikes)),
                          key=lambda i: abs(all_strikes[i] - spot_price))

        start_i  = max(0, nearest_idx - STRIKES_EACH_SIDE)
        end_i    = min(len(all_strikes) - 1, nearest_idx + STRIKES_EACH_SIDE)
        allowed  = set(all_strikes[start_i : end_i + 1])

        show_objs = [o for o in all_objs if round(o.Strike_Price, 1) in allowed]
    else:
        show_objs = all_objs   # fallback: sab dikhao

    # ── Step 3: Response build karo ──
    rows = []
    for obj in show_objs:
        rows.append({
            'Strike_Price'      : fmt(obj.Strike_Price),
            'CE_LTP'            : fmt(obj.CE_LTP),
            'CE_CLTP'           : fmt(obj.CE_CLTP),
            'CE_Volume'         : fmt(obj.CE_Volume),
            'CE_Volume_percent' : fmt(obj.CE_Volume_percent),
            'CE_OI'             : fmt(obj.CE_OI),
            'CE_OI_percent'     : fmt(obj.CE_OI_percent),
            'CE_COI'            : fmt(obj.CE_COI),
            'CE_COI_percent'    : fmt(obj.CE_COI_percent),
            'CE_IV'             : fmt(obj.CE_IV),
            'CE_RANGE'          : fmt(obj.CE_RANGE),
            'CE_Delta'          : fmt(obj.CE_Delta),
            'Reversl_Ce'        : fmt(obj.Reversl_Ce),
            'Reversl_Pe'        : fmt(obj.Reversl_Pe),
            'PE_LTP'            : fmt(obj.PE_LTP),
            'PE_CLTP'           : fmt(obj.PE_CLTP),
            'PE_Volume'         : fmt(obj.PE_Volume),
            'PE_Volume_percent' : fmt(obj.PE_Volume_percent),
            'PE_OI'             : fmt(obj.PE_OI),
            'PE_OI_percent'     : fmt(obj.PE_OI_percent),
            'PE_COI'            : fmt(obj.PE_COI),
            'PE_COI_percent'    : fmt(obj.PE_COI_percent),
            'PE_IV'             : fmt(obj.PE_IV),
            'PE_RANGE'          : fmt(obj.PE_RANGE),
            'PE_Delta'          : fmt(obj.PE_Delta),
        })

    return JsonResponse({
        'rows'      : rows,
        'spot_price': spot_price,
        'timestamp' : timestamp,
        'total_rows': len(rows),
    })


# ─────────────────────────────────────────────────────────────
#  3. AVAILABLE DATES
# ─────────────────────────────────────────────────────────────
def get_replay_dates(request):
    symbol = request.GET.get('symbol', '').strip()
    qs = OptionChain.objects.all()
    if symbol:
        qs = qs.filter(Symbol=symbol)

    times = qs.values_list('Time', flat=True).distinct().order_by('Time')
    seen, date_list = set(), []
    for t in times:
        d = t.astimezone(IST).date().isoformat()
        if d not in seen:
            seen.add(d)
            date_list.append(d)

    return JsonResponse({'dates': date_list})
