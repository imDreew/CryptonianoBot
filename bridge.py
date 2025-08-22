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
    if not msg:
        return

    if msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    content = msg.caption_html or msg.text_html or ""

    file_ids = []
    # Gestione media singolo
    if msg.photo:
        file_ids.append(msg.photo[-1].file_id)
    elif msg.video:
        file_ids.append(msg.video.file_id)
    elif msg.document:
        file_ids.append(msg.document.file_id)

    # Gestione album (media_group_id)
    if hasattr(msg, "media_group_id") and msg.media_group_id:
        # Prendo tutti i messaggi dell'album già salvati in cache
        context.chat_data.setdefault(msg.media_group_id, []).append(msg)
        # Elaboro solo quando ho ricevuto tutto l'album
        return

    try:
        if file_ids:
            for file_id in file_ids:
                file = await context.bot.get_file(file_id)
                file_url = file.file_path

                # Controllo dimensione
                head = requests.head(file_url)
                size_mb = int(head.headers.get("Content-Length", 0)) / (1024 * 1024)
                if size_mb > 8:  # limite classico Discord
                    await notify_admin(context, f"File troppo grande ({size_mb:.2f} MB), non inoltrato.")
                    return

                response = requests.post(
                    DISCORD_WEBHOOK_URL,
                    data={"content": content} if content else None,
                    files={"file": requests.get(file_url).content}
                )
                response.raise_for_status()
                logging.info(f"Inoltrato a Discord: {content[:50]}...")
                content = ""  # testo solo sul primo media
        else:
            # Solo testo
            response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
            response.raise_for_status()
            logging.info(f"Inoltrato a Discord: {content[:50]}...")
    except Exception as e:
        logging.error(f"Errore nell'inoltro a Discord: {e}")
        await notify_admin(context, f"Errore nell'inoltro: {e}")


# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))
    logging.info("✅ Bridge avviato e in ascolto...")
    app.run_polling()


if __name__ == "__main__":
    main()
