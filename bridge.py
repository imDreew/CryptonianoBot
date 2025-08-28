import os
import io
import time
import logging
import tempfile
import re
from html import unescape
from typing import Optional

import requests
from requests import Response

from telegram import Update, Message
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    MessageHandler, filters, CommandHandler
)

# =========================
# Logging
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("bridge")


# =========================
# Env / Config
# =========================
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

# Webhooks Discord
DISCORD_WEBHOOK_SCALPING   = os.getenv("DISCORD_WEBHOOK_SCALPING", "").strip()
DISCORD_WEBHOOK_ALGORITMO  = os.getenv("DISCORD_WEBHOOK_ALGORITMO", "").strip()
DISCORD_WEBHOOK_FORMAZIONE = os.getenv("DISCORD_WEBHOOK_FORMAZIONE", "").strip()
DISCORD_WEBHOOK_DEFAULT    = os.getenv("DISCORD_WEBHOOK_DEFAULT", "").strip()  # opzionale

WEBHOOK_MAP = {
    "SCALPING": DISCORD_WEBHOOK_SCALPING,
    "ALGORITMO": DISCORD_WEBHOOK_ALGORITMO,
    "FORMAZIONE": DISCORD_WEBHOOK_FORMAZIONE,
}

# Telegram file size limit per download diretto via Bot API
BOT_API_LIMIT = 20 * 1024 * 1024  # 20MB

# Pyrogram fallback (per file >20MB)
TG_API_ID   = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION  = os.getenv("PYRO_SESSION", os.getenv("TG_SESSION", "")).strip()
if not TG_SESSION:
    logger.info("TG_SESSION/PYRO_SESSION non impostato: il fallback Pyrogram per >20MB non sarà disponibile.")
else:
    logger.info("Pyrogram abilitato (session string presente).")

# Webhook hosting
PUBLIC_BASE = os.getenv("PUBLIC_BASE", os.getenv("RAILWAY_STATIC_URL", "")).strip()
PORT = int(os.getenv("PORT", "8080"))

# Cache canali già “visti” dallo userbot (per evitare join ripetuti)
PYRO_KNOWN_CHATS: set[int] = set()

# Stato bot
BOT_ID: Optional[int] = None
BOT_USERNAME: Optional[str] = None


# =========================
# HTML Telegram -> Markdown Discord
# =========================
def tg_html_to_discord_md(html: str) -> str:
    if not html:
        return ""
    s = unescape(html)
    # bold / italic / underline / strike
    s = re.sub(r"</?(b|strong)>", "**", s, flags=re.I)
    s = re.sub(r"</?(i|em)>", "*", s, flags=re.I)
    s = re.sub(r"<u>(.*?)</u>", r"__\1__", s, flags=re.I | re.S)
    s = re.sub(r"<(s|del)>(.*?)</\1>", r"~~\2~~", s, flags=re.I | re.S)
    # inline code
    s = re.sub(r"<code>(.*?)</code>", r"`\1`", s, flags=re.I | re.S)
    # code block con language
    s = re.sub(
        r"<pre.*?>\s*<code.*?class=['\"]?language-([\w+\-]+)['\"]?.*?>(.*?)</code>\s*</pre>",
        r"```\1\n\2\n```", s, flags=re.I | re.S
    )
    # code block generico
    s = re.sub(r"<pre.*?>(.*?)</pre>", r"```\n\1\n```", s, flags=re.I | re.S)
    # link e spoiler
    s = re.sub(r'<a\s+href=["\'](.*?)["\']>(.*?)</a>', r"[\2](\1)", s, flags=re.I | re.S)
    s = re.sub(r"<tg-spoiler>(.*?)</tg-spoiler>", r"||\1||", s, flags=re.I | re.S)
    # blockquote semplice
    s = re.sub(r"<blockquote>(.*?)</blockquote>", r">\1", s, flags=re.I | re.S)
    # rimuovi tag residui
    s = re.sub(r"</?[^>]+>", "", s)
    # evita ping accidentali
    s = s.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    # normalizza spazi
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


# =========================
# Discord helpers
# =========================
def _post_with_retry(url: str, **kwargs) -> Response:
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
        logger.info("Discord: nessun webhook configurato, salto invio testo.")
        return False
    logger.info("Discord: invio testo (%d chars) al webhook selezionato.", len(content or ""))
    r = _post_with_retry(webhook_url, json={"content": content})
    ok = 200 <= r.status_code < 300
    logger.log(logging.INFO if ok else logging.ERROR,
               "Discord: risposta invio testo -> %s (%s)", r.status_code, r.text[:200])
    return ok


