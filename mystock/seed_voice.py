import json
from groq import Groq
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods 
from django.utils import timezone

# अपने views.py से लाइव डेटा वाले फ़ंक्शन्स इम्पोर्ट करें
import re
from .views import get_master_levels, cache 
# from .my_deta import system_instruction
from .monika_call import system_instruction

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


# ── Helper: Groq से Monica का जवाब ────────────────────
def get_ai_reply(user_message, history=None):
    # API Key चेक करें
    api_key = getattr(settings, 'GROQ_API_KEY', None)
    if not api_key:
        return "क्षमा करें, API Key सेट नहीं है।"

    client = Groq(api_key=api_key)
    
    # 1. मोनिका का मुख्य व्यक्तित्व
    messages = [{"role": "system", "content": system_instruction}]
    
    # 2. 🚀 नया बदलाव: मोनिका को बैकएंड से लाइव लेवल्स बताना 🚀
    market_context = get_live_market_context()
    messages.append({"role": "system", "content": market_context})
    
    # 3. पुरानी बातें (History) - सिर्फ पिछली 10 बातें याद रखने के लिए
    if history:
        messages += history[-10:]
        
    # 4. नया मैसेज
    messages.append({"role": "user", "content": user_message})
    
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
# WEB CHAT API (VB-Cable और Phone Link के लिए)
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
    