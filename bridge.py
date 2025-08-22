import os
import sys
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ====== VARIABILI D'AMBIENTE ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")  # dove ricevere notifiche

if not TELEGRAM_BOT_TOKEN or not DISCORD_WEBHOOK_URL or not TELEGRAM_SOURCE_CHAT_ID:
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
    """
    Invia notifica all'admin in caso di errore nell'inoltro di messaggi.
    Il messaggio contiene il link diretto al messaggio Telegram.
    """
    if not TELEGRAM_ADMIN_CHAT_ID:
        logging.error("⚠️ TELEGRAM_ADMIN_CHAT_ID non configurato!")
        return

    # Costruisci link cliccabile al messaggio nel canale privato
    # Rimuoviamo il prefisso -100 dal chat_id
    channel_link_id = str(TELEGRAM_SOURCE_CHAT_ID)[4:] if str(TELEGRAM_SOURCE_CHAT_ID).startswith("-100") else str(TELEGRAM_SOURCE_CHAT_ID)
    message_link = f"https://t.me/c/{channel_link_id}/{message_id}"

    alert_text = (
        "‼️ERRORE INOLTRO‼️\n"
        f"CAUSA: {cause}\n"
        f"LINK MESSAGGIO: {message_link}"
    )

    try:
        await context.bot.send_message(chat_id=TELEGRAM_ADMIN_CHAT_ID, text=alert_text)
        logging.info("Notifica inviata all'admin")
    except Exception as e:
        logging.error(f"Impossibile notificare l'admin: {e}")


# ====== HANDLER TELEGRAM ======
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    # Testo principale del messaggio
    content = msg.text_html or ""

    # Gestione media
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
        await notify_admin(context, f"Errore nel recupero del media: {e}", msg.message_id)

    # Costruisci il contenuto da inviare su Discord
    try:
        if file_url and media_type == "image":
    # Immagine → embed con anteprima
            embed_text = caption or content
            embed = {"description": embed_text, "image": {"url": file_url}}
            payload = {"embeds": [embed]}
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload)

        elif file_url and media_type == "video":
    # Video → prima embed con caption
        if caption or content:
            embed = {"description": caption or content}
            requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})
    
    # Poi invia il video come allegato
            video_data = requests.get(file_url).content
            files = {"file": ("video.mp4", video_data)}
            requests.post(DISCORD_WEBHOOK_URL, files=files)

        else:
            # Solo testo o documenti
            payload = {"content": content}
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload)

        response.raise_for_status()
        logging.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logging.error(f"Errore nell'inoltro a Discord: {e}")
        await notify_admin(context, f"Errore nell'inoltro: {e}", msg.message_id)





async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    """Notifica l'admin su Telegram con link cliccabile al messaggio originale."""
    if not TELEGRAM_ADMIN_CHAT_ID:
        logging.warning("⚠️ TELEGRAM_ADMIN_CHAT_ID non impostato, impossibile notificare admin")
        return

    # Genera link al messaggio nel canale privato
    chat_id_abs = str(abs(TELEGRAM_SOURCE_CHAT_ID))
    message_link = f"https://t.me/c/{chat_id_abs[4:]}/{message_id}"

    alert_text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nID MESSAGGIO: {message_link}"

    try:
        await context.bot.send_message(
            chat_id=int(TELEGRAM_ADMIN_CHAT_ID),
            text=alert_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Impossibile notificare l'admin: {e}")


# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))
    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling()


if __name__ == "__main__":
    main()
