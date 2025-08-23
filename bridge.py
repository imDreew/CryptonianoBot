import os
import asyncio
import logging
import json
import requests
from telegram import Update, Message
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🔑 Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# 📌 Mapping hashtag → webhook Discord
HASHTAG_TO_WEBHOOK = {
    "#ANALISI": os.getenv("DISCORD_WEBHOOK_ANALISI"),
    "#COPY_TRADING": os.getenv("DISCORD_WEBHOOK_COPY"),
    "#DISCUSSIONE": os.getenv("DISCORD_WEBHOOK_DISCUSSIONE"),
}

# File per tracciare messaggi
MAPPING_FILE = "messages.json"

# Carica mapping
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)
else:
    MSG_MAP = {}  # {telegram_id: {"discord_id": str, "webhook": str}}


def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)


# 📩 Notifica admin per errori ricorrenti
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Errore {cause} sul messaggio {message_id}"
        )
    except Exception as e:
        logger.error(f"Errore nell'invio notifica admin: {e}")


# 📤 Inoltro messaggio a Discord
def forward_to_discord(telegram_id: int, content: str = None, file_url: str = None, media_type: str = None):
    webhook_url = None
    discord_message_id = None

    # Decidi canale in base all’hashtag
    if content:
        first_word = content.strip().split()[0]
        webhook_url = HASHTAG_TO_WEBHOOK.get(first_word.upper())

    if not webhook_url:
        logger.warning(f"Nessun webhook trovato per messaggio: {content}")
        return

    try:
        if file_url and media_type == "image":
            embed = {"description": content, "image": {"url": file_url}}
            payload = {"embeds": [embed]}
            r = requests.post(webhook_url, json=payload)
            discord_message_id = r.json().get("id")

        elif file_url and media_type == "video":
            # Prima la caption
            if content:
                r = requests.post(webhook_url, json={"embeds": [{"description": content}]})
                discord_message_id = r.json().get("id")

            # Poi il file come allegato
            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            requests.post(webhook_url, files=files)

        else:
            # Solo testo
            payload = {"content": content}
            r = requests.post(webhook_url, json=payload)
            discord_message_id = r.json().get("id")

        # Salva mapping
        if discord_message_id:
            MSG_MAP[str(telegram_id)] = {"discord_id": discord_message_id, "webhook": webhook_url}
            save_mapping()

    except Exception as e:
        logger.error(f"Errore nell'inoltro a Discord: {e}")


# 📌 Handler nuovi messaggi
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.effective_message
    content = message.text or message.caption or ""

    # Controlla se ci sono media
    file_url = None
    media_type = None

    if message.photo:
        photo = message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_url = file.file_path
        media_type = "image"

    elif message.video:
        file = await context.bot.get_file(message.video.file_id)
        file_url = file.file_path
        media_type = "video"

    # Inoltra a Discord
    forward_to_discord(message.message_id, content, file_url, media_type)


# 📌 Handler modifiche messaggi
async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.edited_message
    if not message:
        return
    content = f"✏️ Messaggio modificato:\n{message.text or message.caption}"
    forward_to_discord(message.message_id, content)


# 🔄 Controlla cancellazioni
async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    chat_id = ADMIN_CHAT_ID  # gruppo monitorato
    for telegram_id, data in list(MSG_MAP.items()):
        try:
            msg = await context.bot.forward_message(
                chat_id=ADMIN_CHAT_ID,  # inoltro “di test”
                from_chat_id=chat_id,
                message_id=int(telegram_id)
            )
            # se arriva qui → messaggio esiste ancora → eliminiamo l’inoltro temporaneo
            await msg.delete()
        except Exception:
            # messaggio eliminato su Telegram → cancella anche su Discord
            webhook = data["webhook"]
            discord_id = data["discord_id"]
            url = webhook.replace("webhooks", "webhooks") + f"/messages/{discord_id}"
            requests.delete(url)
            del MSG_MAP[telegram_id]
            save_mapping()
            logger.info(f"❌ Messaggio {telegram_id} eliminato anche su Discord")


# 🚀 Avvio bot
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Nuovi messaggi
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, handle_message))

    # Modifiche messaggi
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))

    # Job per controllare cancellazioni ogni 30s
    app.job_queue.run_repeating(check_deleted_messages, interval=30)

    logger.info("✅ Bridge avviato e in ascolto...")
    await app.run_polling(poll_interval=10)


if __name__ == "__main__":
    asyncio.run(main())
