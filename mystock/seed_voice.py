import json
from groq import Groq
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .monika_call import system_instruction

# ── Helper: Groq से Monica का जवाब ────────────────────
def get_ai_reply(user_message, history=None):
    # API Key चेक करें
    api_key = getattr(settings, 'GROQ_API_KEY', None)
    if not api_key:
        return "क्षमा करें, API Key सेट नहीं है।"

    client = Groq(api_key=api_key)
    
    # सिस्टम निर्देश (मोनिका का व्यक्तित्व)
    messages = [{"role": "system", "content": system_instruction}]
    
    # पुरानी बातें (History) - सिर्फ पिछली 10 बातें याद रखने के लिए
    if history:
        messages += history[-10:]
        
    # नया मैसेज
    messages.append({"role": "user", "content": user_message})
    
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            # model="llama-3.3-70b-versatile",
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