import os
import asyncio
import sys
import logging
import json
import requests
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🔑 Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
DISCORD_WEBHOOKS = os.getenv("DISCORD_WEBHOOKS")  # JSON string

if not TELEGRAM_TOKEN or not DISCORD_WEBHOOKS or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)
DISCORD_WEBHOOKS = json.loads(DISCORD_WEBHOOKS)

MAPPING_FILE = "messages.json"
MSG_MAP = {}
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)

# ====== FUNZIONI UTILI ======
def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)

def get_discord_webhook(message_text: str):
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return DISCORD_WEBHOOKS.get(hashtag)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not TELEGRAM_ADMIN_CHAT_ID:
        logging.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato")
        return
    try:
        message_link = f"https://t.me/c/{str(abs(TELEGRAM_SOURCE_CHAT_ID))[4:]}/{message_id}"
        alert_text = f"‼️ ERRORE INOLTRO ‼️\nCAUSA: {cause}\nLINK: {message_link}"
        await context.bot.send_message(
            chat_id=int(TELEGRAM_ADMIN_CHAT_ID),
            text=alert_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Errore nell'invio notifica admin: {e}")

# Conversione HTML Telegram → Markdown Discord
def telegram_html_to_discord(html_text: str) -> str:
    """Converte tag HTML da Telegram in Markdown compatibile Discord."""
    import re
    text = html_text
    # Grassetto
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    # Corsivo
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    # Barrato
    text = re.sub(r"<s>(.*?)</s>", r"~~\1~~", text, flags=re.DOTALL)
    text = re.sub(r"<strike>(.*?)</strike>", r"~~\1~~", text, flags=re.DOTALL)
    # Code inline
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    # Code block
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    # Links
    text = re.sub(r'<a href="(.*?)">(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)
    # Rimuove eventuali tag rimanenti
    text = re.sub(r"<.*?>", "", text, flags=re.DOTALL)
    return text

# ====== FUNZIONI INOLTRO ======
def forward_to_discord(telegram_id: int, content: str = None, file_url: str = None, media_type: str = None):
    webhook_url = get_discord_webhook(content or "")
    if not webhook_url:
        logger.warning(f"Nessun webhook trovato per il messaggio: {content}")
        return

    payload = {"content": telegram_html_to_discord(content) if content else ""}
    try:
        r = requests.post(webhook_url, json=payload)
        discord_message_id = r.json().get("id")
        if discord_message_id:
            MSG_MAP[str(telegram_id)] = {"discord_id": discord_message_id, "webhook": webhook_url}
            save_mapping()
        logger.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logger.error(f"Errore nell'inoltro a Discord: {e}")

# ====== HANDLER ======
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return
    content = msg.text_html or msg.caption_html or ""
    forward_to_discord(msg.message_id, content)

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.edited_message
    if not message:
        return
    content = f"✏️ Messaggio modificato:\n{message.text_html or message.caption_html or ''}"
    forward_to_discord(message.message_id, content)

async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    # Poll messaggi cancellati
    pass

# ====== MAIN ======
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    # Handler
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    # Job per cancellazioni
    app.job_queue.run_repeating(check_deleted_messages, interval=30)
    logger.info("✅ Bridge avviato e in ascolto...")
    await app.run_polling(poll_interval=10)

if __name__ == "__main__":
    asyncio.run(main())
