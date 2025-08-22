import os
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

print("=== VARIABILI CARICATE ===")
print("TELEGRAM_BOT_TOKEN:", TELEGRAM_BOT_TOKEN)
print("DISCORD_WEBHOOK_URL:", DISCORD_WEBHOOK_URL)
print("TELEGRAM_SOURCE_CHAT_ID:", TELEGRAM_SOURCE_CHAT_ID)
print("===========================")


# ====== CONFIG DA VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")



if not TELEGRAM_BOT_TOKEN or not DISCORD_WEBHOOK_URL or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    print("TELEGRAM_BOT_TOKEN =", TELEGRAM_BOT_TOKEN)
    print("DISCORD_WEBHOOK_URL =", DISCORD_WEBHOOK_URL)
    print("TELEGRAM_SOURCE_CHAT_ID =", TELEGRAM_SOURCE_CHAT_ID)
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)


# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.chat_id == TELEGRAM_SOURCE_CHAT_ID:
        content = update.message.text_html or ""

        # Se ci sono media (foto/video/documenti)
        file_url = None
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
        elif update.message.video:
            file_id = update.message.video.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
        elif update.message.document:
            file_id = update.message.document.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path

        # Costruisci payload per Discord
        payload = {"content": content}
        files = None

        if file_url:
            file_data = requests.get(file_url)
            files = {"file": file_data.content}

        try:
            if files:
                response = requests.post(DISCORD_WEBHOOK_URL, data=payload, files=files)
            else:
                response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
            response.raise_for_status()
            logging.info(f"Inoltrato a Discord: {content[:30]}...")
        except Exception as e:
            logging.error(f"Errore nell'inoltro a Discord: {e}")

# ====== MAIN ======
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_SOURCE_CHAT_ID or not DISCORD_WEBHOOK_URL:
        logging.error("⚠️ Devi configurare TELEGRAM_BOT_TOKEN, TELEGRAM_SOURCE_CHAT_ID e DISCORD_WEBHOOK_URL su Heroku!")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))

    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling()

if __name__ == "__main__":
    main()
