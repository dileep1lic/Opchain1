# token_manager.py

import time
import schedule
import requests
import urllib.parse
import pyotp as otp
from datetime import datetime
import random
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
# Import-Module PSReadLine

# from  utils.logger_config import get_logger
from  utils.telegram_bot import send_message_telegram, send_file_telegram
from  credentials import cliecnt_id, client_secret, redirect_uri, totp_key, USERNAME, PIN, access_token, auth_url, token_url, code

# chromedriver_path = chromedriver

def fetch_access_token_auto(request=None):
    start_time = time.time()  # 🔹 Start stopwatch

    def get_totp():
        return otp.TOTP(totp_key).now()
    
    def wait_for_element(by, value, timeout=10):
        return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, value)))

    # ✅ Build the auth URL
    # auth_url = f'{auth_url}?response_type=code&client_id={cliecnt_id}&redirect_uri={redirect_uri}'
    auth_url = "https://login.upstox.com/login/v2/oauth/authorize?redirect_uri=https://api-v2.upstox.com/login/authorization/redirect&response_type=code&client_id=IND-nyjv70u9xcg2t8e3hkrpdm5b&user_id=p6Loaye4jr2YnJcubqSkOg&user_type=individual"

    # ✅ Set up Chrome
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")     # Comment this to see browser
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    # driver = webdriver.Chrome(service=Service(chrome_driver_path), options=chrome_options)

   


    try:
        print("... Start Fetching Access Token ...")
        print("🚀 Opening Upstox login page...")
        driver.get(auth_url)

        print("📱 Entering mobile number...")
        wait_for_element(By.ID, "mobileNum").send_keys(USERNAME)
        
        print("🔘 Clicking 'Get OTP' button...")
        wait_for_element(By.ID, "getOtp").click()

        print("🔢 Waiting for OTP input box...")
        otp_input = wait_for_element(By.ID, "otpNum")

        print("🔢 Getting & Entering TOTP...")
        totp = get_totp().strip()
        print("✅ Got TOTP: %s", totp)
        otp_input.send_keys(totp)

        print("➡️ Clicking 'Continue' Button...")
        wait_for_element(By.ID, "continueBtn").click()

        print("🔢 Entering PIN...")
        wait_for_element(By.ID, "pinCode").send_keys(PIN)

        print("➡️ Clicking 'Continue' Button...")
        wait_for_element(By.ID, "pinContinueBtn").click()
        
        print("🔁 Waiting for redirect...")
        WebDriverWait(driver, 15).until(lambda d: "code=" in d.current_url)
        current_url = driver.current_url
        print(f"📦 Redirected URL: %s", current_url)

        # ✅ Extract auth_code
        parsed_url = urllib.parse.urlparse(current_url)
        auth_code = urllib.parse.parse_qs(parsed_url.query).get("code", [None])[0]
        print(f"✅ Extracted Auth Code: %s", auth_code)

        if not auth_code:
            return {"error": "❌ Failed to extract auth_code from redirect URL"}

        print(f"✅ Extracted Auth Code: {auth_code}")
    

        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        payload = {
            'code': auth_code,
            'client_id': cliecnt_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        }

        response = requests.post(token_url, headers=headers, data=payload)

        if response.status_code == 200 and 'access_token' in response.json():
            access_token = response.json()['access_token']
            with open("access_token.txt", "w") as file:
                file.write(access_token)
            print("✅ Access token saved to 'access_token.txt'")

            # 🔔 Send to Telegram
            # send_message_telegram(f"✅ New Access Token:\n\n{access_token}")
            send_file_telegram("access_token.txt", caption="🗂️ Access Token File")

            return {
                "status": "success",
                "message": "✅ Access token saved and sent to Telegram",
                "access_token": access_token,
                "time_taken": f"{time.time() - start_time:.2f} sec"
            }


        else:
            error_msg = response.json()
            print(f"❌ Token fetch failed: {error_msg}")
            return {"error": error_msg}

    except Exception as e:
        print(f"⚠️ Exception occurred: {str(e)}")
        return {"error": str(e)}
    finally:
        driver.quit()

def run_token_fetch_with_retry():
    max_attempts = 10
    attempt = 1

    while attempt <= max_attempts:
        print(f"🔄 Attempt {attempt} to fetch access token...")
        result = fetch_access_token_auto()

        if result and result.get("status") == "success":
            print("✅ Token fetch successful.")
            send_message_telegram(f"✅ Access token updated successfully on attempt {attempt}.")
            return
        else:
            error_msg = result.get("error", "❌ Unknown error")
            print(f"⚠️ Error on attempt {attempt}: {error_msg}")
            send_message_telegram(f"⚠️ Error on attempt {attempt} while fetching access token:\n\n{error_msg}")
            attempt += 1
            time.sleep(15)  # 15 सेकंड रुकें और फिर से प्रयास करें

    send_message_telegram("❌ All attempts to fetch access token failed. Manual intervention needed.")




def schedule_token_fetch():
    # print("⏰ Scheduler Started: Will run daily at 08:00 AM...")
    # schedule.every().day.at("17:09").do(run_token_fetch_with_retry)

    mint = str(random.randint(0, 59)).zfill(2)  # रेंडम मिनट (00 से 59)
    time_str = f"17:{mint}"
    print(f"🕒 Random Scheduled Time: {time_str}")
    print("⏰ Scheduler Started: Will run daily at", time_str)

    schedule.every().day.at(time_str).do(run_token_fetch_with_retry)

    while True:
        schedule.run_pending()
        time.sleep(30)



