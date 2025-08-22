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

MAX_FILE_SIZE = 8 * 1024 * 1024  # 8 MB limite Discord

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

# ====== FUNZIONE NOTIFICA ADMIN ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int, chat_id: int = TELEGRAM_SOURCE_CHAT_ID):
    try:
        msg_link = f"https://t.me/c/{str(chat_id)[4:]}/{message_id}" if str(chat_id).startswith("-100") else f"https://t.me/{chat_id}/{message_id}"
        alert = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nMESSAGGIO: [Apri messaggio]({msg_link})"
        await context.bot.send_message(chat_id=TELEGRAM_ADMIN_CHAT_ID, text=alert, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Impossibile notificare l'admin: {e}")

# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or ""
    media_urls = []

    # ====== FOTO ======
    if msg.photo:
        file = await context.bot.get_file(msg.photo[-1].file_id)  # solo versione più grande
        if file.file_size > MAX_FILE_SIZE:
            await notify_admin(context, "File troppo grande", msg.message_id)
            return
        media_urls.append(file.file_path)

    # ====== VIDEO ======
    elif msg.video:
        file = await context.bot.get_file(msg.video.file_id)
        if file.file_size > MAX_FILE_SIZE:
            await notify_admin(context, "File troppo grande", msg.message_id)
            return
        media_urls.append(file.file_path)

    # ====== DOCUMENTO ======
    elif msg.document:
        file = await context.bot.get_file(msg.document.file_id)
        if file.file_size > MAX_FILE_SIZE:
            await notify_admin(context, "File troppo grande", msg.message_id)
            return
        media_urls.append(file.file_path)

    # ====== COSTRUZIONE EMBED DISCORD ======
    try:
        if media_urls:
            embeds = []
            for i, url in enumerate(media_urls):
                if i == 0:
                    embeds.append({"description": content, "image": {"url": url}})
                else:
                    embeds.append({"image": {"url": url}})
            response = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": embeds})
        else:
            response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
        response.raise_for_status()
        logging.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logging.error(f"Errore nell'inoltro a Discord: {e}")
        await notify_admin(context, str(e), msg.message_id)

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))
    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling()

if __name__ == "__main__":
    main()
