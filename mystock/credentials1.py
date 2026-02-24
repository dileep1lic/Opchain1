cliecnt_id = '62197240-1468-473c-b97f-dea31838a513'
client_secret = '5ht00u2ct3'
redirect_uri = 'https://api.upstox.com'
auth_url = 'https://login.upstox.com/login/v2/oauth/authorize?redirect_uri=https://api-v2.upstox.com/login/authorization/redirect'
token_url = 'https://api.upstox.com/v2/login/authorization/token'
code = 'Og43j4'

totp_key = "R75WHFO3LP3FXI3ZOX554ULFNXXI33QD"
USERNAME = "7597971997"
PIN = "258456"

# file = open("access_token.txt","r")
# access_token = file.read()
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # stock_market folder
TOKEN_PATH = os.path.join(BASE_DIR, "access_token.txt")

if not os.path.exists(TOKEN_PATH):
    raise FileNotFoundError(f"Token file not found at: {TOKEN_PATH}")

with open(TOKEN_PATH, "r") as file:
    access_token = file.read().strip()


#print(access_token)
chat_ID1 = '-1002754818102' #'5718090813' # '-4986552826'  #'5718090813'
api_key = '7879413772:AAFlGwXjAnFST2uxrii5zX29kpfaFIDhpMs'    # UserName = @MyStock2NotifierBot
chat_ID = '-1002941813204'# '-4986552826'

# import requests 164947958    170796764

# BOT_TOKEN = api_key

# url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
# response = requests.get(url)
# print(response.json())

# import sqlite3

# # 🔹 डेटाबेस का नाम
# db_name = "support_resistance.db"

# # 🔹 जिन टेबल्स को बचाना है उनका नाम
# tables_to_keep = {"Support_Stocks", "Resistance_Stocks"}

# # 🔹 डेटाबेस से कनेक्ट करें
# conn = sqlite3.connect(db_name)
# cursor = conn.cursor()

# # 🔹 सभी टेबल के नाम प्राप्त करें
# cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
# all_tables = {row[0] for row in cursor.fetchall()}

# # 🔹 डिलीट करने वाली टेबल्स का चयन
# tables_to_drop = all_tables - tables_to_keep

# # 🔹 डिलीट करें
# for table_name in tables_to_drop:
#     drop_query = f"DROP TABLE IF EXISTS {table_name}"
#     print(f"🗑️ Deleting table: {table_name}")
#     cursor.execute(drop_query)

# # 🔹 सेव करें और कनेक्शन बंद करें
# conn.commit()
# conn.close()

# print("✅ टेबल्स हटाए गए, केवल चुनी हुई टेबल्स बची हैं।")