# import time
# import requests
# import urllib.parse
# import pyotp as otp
# from datetime import datetime
# from selenium import webdriver
# from selenium.webdriver.common.by import By
# from selenium.webdriver.chrome.options import Options
# from selenium.webdriver.chrome.service import Service
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC

# # 🔹 आपके credentials & URLs
# # USERNAME = "YOUR_MOBILE_NUMBER"
# # PIN = "YOUR_PIN"
# # totp_key = "YOUR_TOTP_KEY"
# # cliecnt_id = "YOUR_CLIENT_ID"
# # client_secret = "YOUR_CLIENT_SECRET"
# # redirect_uri = "https://api-v2.upstox.com/login/authorization/redirect"
# # token_url = "https://api-v2.upstox.com/login/token"

# # 🔹 ChromeDriver path (प्रोजेक्ट फोल्डर में रखा हुआ)
# chrome_driver_path = r"./chromedriver.exe"

# # # 🔹 Telegram send functions (अपना already defined function use करें)
# # def send_message_telegram(message):
# #     # आपकी existing Telegram send code
# #     pass

# # def send_file_telegram(file_path, caption=""):
# #     # आपकी existing Telegram send file code
# #     pass

# # 🔹 Optimized fetch_access_token_auto
# def fetch_access_token_auto():
#     start_time = time.time()

#     def get_totp():
#         return otp.TOTP(totp_key).now()

#     def wait_for_element(by, value, timeout=5):
#         return WebDriverWait(driver, timeout).until(
#             EC.presence_of_element_located((by, value))
#         )

#     auth_url = f"https://login.upstox.com/login/v2/oauth/authorize?redirect_uri={redirect_uri}&response_type=code&client_id={cliecnt_id}&user_id={USERNAME}&user_type=individual"

#     # 🔹 Chrome setup
#     chrome_service = Service(chrome_driver_path)
#     chrome_options = Options()
#     chrome_options.add_argument("--headless=new")
#     chrome_options.add_argument("--no-sandbox")
#     chrome_options.add_argument("--disable-gpu")
#     chrome_options.add_argument("--disable-dev-shm-usage")
#     chrome_options.add_argument("--disable-extensions")
#     chrome_options.add_argument("--disable-infobars")
#     chrome_options.add_argument("--disable-blink-features=AutomationControlled")

#     driver = webdriver.Chrome(service=chrome_service, options=chrome_options)

#     try:
#         print("🚀 Opening Upstox login page...")
#         driver.get(auth_url)

#         wait_for_element(By.ID, "mobileNum").send_keys(USERNAME)
#         wait_for_element(By.ID, "getOtp").click()

#         otp_input = wait_for_element(By.ID, "otpNum")
#         totp = get_totp().strip()
#         otp_input.send_keys(totp)
#         driver.find_element(By.ID, "continueBtn").click()

#         wait_for_element(By.ID, "pinCode").send_keys(PIN)
#         driver.find_element(By.ID, "pinContinueBtn").click()

#         WebDriverWait(driver, 10).until(lambda d: "code=" in d.current_url)
#         current_url = driver.current_url
#         parsed_url = urllib.parse.urlparse(current_url)
#         auth_code = urllib.parse.parse_qs(parsed_url.query).get("code", [None])[0]

#         if not auth_code:
#             return {"error": "❌ Failed to extract auth_code"}

#         print(f"✅ Auth Code: {auth_code}")

#         headers = {
#             'accept': 'application/json',
#             'Content-Type': 'application/x-www-form-urlencoded',
#         }

#         payload = {
#             'code': auth_code,
#             'client_id': cliecnt_id,
#             'client_secret': client_secret,
#             'redirect_uri': redirect_uri,
#             'grant_type': 'authorization_code',
#         }

#         response = requests.post(token_url, headers=headers, data=payload)

#         if response.status_code == 200 and 'access_token' in response.json():
#             access_token = response.json()['access_token']
#             with open("access_token.txt", "w") as file:
#                 file.write(access_token)

#             send_file_telegram("access_token.txt", caption="🗂️ Access Token File")

#             return {
#                 "status": "success",
#                 "access_token": access_token,
#                 "time_taken": f"{time.time() - start_time:.2f} sec"
#             }

#         else:
#             return {"error": response.json()}

#     except Exception as e:
#         return {"error": str(e)}
#     finally:
#         driver.quit()


# # 🔹 Retry logic (optional)
# def run_token_fetch_with_retry():
#     max_attempts = 10
#     for attempt in range(1, max_attempts + 1):
#         print(f"🔄 Attempt {attempt} to fetch access token...")
#         result = fetch_access_token_auto()

#         if result and result.get("status") == "success":
#             print("✅ Token fetch successful.")
#             send_message_telegram(
#                 f"✅ Access token updated successfully in attempt {attempt}."
#             )
#             return
#         else:
#             error_msg = result.get("error", "❌ Unknown error")
#             print(f"⚠️ Error on attempt {attempt}: {error_msg}")
#             send_message_telegram(
#                 f"⚠️ Error on attempt {attempt} while fetching access token:\n\n{error_msg}"
#             )
#             time.sleep(5)  # ⏳ कम किया गया delay

#     send_message_telegram("❌ All attempts to fetch access token failed. Manual check needed.")

if __name__ == "__main__":
    run_token_fetch_with_retry()


