import os
import sys
import logging
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, filters, ContextTypes
)

# ====== VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_WEBHOOKS = os.getenv("DISCORD_WEBHOOKS")  # JSON string: {"ANALISI": "...", "COPY_TRADING": "..."}
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")  # dove ricevere notifiche

if not TELEGRAM_BOT_TOKEN or not DISCORD_WEBHOOKS or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)
import json
DISCORD_WEBHOOKS = json.loads(DISCORD_WEBHOOKS)

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== FUNZIONE NOTIFICA ADMIN ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    """Notifica l'admin su Telegram con link cliccabile al messaggio originale."""
    if not TELEGRAM_ADMIN_CHAT_ID:
        logging.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato, impossibile notificare admin")
        return

    # Genera link al messaggio nel canale privato
    chat_id_abs = str(abs(TELEGRAM_SOURCE_CHAT_ID))
    message_link = f"https://t.me/c/{chat_id_abs[4:]}/{message_id}"

    alert_text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nLINK MESSAGGIO: {message_link}"

    try:
        await context.bot.send_message(
            chat_id=int(TELEGRAM_ADMIN_CHAT_ID),
            text=alert_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Impossibile notificare l'admin: {e}")


# ====== HELPER ======
def get_discord_webhook(message_text: str):
    """Restituisce il webhook corretto in base all'hashtag iniziale."""
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return DISCORD_WEBHOOKS.get(hashtag)


# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    # Testo principale del messaggio
    content = msg.text_html or ""
    webhook_url = get_discord_webhook(content)
    if not webhook_url:
        logging.info("Messaggio senza hashtag valido, ignorato")
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

    # Costruisci il contenuto da inviare su Discord
    try:
        if file_url and media_type == "image":
            embed_text = caption or content
            embed = {"description": embed_text, "image": {"url": file_url}}
            payload = {"embeds": [embed]}
            requests.post(webhook_url, json=payload)

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


# ====== JOB QUEUE E CHECK MESSAGGI CANCELLATI ======
async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    # Qui puoi implementare polling delle cancellazioni
    pass


# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))

    # JobQueue (poll messaggi cancellati ogni 30s)
    app.job_queue.run_repeating(check_deleted_messages, interval=30)

    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling(poll_interval=10)


if __name__ == "__main__":
    main()
