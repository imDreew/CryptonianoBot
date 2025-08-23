import os
import sys
import logging
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
)

# ====== VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")  # dove ricevere notifiche

# Mappatura hashtag → webhook Discord
WEBHOOK_MAP = {
    "#ANALISI": os.getenv("DISCORD_WEBHOOK_ANALISI"),
    "#COPY_TRADING": os.getenv("DISCORD_WEBHOOK_COPY"),
    "#DISCUSSIONE": os.getenv("DISCORD_WEBHOOK_DISCUSSIONE"),
}

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_SOURCE_CHAT_ID:
    print("❌ ERRORE: manca una variabile di ambiente!")
    sys.exit(1)

TELEGRAM_SOURCE_CHAT_ID = int(TELEGRAM_SOURCE_CHAT_ID)

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== FUNZIONE NOTIFICA ADMIN ======
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    if not TELEGRAM_ADMIN_CHAT_ID:
        logging.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato, impossibile notificare admin")
        return

    chat_id_abs = str(abs(TELEGRAM_SOURCE_CHAT_ID))
    message_link = f"https://t.me/c/{chat_id_abs[4:]}/{message_id}"

    alert_text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nLINK: {message_link}"

    try:
        await context.bot.send_message(
            chat_id=int(TELEGRAM_ADMIN_CHAT_ID),
            text=alert_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Impossibile notificare l'admin: {e}")

# ====== FUNZIONI UTILI ======
def resolve_webhook(content: str) -> str:
    if not content:
        return None
    for tag, webhook in WEBHOOK_MAP.items():
        if webhook and content.strip().startswith(tag):
            return webhook
    return None

# ====== HANDLER PRINCIPALE (nuovo messaggio) ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or ""
    caption = msg.caption_html or ""

    file_url, media_type = None, None
    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            file_url, media_type = file.file_path, "image"
        elif msg.video:
            file_id = msg.video.file_id
            file = await context.bot.get_file(file_id)
            file_url, media_type = file.file_path, "video"
        elif msg.document:
            file_id = msg.document.file_id
            file = await context.bot.get_file(file_id)
            file_url, media_type = file.file_path, "document"
    except Exception as e:
        await notify_admin(context, f"Errore nel recupero media: {e}", msg.message_id)
        return

    webhook_url = resolve_webhook(content or caption)
    if not webhook_url:
        logging.warning("Nessun webhook corrispondente trovato, messaggio ignorato.")
        return

    try:
        if file_url and media_type == "image":
            embed_text = caption or content
            embed = {"description": embed_text, "image": {"url": file_url}}
            requests.post(webhook_url, json={"embeds": [embed]})

        elif file_url and media_type == "video":
            if caption or content:
                embed = {"description": caption or content}
                requests.post(webhook_url, json={"embeds": [embed]})
            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            requests.post(webhook_url, files=files)

        else:
            payload = {"content": content}
            requests.post(webhook_url, json=payload)

        logging.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logging.error(f"Errore inoltro a Discord: {e}")
        await notify_admin(context, f"Errore inoltro: {e}", msg.message_id)

# ====== HANDLER EDIT ======
async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.text_html or msg.caption_html or ""
    webhook_url = resolve_webhook(content)
    if not webhook_url:
        return

    embed = {"description": f"✏️ Messaggio modificato:\n\n{content}"}
    requests.post(webhook_url, json={"embeds": [embed]})

# ====== HANDLER DELETE ======
async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for webhook in WEBHOOK_MAP.values():
        if webhook:
            embed = {"description": "❌ Un messaggio è stato eliminato su Telegram."}
            requests.post(webhook, json={"embeds": [embed]})

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # nuovo messaggio
    app.add_handler(MessageHandler(filters.ALL, forward_message))

    # modifiche (usa filter edited)
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))

    # cancellazioni (usa filter deleted)
    app.add_handler(MessageHandler(filters.UpdateType.DELETED_MESSAGE, handle_delete))

    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling()

if __name__ == "__main__":
    main()
