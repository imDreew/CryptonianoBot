import os
import tempfile
import logging
import traceback
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)
import requests
import asyncio

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID", "0"))
TELEGRAM_ADMIN_CHAT_ID = int(os.getenv("TELEGRAM_ADMIN_CHAT_ID", "0"))
FORWARD_EDITS = os.getenv("FORWARD_EDITS", "false").lower() == "true"
INCLUDE_AUTHOR = os.getenv("INCLUDE_AUTHOR", "false").lower() == "true"

# Pyrogram creds
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("PYRO_SESSION")

# Discord webhook mapping
WEBHOOKS = {
    "#SCALPING": os.getenv("DISCORD_WEBHOOK_SCALPING"),
    "#ALGORITMO": os.getenv("DISCORD_WEBHOOK_ALGORITMO"),
    "#FORMAZIONE": os.getenv("DISCORD_WEBHOOK_FORMAZIONE"),
}

# Cache dei canali Pyrogram già risolti
PYRO_KNOWN_CHATS = set()

# Logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("bridge")


# --- Helper: invio file a Discord ---
def send_discord_file(webhook_url: str, path: str, caption: str) -> bool:
    filename = os.path.basename(path)
    try:
        with open(path, "rb") as fh:
            files = {"file": (filename, fh)}
            data = {"content": caption or ""}
            resp = requests.post(webhook_url, data=data, files=files, timeout=600)
        logger.info("Discord: upload file '%s' (%s bytes).", filename, os.path.getsize(path))
        logger.info("Discord: risposta upload file -> %s (%s)", resp.status_code, resp.text[:200])
        return resp.status_code in (200, 204)
    except Exception:
        logger.exception("Discord: errore upload file.")
        return False


# --- Helper: formattazione testo ---
def telegram_to_discord_markdown(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("**", "\\*\\*")
        .replace("__", "\\_\\_")
        .replace("`", "\\`")
        .replace("~~", "\\~\\~")
    )


# --- Autoinvite per Pyrogram ---
async def _get_autoinvite_for_pyro(context, chat_id: int):
    try:
        logger.info("Autoinvite: provo a creare un link di invito via Bot API per chat_id=%s", chat_id)
        link = await context.bot.create_chat_invite_link(
            chat_id=chat_id,
            name="bridge-autoinvite",
            creates_join_request=False,
            expire_date=None,
            member_limit=0,
        )
        logger.info("Autoinvite: creato con successo.")
        return link.invite_link
    except Exception as e:
        logger.warning("Autoinvite: creazione fallita (%s)", e)
        return None


# --- Pyrogram download ---
async def pyro_download_by_ids(context, chat_id: int, message_id: int) -> str | None:
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        logger.error("Pyrogram: configurazione mancante (TG_API_ID/API_HASH/SESSION).")
        return None
    try:
        from pyro_helper_sync import download_media_via_pyro_async
    except Exception as e:
        logger.exception("Pyrogram: import helper fallito: %s", e)
        return None

    invite_link = None
    if chat_id not in PYRO_KNOWN_CHATS:
        logger.info("Pyrogram: canale %s non in cache, genero autoinvite via Bot API…", chat_id)
        invite_link = await _get_autoinvite_for_pyro(context, chat_id)
        masked = (invite_link[:20] + "…") if invite_link else "None"
        logger.info("Pyrogram: invite_link=%s", masked)

    logger.info("Pyrogram: avvio download (chat_id=%s, message_id=%s)", chat_id, message_id)
    try:
        path = await download_media_via_pyro_async(
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
            session_string=TG_SESSION,
            chat_id=chat_id,
            message_id=message_id,
            download_dir=tempfile.gettempdir(),
            invite_link=invite_link,
        )
        PYRO_KNOWN_CHATS.add(chat_id)
        logger.info("Pyrogram: download completato. Path: %s", path)
        return path
    except Exception as e:
        logger.exception("Pyrogram: errore nel download: %s", e)
        return None


# --- Handler principale ---
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    chat_id = msg.chat_id
    message_id = msg.id
    logger.info("MSG: ricevuto update message_id=%s chat_id=%s", message_id, chat_id)

    # Trova hashtag
    caption = msg.caption or msg.text or ""
    webhook_url = None
    for tag, url in WEBHOOKS.items():
        if tag in caption and url:
            webhook_url = url
            logger.info("Routing: trovato tag %s -> webhook configurato=%s", tag, bool(url))
            break
    if not webhook_url:
        return

    # Prepara testo
    text = telegram_to_discord_markdown(caption)

    # Controlla media
    path = None
    try:
        if msg.video and (msg.video.file_size or 0) >= 20 * 1024 * 1024:
            logger.info("MSG: media=video name=%s size=%s", msg.video.file_name, msg.video.file_size)
            logger.info("FLOW: uso Pyrogram (file >= 20MB o size ignota).")
            path = await pyro_download_by_ids(context, chat_id, message_id)
        elif msg.document and (msg.document.file_size or 0) >= 20 * 1024 * 1024:
            logger.info("MSG: media=document name=%s size=%s", msg.document.file_name, msg.document.file_size)
            logger.info("FLOW: uso Pyrogram (file >= 20MB o size ignota).")
            path = await pyro_download_by_ids(context, chat_id, message_id)
        else:
            # Media piccoli (o solo testo) → Bot API
            if msg.video:
                path = await msg.video.get_file().download_to_drive()
            elif msg.document:
                path = await msg.document.get_file().download_to_drive()
            elif msg.photo:
                path = await msg.photo[-1].get_file().download_to_drive()
            elif msg.audio:
                path = await msg.audio.get_file().download_to_drive()
            elif msg.voice:
                path = await msg.voice.get_file().download_to_drive()

        if path:
            ok = send_discord_file(webhook_url, path, text)
            if not ok:
                logger.error("FLOW: invio Discord fallito.")
        else:
            # fallback: testo solo
            requests.post(webhook_url, json={"content": text})
    except Exception:
        logger.error("FLOW: errore generale gestione media (Pyrogram).\n%s", traceback.format_exc())
    finally:
        # Cleanup file temporanei
        try:
            if path and os.path.exists(path) and os.path.isfile(path):
                os.remove(path)
                logger.info("Cleanup: file temporaneo rimosso -> %s", path)
        except Exception:
            logger.debug("Cleanup: rimozione file temp fallita", exc_info=True)


# --- Avvio bot ---
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN non configurato")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, on_message))

    logger.info("Bridge: avvio polling…")
    app.run_polling()


if __name__ == "__main__":
    main()

