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

    # Aggiungi eventuale caption dei media
    caption = getattr(msg, "caption", None)
    if caption:
        if content:
            content += "\n\n" + caption
        else:
            content = caption

    # Gestione media
    file_url = None
    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
        elif msg.video:
            file_id = msg.video.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
        elif msg.document:
            file_id = msg.document.file_id
            file = await context.bot.get_file(file_id)
            file_url = file.file_path
    except Exception as e:
        await notify_admin(context, f"Errore nel recupero del media: {e}", msg.message_id)

    payload = {"content": content}
    
    try:
        if file_url:
            # invio come link in Discord, con testo sopra
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        else:
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
