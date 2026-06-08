
# file = open("access_token.txt","r")
# access_token = file.read()
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # stock_market folder
TOKEN_PATH = os.path.join(BASE_DIR, "access_token.txt")

if not os.path.exists(TOKEN_PATH):
    raise FileNotFoundError(f"Token file not found at: {TOKEN_PATH}")

with open(TOKEN_PATH, "r") as file:
    access_token = file.read().strip()
