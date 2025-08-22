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
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")

if not all([TELEGRAM_BOT_TOKEN, DISCORD_WEBHOOK_URL, TELEGRAM_SOURCE_CHAT_ID, TELEGRAM_ADMIN_CHAT_ID]):
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)
TELEGRAM_ADMIN_CHAT_ID = int(TELEGRAM_ADMIN_CHAT_ID)

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== HELPERS ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    try:
        # Link diretto al messaggio su canale privato
        short_channel_id = str(TELEGRAM_SOURCE_CHAT_ID)[4:]  # togli -100
        message_link = f"https://t.me/c/{short_channel_id}/{message_id}"

        alert_text = (
            "‼️ERRORE INOLTRO‼️\n"
            f"CAUSA: {cause}\n"
            f"LINK MESSAGGIO: {message_link}"
        )

        await context.bot.send_message(chat_id=TELEGRAM_ADMIN_CHAT_ID, text=alert_text)
        logging.info(f"Notifica inviata all'admin: {alert_text}")
    except Exception as e:
        logging.error(f"Impossibile notificare l'admin: {e}")

# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or ""

    # Gestione media
    media_urls = []
    if msg.photo:  # più foto
        for photo in msg.photo:
            file = await context.bot.get_file(photo.file_id)
            media_urls.append(file.file_path)
    elif msg.video:
        file = await context.bot.get_file(msg.video.file_id)
        media_urls.append(file.file_path)
    elif msg.document:
        file = await context.bot.get_file(msg.document.file_id)
        media_urls.append(file.file_path)

    # Inoltro a Discord
    try:
        if media_urls:
            for url in media_urls:
                # Per Discord: invio come embed con testo sopra
                embed_payload = {
                    "content": content if media_urls.index(url) == 0 else "",  # testo solo sulla prima immagine
                    "embeds": [{"image": {"url": url}}]
                }
                response = requests.post(DISCORD_WEBHOOK_URL, json=embed_payload)
                response.raise_for_status()
            logging.info(f"Inoltrato a Discord: {content[:50]}... + {len(media_urls)} media")
        else:
            # solo testo
            response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
            response.raise_for_status()
            logging.info(f"Inoltrato a Discord: {content[:50]}...")

    except requests.exceptions.RequestException as e:
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
