import os
import re
import html
import json
import logging
import asyncio
from typing import Optional, Tuple

import requests
from requests import Response

from telegram import Update, Message
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))

# Railway fornisce già questa variabile
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL")

# Se PUBLIC_BASE è impostato la usiamo, altrimenti fallback a RAILWAY_STATIC_URL
PUBLIC_BASE = os.getenv("PUBLIC_BASE", RAILWAY_STATIC_URL)

# =========================
# Env
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_SOURCE_CHAT_ID = os.getenv("TELEGRAM_SOURCE_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID")

# Webhook mapping: lettura diretta da variabili d’ambiente (Railway)
DISCORD_WEBHOOKS = {}
for env_name, env_val in os.environ.items():
    if env_name.startswith("DISCORD_WEBHOOK_") and env_val:
        tag = "#" + env_name.replace("DISCORD_WEBHOOK_", "").upper()
        DISCORD_WEBHOOKS[tag] = env_val


# =========================
# Utilità
# =========================
def first_hashtag_upper(text: str) -> Optional[str]:
    """
    Ritorna il primo token che inizia con # (uppercase), altrimenti None.
    """
    if not text:
        return None
    words = text.strip().split()
    if not words:
        return None
    first = words[0]
    if first.startswith("#"):
        return first.upper()
    return None