def send_discord_file(webhook_url: str, file_bytes: bytes, filename: str, content: Optional[str] = None) -> bool:
    if not webhook_url:
        logger.info("Discord: nessun webhook configurato, salto invio file.")
        return False
    logger.info("Discord: upload file '%s' (%d bytes).", filename, len(file_bytes or b""))
    files = {"file": (filename, io.BytesIO(file_bytes))}
    data = {"content": content} if content else {}
    r = _post_with_retry(webhook_url, files=files, data=data)
    ok = 200 <= r.status_code < 300
    logger.log(logging.INFO if ok else logging.ERROR,
               "Discord: risposta upload file -> %s (%s)", r.status_code, r.text[:200])
    return ok


# =========================
# Bot-side: crea autoinvite
# =========================
async def _get_autoinvite_for_pyro(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Optional[str]:
    """
    Crea un invite link (se il BOT è admin con permesso di invitare).
    Ritorna l'URL o None se non possibile.
    """
    logger.info("Autoinvite: provo a creare un link di invito via Bot API per chat_id=%s", chat_id)
    try:
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
        logger.warning("Autoinvite: NON creato (permessi mancanti o non admin?). Dettagli: %s", e)
        return None


# =========================
# Pyrogram fallback (async)
# =========================
async def pyro_download_by_ids(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int
) -> Optional[str]:
    """
    Usa Pyrogram (session string) per scaricare media >20MB.
    - Se lo userbot non conosce il canale, prova a generare un autoinvite e joinare.
    """
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
        logger.info("Pyrogram: canale %s non in cache, provo a generare autoinvite.", chat_id)
        invite_link = await _get_autoinvite_for_pyro(context, chat_id)
        if invite_link:
            logger.debug("Pyrogram: autoinvite generato: %s", invite_link)
        else:
            logger.info("Pyrogram: nessun autoinvite disponibile, tenterò accesso diretto.")

    logger.info("Pyrogram: avvio download per (chat_id=%s, message_id=%s)", chat_id, message_id)
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


# =========================
# Routing & helpers
# =========================
def pick_webhook_from_text(text: str) -> Optional[str]:
    """
    Cerca #SCALPING / #ALGORITMO / #FORMAZIONE (case-insensitive).
    Fallback su DISCORD_WEBHOOK_DEFAULT se non matcha nulla.
    """
    up = (text or "").upper()
    chosen = None
    for tag, url in WEBHOOK_MAP.items():
        if f"#{tag}" in up or f" {tag}" in up:
            chosen = url
            logger.info("Routing: trovato tag #%s -> webhook configurato=%s", tag, bool(url))
            break
    if not chosen:
        chosen = DISCORD_WEBHOOK_DEFAULT or None
        logger.info("Routing: nessun tag trovato -> uso webhook di default presente=%s", bool(chosen))
    return chosen


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


# =========================
# Handlers
# =========================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if not msg:
        return
    logger.info("MSG: ricevuto update message_id=%s chat_id=%s", msg.message_id, msg.chat_id)

    if msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        logger.info("MSG: chat non monitorata (%s != %s). Ignoro.", msg.chat_id, TELEGRAM_SOURCE_CHAT_ID)
        return

    # HTML Telegram -> Markdown Discord
    text_html = msg.caption_html or msg.text_html or ""
    text_md = tg_html_to_discord_md(text_html)
    logger.debug("MSG: testo convertito (len=%d).", len(text_md or ""))

    webhook_url = pick_webhook_from_text(text_md or "")

    # Contenuto per Discord (testo + autore opzionale)
    content = (text_md or "").strip()
    content += author_suffix(msg)

    # Solo testo
    has_media = any([msg.photo, msg.video, msg.document, msg.animation, msg.voice, msg.audio, msg.sticker])
    if not has_media:
        logger.info("MSG: solo testo. Invio verso Discord.")
        if content.strip():
            sent = send_discord_text(webhook_url, content)
            if not sent:
                await notify_admin(context, "Errore invio testo a Discord.")
        return

    # Con media: raccogli info
    telegram_file_id = None
    file_name = "file.bin"
    file_size = 0

    if msg.photo:
        photo = msg.photo[-1]
        telegram_file_id = photo.file_id
        file_name = "image.jpg"
        file_size = photo.file_size or 0
        logger.info("MSG: media=photo size=%s", file_size)
    elif msg.video:
        telegram_file_id = msg.video.file_id
        file_name = msg.video.file_name or "video.mp4"
        file_size = msg.video.file_size or 0
        logger.info("MSG: media=video name=%s size=%s", file_name, file_size)
    elif msg.document:
        telegram_file_id = msg.document.file_id
        file_name = msg.document.file_name or "document.bin"
        file_size = msg.document.file_size or 0
        logger.info("MSG: media=document name=%s size=%s", file_name, file_size)
    elif msg.animation:
        telegram_file_id = msg.animation.file_id
        file_name = msg.animation.file_name or "animation.mp4"
        file_size = msg.animation.file_size or 0
        logger.info("MSG: media=animation name=%s size=%s", file_name, file_size)
    elif msg.audio:
        telegram_file_id = msg.audio.file_id
        file_name = msg.audio.file_name or "audio.mp3"
        file_size = msg.audio.file_size or 0
        logger.info("MSG: media=audio name=%s size=%s", file_name, file_size)
    elif msg.voice:
        telegram_file_id = msg.voice.file_id
        file_name = "voice.ogg"
        file_size = msg.voice.file_size or 0
        logger.info("MSG: media=voice size=%s", file_size)
    elif msg.sticker:
        telegram_file_id = msg.sticker.file_id
        file_name = "sticker.webp"
        file_size = msg.sticker.file_size or 0
        logger.info("MSG: media=sticker size=%s", file_size)

    # <20MB → Bot API (getFile) + upload a Discord
    if file_size and file_size < BOT_API_LIMIT:
        logger.info("FLOW: uso Bot API (file < 20MB).")
        try:
            f = await context.bot.get_file(telegram_file_id)
            logger.debug("BotAPI: file_path=%s", f.file_path)
            resp = requests.get(f.file_path, timeout=600)
            resp.raise_for_status()
            ok = send_discord_file(webhook_url, resp.content, file_name, content if content.strip() else None)
            if not ok:
                await notify_admin(context, f"Errore invio file <20MB a Discord: {file_name}")
        except Exception:
            logger.exception("BotAPI: errore download <20MB.")
            await notify_admin(context, "Errore download <20MB via Bot API")
        return

    # >=20MB o size ignota → Pyrogram (async) con autoinvite
    logger.info("FLOW: uso Pyrogram (file >= 20MB o size ignota).")
    try:
        path = await pyro_download_by_ids(context, msg.chat_id, msg.message_id)
        if not path:
            logger.error("Pyrogram: path None, download fallito.")
            await notify_admin(context, "Download via Pyrogram fallito o non configurato.")
            return
        with open(path, "rb") as fh:
            data = fh.read()
        logger.debug("Pyrogram: letti %d bytes da %s", len(data), path)
        ok = send_discord_file(webhook_url, data, os.path.basename(path), content if content.strip() else None)
        if not ok:
            await notify_admin(context, "Errore invio file >20MB a Discord.")
    except Exception:
        logger.exception("FLOW: errore generale gestione media (Pyrogram).")
        await notify_admin(context, "Errore generale gestione media")


async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FORWARD_EDITS:
        return
    logger.info("EDIT: messaggio editato, reinoltro come nuovo.")
    await on_message(update, context)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot attivo. Inoltro da chat sorgente verso Discord.")


# =========================
# Startup: verifica permessi e stampa setup
# =========================
async def _post_init(app: Application) -> None:
    global BOT_ID, BOT_USERNAME
    bot = await app.bot.get_me()
    BOT_ID = bot.id
    BOT_USERNAME = f"@{bot.username}" if bot.username else None

    logger.info("== Avvio completato ==")
    logger.info("Bot: id=%s username=%s", BOT_ID, BOT_USERNAME)
    logger.info("Source chat: %s | Admin notify: %s", TELEGRAM_SOURCE_CHAT_ID, TELEGRAM_ADMIN_CHAT_ID)
    logger.info("Flags: FORWARD_EDITS=%s INCLUDE_AUTHOR=%s", FORWARD_EDITS, INCLUDE_AUTHOR)
    logger.info(
        "Discord webhooks: SCALPING=%s ALGORITMO=%s FORMAZIONE=%s DEFAULT=%s",
        bool(DISCORD_WEBHOOK_SCALPING), bool(DISCORD_WEBHOOK_ALGORITMO),
        bool(DISCORD_WEBHOOK_FORMAZIONE), bool(DISCORD_WEBHOOK_DEFAULT)
    )
    logger.info("Pyrogram enabled=%s (API_ID set=%s, SESSION set=%s)",
                bool(TG_SESSION), bool(TG_API_ID and TG_API_HASH), bool(TG_SESSION))

    # Verifica stato bot nella chat sorgente
    try:
        member = await app.bot.get_chat_member(TELEGRAM_SOURCE_CHAT_ID, BOT_ID)
        is_admin = getattr(member, "can_manage_chat", False) or member.status in ("administrator", "creator")
        logger.info("Bot nella source chat: status=%s is_admin=%s", member.status, is_admin)
    except Exception as e:
        logger.warning("Impossibile leggere stato bot nella source chat: %s", e)


# =========================
# App wiring
# =========================
def build_application() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = _post_init
    return app


def add_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.ALL), on_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED, on_edited_message))


def run_webhook(app: Application) -> None:
    if not PUBLIC_BASE:
        raise RuntimeError("Devi impostare PUBLIC_BASE o RAILWAY_STATIC_URL per esporre il webhook.")
    path = f"/{BOT_TOKEN}"
    logger.info("Webhook: ascolto su 0.0.0.0:%s path=%s base=%s", PORT, path, PUBLIC_BASE)
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
        logger.info("Avvio in polling…")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    else:
        run_webhook(app)
