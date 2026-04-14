from django.shortcuts import render
from django.http import JsonResponse
from django.db.models.functions import TruncSecond, TruncDate
from .models import OptionChain
import datetime
from collections import defaultdict

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
UTC = datetime.timezone.utc

STRIKES_EACH_SIDE = 15

TICK_FIELDS = [
    'Strike_Price', 'Spot_Price',
    'CE_LTP', 'CE_CLTP', 'CE_Volume', 'CE_Volume_percent',
    'CE_OI', 'CE_OI_percent', 'CE_COI', 'CE_COI_percent',
    'CE_IV', 'CE_RANGE', 'CE_Delta', 'Reversl_Ce',
    'Reversl_Pe',
    'PE_LTP', 'PE_CLTP', 'PE_Volume', 'PE_Volume_percent',
    'PE_OI', 'PE_OI_percent', 'PE_COI', 'PE_COI_percent',
    'PE_IV', 'PE_RANGE', 'PE_Delta',
]


def ist_str_to_utc(ts_str):
    naive = datetime.datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
    return naive.replace(tzinfo=IST).astimezone(UTC)


def _day_range_utc(date_obj):
    start = datetime.datetime(date_obj.year, date_obj.month, date_obj.day, 0, 0, 0, tzinfo=IST)
    end   = datetime.datetime(date_obj.year, date_obj.month, date_obj.day, 23, 59, 59, tzinfo=IST)
    return start.astimezone(UTC), end.astimezone(UTC)


def _fmt(val):
    if val is None:
        return ''
    return round(val, 2) if isinstance(val, float) else val


def market_replay_view(request):
    symbols     = list(OptionChain.objects.values_list('Symbol', flat=True).distinct().order_by('Symbol'))
    expiry_list = list(OptionChain.objects.values_list('Expiry_Date', flat=True).distinct().order_by('Expiry_Date'))
    expiry_list = [d.strftime('%Y-%m-%d') for d in expiry_list if d]
    return render(request, 'mystock/market_replay.html', {
        'symbols': symbols, 'expiry_dates': expiry_list,
    })


# ─────────────────────────────────────────────────────────────
#  BULK LOAD — EK CALL MEIN POORE DIN KA DATA
#  Yeh naya endpoint hai jo timestamps + sab tick data ek saath
#  bhejta hai. JS ko ab har tick pe alag request nahi bhejni.
# ─────────────────────────────────────────────────────────────
def get_replay_bulk(request):
    symbol      = request.GET.get('symbol', '').strip()
    expiry_date = request.GET.get('expiry_date', '').strip()
    replay_date = request.GET.get('replay_date', '').strip()

    if not (symbol and replay_date):
        return JsonResponse({'error': 'symbol aur replay_date zaroori hain'}, status=400)

    try:
        date_obj = datetime.date.fromisoformat(replay_date)
    except ValueError:
        return JsonResponse({'error': 'replay_date format galat'}, status=400)

    day_start, day_end = _day_range_utc(date_obj)

    # ✅ EK HI QUERY — poore din ka data
    qs = (
        OptionChain.objects
        .filter(Symbol=symbol, Time__gte=day_start, Time__lte=day_end)
        .order_by('Time', 'Strike_Price')
        .values('Time', *TICK_FIELDS)
    )
    if expiry_date:
        qs = qs.filter(Expiry_Date=expiry_date)

    # ── Group by second (IST) ──
    ticks_raw   = defaultdict(dict)   # ts_key → {strike: row}
    spot_by_ts  = {}
    order       = []                  # timestamp order preserve karo

    for row in qs:
        ts_key = row['Time'].astimezone(IST).strftime('%Y-%m-%d %H:%M:%S')
        sp = round(row['Strike_Price'], 1) if row['Strike_Price'] else None
        if sp is None:
            continue

        if ts_key not in ticks_raw:
            ticks_raw[ts_key] = {}
            order.append(ts_key)
        if sp not in ticks_raw[ts_key]:          # duplicate strike skip
            ticks_raw[ts_key][sp] = row
        if ts_key not in spot_by_ts and row['Spot_Price']:
            spot_by_ts[ts_key] = row['Spot_Price']

    if not order:
        return JsonResponse({'timestamps': [], 'total_ticks': 0, 'ticks': {}})

    # ── Har tick ke liye nearest ±15 strikes window ──
    result = {}
    for ts_key in order:
        seen        = ticks_raw[ts_key]
        spot        = spot_by_ts.get(ts_key)
        all_strikes = sorted(seen.keys())

        if spot and all_strikes:
            nearest_idx = min(range(len(all_strikes)),
                              key=lambda i: abs(all_strikes[i] - spot))
            s = max(0, nearest_idx - STRIKES_EACH_SIDE)
            e = min(len(all_strikes) - 1, nearest_idx + STRIKES_EACH_SIDE)
            show = all_strikes[s: e + 1]
        else:
            show = all_strikes

        result[ts_key] = {
            'spot_price': spot,
            'rows': [
                {k: _fmt(seen[sp][k]) for k in TICK_FIELDS if k != 'Spot_Price'}
                for sp in show
            ],
        }

    return JsonResponse({
        'timestamps' : order,
        'total_ticks': len(order),
        'ticks'      : result,
    })


# ─────────────────────────────────────────────────────────────
#  DATES (unchanged logic, DB-level grouping)
# ─────────────────────────────────────────────────────────────
def get_replay_dates(request):
    symbol = request.GET.get('symbol', '').strip()
    import pytz
    ist_tz = pytz.timezone('Asia/Kolkata')
    qs = OptionChain.objects.all()
    if symbol:
        qs = qs.filter(Symbol=symbol)
    dates = (
        qs.annotate(d=TruncDate('Time', tzinfo=ist_tz))
          .values_list('d', flat=True)
          .distinct()
          .order_by('d')
    )
    return JsonResponse({'dates': [d.isoformat() for d in dates if d]})


# ─────────────────────────────────────────────────────────────
#  SYMBOL CHANGE ke liye (dates fetch)
# ─────────────────────────────────────────────────────────────
def get_replay_timestamps(request):
    """Backward compat — ab get_replay_bulk use hota hai."""
    return get_replay_bulk(request)



def get_replay_tick(request):
    """Backward compat — bulk mein sab aata hai."""
    return JsonResponse({'rows': [], 'spot_price': None, 'timestamp': ''})
