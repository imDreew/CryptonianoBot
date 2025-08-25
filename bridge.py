import os
import asyncio
import sys
import logging
import json
import requests
from telegram import Update, Message
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    ApplicationBuilder
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🔑 Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DISCORD_WEBHOOKS = os.getenv("DISCORD_WEBHOOKS")  # JSON string: {"#ANALISI": "...", ...}
TELEGRAM_SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID") or 0)
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")  # dove ricevere notifiche

# File per tracciare messaggi
MAPPING_FILE = "messages.json"
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)
else:
    MSG_MAP = {}  # {telegram_id: {"discord_id": str, "webhook": str}}

# Carica i webhook
DISCORD_WEBHOOKS = json.loads(DISCORD_WEBHOOKS) if DISCORD_WEBHOOKS else {}

# ==========================
# Funzione per convertire HTML Telegram → Markdown Discord
# ==========================
def telegram_html_to_discord(text: str) -> str:
    if not text:
        return ""

    # Grassetto <b> o <strong>
    text = text.replace("<b>", "**").replace("</b>", "**")
    text = text.replace("<strong>", "**").replace("</strong>", "**")

    # Corsivo <i> o <em>
    text = text.replace("<i>", "*").replace("</i>", "*")
    text = text.replace("<em>", "*").replace("</em>", "*")

    # Sottolineato <u>
    text = text.replace("<u>", "__").replace("</u>", "__")

    # Barrato <s>, <strike>, <del>
    text = text.replace("<s>", "~~").replace("</s>", "~~")
    text = text.replace("<strike>", "~~").replace("</strike>", "~~")
    text = text.replace("<del>", "~~").replace("</del>", "~~")

    # Link <a href="URL">testo</a> → [testo](URL)
    import re
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"[\2](\1)", text)

    # Code inline <code>
    text = text.replace("<code>", "`").replace("</code>", "`")

    # Code block <pre>
    text = text.replace("<pre>", "```").replace("</pre>", "```")

    # Rimozione tag HTML residui
    text = re.sub(r"<[^>]+>", "", text)

    return text.strip()


# ==========================
# Funzioni già presenti nel tuo codice
# ==========================
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not TELEGRAM_ADMIN_CHAT_ID:
        logging.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato, impossibile notificare admin")
        return
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Errore {cause} sul messaggio {message_id}"
        )
    except Exception as e:
        logger.error(f"Errore nell'invio notifica admin: {e}")

def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)

def get_discord_webhook(message_text: str):
    """Restituisce il webhook corretto in base all'hashtag iniziale."""
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return DISCORD_WEBHOOKS.get(hashtag)


def forward_to_discord(telegram_id: int, content: str = None, file_url: str = None, media_type: str = None):
    webhook_url = get_discord_webhook(content)
    if not webhook_url:
        logger.warning(f"Nessun webhook trovato per messaggio: {content}")
        return

    # Converte il contenuto da HTML → Markdown Discord
    formatted_content = telegram_html_to_discord(content)

    try:
        discord_message_id = None
        if file_url and media_type == "image":
            embed_text = formatted_content or ""
            embed = {"description": embed_text, "image": {"url": file_url}}
            payload = {"embeds": [embed]}
            r = requests.post(webhook_url, json=payload)
            discord_message_id = r.json().get("id")

        elif file_url and media_type == "video":
            if formatted_content:
                r = requests.post(webhook_url, json={"embeds": [{"description": formatted_content}]})
                discord_message_id = r.json().get("id")
            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            requests.post(webhook_url, files=files)

        else:
            payload = {"content": formatted_content}
            r = requests.post(webhook_url, json=payload)
            discord_message_id = r.json().get("id")

        if discord_message_id:
            MSG_MAP[str(telegram_id)] = {"discord_id": discord_message_id, "webhook": webhook_url}
            save_mapping()

        logger.info(f"Inoltrato a Discord: {formatted_content[:50]}...")
    except Exception as e:
        logger.error(f"Errore nell'inoltro a Discord: {e}")


# Handler messaggi
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.effective_message
    content = message.text or message.caption or ""

    file_url = None
    media_type = None

    if message.photo:
        file = await context.bot.get_file(message.photo[-1].file_id)
        file_url = file.file_path
        media_type = "image"

    elif message.video:
        file = await context.bot.get_file(message.video.file_id)
        file_url = file.file_path
        media_type = "video"

    forward_to_discord(message.message_id, content, file_url, media_type)


# Handler modifiche messaggi
async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.edited_message
    if not message:
        return
    content = f"✏️ Messaggio modificato:\n{message.text or message.caption}"
    forward_to_discord(message.message_id, content)


# Check cancellazioni (placeholder)
async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    pass


# ==========================
# Main
# ==========================
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Nuovi messaggi
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, handle_message))
    # Modifiche messaggi
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    # Job per controllare cancellazioni ogni 30s
    app.job_queue.run_repeating(check_deleted_messages, interval=30)

    logger.info("✅ Bridge avviato e in ascolto...")
    await app.run_polling(poll_interval=10)


if __name__ == "__main__":
    asyncio.run(main())
