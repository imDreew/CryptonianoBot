import os
import sys
import logging
import json
import requests
import nest_asyncio
import asyncio
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()  # Permette di riusare l'event loop già in esecuzione su Railway

# ====== CONFIG ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_WEBHOOKS_JSON = os.getenv("DISCORD_WEBHOOKS")  # JSON string: {"#ANALISI": "...", "#COPY_TRADING": "..."}
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

# ====== HELPER ======
def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)

def telegram_html_to_discord(html_text: str) -> str:
    """Convert Telegram HTML formatting to Discord Markdown."""
    if not html_text:
        return ""
    text = html_text
    # Bold
    text = text.replace("<b>", "**").replace("</b>", "**")
    # Italic
    text = text.replace("<i>", "*").replace("</i>", "*")
    # Underline
    text = text.replace("<u>", "__").replace("</u>", "__")
    # Strikethrough
    text = text.replace("<s>", "~~").replace("</s>", "~~")
    # Inline code
    text = text.replace("<code>", "`").replace("</code>", "`")
    # Code block
    text = text.replace("<pre>", "```").replace("</pre>", "```")
    # Links
    import re
    text = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r'[\2](\1)', text)
    # Remove remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    return text

def get_discord_webhook(message_text: str):
    """Restituisce il webhook corretto in base all'hashtag iniziale."""
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return DISCORD_WEBHOOKS.get(hashtag)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    """Invia notifica all'admin in caso di errore."""
    if not TELEGRAM_ADMIN_CHAT_ID:
        logger.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato, impossibile notificare admin")
        return
    message_link = f"https://t.me/c/{str(abs(TELEGRAM_SOURCE_CHAT_ID))[4:]}/{message_id}"
    alert_text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nLINK: {message_link}"
    try:
        await context.bot.send_message(chat_id=TELEGRAM_ADMIN_CHAT_ID, text=alert_text)
    except Exception as e:
        logger.error(f"Errore nell'invio notifica admin: {e}")

# ====== FORWARD MESSAGE ======
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

    # Gestione media
    file_url = None
    media_type = None
    caption = content

    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "image"
        elif msg.video:
            file_id = msg.video.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "video"
        elif msg.document:
            file_id = msg.document.file_id
            file = await context.bot.get_file(file_id)
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
            # Embed caption
            if content:
                r = requests.post(webhook_url, json={"embeds": [{"description": caption}]})
                discord_message_id = r.json().get("id")
            # File
            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            requests.post(webhook_url, files=files)
        else:
            payload = {"content": content}
            r = requests.post(webhook_url, json=payload)
            discord_message_id = r.json().get("id")

        if discord_message_id:
            MSG_MAP[str(msg.message_id)] = {"discord_id": discord_message_id, "webhook": webhook_url}
            save_mapping()

        logger.info(f"Inoltrato a Discord: {content[:50]}...")

    except Exception as e:
        logger.error(f"Errore nell'inoltro a Discord: {e}")
        await notify_admin(context, f"Errore inoltro: {e}", msg.message_id)

# ====== HANDLE EDITS ======
async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    if not msg:
        return
    content_html = msg.text_html or msg.caption_html or ""
    content = telegram_html_to_discord(content_html)
    await forward_message(update, context)

# ====== POLLING MESSAGGI CANCELLATI ======
async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    for telegram_id, data in list(MSG_MAP.items()):
        webhook = data["webhook"]
        discord_id = data["discord_id"]
        url = f"{webhook}/messages/{discord_id}"
        try:
            requests.delete(url)
            del MSG_MAP[telegram_id]
        except Exception as e:
            logger.warning(f"Non è stato possibile cancellare il messaggio Discord {discord_id}: {e}")
    save_mapping()

# ====== MAIN ======
async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, forward_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    app.job_queue.run_repeating(check_deleted_messages, interval=30)
    logger.info("✅ Bridge avviato e in ascolto...")
    await app.run_polling(poll_interval=10)

# ====== ENTRY POINT ======
if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
