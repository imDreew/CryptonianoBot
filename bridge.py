import os
import logging
import requests
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ====== VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")  # percorso webhook
PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_BOT_TOKEN or not DISCORD_WEBHOOK_URL or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ Manca una variabile di ambiente!")
    exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== HANDLER ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.chat_id == TELEGRAM_SOURCE_CHAT_ID:
        content = update.message.text_html or ""

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
            logging.info(f"Inoltrato a Discord: {content[:50]}...")
        except Exception as e:
            logging.error(f"Errore nell'inoltro a Discord: {e}")

# ====== SERVER WEB PER WEBHOOK ======
async def handle_update(request):
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.update_queue.put(update)
    return web.Response(text="ok")

# ====== MAIN ======
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))

    # Imposta webhook su Telegram
    public_url = os.getenv("WEBHOOK_URL")
    if not public_url:
        logging.error("❌ Devi impostare WEBHOOK_URL come variabile di ambiente!")
        exit(1)

    # Setta il webhook
    import requests
    webhook_set = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
        data={"url": f"{public_url}{WEBHOOK_PATH}"}
    )
    if webhook_set.status_code == 200:
        logging.info(f"✅ Webhook impostato correttamente su {public_url}{WEBHOOK_PATH}")
    else:
        logging.error(f"❌ Errore impostando webhook: {webhook_set.text}")

    # Avvia server aiohttp per ricevere gli update
    web_app = web.Application()
    web_app.router.add_post(WEBHOOK_PATH, handle_update)

    logging.info("✅ Bridge avviato e in ascolto...")
    web.run_app(web_app, host="0.0.0.0", port=PORT)
