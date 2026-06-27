import json
from google import genai
from google.genai import types
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
from .models import PaperTrade, BotSettings
from django.db.models import Sum, Count, Avg, Q
from datetime import timedelta

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

            pts_to_put = "पता नहीं"
            pts_to_call = "पता नहीं"
            try:
                if spot and master_levels.get("R", {}).get("entry"):
                    pts_to_put = str(round(float(master_levels["R"]["entry"]) - float(spot), 1))
                if spot and master_levels.get("S", {}).get("entry"):
                    pts_to_call = str(round(float(spot) - float(master_levels["S"]["entry"]), 1))
            except: pass

            # 🚀 यहाँ हमने साफ़ लिख दिया है कि यह डेटा केवल निफ़्टी का है
            return (
                f"[सिस्टम निर्देश: यह डेटा केवल और केवल निफ्टी (NIFTY) का है। "
                f"निफ्टी का रेजिस्टेंस लेवल {r_strike} है, स्टेटस '{r_status_clean}' है, और पुट ट्रेड की एंट्री {r_entry} है। "
                f"निफ्टी का सपोर्ट लेवल {s_strike} है, स्टेटस '{s_status_clean}' है, and कॉल ट्रेड की एंट्री {s_entry} है। "
                f"निफ्टी का स्पॉट प्राइस {spot_text} है। "
                f"पुट (PUT) ट्रेड की एंट्री आने में {pts_to_put} पॉइंट्स बचे हैं। "
                f"कॉल (CALL) ट्रेड की एंट्री आने में {pts_to_call} पॉइंट्स बचे हैं। "
                f"अगर कोई बैंकनिफ्टी या अन्य स्टॉक का लेवल पूछे, तो इस डेटा का इस्तेमाल न करें और नियम 5 के अनुसार मना कर दें।]"
                # f"प्राइस बताने के तुरंत बाद यह चेतावनी जरूर दें: ध्यान दें यह एक सिस्टम जनरेटेड डेटा है। डेटा या सुझावों में गलती हो सकती है। किसी भी ट्रेड या निवेश निर्णय से पहले खुद जाँच अवश्य करें। "
            )
    except Exception as e:
        print(f"Market Context Error: {e}")
        
    return "[सिस्टम जानकारी: लाइव मार्केट का डेटा अभी उपलब्ध नहीं है।]"


# ── Helper: आज की Trades का Context ────────────────────────
def get_today_trades_context():
    """आज की सभी trades का सारांश — Monika को बताने के लिए"""
    try:
        today = timezone.now().date()
        trades = PaperTrade.objects.filter(trade_date=today).order_by('entry_time')

        if not trades.exists():
            return "[आज (Today) की ट्रेड जानकारी: आज अभी तक कोई पेपर ट्रेड नहीं हुई है। (लेकिन पिछले दिनों की ट्रेड्स का डेटा तुम्हारे पास मौजूद है)]"

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
            bot = BotSettings.objects.first()
            target_pts = bot.default_target if bot else 50.0
            sl_pts = bot.default_sl if bot else 50.0
            extra_open = ""

            if result == 'OPEN':
                emoji, label = '🔄', 'OPEN'
                open_trades.append(i)
                pnl_pts = 0.0; pnl_rs = 0.0
                try:
                    if tr.trade_type == 'CALL':
                        tgt = float(tr.entry_spot) + target_pts
                        sl = float(tr.entry_spot) - sl_pts
                    else:
                        tgt = float(tr.entry_spot) - target_pts
                        sl = float(tr.entry_spot) + sl_pts
                    extra_open = f" | Target: {round(tgt,1)}, SL: {round(sl,1)}"
                except: pass
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
                f"PnL: {pnl_sign}{safe_num(pnl_pts)} pts ({pnl_sign}₹{safe_num(abs(pnl_rs))}){extra_open}"
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


