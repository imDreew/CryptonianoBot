# bridge.py
import os
import logging
import html
import asyncio
import requests

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
)

import pyro_helper_sync as pyro_sync

# =====================================================
# CONFIGURAZIONE LOGGING
# =====================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =====================================================
# VARIABILI D’AMBIENTE
# =====================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")  # default webhook
if not BOT_TOKEN or not DISCORD_WEBHOOK:
    logger.error("❌ ERRORE: Manca BOT_TOKEN o DISCORD_WEBHOOK nelle variabili d'ambiente!")
    exit(1)

# =====================================================
# UTILITY
# =====================================================
def telegram_html_to_discord(text: str) -> str:
    """Semplifica il testo da Telegram a formato leggibile su Discord"""
    return html.unescape(text)


async def send_to_discord(content: str = None, file=None, filename: str = None, webhook_url: str = None):
    """Invia testo o file a Discord"""
    url = webhook_url or DISCORD_WEBHOOK
    try:
        if file:
            with open(file, "rb") as f:
                files = {"file": (filename or "file", f)}
                r = requests.post(url, files=files, data={"content": content or ""})
        else:
            r = requests.post(url, json={"content": content or ""})
        r.raise_for_status()
        logger.info("✅ Messaggio inoltrato a Discord")
    except Exception as e:
        logger.error(f"Errore nell'invio a Discord: {e}")

# =====================================================
# HANDLER MESSAGGI
# =====================================================
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler principale: inoltra messaggi Telegram -> Discord"""
    msg = update.effective_message
    caption = telegram_html_to_discord(msg.caption or msg.text or "")

    try:
        # Caso: solo testo
        if msg.text:
            await send_to_discord(content=caption)
            return

        # Caso: media normale (entro i limiti Telegram API)
        if msg.effective_attachment:
            try:
                file = await msg.effective_attachment.get_file()
                file_path = await file.download_to_drive()
                await send_to_discord(content=caption, file=file_path, filename=os.path.basename(file_path))
                return
            except Exception as e:
                if "File is too big" in str(e):
                    logger.warning("⚠️ File troppo grande per Telegram API, uso Pyrogram...")
                    await asyncio.to_thread(
                        pyro_sync.download_and_forward_sync,
                        msg.chat_id, msg.id, DISCORD_WEBHOOK, caption
                    )
                    return
                else:
                    logger.error(f"Errore scaricando media: {e}")
                    await send_to_discord(content=f"‼️ ERRORE inoltro media: {e}")
                    return

    except Exception as e:
        logger.exception("Errore inatteso handler")
        await send_to_discord(content=f"‼️ ERRORE INOLTRANDO MESSAGGIO: {e}")

# =====================================================
# MAIN
# =====================================================
def main():
    logger.info("🚀 Avvio bot bridge Telegram -> Discord")

    # Avvia Pyrogram prima di run_polling()
    pyro_sync.init_client_sync()

    app = Application.builder().token(BOT_TOKEN).build()

    # Aggiunge handler (tutti i messaggi)
    app.add_handler(MessageHandler(filters.ALL, forward_message))

    try:
        app.run_polling(poll_interval=10)
    finally:
        pyro_sync.stop_client_sync()
        logger.info("✅ Bot terminato.")


if __name__ == "__main__":
    main()
