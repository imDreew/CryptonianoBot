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
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🔑 Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
TELEGRAM_SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID", "0"))
DISCORD_WEBHOOKS = json.loads(os.getenv("DISCORD_WEBHOOKS", "{}"))

MAPPING_FILE = "messages.json"

# Carica mapping
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)
else:
    MSG_MAP = {}  # {telegram_id: {"discord_id": str, "webhook": str}}

# ====== Funzione helper ======
def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)

def telegram_html_to_discord(html_text: str) -> str:
    """Converte HTML di Telegram in markdown compatibile Discord."""
    import re
    text = html_text or ""
    # grassetto <b>
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)
    # corsivo <i>
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)
    # sottolineato <u>
    text = re.sub(r'<u>(.*?)</u>', r'__\1__', text, flags=re.DOTALL)
    # barrato <s>
    text = re.sub(r'<s>(.*?)</s>', r'~~\1~~', text, flags=re.DOTALL)
    # link <a href="url">text</a>
    text = re.sub(r'<a href="(.*?)">(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)
    # codice inline <code>
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)
    # blocco codice <pre>
    text = re.sub(r'<pre>(.*?)</pre>', r'```\1```', text, flags=re.DOTALL)
    # emoji Telegram escaped
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text

def get_discord_webhook(message_text: str):
    """Restituisce il webhook corretto in base all'hashtag iniziale."""
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return DISCORD_WEBHOOKS.get(hashtag)

# ====== Notifica admin ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not ADMIN_CHAT_ID:
        logger.warning("⚠️ ADMIN_CHAT_ID non impostato")
        return
    try:
        message_link = f"https://t.me/c/{str(TELEGRAM_SOURCE_CHAT_ID)[4:]}/{message_id}"
        alert_text = f"‼️ERRORE‼️\nCAUSA: {cause}\nLINK MESSAGGIO: {message_link}"
        await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=alert_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Errore nell'invio notifica admin: {e}")

# ====== Inoltro Discord ======
def forward_to_discord(telegram_id: int, content: str = None, file_url: str = None, media_type: str = None):
    webhook_url = get_discord_webhook(content or "")
    if not webhook_url:
        logger.warning(f"Nessun webhook trovato per messaggio: {content}")
        return
    try:
        payload = {"content": telegram_html_to_discord(content)}
        r = requests.post(webhook_url, json=payload)
        discord_message_id = r.json().get("id")
        if discord_message_id:
            MSG_MAP[str(telegram_id)] = {"discord_id": discord_message_id, "webhook": webhook_url}
            save_mapping()
        logger.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logger.error(f"Errore inoltro Discord: {e}")

# ====== Handler Telegram ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return
    content = msg.text_html or msg.caption_html or ""
    forward_to_discord(msg.message_id, content)

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    if not msg:
        return
    content = f"✏️ Messaggio modificato:\n{msg.text_html or msg.caption_html}"
    forward_to_discord(msg.message_id, content)

# ====== Job cancellazioni (placeholder) ======
async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    # da implementare se vuoi monitorare messaggi cancellati
    pass

# ====== MAIN ======
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Cancella webhook esistenti per evitare 409 Conflict
    await app.bot.delete_webhook(drop_pending_updates=True)

    # Handler
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, forward_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))

    # Job queue
    app.job_queue.run_repeating(check_deleted_messages, interval=30)

    logger.info("✅ Bridge avviato e in ascolto...")
    await app.run_polling(poll_interval=5)

if __name__ == "__main__":
    asyncio.run(main())
