import os
import asyncio
import sys
import logging
import json
import requests
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes
)
from html import unescape
import re

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====== CONFIG ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
TELEGRAM_SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID", "0"))

# Discord Webhooks mappate per hashtag
DISCORD_WEBHOOKS = {
    "#ANALISI": os.getenv("DISCORD_WEBHOOK_ANALISI"),
    "#COPY_TRADING": os.getenv("DISCORD_WEBHOOK_COPY"),
    "#DISCUSSIONE": os.getenv("DISCORD_WEBHOOK_DISCUSSIONE"),
}

if not TELEGRAM_TOKEN or not DISCORD_WEBHOOKS or not TELEGRAM_SOURCE_CHAT_ID:
    logger.error("❌ Variabili ambiente mancanti")
    sys.exit(1)

# File per salvare mapping Telegram -> Discord
MAPPING_FILE = "messages.json"
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)
else:
    MSG_MAP = {}  # {telegram_id: {"discord_id": str, "webhook": str}}

# ====== FUNZIONI UTILI ======
def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)


def telegram_html_to_discord(html_text: str) -> str:
    """
    Converte HTML di Telegram in formattazione Discord.
    Copre tutti i principali tag:
    <b>, <i>, <u>, <s>, <code>, <pre>, <a href="">
    """
    text = html_text

    # Decode entità HTML
    text = unescape(text)

    # <b> -> **
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL|re.IGNORECASE)
    # <strong> -> **
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL|re.IGNORECASE)
    # <i> -> *
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL|re.IGNORECASE)
    # <em> -> *
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL|re.IGNORECASE)
    # <u> -> __
    text = re.sub(r"<u>(.*?)</u>", r"__\1__", text, flags=re.DOTALL|re.IGNORECASE)
    # <s> -> ~~
    text = re.sub(r"<s>(.*?)</s>", r"~~\1~~", text, flags=re.DOTALL|re.IGNORECASE)
    # <strike> -> ~~
    text = re.sub(r"<strike>(.*?)</strike>", r"~~\1~~", text, flags=re.DOTALL|re.IGNORECASE)
    # <code> -> `
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL|re.IGNORECASE)
    # <pre> -> ```
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL|re.IGNORECASE)
    # <a href="URL">text</a> -> [text](URL)
    text = re.sub(r'<a href="(.*?)">(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL|re.IGNORECASE)
    # Rimuove eventuali tag rimanenti
    text = re.sub(r"<.*?>", "", text, flags=re.DOTALL)
    return text.strip()


def get_discord_webhook(message_text: str):
    """Restituisce il webhook corretto in base all'hashtag iniziale."""
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return DISCORD_WEBHOOKS.get(hashtag)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    """Notifica l'admin su Telegram con link cliccabile al messaggio originale."""
    if not ADMIN_CHAT_ID:
        logger.warning("⚠️ ADMIN_CHAT_ID non impostato")
        return
    try:
        chat_id_abs = str(abs(TELEGRAM_SOURCE_CHAT_ID))
        message_link = f"https://t.me/c/{chat_id_abs[4:]}/{message_id}"
        alert_text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nLINK: {message_link}"
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=alert_text)
    except Exception as e:
        logger.error(f"Errore notifica admin: {e}")


def forward_to_discord(telegram_id: int, content: str, file_url: str = None, media_type: str = None):
    """Invia messaggio a Discord tramite webhook e salva mapping."""
    webhook_url = get_discord_webhook(content)
    if not webhook_url:
        logger.warning(f"Nessun webhook trovato per {content[:50]}...")
        return

    content_formatted = telegram_html_to_discord(content)

    try:
        if media_type == "image" and file_url:
            payload = {"embeds": [{"description": content_formatted, "image": {"url": file_url}}]}
            r = requests.post(webhook_url, json=payload)
        elif media_type == "video" and file_url:
            if content_formatted:
                r = requests.post(webhook_url, json={"embeds": [{"description": content_formatted}]})
            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            r = requests.post(webhook_url, files=files)
        else:
            r = requests.post(webhook_url, json={"content": content_formatted})

        discord_id = r.json().get("id")
        if discord_id:
            MSG_MAP[str(telegram_id)] = {"discord_id": discord_id, "webhook": webhook_url}
            save_mapping()
        logger.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logger.error(f"Errore inoltro a Discord: {e}")


# ====== HANDLER TELEGRAM ======
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.effective_message
    if not message:
        return
    content = message.text_html or message.caption_html or ""
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
    elif message.document:
        file = await context.bot.get_file(message.document.file_id)
        file_url = file.file_path
        media_type = "document"

    forward_to_discord(message.message_id, content, file_url, media_type)


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.edited_message
    if not message:
        return
    content = f"✏️ Messaggio modificato:\n{message.text_html or message.caption_html}"
    forward_to_discord(message.message_id, content)


async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    """Polling per messaggi eliminati su Telegram e Discord."""
    for telegram_id, data in list(MSG_MAP.items()):
        try:
            msg = await context.bot.forward_message(
                chat_id=ADMIN_CHAT_ID,
                from_chat_id=TELEGRAM_SOURCE_CHAT_ID,
                message_id=int(telegram_id)
            )
            await msg.delete()
        except Exception:
            # eliminato su Telegram → cancella su Discord
            discord_id = data["discord_id"]
            webhook = data["webhook"]
            requests.delete(f"{webhook}/messages/{discord_id}")
            del MSG_MAP[telegram_id]
            save_mapping()
            logger.info(f"Messaggio {telegram_id} eliminato anche su Discord")


# ====== MAIN ======
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))

    if app.job_queue:
        app.job_queue.run_repeating(check_deleted_messages, interval=30)
    else:
        logger.warning("JobQueue non disponibile, polling cancellazioni disabilitato")

    logger.info("✅ Bridge avviato e in ascolto...")
    await app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception(f"Errore critico nel bot: {e}")