def get_all_stats_context():
    try:
        today = timezone.now().date()
        yesterday = today - timedelta(days=1)
        week_ago = today - timedelta(days=7)
        month_ago = today - timedelta(days=30)
        
        # All time
        all_stats = PaperTrade.objects.exclude(result__in=['OPEN','SKIPPED']).aggregate(
            t=Count('id'), w=Count('id', filter=Q(result='TARGET')), 
            pnl=Sum('pnl'), rs=Sum('pnl_rupees')
        )
        
        # Week
        week_stats = PaperTrade.objects.filter(trade_date__gte=week_ago).exclude(result__in=['OPEN','SKIPPED']).aggregate(
            t=Count('id'), w=Count('id', filter=Q(result='TARGET')), 
            pnl=Sum('pnl'), rs=Sum('pnl_rupees')
        )
        
        # Month
        month_stats = PaperTrade.objects.filter(trade_date__gte=month_ago).exclude(result__in=['OPEN','SKIPPED']).aggregate(
            t=Count('id'), w=Count('id', filter=Q(result='TARGET')), 
            pnl=Sum('pnl'), rs=Sum('pnl_rupees')
        )
        
        # Yesterday's exact trades
        yt_trades = PaperTrade.objects.filter(trade_date=yesterday).exclude(result__in=['OPEN','SKIPPED'])
        yt_str = ""
        if yt_trades.exists():
            yt_lines = []
            for tr in yt_trades:
                res = "प्रॉफिट" if tr.result == "TARGET" else ("लॉस" if tr.result == "SL" else tr.result)
                yt_lines.append(f"{tr.trade_type} में {res} ({tr.pnl} pts)")
            yt_str = "कल की ट्रेड्स: " + ", ".join(yt_lines)
        else:
            yt_str = "कल कोई ट्रेड नहीं हुई थी।"

        best = PaperTrade.objects.filter(result='TARGET').order_by('-pnl').first()
        worst = PaperTrade.objects.filter(result='SL').order_by('pnl').first()
        
        best_str = f"{best.symbol} {best.trade_type} (+{best.pnl} pts)" if best else "कोई नहीं"
        worst_str = f"{worst.symbol} {worst.trade_type} ({worst.pnl} pts)" if worst else "कोई नहीं"
        
        bot = BotSettings.objects.first()
        bot_status = "ON" if bot and bot.trading_enabled else "OFF"
        
        return (
            f"[ऑल-टाइम परफॉरमेंस: कुल {all_stats['t'] or 0} ट्रेड्स, {all_stats['w'] or 0} प्रॉफिट। कुल PnL: {round(all_stats['pnl'] or 0,1)} pts (₹{round(all_stats['rs'] or 0, 0)})\n"
            f"इस हफ्ते की परफॉरमेंस: कुल {week_stats['t'] or 0} ट्रेड्स, {week_stats['w'] or 0} प्रॉफिट। PnL: {round(week_stats['pnl'] or 0,1)} pts (₹{round(week_stats['rs'] or 0, 0)})\n"
            f"इस महीने की परफॉरमेंस: कुल {month_stats['t'] or 0} ट्रेड्स, {month_stats['w'] or 0} प्रॉफिट। PnL: {round(month_stats['pnl'] or 0,1)} pts (₹{round(month_stats['rs'] or 0, 0)})\n"
            f"{yt_str}\n"
            f"अब तक की सबसे अच्छी ट्रेड: {best_str}\n"
            f"अब तक की सबसे खराब ट्रेड: {worst_str}\n"
            f"ऑटो-बॉट स्टेटस: {bot_status}]"
        )
    except Exception as e:
        print(f"Stats Context Error: {e}")
        return ""

def get_current_date_context():
    """आज की तारीख और समय Monika को बताने के लिए"""
    try:
        now = timezone.now()
        local_time = timezone.localtime(now)
        date_str = local_time.strftime("%d %B %Y")
        time_str = local_time.strftime("%I:%M %p")
        return f"[सिस्टम जानकारी: आज की तारीख {date_str} है, और अभी का समय {time_str} है।]"
    except Exception as e:
        print(f"Date Context Error: {e}")
        return ""

