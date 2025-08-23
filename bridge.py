import os
import sys
import logging
import requests
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ====== VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
DEFAULT_WEBHOOK_URL = os.getenv("DEFAULT_WEBHOOK_URL")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 10))  # in secondi

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)

# Mappatura hashtag → webhook Discord
def load_hashtag_map():
    raw = os.getenv("HASHTAG_MAP", "")
    mapping = {}
    for item in raw.split(","):
        if "=" in item:
            key, url = item.strip().split("=", 1)
            mapping[key.strip()] = url.strip()
    return mapping

HASHTAG_MAP = load_hashtag_map()

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== NOTIFICA ADMIN ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not TELEGRAM_ADMIN_CHAT_ID:
        logging.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato, impossibile notificare admin")
        return

    chat_id_abs = str(abs(TELEGRAM_SOURCE_CHAT_ID))
    message_link = f"https://t.me/c/{chat_id_abs[4:]}/{message_id}"

    alert_text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nID MESSAGGIO: {message_link}"

    try:
        await context.bot.send_message(
            chat_id=int(TELEGRAM_ADMIN_CHAT_ID),
            text=alert_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Impossibile notificare l'admin: {e}")

# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or ""

    # Routing webhook in base al primo hashtag
    first_word = (content.split(maxsplit=1)[0] if content else "").strip()
    webhook_url = HASHTAG_MAP.get(first_word, DEFAULT_WEBHOOK_URL)
    if not webhook_url:
        logging.warning(f"Nessun webhook trovato per hashtag: {first_word}")
        return

    # Gestione media
    file_url = None
    media_type = None
    caption = None

    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "image"
            caption = msg.caption_html or ""
        elif msg.video:
            file_id = msg.video.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "video"
            caption = msg.caption_html or ""
        elif msg.document:
            file_id = msg.document.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "document"
            caption = msg.caption_html or ""
    except Exception as e:
        await notify_admin(context, f"Errore nel recupero del media: {e}", msg.message_id)
        return

    # Costruisci il payload Discord
    try:
        if file_url and media_type == "image":
            embed_text = caption or content
            embed = {"description": embed_text, "image": {"url": file_url}}
            requests.post(webhook_url, json={"embeds": [embed]})

        elif file_url and media_type == "video":
            if caption or content:
                embed = {"description": caption or content}
                requests.post(webhook_url, json={"embeds": [embed]})

            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            requests.post(webhook_url, files=files)

        else:
            payload = {"content": content}
            requests.post(webhook_url, json=payload)

        logging.info(f"Inoltrato a Discord: {content[:50]}...")

    except Exception as e:
        logging.error(f"Errore nell'inoltro a Discord: {e}")
        await notify_admin(context, f"Errore nell'inoltro: {e}", msg.message_id)

# ====== POLLING BASE PER MODIFICHE/CANCELLAZIONI ======
async def polling_updates(app):
    while True:
        # Qui puoi aggiungere logica di sync update/delete su Discord
        await asyncio.sleep(POLL_INTERVAL)

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(start_polling_task).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))
    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling()

async def start_polling_task(app):
    # Questo viene eseguito nel loop già avviato da Telegram
    asyncio.create_task(polling_updates(app))


if __name__ == "__main__":
    main()
