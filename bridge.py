import os
import io
import time
import json
import logging
import tempfile
from typing import Optional, Dict, Any

import requests
from requests import Response

from telegram import Update, Message, MessageEntity
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    MessageHandler, filters, CallbackQueryHandler, CommandHandler
)

# === Logging ================================================================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bridge")

# === Env / Config ==========================================================
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1", "true", "yes", "on")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN mancante.")

TELEGRAM_SOURCE_CHAT_ID = int(os.getenv("TELEGRAM_SOURCE_CHAT_ID", "0"))
TELEGRAM_ADMIN_CHAT_ID = int(os.getenv("TELEGRAM_ADMIN_CHAT_ID", "0"))

FORWARD_EDITS = env_bool("FORWARD_EDITS", False)
INCLUDE_AUTHOR = env_bool("INCLUDE_AUTHOR", False)

# Webhooks Discord per categorie/hashtag
DISCORD_WEBHOOK_SCALPING   = os.getenv("DISCORD_WEBHOOK_SCALPING", "").strip()
DISCORD_WEBHOOK_ALGORITMO  = os.getenv("DISCORD_WEBHOOK_ALGORITMO", "").strip()
DISCORD_WEBHOOK_FORMAZIONE = os.getenv("DISCORD_WEBHOOK_FORMAZIONE", "").strip()
DISCORD_WEBHOOK_DEFAULT    = os.getenv("DISCORD_WEBHOOK_DEFAULT", "").strip()  # opzionale

WEBHOOK_MAP = {
    "SCALPING": DISCORD_WEBHOOK_SCALPING,
    "ALGORITMO": DISCORD_WEBHOOK_ALGORITMO,
    "FORMAZIONE": DISCORD_WEBHOOK_FORMAZIONE,
}

# Telegram file size limit per download diretto via getFile + HTTP
BOT_API_LIMIT = 20 * 1024 * 1024  # 20MB

# Pyrogram fallback (download media via client)
TG_API_ID  = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION  = os.getenv("PYRO_SESSION", os.getenv("TG_SESSION", "")).strip()  # compat
if not TG_SESSION:
    logger.info("TG_SESSION/PYRO_SESSION non impostato: il fallback Pyrogram per >20MB non sarà disponibile.")

# Webhook HTTP server (Railway/Render/Heroku)
PUBLIC_BASE = os.getenv("PUBLIC_BASE", os.getenv("RAILWAY_STATIC_URL", "")).strip()
PORT = int(os.getenv("PORT", "8080"))

# === Utils Discord =========================================================
def _post_with_retry(url: str, **kwargs) -> Response:
    """Retry backoff su 429 / 5xx."""
    for attempt in range(5):
        r = requests.post(url, timeout=120, **kwargs)
        if r.status_code not in (429,) and r.status_code < 500:
            return r
        wait = min(2 ** attempt, 30)
        logger.warning("Discord POST %s -> %s. Retry tra %ss", url, r.status_code, wait)
        time.sleep(wait)
    return r

def send_discord_text(webhook_url: str, content: str) -> bool:
    if not webhook_url:
        logger.info("Webhook Discord mancante; skip.")
        return False
    r = _post_with_retry(webhook_url, json={"content": content})
    ok = 200 <= r.status_code < 300
    if not ok:
        logger.error("Errore Discord text: %s %s", r.status_code, r.text)
    return ok

def send_discord_file(webhook_url: str, file_bytes: bytes, filename: str, content: Optional[str] = None) -> bool:
    if not webhook_url:
        logger.info("Webhook Discord mancante; skip.")
        return False
    files = {"file": (filename, io.BytesIO(file_bytes))}
    data = {"content": content} if content else {}
    r = _post_with_retry(webhook_url, files=files, data=data)
    ok = 200 <= r.status_code < 300
    if not ok:
        logger.error("Errore Discord file: %s %s", r.status_code, r.text)
    return ok

# === Pyrogram fallback ======================================================
def pyro_download_by_ids(chat_id: int, message_id: int) -> Optional[str]:
    """
    Usa Pyrogram (session string) per scaricare media >20MB.
    Restituisce path locale del file, o None su errore.
    """
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        return None
    try:
        from pyro_helper_sync import download_media_via_pyro
    except Exception as e:
        logger.exception("Impossibile importare pyro_helper_sync: %s", e)
        return None
    try:
        path = download_media_via_pyro(
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
            session_string=TG_SESSION,
            chat_id=chat_id,
            message_id=message_id,
            download_dir=tempfile.gettempdir(),
        )
        return path
    except Exception as e:
        logger.exception("Errore Pyrogram download: %s", e)
        return None

# === Parsing & routing ======================================================
def pick_webhook_from_text(text: str) -> Optional[str]:
    """
    Cerca #SCALPING / #ALGORITMO / #FORMAZIONE (case-insensitive).
    """
    if not text:
        return WEBHOOK_MAP.get("SCALPING") or DISCORD_WEBHOOK_DEFAULT or None

    up = text.upper()
    for tag, url in WEBHOOK_MAP.items():
        if f"#{tag}" in up or f" {tag}" in up:
            return url or DISCORD_WEBHOOK_DEFAULT or None
    return DISCORD_WEBHOOK_DEFAULT or None

