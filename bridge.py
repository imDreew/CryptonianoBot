import os
import sys
import logging
import json
import requests
import asyncio
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ====== CONFIG ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_WEBHOOKS_JSON = os.getenv("DISCORD_WEBHOOKS")  # {"#ANALISI": "...", "#COPY_TRADING": "..."}
TELEGRAM_SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID", 0))
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not DISCORD_WEBHOOKS_JSON or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

DISCORD_WEBHOOKS = json.loads(DISCORD_WEBHOOKS_JSON)

# File mapping messaggi Telegram → Discord
MAPPING_FILE = "messages.json"
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)
else:
    MSG_MAP = {}

# ====== LOGGING ======
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== HELPERS ======
def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)

def telegram_html_to_discord(html_text: str) -> str:
    if not html_text:
        return ""
    text = html_text
    text = text.replace("<b>", "**").replace("</b>", "**")
    text = text.replace("<i>", "*").replace("</i>", "*")
    text = text.replace("<u>", "__").replace("</u>", "__")
    text = text.replace("<s>", "~~").replace("</s>", "~~")
    text = text.replace("<code>", "`").replace("</code>", "`")
    text = text.replace("<pre>", "```").replace("</pre>", "```")
    import re
    text = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r'[\2](\1)', text)
    text = re.sub(r"<[^>]+>", "", text)
    return text

def get_discord_webhook(message_text: str):
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return DISCORD_WEBHOOKS.get(hashtag)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not TELEGRAM_ADMIN_CHAT_ID:
        logger.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato")
        return
    link = f"https://t.me/c/{str(abs(TELEGRAM_SOURCE_CHAT_ID))[4:]}/{message_id}"
    try:
        await context.bot.send_message(chat_id=TELEGRAM_ADMIN_CHAT_ID, text=f"‼️Errore‼️ {cause}\nLink: {link}")
    except Exception as e:
        logger.error(f"Errore notifica admin: {e}")

# ====== FORWARD ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return
    content_html = msg.text_html or msg.caption_html or ""
    content = telegram_html_to_discord(content_html)
    webhook_url = get_discord_webhook(content)
    if not webhook_url:
        logger.info(f"Messaggio senza hashtag valido: {content}")
        return

    file_url = None
    media_type = None
    caption = content

    try:
        if msg.photo:
            file = await context.bot.get_file(msg.photo[-1].file_id)
            file_url = file.file_path
            media_type = "image"
        elif msg.video:
            file = await context.bot.get_file(msg.video.file_id)
            file_url = file.file_path
            media_type = "video"
        elif msg.document:
            file = await context.bot.get_file(msg.document.file_id)
            file_url = file.file_path
            media_type = "document"
    except Exception as e:
        await notify_admin(context, f"Errore recupero media: {e}", msg.message_id)
        return

    try:
        discord_message_id = None
        if file_url and media_type == "image":
            payload = {"embeds": [{"description": caption, "image": {"url": file_url}}]}
            r = requests.post(webhook_url, json=payload)
            discord_message_id = r.json().get("id")
        elif file_url and media_type == "video":
            if content:
                r = requests.post(webhook_url, json={"embeds": [{"description": caption}]})
                discord_message_id = r.json().get("id")
            video_data = requests.get(file_url).content
            requests.post(webhook_url, files={"file": ("video.mp4", video_data)})
        else:
            r = requests.post(webhook_url, json={"content": content})
            discord_message_id = r.json().get("id")

        if discord_message_id:
            MSG_MAP[str(msg.message_id)] = {"discord_id": discord_message_id, "webhook": webhook_url}
            save_mapping()

        logger.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logger.error(f"Errore inoltro Discord: {e}")
        await notify_admin(context, f"Inoltro Discord: {e}", msg.message_id)

# ====== CHECK CANCELLAZIONI ======
async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    for telegram_id, data in list(MSG_MAP.items()):
        try:
            requests.delete(f"{data['webhook']}/messages/{data['discord_id']}")
            del MSG_MAP[telegram_id]
        except Exception as e:
            logger.warning(f"Errore cancellazione Discord {data['discord_id']}: {e}")
    save_mapping()

# ====== MAIN ======
async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, forward_message))
    app.job_queue.run_repeating(check_deleted_messages, interval=30)
    logger.info("✅ Bridge avviato...")
    await app.run_polling(poll_interval=10)

# ====== ENTRY POINT ======
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
