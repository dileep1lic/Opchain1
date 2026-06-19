import json
from groq import Groq
from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods 
from django.utils import timezone

# अपने views.py से लाइव डेटा वाले फ़ंक्शन्स इम्पोर्ट करें
import re
from .views import get_master_levels, cache 
# from .my_deta import system_instruction
from .monika_call import system_instruction
from .models import PaperTrade

# ── Helper: Live Market Context (अपडेटेड फ़ंक्शन) ────────
def get_live_market_context():
    """बैकएंड से लाइव निफ्टी लेवल्स निकालकर साफ शब्दों में मोनिका को देना"""
    try:
        today = timezone.now().date()
        master_levels = get_master_levels('NIFTY', today)
        spot = cache.get('live_nifty_spot_NIFTY')
        
        # नंबर से '.0' हटाने का सुरक्षित तरीका
        def safe_int(val):
            try:
                return str(int(float(val)))
            except (ValueError, TypeError):
                return "उपलब्ध नहीं"

        # स्टेटस को साफ करने का लॉजिक
        def clean_status(raw_status):
            text = re.sub(r'(?i)(Resistance|Support)\s*\([^)]*\)\s*', '', raw_status).strip()
            if 'STRONG' in text.upper():
                return 'Strong'
            return text

        if master_levels:
            r_strike = safe_int(master_levels.get("R", {}).get("strike"))
            r_entry  = safe_int(master_levels.get("R", {}).get("entry"))
            r_status_clean = clean_status(master_levels.get("R", {}).get("status", ""))
            
            s_strike = safe_int(master_levels.get("S", {}).get("strike"))
            s_entry  = safe_int(master_levels.get("S", {}).get("entry"))
            s_status_clean = clean_status(master_levels.get("S", {}).get("status", ""))

            spot_text = safe_int(spot) if spot else "अभी अपडेट नहीं हुआ"

            # 🚀 यहाँ हमने साफ़ लिख दिया है कि यह डेटा केवल निफ़्टी का है
            return (
                f"[सिस्टम निर्देश: यह डेटा केवल और केवल निफ्टी (NIFTY) का है। "
                f"निफ्टी का रेजिस्टेंस लेवल {r_strike} है, स्टेटस '{r_status_clean}' है, और पुट ट्रेड की एंट्री {r_entry} है। "
                f"निफ्टी का सपोर्ट लेवल {s_strike} है, स्टेटस '{s_status_clean}' है, and कॉल ट्रेड की एंट्री {s_entry} है। "
                f"निफ्टी का स्पॉट प्राइस {spot_text} है। "
                f"अगर कोई बैंकनिफ्टी या अन्य स्टॉक का लेवल पूछे, तो इस डेटा का इस्तेमाल न करें और नियम 5 के अनुसार मना कर दें।]"
            )
    except Exception as e:
        print(f"Market Context Error: {e}")
        
    return "[सिस्टम जानकारी: लाइव मार्केट का डेटा अभी उपलब्ध नहीं है।]"


# ── Helper: आज की Trades का Context ────────────────────────
def get_today_trades_context():
    """आज की सभी trades का सारांश — Monica को बताने के लिए"""
    try:
        today = timezone.now().date()
        trades = PaperTrade.objects.filter(trade_date=today).order_by('entry_time')

        if not trades.exists():
            return "[ट्रेड जानकारी: आज अभी तक कोई पेपर ट्रेड नहीं हुई है।]"

        def safe_num(v, decimals=0):
            try:
                return str(round(float(v), decimals)) if v is not None else "—"
            except:
                return "—"

        total_pnl_pts  = 0.0
        total_pnl_rs   = 0.0
        trade_lines    = []
        profit_trades  = []
        loss_trades    = []
        open_trades    = []
        skipped_trades = []

        for i, tr in enumerate(trades, 1):
            pnl_pts = float(tr.pnl) if tr.pnl is not None else 0.0
            pnl_rs  = float(tr.pnl_rupees) if tr.pnl_rupees is not None else 0.0
            result  = tr.result or 'OPEN'

            # emoji + label
            if result == 'TARGET':
                emoji, label = '✅', 'प्रॉफिट'
                profit_trades.append(i)
            elif result == 'SL':
                emoji, label = '❌', 'लॉस'
                loss_trades.append(i)
            elif result == 'OPEN':
                emoji, label = '🔄', 'OPEN'
                open_trades.append(i)
                pnl_pts = 0.0; pnl_rs = 0.0
            elif result == 'SKIPPED':
                emoji, label = '⏭', 'SKIP'
                skipped_trades.append(i)
                pnl_pts = 0.0; pnl_rs = 0.0
            else:
                emoji, label = '📌', result

            entry_t = tr.entry_time.strftime('%H:%M') if tr.entry_time else '—'
            pnl_sign = '+' if pnl_pts >= 0 else ''

            line = (
                f"ट्रेड {i}: {tr.symbol} | {tr.trade_type} | {tr.trigger_level}-लेवल | "
                f"एंट्री {safe_num(tr.entry_spot)} @ {entry_t} | "
                f"रिज़ल्ट: {emoji}{label} | "
                f"PnL: {pnl_sign}{safe_num(pnl_pts)} pts ({pnl_sign}₹{safe_num(abs(pnl_rs))})"
            )
            trade_lines.append(line)

            if result not in ('OPEN', 'SKIPPED'):
                total_pnl_pts += pnl_pts
                total_pnl_rs  += pnl_rs

        # Summary
        total_sign = '+' if total_pnl_pts >= 0 else ''
        summary_parts = []
        if profit_trades:
            summary_parts.append(f"प्रॉफिट ट्रेड्स: {profit_trades}")
        if loss_trades:
            summary_parts.append(f"लॉस ट्रेड्स: {loss_trades}")
        if open_trades:
            summary_parts.append(f"OPEN ट्रेड्स: {open_trades}")
        if skipped_trades:
            summary_parts.append(f"SKIP ट्रेड्स: {skipped_trades}")

        trades_text = '\n'.join(trade_lines)
        summary = ', '.join(summary_parts)

        return (
            f"[आज की ट्रेड रिपोर्ट ({today}):\n"
            f"{trades_text}\n"
            f"कुल PnL: {total_sign}{round(total_pnl_pts,1)} pts ({total_sign}₹{round(abs(total_pnl_rs),0)})\n"
            f"{summary}\n"
            f"नोट: यह डेटा केवल पेपर ट्रेड का है। इसे अपनी ट्रेड रिपोर्ट के लिए use करो।]"
        )
    except Exception as e:
        print(f"Trade Context Error: {e}")
        return "[ट्रेड जानकारी: आज की ट्रेड डेटा अभी उपलब्ध नहीं है।]"