def telegram_html_to_discord(s: str) -> str:
    """
    Converte un sottoinsieme del markup HTML/Entities di Telegram in Markdown supportato da Discord.
    Copre: <b>/<strong>, <i>/<em>, <u>, <s>/<strike>/<del>, <code>, <pre>, <blockquote>,
           <a href="...">, <tg-spoiler>, entità HTML, e rimuove tag ignoti.
    Le inline link vengono rese come: testo (url) perché Discord non supporta [testo](url) nei messaggi normali.
    """
    if not s:
        return ""

    # Unescape entità HTML di Telegram
    s = html.unescape(s)

    # Normalizza <br> in newline
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)

    # Blocchi <pre> -> ```code```
    def _pre_block(m):
        inner = m.group(1)
        # Rimuovi eventuali <code> interni
        inner = re.sub(r"(?is)</?code>", "", inner)
        return f"\n```{inner}```\n"
    s = re.sub(r"(?is)<pre[^>]*>(.*?)</pre>", _pre_block, s)

    # Inline code <code> -> `code`
    s = re.sub(r"(?is)<code[^>]*>(.*?)</code>", lambda m: f"`{m.group(1).strip()}`", s)

    # Bold
    s = re.sub(r"(?is)<(b|strong)>(.*?)</\1>", lambda m: f"**{m.group(2)}**", s)
    # Italic
    s = re.sub(r"(?is)<(i|em)>(.*?)</\1>", lambda m: f"*{m.group(2)}*", s)
    # Underline
    s = re.sub(r"(?is)<u>(.*?)</u>", lambda m: f"__{m.group(1)}__", s)
    # Strikethrough
    s = re.sub(r"(?is)<(s|strike|del)>(.*?)</\1>", lambda m: f"~~{m.group(2)}~~", s)
    # Spoiler
    s = re.sub(r"(?is)<tg-spoiler>(.*?)</tg-spoiler>", lambda m: f"||{m.group(1)}||", s)

    # Blockquote
    def _blockquote(m):
        inner = m.group(1).strip()
        lines = inner.splitlines()
        return "\n".join([f"> {ln}" if ln.strip() else ">" for ln in lines])
    s = re.sub(r"(?is)<blockquote[^>]*>(.*?)</blockquote>", _blockquote, s)

    # Link <a href="...">text</a> -> "text (url)"
    def _a(m):
        href = m.group(1).strip()
        text = m.group(2).strip()
        if not text:
            return href
        if href:
            return f"{text} ({href})"
        return text
    s = re.sub(r'(?is)<a\s+[^>]*href=["\'](.*?)["\'][^>]*>(.*?)</a>', _a, s)

    # Liste (<li>) -> "- ..."; rimuoviamo <ul>/<ol>
    s = re.sub(r"(?is)</?(ul|ol)>", "", s)
    s = re.sub(r"(?is)<li>(.*?)</li>", lambda m: f"- {m.group(1)}\n", s)

    # Rimuovi qualsiasi altro tag residuo mantenendo il testo
    s = re.sub(r"(?is)</?[^>]+>", "", s)

    # Normalizza whitespace e doppie linee
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    return s

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, cause: str, message_id: int):
    """
    Invia alert all'admin con link al messaggio sorgente (se canale privato).
    """
    if not TELEGRAM_ADMIN_CHAT_ID:
        return
    try:
        # Link cliccabile al messaggio nel canale privato: t.me/c/<id_senza_-100>/<message_id>
        chat_id_abs = str(abs(TELEGRAM_SOURCE_CHAT_ID))
        # Se è un canale/supergruppo privato: rimuovi "100" iniziali
        channel_link_part = chat_id_abs[3:] if chat_id_abs.startswith("100") else chat_id_abs
        link = f"https://t.me/c/{channel_link_part}/{message_id}"
        text = f"‼️ERRORE INOLTRO‼️\nCAUSA: {cause}\nID MESSAGGIO: {link}"
        await context.bot.send_message(chat_id=int(TELEGRAM_ADMIN_CHAT_ID), text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Impossibile notificare admin: {e}")

def get_discord_webhook_for_text(text: str) -> Optional[str]:
    tag = first_hashtag_upper(text or "")
    if not tag:
        return None
    return DISCORD_WEBHOOKS.get(tag)

def strip_leading_hashtag(text: str) -> str:
    if not text:
        return ""
    parts = text.strip().split(maxsplit=1)
    if parts and parts[0].startswith("#"):
        return parts[1] if len(parts) > 1 else ""
    return text

def build_telegram_file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

def post_discord_text(webhook_url: str, content: str) -> Response:
    payload = {"content": content}
    return requests.post(webhook_url, json=payload, timeout=60)

def post_discord_embed_with_image(webhook_url: str, description: str, image_url: str) -> Response:
    payload = {"embeds": [{"description": description, "image": {"url": image_url}}]}
    return requests.post(webhook_url, json=payload, timeout=60)

def post_discord_file(webhook_url: str, filename: str, file_bytes: bytes, description: Optional[str] = None) -> Response:
    files = {"file": (filename, file_bytes)}
    data = {}
    if description:
        data["content"] = description
    return requests.post(webhook_url, data=data, files=files, timeout=600)

def pyrogram_available() -> bool:
    return all([PYROGRAM_API_ID, PYROGRAM_API_HASH, PYROGRAM_SESSION, TG_LOCAL_URL])

def download_via_pyro_helper(chat_id: int, message_id: int) -> Optional[str]:
    """
    Chiama il modulo helper (se presente) per scaricare media grandi via Pyrogram + Telegram Local API.
    Ritorna percorso del file locale, oppure None su errore.
    """
    try:
        from pyro_helper_sync import download_media_via_pyro
    except Exception as e:
        logger.error(f"Pyrogram helper non disponibile: {e}")
        return None

    try:
        path = download_media_via_pyro(
            api_id=int(PYROGRAM_API_ID),
            api_hash=PYROGRAM_API_HASH,
            session_string=PYROGRAM_SESSION,
            tg_local_url=TG_LOCAL_URL,
            chat_id=chat_id,
            message_id=message_id,
            download_dir="/tmp",
        )
        return path
    except Exception as e:
        logger.error(f"Errore Pyro helper: {e}")
        return None

# =========================
# Handler
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Optional[Message] = update.message or update.channel_post
    if not msg:
        return

    if msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    # Testo/caption (versione HTML da Telegram) → convertiamo
    raw_text_html = msg.text_html or msg.caption_html or ""
    webhook_url = get_discord_webhook_for_text(raw_text_html)
    if not webhook_url:
        logger.info("Messaggio senza hashtag valido o webhook mancante: ignorato")
        return

    # Pulizia hashtag e conversione markup
    content_for_discord = strip_leading_hashtag(telegram_html_to_discord(raw_text_html)).strip()

    # ====== Contenuti senza media ======
    if not (msg.photo or msg.video or msg.document):
        if content_for_discord:
            r = post_discord_text(webhook_url, content_for_discord)
            logger.info(f"Discord text status={r.status_code}")
        return

    # ====== Con media ======
    # Determina tipo e dimensione, scegliendo la strategia di upload
    media_type = None
    file_id = None
    file_name = None
    approx_size = 0

    try:
        if msg.photo:
            media_type = "image"
            # Foto: scegliamo l'ultima (maggiore risoluzione)
            photo = msg.photo[-1]
            file_id = photo.file_id
            approx_size = getattr(photo, "file_size", 0) or 0
            file_name = "photo.jpg"
        elif msg.video:
            media_type = "video"
            file_id = msg.video.file_id
            approx_size = getattr(msg.video, "file_size", 0) or 0
            # prova a usare file_name del media se presente
            file_name = (getattr(msg.video, "file_name", None) or "video.mp4")
        elif msg.document:
            media_type = "document"
            file_id = msg.document.file_id
            approx_size = getattr(msg.document, "file_size", 0) or 0
            file_name = (getattr(msg.document, "file_name", None) or "document.bin")
    except Exception as e:
        await notify_admin(context, f"Errore nel parsing media: {e}", msg.message_id)
        return

    # Se il file è <= 20MB proviamo via Bot API; altrimenti fallback Pyrogram
    use_pyro = approx_size > BOT_API_LIMIT

    # ========== VIA BOT API (<=20MB) ==========
    if not use_pyro:
        try:
            fobj = await context.bot.get_file(file_id)
            file_url = build_telegram_file_url(fobj.file_path)

            if media_type == "image":
                # Su immagini piccole possiamo usare l'embed con URL Telegram
                # (Discord scarica l'immagine; funziona con URL file/bot<token>/...)
                desc = content_for_discord or ""
                r = post_discord_embed_with_image(webhook_url, desc, file_url)
                logger.info(f"Discord embed image status={r.status_code}")
                return

            # Per video/documenti preferiamo caricare il file (Discord potrebbe non
            # incorporare video da URL Telegram). Scarichiamo in RAM e inviamo multipart.
            with requests.get(file_url, stream=True, timeout=600) as resp:
                resp.raise_for_status()
                file_bytes = resp.content

            r = post_discord_file(webhook_url, file_name, file_bytes, description=content_for_discord)
            logger.info(f"Discord file (<=20MB) status={r.status_code}")
            return

        except Exception as e:
            # Se fallisce (es. Telegram risponde "File is too big" o altri 4xx), tentiamo Pyrogram
            logger.warning(f"Bot API fallita, provo Pyrogram: {e}")
            use_pyro = True

    # ========== FALLBACK PYROGRAM (>20MB) ==========
    if use_pyro:
        if not pyrogram_available():
            await notify_admin(context, f"Media grande (>20MB) e Pyrogram non configurato", msg.message_id)
            return
        local_path = download_via_pyro_helper(chat_id=msg.chat_id, message_id=msg.message_id)
        if not local_path or not os.path.exists(local_path):
            await notify_admin(context, f"Download via Pyrogram fallito", msg.message_id)
            return
        try:
            with open(local_path, "rb") as fh:
                file_bytes = fh.read()
            r = post_discord_file(webhook_url, os.path.basename(local_path), file_bytes, description=content_for_discord)
            logger.info(f"Discord file (>20MB) status={r.status_code}")
        except Exception as e:
            await notify_admin(context, f"Inoltro Discord fallito (Pyro): {e}", msg.message_id)
        finally:
            try:
                os.remove(local_path)
            except Exception:
                pass

# (Opzionale) gestiamo anche le modifiche: reinoltriamo come messaggio nuovo con prefisso
async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message or update.edited_channel_post
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return
    raw_text_html = msg.text_html or msg.caption_html or ""
    webhook_url = get_discord_webhook_for_text(raw_text_html)
    if not webhook_url:
        return
    content = strip_leading_hashtag(telegram_html_to_discord(raw_text_html)).strip()
    if content:
        content = f"✏️ Messaggio modificato:\n{content}"
        r = post_discord_text(webhook_url, content)
        logger.info(f"Discord edit text status={r.status_code}")

# Error handler per evitare crash silenziosi
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Eccezione non gestita", exc_info=context.error)
    try:
        if TELEGRAM_ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=int(TELEGRAM_ADMIN_CHAT_ID),
                text=f"⚠️ Errore: {context.error}",
            )
    except Exception:
        pass

# =========================
# MAIN (Webhook mode)
# =========================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, handle_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    app.add_error_handler(on_error)

    # Log
    logger.info("✅ Bridge avviato in modalità webhook...")

    # Costruzione webhook URL pubblico
    if not PUBLIC_BASE:
        raise RuntimeError("Devi impostare WEBHOOK_BASE_URL o RAILWAY_STATIC_URL per esporre il webhook.")
    webhook_full = f"{PUBLIC_BASE.rstrip('/')}/{BOT_TOKEN}"

    # Avvio webhook (gestisce da solo l'event loop)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,           # path 'segreto'
        webhook_url=webhook_full,     # URL pubblico su cui Telegram invierà gli update
        secret_token=None,            # opzionale
    )

if __name__ == "__main__":
    main()
