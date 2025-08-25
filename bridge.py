import os
import asyncio
import sys
import logging
import json
import re
import requests
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
)

# ====== CONFIG ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID") or 0)
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
DISCORD_WEBHOOKS_JSON = os.getenv("DISCORD_WEBHOOKS")  # {"#ANALISI": "...", "#COPY_TRADING": "...", "#DISCUSSIONE": "..."}

if not TELEGRAM_TOKEN or not TELEGRAM_SOURCE_CHAT_ID or not DISCORD_WEBHOOKS_JSON:
    print("❌ ERRORE: variabili di ambiente mancanti")
    sys.exit(1)

DISCORD_WEBHOOKS = json.loads(DISCORD_WEBHOOKS_JSON)
MAPPING_FILE = "messages.json"

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====== CARICAMENTO MAPPING ======
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)
else:
    MSG_MAP = {}

# ========================================
# Funzione per convertire HTML Telegram → Markdown Discord
# ========================================
def telegram_html_to_discord(text: str) -> str:
    if not text:
        return ""

    # Grassetto <b> o <strong>
    text = re.sub(r"<(/)?(b|strong)>", r"**", text)
    # Corsivo <i> o <em>
    text = re.sub(r"<(/)?(i|em)>", r"*", text)
    # Sottolineato <u>
    text = re.sub(r"<(/)?u>", r"__", text)
    # Barrato <s>, <strike>, <del>
    text = re.sub(r"<(/)?(s|strike|del)>", r"~~", text)
    # Link <a href="URL">testo</a>
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"[\2](\1)", text)
    # Code inline <code>
    text = re.sub(r"<(/)?code>", r"`", text)
    # Code block <pre>
    text = re.sub(r"<(/)?pre>", r"```", text)
    # Rimuove eventuali altri tag residui
    text = re.sub(r"<[^>]+>", "", text)
    # Decodifica entità HTML comuni
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&#x27;", "'")
    return text.strip()

# ====== FUNZIONE NOTIFICA ADMIN ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not TELEGRAM_ADMIN_CHAT_ID:
        logger.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato")
        return
    chat_id_abs = str(abs(TELEGRAM_SOURCE_CHAT_ID))
    message_link = f"https://t.me/c/{chat_id_abs[4:]}/{message_id}"
    alert_text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nLINK MESSAGGIO: {message_link}"
    try:
        await context.bot.send_message(
            chat_id=int(TELEGRAM_ADMIN_CHAT_ID),
            text=alert_text,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Impossibile notificare admin: {e}")

def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)

# ====== HELPER ======
def get_discord_webhook(message_text: str):
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

    try:
        discord_message_id = None

        # Immagine
        if file_url and media_type == "image":
            payload = {"embeds": [{"description": content, "image": {"url": file_url}}]}
            r = requests.post(webhook_url, json=payload)
            discord_message_id = r.json().get("id")

        # Video
        elif file_url and media_type == "video":
            if content:
                r = requests.post(webhook_url, json={"embeds": [{"description": content}]})
                discord_message_id = r.json().get("id")
            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            requests.post(webhook_url, files=files)

        # Solo testo o documenti
        else:
            payload = {"content": content}
            r = requests.post(webhook_url, json=payload)
            discord_message_id = r.json().get("id")

        # Salva mapping
        if discord_message_id:
            MSG_MAP[str(telegram_id)] = {"discord_id": discord_message_id, "webhook": webhook_url}
            save_mapping()

        logger.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logger.error(f"Errore nell'inoltro a Discord: {e}")

# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or msg.caption_html or ""
    content = telegram_html_to_discord(content)

    file_url = None
    media_type = None
    caption = None

    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "image"
            caption = msg.caption_html or ""
        elif msg.video:
            file_id = msg.video.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "video"
            caption = msg.caption_html or ""
        elif msg.document:
            file_id = msg.document.file_id
            file = await context.bot.get_file(file_id)
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
    content = message.text_html or message.caption_html or ""
    content = telegram_html_to_discord(content)
    forward_to_discord(message.message_id, content)

# ====== JOB CANCELLAZIONI ======
async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    for telegram_id, data in list(MSG_MAP.items()):
        try:
            msg = await context.bot.forward_message(
                chat_id=TELEGRAM_ADMIN_CHAT_ID,
                from_chat_id=TELEGRAM_SOURCE_CHAT_ID,
                message_id=int(telegram_id),
            )
            await msg.delete()
        except Exception:
            webhook = data["webhook"]
            discord_id = data["discord_id"]
            url = webhook + f"/messages/{discord_id}"
            requests.delete(url)
            del MSG_MAP[telegram_id]
            save_mapping()
            logger.info(f"❌ Messaggio {telegram_id} eliminato anche su Discord")

# ====== MAIN ======
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, forward_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))

    app.job_queue.run_repeating(check_deleted_messages, interval=30)

    logger.info("✅ Bridge avviato e in ascolto...")
    await app.run_polling(poll_interval=10)

if __name__ == "__main__":
    asyncio.run(main())
