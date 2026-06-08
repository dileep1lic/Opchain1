from django.core.cache import cache
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.contrib import messages

class KickOutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # अगर यूजर लॉगिन है, तभी चेक करें
        if request.user.is_authenticated:
            current_session_key = request.session.session_key
            
            # Cache से निकालें कि इस यूजर का सबसे लेटेस्ट लॉगिन सेशन कौन सा है
            latest_session_key = cache.get(f"user_login_{request.user.pk}")
            
            # अगर लेटेस्ट सेशन मौजूद है और वह मौजूदा (Current) ब्राउज़र के सेशन से अलग है...
            if latest_session_key and current_session_key != latest_session_key:
                # ...तो इसका मतलब है कि यूजर ने कहीं और (नए डिवाइस पर) लॉगिन कर लिया है।
                logout(request) # तुरंत पुराने वाले को लॉगआउट करें
                messages.error(request, "⚠️ आपका अकाउंट किसी दूसरे डिवाइस पर लॉगिन हो गया है, इसलिए आपको यहाँ से लॉगआउट कर दिया गया है।")
                return redirect('login')

        return self.get_response(request)