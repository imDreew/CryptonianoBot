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
    msg = update.message or update.channel_post
    if not msg:
        return

    if msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or ""

    # Gestione media (inviare link per avere anteprima)
    media_url = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
        file = await context.bot.get_file(file_id)
        media_url = file.file_path
    elif msg.video:
        file_id = msg.video.file_id
        file = await context.bot.get_file(file_id)
        media_url = file.file_path
    elif msg.document:
        file_id = msg.document.file_id
        file = await context.bot.get_file(file_id)
        media_url = file.file_path

    # Se c'è media, aggiungi il link nel messaggio
    if media_url:
        content = f"{content}\n{media_url}" if content else media_url

    payload = {"content": content}

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
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
