import os
import sys
import logging
import requests
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ====== VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not DISCORD_WEBHOOK_URL or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.message or update.channel_post
    if not msg:
        return

    if msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.caption_html or msg.text_html or ""

    files = []

    try:
        # ===== FOTO (anche multiple in album) =====
        if msg.photo:
            file_id = msg.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            photo_data = requests.get(file.file_path)
            files.append(("file", ("photo.jpg", photo_data.content)))

        # ===== VIDEO =====
        if msg.video:
            file_id = msg.video.file_id
            file = await context.bot.get_file(file_id)
            video_data = requests.get(file.file_path)
            files.append(("file", ("video.mp4", video_data.content)))

        # ===== DOCUMENTI =====
        if msg.document:
            file_id = msg.document.file_id
            file = await context.bot.get_file(file_id)
            doc_data = requests.get(file.file_path)
            filename = msg.document.file_name or "document"
            files.append(("file", (filename, doc_data.content)))

        # ===== GESTIONE ALBUM (MediaGroup) =====
        if msg.media_group_id:
            # recupera tutti i messaggi dello stesso gruppo
            group_messages = [
                m for m in context.bot_data.get(msg.media_group_id, []) if m != msg
            ]
            # aggiungi quello attuale
            group_messages.append(msg)
            # scarica tutti i file del gruppo
            files = []
            for m in group_messages:
                if m.photo:
                    file_id = m.photo[-1].file_id
                    file = await context.bot.get_file(file_id)
                    photo_data = requests.get(file.file_path)
                    files.append(("file", ("photo.jpg", photo_data.content)))
                elif m.video:
                    file_id = m.video.file_id
                    file = await context.bot.get_file(file_id)
                    video_data = requests.get(file.file_path)
                    files.append(("file", ("video.mp4", video_data.content)))

        # ===== INVIO SU DISCORD =====
        if files:
            response = requests.post(DISCORD_WEBHOOK_URL, data={"content": content}, files=files)
        else:
            response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})

        response.raise_for_status()
        logging.info(f"Inoltrato a Discord: {content[:50]}...")

    except Exception as e:
        logging.error(f"Errore nell'inoltro a Discord: {e}")

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))
    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling()

if __name__ == "__main__":
    main()