def get_gemini_contents(user_message, history=None, user_name=None):
    """Gemini के लिए contents list तैयार करना"""
    user_ctx = f"[उपयोगकर्ता जानकारी: इस चैट विंडो में बात करने वाले व्यक्ति का नाम '{user_name}' है। इन्हें नाम लेकर संबोधित करो।]" if user_name else ""
    sys_inst = f"{system_instruction}\n\n{user_ctx}\n\n{get_current_date_context()}\n\n{get_live_market_context()}\n\n{get_all_stats_context()}\n\n{get_today_trades_context()}"
    
    formatted_contents = []
    if history:
        for h in history[-10:]:
            role = 'user' if h['role'] == 'user' else 'model'
            formatted_contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h['content'])]))
    
    formatted_contents.append(types.Content(role='user', parts=[types.Part.from_text(text=user_message)]))
    
    return sys_inst, formatted_contents



def get_groq_messages(user_message, history=None, user_name=None):
    """Groq के लिए messages list तैयार करना"""
    messages = [{"role": "system", "content": system_instruction}]
    if user_name:
        messages.append({"role": "system", "content": f"[उपयोगकर्ता जानकारी: इस चैट विंडो में बात करने वाले व्यक्ति का नाम '{user_name}' है। इन्हें नाम लेकर संबोधित करो।]"})
    messages.append({"role": "system", "content": get_current_date_context()})
    messages.append({"role": "system", "content": get_live_market_context()})
    messages.append({"role": "system", "content": get_all_stats_context()})
    messages.append({"role": "system", "content": get_today_trades_context()})
    if history:
        messages += history[-10:]
    messages.append({"role": "user", "content": user_message})
    return messages

# ── Helper: Gemini से Monika का जवाब (Non-Streaming) ────────────────────
def get_ai_reply(user_message, history=None, user_name=None):
    api_key = getattr(settings, 'GEMINI_API_KEY', None)
    if not api_key:
        return "क्षमा करें, API Key सेट नहीं है।"

    sys_inst, formatted_contents = get_gemini_contents(user_message, history, user_name)
    
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=formatted_contents,
            config=types.GenerateContentConfig(
                system_instruction=sys_inst,
                temperature=0.7,
                max_output_tokens=800,
            )
        )
        return response.text.strip()
    except Exception as e:
        print(f"Gemini Error (Falling back to Groq): {e}")
        try:
            from groq import Groq
            groq_key = getattr(settings, 'GROQ_API_KEY', None)
            if not groq_key:
                return "Gemini की लिमिट पूरी हो गई है और Groq API Key सेट नहीं है।"
            
            groq_client = Groq(api_key=groq_key)
            groq_messages = get_groq_messages(user_message, history)
            
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=groq_messages,
                temperature=0.7,
                max_tokens=800,
            )
            return response.choices[0].message.content.strip()
        except Exception as groq_e:
            print(f"Groq Fallback Error: {groq_e}")
            return "क्षमा करें, अभी नेटवर्क या लिमिट में समस्या है। कृपया थोड़ी देर बाद प्रयास करें।"

# ── Page ──────────────────────────────────────────────
def index(request):
    return render(request, 'index.html')