def author_suffix(msg: Message) -> str:
    if not INCLUDE_AUTHOR or not msg.from_user:
        return ""
    u = msg.from_user
    handle = f"@{u.username}" if u.username else ""
    name = u.full_name or handle or str(u.id)
    return f"\n\n— {name} {handle}".strip()

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if TELEGRAM_ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(TELEGRAM_ADMIN_CHAT_ID, text[:4000])
        except Exception:
            logger.exception("notify_admin fallito")

# === Handlers Telegram ======================================================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if not msg:
        return
    if msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    text = msg.caption_html or msg.text_html or ""
    webhook_url = pick_webhook_from_text(text or "")

    # Componi contenuto testuale per Discord
    content = text or ""
    content += author_suffix(msg)

    # 1) Se non c'è media → manda solo testo
    if not (msg.photo or msg.video or msg.document or msg.animation or msg.voice or msg.audio or msg.sticker):
        if content.strip():
            sent = send_discord_text(webhook_url, content)
            if not sent:
                await notify_admin(context, "Errore invio testo a Discord.")
        return

    # 2) Con media: decide se usare Telegram file API o Pyrogram.
    # Prova a ottenere file info/size
    telegram_file_id = None
    file_name = "file.bin"
    file_size = 0

    if msg.photo:
        photo = msg.photo[-1]  # largest
        telegram_file_id = photo.file_id
        file_name = "image.jpg"
        file_size = photo.file_size or 0
    elif msg.video:
        telegram_file_id = msg.video.file_id
        file_name = msg.video.file_name or "video.mp4"
        file_size = msg.video.file_size or 0
    elif msg.document:
        telegram_file_id = msg.document.file_id
        file_name = msg.document.file_name or "document.bin"
        file_size = msg.document.file_size or 0
    elif msg.animation:
        telegram_file_id = msg.animation.file_id
        file_name = msg.animation.file_name or "animation.mp4"
        file_size = msg.animation.file_size or 0
    elif msg.audio:
        telegram_file_id = msg.audio.file_id
        file_name = msg.audio.file_name or "audio.mp3"
        file_size = msg.audio.file_size or 0
    elif msg.voice:
        telegram_file_id = msg.voice.file_id
        file_name = "voice.ogg"
        file_size = msg.voice.file_size or 0
    elif msg.sticker:
        telegram_file_id = msg.sticker.file_id
        file_name = "sticker.webp"
        file_size = msg.sticker.file_size or 0

    # <20MB → scarico via getFile + HTTP; >=20MB → Pyrogram
    if file_size and file_size < BOT_API_LIMIT:
        try:
            f = await context.bot.get_file(telegram_file_id)
            # Scarico bytes e carico su Discord: NON esporre URL bot
            resp = requests.get(f.file_path, timeout=600)
            resp.raise_for_status()
            ok = send_discord_file(webhook_url, resp.content, file_name, content if content.strip() else None)
            if not ok:
                await notify_admin(context, f"Errore invio file <20MB a Discord: {file_name}")
        except Exception:
            logger.exception("Errore download <20MB via Bot API")
            await notify_admin(context, "Errore download <20MB via Bot API")
        return

    # Fallback Pyrogram (file grandi o size ignota)
    try:
        path = pyro_download_by_ids(msg.chat_id, msg.message_id)
        if not path:
            await notify_admin(context, "Download via Pyrogram fallito o non configurato.")
            return
        with open(path, "rb") as fh:
            data = fh.read()
        ok = send_discord_file(webhook_url, data, os.path.basename(path), content if content.strip() else None)
        if not ok:
            await notify_admin(context, "Errore invio file >20MB a Discord.")
    except Exception:
        logger.exception("Errore generale gestione media")
        await notify_admin(context, "Errore generale gestione media")

async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FORWARD_EDITS:
        return
    # per semplicità, riutilizza on_message (inoltra come nuovo)
    await on_message(update, context)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot attivo. Inoltro da chat sorgente verso Discord.")

# === Main ==================================================================
def build_application() -> Application:
    return ApplicationBuilder().token(BOT_TOKEN).build()

def add_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.ALL), on_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED, on_edited_message))

def run_webhook(app: Application) -> None:
    if not PUBLIC_BASE:
        raise RuntimeError("Devi impostare PUBLIC_BASE o RAILWAY_STATIC_URL per esporre il webhook.")
    # Path segreto col token per semplicità
    path = f"/{BOT_TOKEN}"
    logger.info("Avvio webhook su 0.0.0.0:%s, path=%s, base=%s", PORT, path, PUBLIC_BASE)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=path,
        webhook_url=(PUBLIC_BASE.rstrip("/") + path),
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    app = build_application()
    add_handlers(app)
    mode = os.getenv("MODE", "webhook").lower()
    if mode == "polling":
        logger.info("Avvio in polling...")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    else:
        run_webhook(app)


if __name__ == "__main__":
    main()