def build_messages(user_message, history=None):
    """Monica के लिए messages list तैयार करना (streaming और non-streaming दोनों में काम आता है)"""
    messages = [{"role": "system", "content": system_instruction}]

    # 1. लाइव मार्केट लेवल्स
    market_context = get_live_market_context()
    messages.append({"role": "system", "content": market_context})

    # 2. आज की ट्रेड रिपोर्ट — Monica को हमेशा पता रहे
    trade_context = get_today_trades_context()
    messages.append({"role": "system", "content": trade_context})

    if history:
        messages += history[-10:]
    messages.append({"role": "user", "content": user_message})
    return messages



# ── Helper: Groq से Monica का जवाब (Non-Streaming) ────────────────────
def get_ai_reply(user_message, history=None):
    # API Key चेक करें
    api_key = getattr(settings, 'GROQ_API_KEY', None)
    if not api_key:
        return "क्षमा करें, API Key सेट नहीं है।"

    client = Groq(api_key=api_key)
    messages = build_messages(user_message, history)
    
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=150, # फोन पर बात करने के लिए छोटे जवाब
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq Error: {e}")
        return "क्षमा करें, अभी नेटवर्क में थोड़ी समस्या है। क्या आप अपनी बात दोहरा सकते हैं?"

# ── Page ──────────────────────────────────────────────
def index(request):
    return render(request, 'index.html')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WEB CHAT API (Non-Streaming — Fallback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@csrf_exempt
@require_http_methods(["POST"])
def voice_chat_api(request):
    try:
        data    = json.loads(request.body)
        message = data.get('message', '').strip()
        history = data.get('history', [])
        
        if not message:
            return JsonResponse({'success': False, 'error': 'Message खाली है'}, status=400)
            
        # AI से जवाब लें
        reply = get_ai_reply(message, history)
        
        # हिस्ट्री अपडेट करें
        updated_history = history + [
            {"role": "user",      "content": message},
            {"role": "assistant", "content": reply},
        ]
        
        return JsonResponse({
            'success': True, 
            'reply': reply,
            'history': updated_history
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🚀 STREAMING CHAT API (SSE — Real-time Tokens)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@csrf_exempt
@require_http_methods(["POST"])
def voice_chat_stream(request):
    """
    Server-Sent Events (SSE) endpoint.
    हर token real-time में frontend को भेजा जाता है।
    
    SSE Format:
        data: {"token": "..."}\n\n         ← हर word/token
        data: {"done": true, "history": [...]}\n\n  ← अंत में
        data: {"error": "..."}\n\n         ← error की स्थिति में
    """
    try:
        data    = json.loads(request.body)
        message = data.get('message', '').strip()
        history = data.get('history', [])

        if not message:
            def error_gen():
                yield 'data: ' + json.dumps({'error': 'Message खाली है'}) + '\n\n'
            return StreamingHttpResponse(error_gen(), content_type='text/event-stream')

        api_key = getattr(settings, 'GROQ_API_KEY', None)
        if not api_key:
            def error_gen():
                yield 'data: ' + json.dumps({'error': 'API Key सेट नहीं है।'}) + '\n\n'
            return StreamingHttpResponse(error_gen(), content_type='text/event-stream')

    except Exception as e:
        def error_gen():
            yield 'data: ' + json.dumps({'error': str(e)}) + '\n\n'
        return StreamingHttpResponse(error_gen(), content_type='text/event-stream')

    def sse_generator():
        """Groq Streaming से tokens yield करना"""
        client = Groq(api_key=api_key)
        messages = build_messages(message, history)
        full_reply = ""

        try:
            stream = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=150,
                stream=True,  # 🚀 Streaming ON
            )

            for chunk in stream:
                token = chunk.choices[0].delta.content
                if token:
                    full_reply += token
                    # हर token SSE format में भेजें
                    yield 'data: ' + json.dumps({'token': token}, ensure_ascii=False) + '\n\n'

            # Stream पूरा होने पर updated history भेजें
            updated_history = history + [
                {"role": "user",      "content": message},
                {"role": "assistant", "content": full_reply.strip()},
            ]
            yield 'data: ' + json.dumps({
                'done': True,
                'reply': full_reply.strip(),
                'history': updated_history
            }, ensure_ascii=False) + '\n\n'

        except Exception as e:
            print(f"Groq Streaming Error: {e}")
            yield 'data: ' + json.dumps({'error': f'नेटवर्क में समस्या: {str(e)}'}, ensure_ascii=False) + '\n\n'

    response = StreamingHttpResponse(sse_generator(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'   # Nginx buffering बंद करें
    return response