import os
import re
import logging
import requests
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, Application

from pyro_helper_sync import download_media

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.DEBUG)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"📩 Nuovo messaggio da Telegram: {update.message}")

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOKS = {}  # hashtag -> webhook URL
for k, v in os.environ.items():
    if k.startswith("DISCORD_WEBHOOK_"):
        hashtag = k.replace("DISCORD_WEBHOOK_", "").lower()
        WEBHOOKS[f"#{hashtag}"] = v

if not BOT_TOKEN or not WEBHOOKS:
    logger.error("❌ ERRORE: manca BOT_TOKEN o webhook nelle variabili d'ambiente!")
    exit(1)

# === Funzione di conversione HTML Telegram → Markdown Discord ===
def tg_to_discord(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<u>(.*?)</u>", r"__\1__", text, flags=re.DOTALL)
    text = re.sub(r"<s>(.*?)</s>", r"~~\1~~", text, flags=re.DOTALL)
    text = re.sub(r'<a href="(.*?)">(.*?)</a>', r'[\2](\1)', text)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    return text

# === Handler principale ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # Determina l'hashtag
    text = msg.text or msg.caption or ""
    hashtags = [word for word in text.split() if word.startswith("#")]
    webhook = None
    for h in hashtags:
        if h.lower() in WEBHOOKS:
            webhook = WEBHOOKS[h.lower()]
            break
    if not webhook:
        logger.info("⚠️ Nessun webhook trovato per il messaggio")
        return

    caption = tg_to_discord(msg.caption_html or msg.text_html)

    # Controllo media
    media = msg.effective_attachment
    if media:
        file_size = getattr(media, "file_size", 0)
        if file_size and file_size > 20 * 1024 * 1024:
            # Pyrogram helper per scaricare file grossi
            try:
                tme_link = f"https://t.me/c/{msg.chat.id}/{msg.message_id}"
                local_path = download_media(tme_link)
                logger.info(f"Scaricato file grosso: {local_path}")
                with open(local_path, "rb") as f:
                    resp = requests.post(
                        webhook,
                        data={"content": caption},
                        files={"file": f},
                    )
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Errore con Pyrogram helper: {e}")
        else:
            # Media piccolo -> via Bot API
            try:
                file = await context.bot.get_file(media.file_id)
                file_path = await file.download_to_drive()
                with open(file_path, "rb") as f:
                    resp = requests.post(
                        webhook,
                        data={"content": caption},
                        files={"file": f},
                    )
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Errore inoltro media piccolo: {e}")
    else:
        # Solo testo
        try:
            resp = requests.post(webhook, json={"content": caption})
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Errore inoltro testo: {e}")

async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.ALL, handle_message))

    # Imposta il webhook invece del polling
    PORT = int(os.getenv("PORT", 8080))
    WEBHOOK_URL = f"https://{os.getenv('RAILWAY_STATIC_URL')}/{BOT_TOKEN}"

    logger.info(f"✅ Impostazione webhook su {WEBHOOK_URL}")

    await app.bot.set_webhook(WEBHOOK_URL)

    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=WEBHOOK_URL,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Bridge interrotto manualmente.")
