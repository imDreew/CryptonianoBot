import os
import sys
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ====== VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")  # chat privata admin

if not TELEGRAM_BOT_TOKEN or not DISCORD_WEBHOOK_URL or not TELEGRAM_SOURCE_CHAT_ID or not TELEGRAM_ADMIN_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)
TELEGRAM_ADMIN_CHAT_ID = int(TELEGRAM_ADMIN_CHAT_ID)

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== COSTANTI ======
MAX_FILE_SIZE = 8 * 1024 * 1024  # 8MB limite Discord

# ====== FUNZIONE DI NOTIFICA ADMIN ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    link = f"https://t.me/c/{str(TELEGRAM_SOURCE_CHAT_ID)[4:]}/{message_id}"  # link messaggio privato canale
    text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nMESSAGGIO: [Apri su Telegram]({link})"
    try:
        await context.bot.send_message(chat_id=TELEGRAM_ADMIN_CHAT_ID, text=text, parse_mode="Markdown")
        logging.info("Notifica admin inviata")
    except Exception as e:
        logging.error(f"Impossibile notificare l'admin: {e}")

# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return
    if msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or ""
    file_too_big = False
    media_urls = []

    # Gestione media
    if msg.photo:
        for photo in msg.photo:
            if photo.file_size and photo.file_size > MAX_FILE_SIZE:
                file_too_big = True
                break
            file = await context.bot.get_file(photo.file_id)
            media_urls.append(file.file_path)
    elif msg.video:
        if msg.video.file_size and msg.video.file_size > MAX_FILE_SIZE:
            file_too_big = True
        else:
            file = await context.bot.get_file(msg.video.file_id)
            media_urls.append(file.file_path)
    elif msg.document:
        if msg.document.file_size and msg.document.file_size > MAX_FILE_SIZE:
            file_too_big = True
        else:
            file = await context.bot.get_file(msg.document.file_id)
            media_urls.append(file.file_path)

    if file_too_big:
        await notify_admin(context, cause="File troppo grande", message_id=msg.message_id)
        return

    # Inoltro a Discord
    try:
        if media_urls:
            embeds = [{"description": content, "image": {"url": url}} if i == 0 else {"image": {"url": url}}
                      for i, url in enumerate(media_urls)]
            payload = {"embeds": embeds}
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        else:
            payload = {"content": content}
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        response.raise_for_status()
        logging.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logging.error(f"Errore nell'inoltro a Discord: {e}")
        await notify_admin(context, cause=str(e), message_id=msg.message_id)

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))
    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling()

if __name__ == "__main__":
    main()
