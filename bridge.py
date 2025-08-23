import os
import sys
import logging
import requests
import asyncio
from telegram import Update
from telegram.ext import Application, MessageHandler, filters

# ====== VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")  # opzionale

# Mappatura hashtag → webhook Discord
DISCORD_WEBHOOKS = {
    "#ANALISI": os.getenv("DISCORD_ANALISI_WEBHOOK"),
    "#COPY_TRADING": os.getenv("DISCORD_COPY_WEBHOOK"),
    "#DISCUSSIONE": os.getenv("DISCORD_DISCUSSIONE_WEBHOOK"),
}

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Cache locale per sync cancellazioni
# telegram_msg_id → {"discord_id": int, "webhook": str}
MSG_MAP = {}


# ====== NOTIFICA ADMIN ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not TELEGRAM_ADMIN_CHAT_ID:
        return
    link = f"https://t.me/c/{str(TELEGRAM_SOURCE_CHAT_ID)[4:]}/{message_id}"
    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_ADMIN_CHAT_ID,
            text=f"‼️ERRORE‼️\nCausa: {cause}\nMessaggio: {link}"
        )
    except Exception as e:
        logging.error(f"Notifica admin fallita: {e}")


# ====== INOLTRO MESSAGGI ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    text = msg.text or msg.caption or ""
    target_webhook = None

    for hashtag, webhook in DISCORD_WEBHOOKS.items():
        if text.startswith(hashtag) and webhook:
            target_webhook = webhook
            break

    if not target_webhook:
        logging.info("Nessun hashtag valido, messaggio ignorato")
        return

    try:
        payload = {"content": text}
        response = requests.post(target_webhook, json=payload)
        if response.status_code == 200 or response.status_code == 204:
            discord_msg = response.json() if response.text else {}
            if "id" in discord_msg:
                MSG_MAP[msg.message_id] = {
                    "discord_id": discord_msg["id"],
                    "webhook": target_webhook
                }
            logging.info(f"Inoltrato a Discord: {text[:50]}")
        else:
            raise Exception(f"Errore HTTP {response.status_code}")
    except Exception as e:
        logging.error(f"Errore inoltro: {e}")
        await notify_admin(context, str(e), msg.message_id)


# ====== POLLING CANCELLAZIONI ======
async def sync_deletions(app):
    """Controlla periodicamente se messaggi Telegram sono stati cancellati."""
    bot = app.bot
    while True:
        try:
            history = await bot.get_chat_history(chat_id=TELEGRAM_SOURCE_CHAT_ID, limit=100)
            alive_ids = {m.message_id for m in history}

            for tg_id in list(MSG_MAP.keys()):
                if tg_id not in alive_ids:  # messaggio sparito → delete anche su Discord
                    data = MSG_MAP.pop(tg_id)
                    try:
                        del_url = f"{data['webhook']}/messages/{data['discord_id']}"
                        r = requests.delete(del_url)
                        logging.info(f"Cancellato messaggio Telegram {tg_id} anche su Discord ({r.status_code})")
                    except Exception as e:
                        logging.error(f"Errore cancellazione Discord: {e}")

        except Exception as e:
            logging.error(f"Errore polling cancellazioni: {e}")

        await asyncio.sleep(15)  # controlla ogni 15s


async def main():
    app = Application.builder().token("YOUR_TELEGRAM_BOT_TOKEN").build()

    app.add_handler(MessageHandler(filters.ALL, forward_message))

    # Avvia la task di sync deletions in background
    asyncio.create_task(sync_deletions(app))

    # Avvia il polling
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
