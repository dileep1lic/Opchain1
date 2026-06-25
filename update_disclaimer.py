import os
import re

files = [
    "/home/dileep/Documents/Opchain1/mystock/templates/index.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/journal/journal_list.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/mystock/chart.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/mystock/sr_data_page.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/mystock/all_stocks.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/mystock/backtesta.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/mystock/search_dashboard.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/mystock/trade_dashboard.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/mystock/resistance_dashboard.html",
    "/home/dileep/Documents/Opchain1/mystock/templates/voice_commands.html"
]

new_disclaimer = """    ⚠️ <strong>ध्यान दें: </strong> यह AI-जनरेटेड डेटा है, जिसमें गलतियां हो सकती हैं। हम SEBI-पंजीकृत सलाहकार नहीं हैं। कृपया निवेश या ट्रेडिंग से पहले अपनी खुद की रिसर्च अवश्य करें। बाज़ार के जोखिमों की ज़िम्मेदारी आपकी स्वयं की होगी।
    &nbsp;&nbsp;&nbsp;&nbsp;⚠️ <strong>Disclaimer: </strong> This is AI-generated data and may contain errors. We are not SEBI-registered advisors. Please do your own research before investing or trading. You are solely responsible for any market risks."""

pattern = re.compile(r'(<span class="disclaimer-ticker">)(.*?)(</span>)', re.DOTALL)

for filepath in files:
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        def replacer(match):
            return match.group(1) + '\n' + new_disclaimer + '\n  ' + match.group(3)
            
        new_content, count = pattern.subn(replacer, content)
        
        if count > 0:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Updated {filepath}")
        else:
            print(f"No match found in {filepath}")