# ─────────────────────────────────────────────────────
# Helper: logged-in user का नाम DB से लेना
# Priority: first_name > username > BotSettings.user_name > 'जी'
# ─────────────────────────────────────────────────────
def get_logged_in_user_name(request):
    try:
        user = request.user
        if user and user.is_authenticated:
            if user.first_name and user.first_name.strip():
                return user.first_name.strip()
            if user.username and user.username.strip():
                return user.username.strip()
    except Exception:
        pass
    try:
        bot = BotSettings.objects.first()
        if bot and bot.user_name and bot.user_name.strip():
            return bot.user_name.strip()
    except Exception:
        pass
    return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GET USER NAME API — Frontend को DB से नाम देना
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@require_http_methods(["GET"])
def get_user_name_api(request):
    """लॉगिन यूजर का नाम DB से लेकर JSON में देना"""
    name = get_logged_in_user_name(request)
    return JsonResponse({'user_name': name or ''})

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
        data      = json.loads(request.body)
        message   = data.get('message', '').strip()
        history   = data.get('history', [])
        # DB से नाम लें (लॉगिन यूजर से) — frontend से आए नाम को override करें
        user_name = get_logged_in_user_name(request)

        if not message:
            def error_gen():
                yield 'data: ' + json.dumps({'error': 'Message खाली है'}) + '\n\n'
            return StreamingHttpResponse(error_gen(), content_type='text/event-stream')

        api_key = getattr(settings, 'GEMINI_API_KEY', None)
        if not api_key:
            def error_gen():
                yield 'data: ' + json.dumps({'error': 'API Key सेट नहीं है।'}) + '\n\n'
            return StreamingHttpResponse(error_gen(), content_type='text/event-stream')

    except Exception as e:
        def error_gen():
            yield 'data: ' + json.dumps({'error': str(e)}) + '\n\n'
        return StreamingHttpResponse(error_gen(), content_type='text/event-stream')

    def sse_generator():
        """Gemini Streaming से tokens yield करना (with Groq Fallback)"""
        sys_inst, formatted_contents = get_gemini_contents(message, history, user_name)
        full_reply = ""
        fallback_to_groq = False

        try:
            client = genai.Client(api_key=api_key)
            stream = client.models.generate_content_stream(
                model="gemini-2.5-flash",
                contents=formatted_contents,
                config=types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.7,
                    max_output_tokens=800,
                )
            )

            for chunk in stream:
                token = chunk.text
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
            print(f"Gemini Streaming Error: {e}")
            if not full_reply: # सिर्फ तभी fallback करें जब कोई response शुरू ना हुआ हो
                fallback_to_groq = True
            else:
                yield 'data: ' + json.dumps({'error': f'नेटवर्क में समस्या: {str(e)}'}, ensure_ascii=False) + '\n\n'

        if fallback_to_groq:
            print("Falling back to Groq Streaming...")
            try:
                from groq import Groq
                groq_key = getattr(settings, 'GROQ_API_KEY', None)
                if not groq_key:
                    yield 'data: ' + json.dumps({'error': 'Gemini लिमिट पूरी हो गई है और Groq Key सेट नहीं है।'}, ensure_ascii=False) + '\n\n'
                    return
                
                groq_client = Groq(api_key=groq_key)
                groq_messages = get_groq_messages(message, history, user_name)
                
                stream = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=groq_messages,
                    temperature=0.7,
                    max_tokens=800,
                    stream=True,
                )
                
                for chunk in stream:
                    token = chunk.choices[0].delta.content
                    if token:
                        full_reply += token
                        yield 'data: ' + json.dumps({'token': token}, ensure_ascii=False) + '\n\n'
                        
                updated_history = history + [
                    {"role": "user",      "content": message},
                    {"role": "assistant", "content": full_reply.strip()},
                ]
                yield 'data: ' + json.dumps({
                    'done': True,
                    'reply': full_reply.strip(),
                    'history': updated_history
                }, ensure_ascii=False) + '\n\n'
                
            except Exception as groq_e:
                print(f"Groq Streaming Error: {groq_e}")
                yield 'data: ' + json.dumps({'error': f'नेटवर्क या लिमिट में समस्या (Groq Fallback Failed): {str(groq_e)}'}, ensure_ascii=False) + '\n\n'

    response = StreamingHttpResponse(sse_generator(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'   # Nginx buffering बंद करें
    return response