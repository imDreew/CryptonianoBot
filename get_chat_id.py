import os
import requests
import time

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("⚠️ Devi impostare TELEGRAM_BOT_TOKEN nelle variabili d'ambiente.")

URL = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

print("✅ Script avviato. Scrivi un messaggio al tuo bot su Telegram...")

while True:
    resp = requests.get(URL)
    data = resp.json()

    if "result" in data and len(data["result"]) > 0:
        for update in data["result"]:
            try:
                chat_id = update["message"]["chat"]["id"]
                chat_type = update["message"]["chat"]["type"]
                print(f"📌 Chat ID trovato: {chat_id} (tipo: {chat_type})")
                exit(0)
            except KeyError:
                continue

    time.sleep(2)
