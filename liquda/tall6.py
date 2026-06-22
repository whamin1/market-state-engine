import os

import requests
from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, "../.env")
load_dotenv(ENV_PATH)


telegram_token = os.getenv("TELEGRAM4_TOKEN")
telegram_chat = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_message(message):
    if not telegram_token or not telegram_chat:
        print("telegram send failed: missing TELEGRAM4_TOKEN or TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": telegram_chat, "text": message}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"telegram send failed: {response.text}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"telegram send error: {exc}")
        return False
