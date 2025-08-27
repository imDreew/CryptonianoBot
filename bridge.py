# bridge.py
import os
import re
import json
import logging
import requests
import asyncio

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
)

import pyro_helper  # <--- modulo helper che scarica i file grossi via Pyrogram

# logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# env vars
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID", "0"))
ADMIN_CHAT_ID = int(os.getenv("TELEGRAM_ADMIN_CHAT_ID", "0"))

# mapping webhook Discord
DISCORD_WEBHOOKS = {}
try:
    DISCORD_WEBHOOKS = json.loads(os.getenv("DISCORD_WEBHOOKS", "{}"))
except Exception:
    logger.warning("Variabile DISCORD_WEBHOOKS non è un JSON valido.")


# --- utils --- #
def telegram_html_to_discord(text: str) -> str:
    """Conversione minimale da HTML Telegram a testo Discord."""
    if not text:
        return ""
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<u>(.*?)</u>", r"__\1__", text, flags=re.DOTALL)
    text = re.sub(r"<a href=['\"](.*?)['\"]>(.*?)</a>", r"[\2](\1)", text)
    text = re.sub(r"<.*?>", "", text)
    return text


def get_discord_webhook(content: str) -> str | None:
    """Determina il webhook Discord in base agli hashtag nel messaggio."""
    if not content:
        return None
    hashtags = re.findall(r"#(\w+)", content)
    for tag in hashtags:
        if tag.lower() in DISCORD_WEBHOOKS:
            return DISCORD_WEBHOOKS[tag.lower()]
    return None


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str, message_id: int = None):
    """Manda una notifica all'admin se configurato."""
    if not ADMIN_CHAT_ID:
        return
    try:
        if message_id:
            await context.bot.send_message(ADMIN_CHAT_ID, f"{text}\n(rif. messaggio {message_id})")
        else:
            await context.bot.send_message(ADMIN_CHAT_ID, text)
    except Exception as e:
        logger.error("Errore notify_admin: %s", e)


# --- core --- #
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    try:
        # recupero content (html -> testo Discord)
        content_html = msg.text_html or msg.caption_html or ""
        content = telegram_html_to_discord(content_html)

        # webhook per forwarding
        webhook_url = get_discord_webhook(content)
        if not webhook_url:
            return

        media_type = None
        file_url = None
        caption = msg.caption_html or ""

        try:
            if msg.photo:
                file_id = msg.photo[-1].file_id
                file = await context.bot.get_file(file_id)
                file_url = file.file_path
                media_type = "image"
            elif msg.video:
                file_id = msg.video.file_id
                file = await context.bot.get_file(file_id)
                file_url = file.file_path
                media_type = "video"
            elif msg.document:
                file_id = msg.document.file_id
                file = await context.bot.get_file(file_id)
                file_url = file.file_path
                media_type = "document"
            else:
                # solo testo → embed su Discord
                requests.post(webhook_url, json={"embeds": [{"description": content}]})
                return

        except Exception as e:
            err_text = str(e)
            # fallback se il file è troppo grande per Bot API
            if "File is too big" in err_text or "file is too big" in err_text.lower():
                try:
                    await pyro_helper.download_and_forward(
                        chat_id=msg.chat_id,
                        message_id=msg.message_id,
                        webhook_url=webhook_url,
                        caption=content,
                        media_type=media_type,
                    )
                except Exception as e2:
                    await notify_admin(context, f"Errore Pyrogram helper: {e2}", msg.message_id)
                return
            else:
                await notify_admin(context, f"Errore nel recupero media: {e}", msg.message_id)
                return

        # se il file è piccolo, invio normale
        if caption:
            requests.post(webhook_url, json={"embeds": [{"description": telegram_html_to_discord(caption)}]})
        if file_url:
            file_bytes = await context.bot.download_file(file.file_path)
            files = {"file": (os.path.basename(file.file_path), file_bytes)}
            requests.post(webhook_url, files=files)

    except Exception as e:
        logger.error("Errore in forward_message: %s", e)
        await notify_admin(context, f"Errore generico in forward_message: {e}", msg.message_id)


# --- main --- #
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Chat(SOURCE_CHAT_ID), forward_message))

    # init client Pyrogram
    await pyro_helper.init_client()

    try:
        await app.run_polling(poll_interval=10)
    finally:
        await pyro_helper.stop_client()


if __name__ == "__main__":
    asyncio.run(main())
