import os
import sys
import json
import logging
import requests
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
)

# ===== CONFIG =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID", 0))
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
DISCORD_WEBHOOKS = json.loads(os.getenv("DISCORD_WEBHOOKS", "{}"))  # es: {"#ANALISI": "...", "#COPY_TRADING": "...", "#DISCUSSIONE": "..."}

MAPPING_FILE = "messages.json"

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== LOAD MAPPINGS =====
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)
else:
    MSG_MAP = {}

# ===== UTILITIES =====
def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)

def telegram_html_to_discord(text: str) -> str:
    """Convert Telegram HTML to Discord Markdown."""
    if not text:
        return ""
    conv = text
    # Grassetto
    conv = conv.replace("<b>", "**").replace("</b>", "**")
    # Corsivo
    conv = conv.replace("<i>", "*").replace("</i>", "*")
    # Barrato
    conv = conv.replace("<s>", "~~").replace("</s>", "~~")
    # Inline code
    conv = conv.replace("<code>", "`").replace("</code>", "`")
    # Code block
    conv = conv.replace("<pre>", "```").replace("</pre>", "```")
    # Link
    import re
    conv = re.sub(r'<a href="(.*?)">(.*?)</a>', r'[\2](\1)', conv)
    # Rimuove eventuali tag rimanenti
    conv = re.sub(r'<.*?>', '', conv)
    return conv

def get_discord_webhook(message_text: str):
    """Restituisce il webhook corretto in base all'hashtag iniziale."""
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return DISCORD_WEBHOOKS.get(hashtag)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not TELEGRAM_ADMIN_CHAT_ID:
        return
    message_link = f"https://t.me/c/{str(abs(TELEGRAM_SOURCE_CHAT_ID))[4:]}/{message_id}"
    alert_text = f"‼️ERRORE‼️\nCAUSA: {cause}\nLINK: {message_link}"
    try:
        await context.bot.send_message(chat_id=TELEGRAM_ADMIN_CHAT_ID, text=alert_text)
    except Exception as e:
        logger.error(f"Impossibile notificare l'admin: {e}")

# ===== DISCORD FORWARD =====
def forward_to_discord(telegram_id: int, content: str = None, file_url: str = None, media_type: str = None):
    webhook_url = get_discord_webhook(content)
    if not webhook_url:
        logger.warning(f"Nessun webhook trovato per messaggio: {content}")
        return

    content_md = telegram_html_to_discord(content)
    try:
        if file_url and media_type == "image":
            embed = {"description": content_md, "image": {"url": file_url}}
            payload = {"embeds": [embed]}
            r = requests.post(webhook_url, json=payload)
            discord_id = r.json().get("id")
        elif file_url and media_type == "video":
            # caption come embed
            if content_md:
                r = requests.post(webhook_url, json={"embeds": [{"description": content_md}]})
                discord_id = r.json().get("id")
            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            requests.post(webhook_url, files=files)
        else:
            payload = {"content": content_md}
            r = requests.post(webhook_url, json=payload)
            discord_id = r.json().get("id")

        if discord_id:
            MSG_MAP[str(telegram_id)] = {"discord_id": discord_id, "webhook": webhook_url}
            save_mapping()

        logger.info(f"Inoltrato a Discord: {content_md[:50]}...")
    except Exception as e:
        logger.error(f"Errore inoltro Discord: {e}")

# ===== HANDLERS =====
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or ""
    file_url = None
    media_type = None
    caption = None

    try:
        if msg.photo:
            file = await context.bot.get_file(msg.photo[-1].file_id)
            file_url = file.file_path
            media_type = "image"
            caption = msg.caption_html or ""
        elif msg.video:
            file = await context.bot.get_file(msg.video.file_id)
            file_url = file.file_path
            media_type = "video"
            caption = msg.caption_html or ""
        elif msg.document:
            file = await context.bot.get_file(msg.document.file_id)
            file_url = file.file_path
            media_type = "document"
            caption = msg.caption_html or ""
    except Exception as e:
        await notify_admin(context, f"Errore recupero media: {e}", msg.message_id)
        return

    forward_to_discord(msg.message_id, content, file_url, media_type)

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.edited_message
    if not message:
        return
    content = f"✏️ Messaggio modificato:\n{message.text or message.caption}"
    forward_to_discord(message.message_id, content)

async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    for telegram_id, data in list(MSG_MAP.items()):
        try:
            msg = await context.bot.forward_message(chat_id=TELEGRAM_ADMIN_CHAT_ID, from_chat_id=TELEGRAM_SOURCE_CHAT_ID, message_id=int(telegram_id))
            await msg.delete()
        except Exception:
            # elimina su Discord
            webhook = data["webhook"]
            discord_id = data["discord_id"]
            url = f"{webhook}/messages/{discord_id}"
            requests.delete(url)
            del MSG_MAP[telegram_id]
            save_mapping()
            logger.info(f"❌ Messaggio {telegram_id} eliminato anche su Discord")

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, forward_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    app.job_queue.run_repeating(check_deleted_messages, interval=30)
    logger.info("✅ Bridge avviato e in ascolto...")
    app.run_polling(poll_interval=10)  # PTB gestisce il loop

if __name__ == "__main__":
    main()
