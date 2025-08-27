import os
import sys
import json
import logging
import asyncio
import requests
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")

# Webhook multipli per hashtag
HASHTAG_TO_WEBHOOK = {
    "#ANALISI": os.getenv("DISCORD_WEBHOOK_ANALISI"),
    "#COPY_TRADING": os.getenv("DISCORD_WEBHOOK_COPY"),
    "#DISCUSSIONE": os.getenv("DISCORD_WEBHOOK_DISCUSSIONE"),
}

if not BOT_TOKEN or not HASHTAG_TO_WEBHOOK or not TELEGRAM_SOURCE_CHAT_ID:
    logger.error("❌ ERRORE: Manca BOT_TOKEN o DISCORD_WEBHOOK nelle variabili d'ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)

# File mapping per messaggi inoltrati
MAPPING_FILE = "messages.json"
if os.path.exists(MAPPING_FILE):
    with open(MAPPING_FILE, "r") as f:
        MSG_MAP = json.load(f)
else:
    MSG_MAP = {}  # {telegram_id: {"discord_id": str, "webhook": str}}

# ================== HELPERS ==================
def telegram_html_to_discord(text: str) -> str:
    """
    Converte HTML Telegram in formattazione Discord.
    Gestisce: <b>, <strong>, <i>, <em>, <u>, <s>, <code>, <pre>, <a>
    """
    import re
    if not text:
        return ""

    # Grassetto
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)

    # Corsivo
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)

    # Sottolineato (Discord non supporta nativo, mettiamo __)
    text = re.sub(r"<u>(.*?)</u>", r"__\1__", text, flags=re.DOTALL)

    # Barrato
    text = re.sub(r"<s>(.*?)</s>", r"~~\1~~", text, flags=re.DOTALL)

    # Codice inline
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)

    # Blocco codice
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)

    # Link
    text = re.sub(r'<a href="(.*?)">(.*?)</a>', r"[\2](\1)", text, flags=re.DOTALL)

    # Rimuove eventuali tag rimasti
    text = re.sub(r"<.*?>", "", text, flags=re.DOTALL)

    return text

def save_mapping():
    with open(MAPPING_FILE, "w") as f:
        json.dump(MSG_MAP, f)

def get_discord_webhook(message_text: str):
    """Restituisce il webhook Discord corretto in base all'hashtag in testa."""
    words = message_text.strip().split()
    if not words:
        return None
    hashtag = words[0].upper()
    return HASHTAG_TO_WEBHOOK.get(hashtag)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    """Notifica l'admin su Telegram con link al messaggio originale."""
    if not ADMIN_CHAT_ID:
        logger.warning("⚠️ ADMIN_CHAT_ID non impostato, impossibile notificare admin")
        return
    chat_id_abs = str(abs(TELEGRAM_SOURCE_CHAT_ID))
    message_link = f"https://t.me/c/{chat_id_abs[4:]}/{message_id}"
    alert_text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nLINK MESSAGGIO: {message_link}"
    try:
        await context.bot.send_message(
            chat_id=int(ADMIN_CHAT_ID),
            text=alert_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Impossibile notificare admin: {e}")

def forward_to_discord(telegram_id: int, content: str = None, file_url: str = None, media_type: str = None):
    """Inoltra il messaggio su Discord tramite webhook, testo e file."""
    if not content:
        content = ""
    content = telegram_html_to_discord(content)

    webhook_url = get_discord_webhook(content)
    if not webhook_url:
        logger.warning(f"Nessun webhook trovato per messaggio: {content}")
        return

    payload = {"content": content}

    try:
        if file_url:
            if media_type in ["image", "video", "document"]:
                file_data = requests.get(file_url).content
                filename = file_url.split("/")[-1]
                files = {"file": (filename, file_data)}
                requests.post(webhook_url, data={"content": content}, files=files)
            else:
                requests.post(webhook_url, json=payload)
        else:
            requests.post(webhook_url, json=payload)

        # Salva mapping
        MSG_MAP[str(telegram_id)] = {"webhook": webhook_url}
        save_mapping()
        logger.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logger.error(f"Errore nell'inoltro a Discord: {e}")

# ================= HANDLER TELEGRAM =================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.effective_message
    if not message or message.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = message.text_html or message.caption_html or ""
    file_url = None
    media_type = None

    try:
        if message.photo:
            file_id = message.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "image"
        elif message.video:
            file_id = message.video.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "video"
        elif message.document:
            file_id = message.document.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
            media_type = "document"
    except Exception as e:
        await notify_admin(context, f"Errore nel recupero del media: {e}", message.message_id)
        return

    forward_to_discord(message.message_id, content, file_url, media_type)

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.edited_message
    if not message:
        return
    content = f"✏️ Messaggio modificato:\n{message.text_html or message.caption_html or ''}"
    forward_to_discord(message.message_id, content)

async def check_deleted_messages(context: ContextTypes.DEFAULT_TYPE):
    """Controlla i messaggi cancellati e li elimina da Discord."""
    for telegram_id, data in list(MSG_MAP.items()):
        try:
            await context.bot.forward_message(
                chat_id=int(ADMIN_CHAT_ID),
                from_chat_id=TELEGRAM_SOURCE_CHAT_ID,
                message_id=int(telegram_id)
            )
        except Exception:
            # Messaggio cancellato su Telegram → cancella anche su Discord
            webhook = data["webhook"]
            discord_id = data.get("discord_id")
            if discord_id:
                url = webhook + f"/messages/{discord_id}"
                requests.delete(url)
            del MSG_MAP[telegram_id]
            save_mapping()
            logger.info(f"❌ Messaggio {telegram_id} eliminato anche su Discord")

# ================= MAIN =================
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    app.job_queue.run_repeating(check_deleted_messages, interval=30)

    logger.info("✅ Bridge avviato e in ascolto...")
    await app.run_polling(poll_interval=10)

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()  # Permette di riusare il loop già in esecuzione

    import asyncio
    asyncio.get_event_loop().run_until_complete(main())
