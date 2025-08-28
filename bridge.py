# bridge.py
import os
import io
import re
import time
import math
import logging
import tempfile
import shutil
import subprocess
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

# Discord hard limit
DISCORD_MAX_BYTES = 100 * 1024 * 1024  # 100MB

# Pyrogram fallback (per file >20MB) — sessione **utente**
TG_API_ID   = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION  = os.getenv("PYRO_SESSION", os.getenv("TG_SESSION", "")).strip()
if not TG_SESSION:
    logger.info("TG_SESSION/PYRO_SESSION non impostato: il fallback Pyrogram per >20MB non sarà disponibile.")
else:
    logger.info("Pyrogram abilitato (session string presente).")

# Webhook hosting (se userai webhook invece del polling)
PUBLIC_BASE = os.getenv("PUBLIC_BASE", os.getenv("RAILWAY_STATIC_URL", "")).strip()
PORT = int(os.getenv("PORT", "8080"))

# Cache canali già “visti” dallo userbot
PYRO_KNOWN_CHATS: set[int] = set()

# Stato bot (per log)
BOT_ID: Optional[int] = None
BOT_USERNAME: Optional[str] = None


# =========================
# Util vari
# =========================
def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0

def human_size(n: int) -> str:
    x = float(n)
    for unit in ["B","KB","MB","GB","TB"]:
        if x < 1024 or unit == "TB":
            return f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}B"


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
        r = requests.post(url, timeout=600, **kwargs)
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


def send_discord_file_bytes(webhook_url: str, file_bytes: bytes, filename: str, content: Optional[str] = None) -> bool:
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


def send_discord_file_path(webhook_url: str, path: str, content: Optional[str] = None) -> bool:
    with open(path, "rb") as fh:
        data = fh.read()
    name = os.path.basename(path)
    return send_discord_file_bytes(webhook_url, data, name, content)


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
    Usa Pyrogram (session string utente) per scaricare media >20MB.
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


# =========================
# FFmpeg / Compressione ABR
# =========================
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

def is_video_filename(name: str) -> bool:
    name = (name or "").lower()
    return any(name.endswith(ext) for ext in (".mp4",".mov",".mkv",".webm",".avi",".m4v"))

def probe_duration_seconds(input_path: str) -> Optional[float]:
    """Ritorna la durata in secondi usando ffprobe, oppure None."""
    if not ffmpeg_available():
        return None
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=30)
        return float(out.decode().strip())
    except Exception as e:
        logger.warning("FFprobe: impossibile leggere durata: %s", e)
        return None

def compress_video_to_limit(input_path: str, max_bytes: int = DISCORD_MAX_BYTES) -> Optional[str]:
    """
    Transcodifica singola a bitrate calcolato (ABR) per stare sotto max_bytes.
    - Stima bitrate = (max_bytes * 8 / duration) * safety
    - Limita risorse: threads=1, preset veryfast
    - Scala max 1920x1080, audio AAC 128k (o meno se necessario)
    Ritorna il path del file compresso se <= max_bytes, altrimenti None.
    """
    if not ffmpeg_available():
        logger.warning("ffmpeg non disponibile nel sistema: impossibile comprimere.")
        return None

    duration = probe_duration_seconds(input_path)
    if not duration or duration <= 0:
        logger.warning("FFmpeg: durata non disponibile; salto compressione ABR.")
        return None

    base_dir = os.path.dirname(input_path) or tempfile.gettempdir()
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    out_path = os.path.join(base_dir, f"{base_name}_abr.mp4")
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
    except Exception:
        pass

    # Obiettivo: stare sotto max_bytes con ~5% margine di sicurezza
    safety = 0.95
    target_bits_total = max_bytes * 8 * safety  # bytes -> bit

    # Audio di base 128k, ma riduciamo se necessario
    audio_kbps = 128

    # video_bps = target_bits_total/duration - audio_bps
    video_bps = (target_bits_total / duration) - (audio_kbps * 1000)
    if video_bps < 200_000:  # minima guardia qualità
        video_bps = 200_000
        # Se siamo molto stretti, riduci audio
        if (target_bits_total / duration) < (video_bps + 96_000):
            audio_kbps = 96
        if (target_bits_total / duration) < (video_bps + 64_000):
            audio_kbps = 64

    video_kbps = int(video_bps // 1000)
    maxrate_kbps = int(video_kbps * 1.15)          # picchi consentiti
    bufsize_kbps = int(max(video_kbps * 2, 500))   # buffer

    logger.info(
        "FFmpeg ABR: duration=%.2fs, video=%dk, audio=%dk, maxrate=%dk, bufsize=%dk",
        duration, video_kbps, audio_kbps, maxrate_kbps, bufsize_kbps
    )

    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-threads", "1",
        "-i", input_path,
        "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{maxrate_kbps}k",
        "-bufsize", f"{bufsize_kbps}k",
        "-c:a", "aac",
        "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        out_path
    ]
    logger.info("FFmpeg: avvio compressione ABR -> %s", out_path)

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            logger.warning("FFmpeg: ritorno %s. stderr: %s",
                           proc.returncode, proc.stderr.decode(errors="ignore")[-500:])
            return None
    except Exception as e:
        logger.exception("FFmpeg: errore esecuzione: %s", e)
        return None

    size = file_size(out_path)
    logger.info("FFmpeg: output ABR -> %s", human_size(size))
    if size <= max_bytes:
        return out_path

    # Se siamo leggermente sopra, riprova al volo con un -10% bitrate video
    if size < int(max_bytes * 1.10):
        more_kbps = int(video_kbps * 0.9)
        if more_kbps < 200:
            more_kbps = 200
        out_path2 = os.path.join(base_dir, f"{base_name}_abr_tight.mp4")
        try:
            if os.path.exists(out_path2):
                os.remove(out_path2)
        except Exception:
            pass
        cmd2 = [
            "ffmpeg", "-y", "-nostdin",
            "-threads", "1",
            "-i", input_path,
            "-vf", "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{more_kbps}k",
            "-maxrate", f"{int(more_kbps*1.15)}k",
            "-bufsize", f"{max(int(more_kbps*2),500)}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            out_path2
        ]
        logger.info("FFmpeg: ritento ABR più stretto -> %s", out_path2)
        try:
            proc2 = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc2.returncode == 0 and file_size(out_path2) <= max_bytes:
                try:
                    os.remove(out_path)
                except Exception:
                    pass
                return out_path2
        except Exception as e:
            logger.exception("FFmpeg: errore esecuzione ritento ABR: %s", e)

    logger.warning("FFmpeg: impossibile scendere sotto %s con ABR.", human_size(max_bytes))
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
    file_size_tg = 0
    is_video = False

    if msg.photo:
        photo = msg.photo[-1]
        telegram_file_id = photo.file_id
        file_name = "image.jpg"
        file_size_tg = photo.file_size or 0
        logger.info("MSG: media=photo size=%s", file_size_tg)
    elif msg.video:
        telegram_file_id = msg.video.file_id
        file_name = msg.video.file_name or "video.mp4"
        file_size_tg = msg.video.file_size or 0
        is_video = True
        logger.info("MSG: media=video name=%s size=%s", file_name, file_size_tg)
    elif msg.document:
        telegram_file_id = msg.document.file_id
        file_name = msg.document.file_name or "document.bin"
        file_size_tg = msg.document.file_size or 0
        is_video = is_video_filename(file_name)
        logger.info("MSG: media=document name=%s size=%s", file_name, file_size_tg)
    elif msg.animation:
        telegram_file_id = msg.animation.file_id
        file_name = msg.animation.file_name or "animation.mp4"
        file_size_tg = msg.animation.file_size or 0
        is_video = True
        logger.info("MSG: media=animation name=%s size=%s", file_name, file_size_tg)
    elif msg.audio:
        telegram_file_id = msg.audio.file_id
        file_name = msg.audio.file_name or "audio.mp3"
        file_size_tg = msg.audio.file_size or 0
        logger.info("MSG: media=audio name=%s size=%s", file_name, file_size_tg)
    elif msg.voice:
        telegram_file_id = msg.voice.file_id
        file_name = "voice.ogg"
        file_size_tg = msg.voice.file_size or 0
        logger.info("MSG: media=voice size=%s", file_size_tg)
    elif msg.sticker:
        telegram_file_id = msg.sticker.file_id
        file_name = "sticker.webp"
        file_size_tg = msg.sticker.file_size or 0
        logger.info("MSG: media=sticker size=%s", file_size_tg)

    path = None
    comp_path = None
    try:
        # <20MB → Bot API (getFile)
        if file_size_tg and file_size_tg < BOT_API_LIMIT:
            logger.info("FLOW: uso Bot API (file < 20MB).")
            f = await context.bot.get_file(telegram_file_id)
            logger.debug("BotAPI: file_path=%s", f.file_path)
            resp = requests.get(f.file_path, timeout=600)
            resp.raise_for_status()
            # scrivi su disco per uniformare il flusso
            with tempfile.NamedTemporaryFile(prefix="tg_dl_", suffix=os.path.splitext(file_name)[1] or ".bin", delete=False) as fh:
                fh.write(resp.content)
                path = fh.name
        else:
            # >=20MB o size ignota → Pyrogram
            logger.info("FLOW: uso Pyrogram (file >= 20MB o size ignota).")
            path = await pyro_download_by_ids(context, msg.chat_id, msg.message_id)
            if not path:
                logger.error("Pyrogram: path None, download fallito.")
                await notify_admin(context, "Download via Pyrogram fallito o non configurato.")
                return

        # Se il file è >100MB ed è video → tenta compressione ABR
        if is_video and file_size(path) > DISCORD_MAX_BYTES:
            logger.info("Limite Discord: file %s è %s (> 100MB). Avvio compressione.",
                        os.path.basename(path), human_size(file_size(path)))
            comp_path = compress_video_to_limit(path, DISCORD_MAX_BYTES)

            if comp_path and file_size(comp_path) <= DISCORD_MAX_BYTES:
                logger.info("Compressione OK: %s", human_size(file_size(comp_path)))
                ok = send_discord_file_path(webhook_url, comp_path, content if content.strip() else None)
                if not ok:
                    await notify_admin(context, "Errore invio file compresso a Discord.")
            else:
                # fallback: invia avviso
                warn = (
                    f"⚠️ Il video supera il limite di 100MB di Discord.\n"
                    f"- Originale: {human_size(file_size(path))}\n"
                    f"- Compressione: {'ffmpeg non disponibile/durata non nota' if not ffmpeg_available() else 'non sufficiente'}\n"
                    f"Soluzione: carica su un host esterno (Drive/Streamable) o aumenta risorse del container."
                )
                send_discord_text(webhook_url, (content + "\n\n" + warn).strip())
        else:
            # Altrimenti invia direttamente
            ok = send_discord_file_path(webhook_url, path, content if content.strip() else None)
            if not ok:
                await notify_admin(context, "Errore invio file a Discord.")
    except Exception:
        logger.exception("FLOW: errore generale gestione media.")
        await notify_admin(context, "Errore generale gestione media")
    finally:
        # Cleanup dei file temporanei
        for p in (comp_path, path):
            try:
                if p and os.path.exists(p) and os.path.isfile(p):
                    os.remove(p)
                    logger.info("Cleanup: file temporaneo rimosso -> %s", p)
            except Exception:
                logger.debug("Cleanup: rimozione file temp fallita", exc_info=True)


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


# =========================
# App wiring (polling)
# =========================
def build_application() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = _post_init
    return app


def add_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ALL & (~filters.StatusUpdate.ALL), on_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED, on_edited_message))


def run_polling(app: Application) -> None:
    logger.info("Avvio in polling…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    application = build_application()
    add_handlers(application)
    run_polling(application)
