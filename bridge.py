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
import sqlite3
import asyncio
from html import unescape
from typing import Optional, Tuple, List

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

# Cache canali “visti” dallo userbot
PYRO_KNOWN_CHATS: set[int] = set()

# Stato bot (per log)
BOT_ID: Optional[int] = None
BOT_USERNAME: Optional[str] = None

# Mapping store (SQLite)
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "map.sqlite")


# =========================
# SQLite mapping Telegram ↔ Discord (con thread_id)
# =========================
def db_init():
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""
        CREATE TABLE IF NOT EXISTS map (
            tg_chat_id INTEGER NOT NULL,
            tg_message_id INTEGER NOT NULL,
            tg_edit_ts INTEGER NOT NULL,
            discord_message_id TEXT NOT NULL,
            webhook_url TEXT NOT NULL,
            last_content TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            discord_channel_id TEXT,
            discord_thread_id TEXT,
            PRIMARY KEY (tg_chat_id, tg_message_id)
        )
        """)
        # migrazioni soft-add per sicurezza (ignora errori se esistono già)
        try: con.execute("ALTER TABLE map ADD COLUMN discord_channel_id TEXT")
        except Exception: pass
        try: con.execute("ALTER TABLE map ADD COLUMN discord_thread_id TEXT")
        except Exception: pass
        con.commit()
    finally:
        con.close()

def db_upsert_mapping(tg_chat_id: int, tg_message_id: int, tg_edit_ts: int,
                      discord_message_id: str, webhook_url: str, last_content: str,
                      discord_channel_id: Optional[str], discord_thread_id: Optional[str]):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""
        INSERT INTO map (tg_chat_id, tg_message_id, tg_edit_ts, discord_message_id, webhook_url, last_content, deleted, discord_channel_id, discord_thread_id)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(tg_chat_id, tg_message_id) DO UPDATE SET
            tg_edit_ts=excluded.tg_edit_ts,
            discord_message_id=excluded.discord_message_id,
            webhook_url=excluded.webhook_url,
            last_content=excluded.last_content,
            deleted=0,
            discord_channel_id=excluded.discord_channel_id,
            discord_thread_id=excluded.discord_thread_id
        """, (tg_chat_id, tg_message_id, tg_edit_ts, discord_message_id, webhook_url, last_content, discord_channel_id, discord_thread_id))
        con.commit()
    finally:
        con.close()

def db_mark_deleted(tg_chat_id: int, tg_message_id: int):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE map SET deleted=1 WHERE tg_chat_id=? AND tg_message_id=?", (tg_chat_id, tg_message_id))
        con.commit()
    finally:
        con.close()

def db_update_edit_ts_and_content(tg_chat_id: int, tg_message_id: int, edit_ts: int, content: str):
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE map SET tg_edit_ts=?, last_content=? WHERE tg_chat_id=? AND tg_message_id=?",
                    (edit_ts, content, tg_chat_id, tg_message_id))
        con.commit()
    finally:
        con.close()

def db_get_recent_mappings(tg_chat_id: int, limit: int = 10) -> List[Tuple[int, str, str, int, str, Optional[str]]]:
    """
    Ritorna (tg_message_id, discord_message_id, webhook_url, tg_edit_ts, last_content, discord_thread_id)
    ultimi N messaggi non cancellati.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("""
          SELECT tg_message_id, discord_message_id, webhook_url, tg_edit_ts, last_content, discord_thread_id
          FROM map
          WHERE tg_chat_id=? AND deleted=0
          ORDER BY tg_message_id DESC
          LIMIT ?
        """, (tg_chat_id, limit))
        return list(cur.fetchall())
    finally:
        con.close()


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
    s = re.sub(r"</?(b|strong)>", "**", s, flags=re.I)
    s = re.sub(r"</?(i|em)>", "*", s, flags=re.I)
    s = re.sub(r"<u>(.*?)</u>", r"__\1__", s, flags=re.I | re.S)
    s = re.sub(r"<(s|del)>(.*?)</\1>", r"~~\2~~", s, flags=re.I | re.S)
    s = re.sub(r"<code>(.*?)</code>", r"`\1`", s, flags=re.I | re.S)
    s = re.sub(
        r"<pre.*?>\s*<code.*?class=['\"]?language-([\w+\-]+)['\"]?.*?>(.*?)</code>\s*</pre>",
        r"```\1\n\2\n```", s, flags=re.I | re.S
    )
    s = re.sub(r"<pre.*?>(.*?)</pre>", r"```\n\1\n```", s, flags=re.I | re.S)
    s = re.sub(r'<a\s+href=["\'](.*?)["\']>(.*?)</a>', r"[\2](\1)", s, flags=re.I | re.S)
    s = re.sub(r"<tg-spoiler>(.*?)</tg-spoiler>", r"||\1||", s, flags=re.I | re.S)
    s = re.sub(r"<blockquote>(.*?)</blockquote>", r">\1", s, flags=re.I | re.S)
    s = re.sub(r"</?[^>]+>", "", s)
    s = s.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


# =========================
# Discord helpers (con wait=true e thread_id)
# =========================
def _ensure_wait(url: str) -> str:
    return url + ("&wait=true" if "?" in url else "?wait=true")

def _post_with_retry(url: str, **kwargs) -> Response:
    for attempt in range(5):
        r = requests.post(url, timeout=600, **kwargs)
        if r.status_code not in (429,) and r.status_code < 500:
            return r
        wait = min(2 ** attempt, 30)
        logger.warning("Discord POST %s -> %s. Retry tra %ss", url, r.status_code, wait)
        time.sleep(wait)
    return r

def _patch_with_retry(url: str, **kwargs) -> Response:
    for attempt in range(5):
        r = requests.patch(url, timeout=600, **kwargs)
        if r.status_code not in (429,) and r.status_code < 500:
            return r
        wait = min(2 ** attempt, 30)
        logger.warning("Discord PATCH %s -> %s. Retry tra %ss", url, r.status_code, wait)
        time.sleep(wait)
    return r

def _delete_with_retry(url: str, **kwargs) -> Response:
    for attempt in range(5):
        r = requests.delete(url, timeout=600, **kwargs)
        if r.status_code not in (429,) and r.status_code < 500:
            return r
        wait = min(2 ** attempt, 30)
        logger.warning("Discord DELETE %s -> %s. Retry tra %ss", url, r.status_code, wait)
        time.sleep(wait)
    return r

def send_discord_text(webhook_url: str, content: str) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """Ritorna (ok, message_id, channel_id, thread_id?)"""
    if not webhook_url:
        logger.info("Discord: nessun webhook configurato, salto invio testo.")
        return False, None, None, None
    url = _ensure_wait(webhook_url)
    r = _post_with_retry(url, json={"content": content})
    ok, msg_id, ch_id, th_id = False, None, None, None
    if 200 <= r.status_code < 300:
        try:
            j = r.json()
            msg_id = j.get("id")
            ch_id = j.get("channel_id")
            th_id = ch_id  # se è thread, channel_id del messaggio è il thread id
            ok = True
        except Exception:
            ok = True
    logger.log(logging.INFO if ok else logging.ERROR,
               "Discord: risposta invio testo -> %s (%s)", r.status_code, r.text[:200])
    return ok, msg_id, ch_id, th_id

def send_discord_file_bytes(webhook_url: str, file_bytes: bytes, filename: str, content: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    if not webhook_url:
        logger.info("Discord: nessun webhook configurato, salto invio file.")
        return False, None, None, None
    url = _ensure_wait(webhook_url)
    logger.info("Discord: upload file '%s' (%d bytes).", filename, len(file_bytes or b""))
    files = {"file": (filename, io.BytesIO(file_bytes))}
    data = {"content": content} if content else {}
    r = _post_with_retry(url, files=files, data=data)
    ok, msg_id, ch_id, th_id = False, None, None, None
    if 200 <= r.status_code < 300:
        try:
            j = r.json()
            msg_id = j.get("id")
            ch_id = j.get("channel_id")
            th_id = ch_id
            ok = True
        except Exception:
            ok = True
    logger.log(logging.INFO if ok else logging.ERROR,
               "Discord: risposta upload file -> %s (%s)", r.status_code, r.text[:200])
    return ok, msg_id, ch_id, th_id

def send_discord_file_path(webhook_url: str, path: str, content: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    with open(path, "rb") as fh:
        data = fh.read()
    name = os.path.basename(path)
    return send_discord_file_bytes(webhook_url, data, name, content)

def edit_discord_message(webhook_url: str, message_id: str, new_content: str, thread_id: Optional[str] = None) -> bool:
    url = f"{webhook_url}/messages/{message_id}"
    if thread_id:
        url += f"?thread_id={thread_id}"
        url = _ensure_wait(url)
    else:
        url = _ensure_wait(url)
    r = _patch_with_retry(url, json={"content": new_content})
    ok = 200 <= r.status_code < 300
    logger.log(logging.INFO if ok else logging.ERROR,
               "Discord: edit -> %s (%s)", r.status_code, r.text[:200])
    return ok

def delete_discord_message(webhook_url: str, message_id: str, thread_id: Optional[str] = None) -> bool:
    url = f"{webhook_url}/messages/{message_id}"
    if thread_id:
        url += f"?thread_id={thread_id}"
    r = _delete_with_retry(url)
    if 200 <= r.status_code < 300 or r.status_code == 404:
        logger.info("Discord: delete -> %s", r.status_code)
        return True
    logger.warning("Discord: delete fallito (%s). Fallback tombstone.", r.status_code)
    # fallback: edit a "eliminato"
    return edit_discord_message(webhook_url, message_id, "*(eliminato su Telegram)*", thread_id=thread_id)


# =========================
# Bot-side: crea autoinvite (per Pyrogram)
# =========================
async def _get_autoinvite_for_pyro(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Optional[str]:
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
# Pyrogram fallback (async download via helper)
# =========================
async def pyro_download_by_ids(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int
) -> Optional[str]:
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
# FFmpeg / Compressione ABR (con fallback anti-OOM)
# =========================
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

def is_video_filename(name: str) -> bool:
    name = (name or "").lower()
    return any(name.endswith(ext) for ext in (".mp4",".mov",".mkv",".webm",".avi",".m4v"))

def probe_duration_seconds(input_path: str) -> Optional[float]:
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
    Transcodifica a bitrate calcolato (ABR) con fallback progressivi per evitare OOM/SIGKILL.
    Step:
      A) 1080p, audio 128k
      B) 720p,  audio 96k,  -15% bitrate video
      C) 540p,  audio 64k,  -25% bitrate video, preset ultrafast
    Ritorna path file compresso <= max_bytes, altrimenti None.
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

    safety = 0.95
    target_bits_total = max_bytes * 8 * safety  # bytes -> bit
    base_audio_kbps = 128
    base_video_bps = (target_bits_total / duration) - (base_audio_kbps * 1000)
    if base_video_bps < 200_000:
        base_video_bps = 200_000
        if (target_bits_total / duration) < (base_video_bps + 96_000):
            base_audio_kbps = 96
        if (target_bits_total / duration) < (base_video_bps + 64_000):
            base_audio_kbps = 64
    base_video_kbps = int(base_video_bps // 1000)

    attempts = [
        ("_abr1080", 1080, 1.00, base_audio_kbps, "veryfast"),
        ("_abr720",   720, 0.85, max(96, base_audio_kbps if base_audio_kbps <= 128 else 128), "veryfast"),
        ("_abr540",   540, 0.75, 64, "ultrafast"),
    ]

    def run_once(suffix: str, max_h: int, v_factor: float, a_kbps: int, preset: str) -> Optional[str]:
        v_kbps = max(200, int(base_video_kbps * v_factor))
        maxrate_kbps = int(v_kbps * 1.10)
        bufsize_kbps = max(int(v_kbps * 1.5), 300)

        out_path = os.path.join(base_dir, f"{base_name}{suffix}.mp4")
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass

        logger.info(
            "FFmpeg ABR try %s: height<=%d, video=%dk, audio=%dk, preset=%s",
            suffix, max_h, v_kbps, a_kbps, preset
        )

        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-threads", "1",
            "-filter_threads", "1",
            "-i", input_path,
            "-vf", f"scale='min(1920,iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease",
            "-c:v", "libx264",
            "-preset", preset,
            "-b:v", f"{v_kbps}k",
            "-maxrate", f"{maxrate_kbps}k",
            "-bufsize", f"{bufsize_kbps}k",
            "-c:a", "aac",
            "-b:a", f"{a_kbps}k",
            "-movflags", "+faststart",
            "-max_muxing_queue_size", "1024",
            out_path
        ]

        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc.returncode != 0:
                tail = proc.stderr.decode(errors="ignore")[-500:]
                logger.warning("FFmpeg: ritorno %s. stderr: %s", proc.returncode, tail)
                return None
        except Exception as e:
            logger.exception("FFmpeg: errore esecuzione (%s): %s", suffix, e)
            return None

        sz = file_size(out_path)
        logger.info("FFmpeg: output %s -> %s", suffix, human_size(sz))
        return out_path if sz <= max_bytes else None

    for suf, mh, vf, ak, pre in attempts:
        result = run_once(suf, mh, vf, ak, pre)
        if result:
            return result

    logger.warning("FFmpeg: impossibile scendere sotto %s dopo i fallback.", human_size(max_bytes))
    return None


# =========================
# Routing & helpers
# =========================
def pick_webhook_from_text(text: str) -> Optional[str]:
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

    text_html = msg.caption_html or msg.text_html or ""
    text_md = tg_html_to_discord_md(text_html)
    logger.debug("MSG: testo convertito (len=%d).", len(text_md or ""))

    webhook_url = pick_webhook_from_text(text_md or "")
    content = (text_md or "").strip()
    content += author_suffix(msg)

    # media?
    has_media = any([msg.photo, msg.video, msg.document, msg.animation, msg.voice, msg.audio, msg.sticker])

    discord_id = None
    discord_ch = None
    discord_thread = None
    path = None
    comp_path = None
    try:
        if not has_media:
            logger.info("MSG: solo testo. Invio verso Discord.")
            ok, discord_id, discord_ch, discord_thread = send_discord_text(webhook_url, content)
            if not ok:
                await notify_admin(context, "Errore invio testo a Discord.")
        else:
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

            # <20MB → Bot API
            if file_size_tg and file_size_tg < BOT_API_LIMIT:
                logger.info("FLOW: uso Bot API (file < 20MB).")
                f = await context.bot.get_file(telegram_file_id)
                resp = requests.get(f.file_path, timeout=600)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(prefix="tg_dl_", suffix=os.path.splitext(file_name)[1] or ".bin", delete=False) as fh:
                    fh.write(resp.content)
                    path = fh.name
            else:
                logger.info("FLOW: uso Pyrogram (file >= 20MB o size ignota).")
                path = await pyro_download_by_ids(context, msg.chat_id, msg.message_id)
                if not path:
                    logger.error("Pyrogram: path None, download fallito.")
                    await notify_admin(context, "Download via Pyrogram fallito o non configurato.")
                    return

            # compressione se necessario
            if is_video and file_size(path) > DISCORD_MAX_BYTES:
                logger.info("Limite Discord: file %s è %s (> 100MB). Avvio compressione.",
                            os.path.basename(path), human_size(file_size(path)))
                comp_path = compress_video_to_limit(path, DISCORD_MAX_BYTES)

                if comp_path and file_size(comp_path) <= DISCORD_MAX_BYTES:
                    logger.info("Compressione OK: %s", human_size(file_size(comp_path)))
                    ok, discord_id, discord_ch, discord_thread = send_discord_file_path(webhook_url, comp_path, content if content.strip() else None)
                    if not ok:
                        await notify_admin(context, "Errore invio file compresso a Discord.")
                else:
                    warn = (
                        f"⚠️ Il video supera il limite di 100MB di Discord.\n"
                        f"- Originale: {human_size(file_size(path))}\n"
                        f"- Compressione: {'ffmpeg non disponibile/durata non nota' if not ffmpeg_available() else 'non sufficiente'}\n"
                        f"Soluzione: carica su un host esterno (Drive/Streamable) o aumenta risorse del container."
                    )
                    ok, discord_id, discord_ch, discord_thread = send_discord_text(webhook_url, (content + "\n\n" + warn).strip())
            else:
                ok, discord_id, discord_ch, discord_thread = send_discord_file_path(webhook_url, path, content if content.strip() else None)
                if not ok:
                    await notify_admin(context, "Errore invio file a Discord.")
    except Exception:
        logger.exception("FLOW: errore generale gestione media.")
        await notify_admin(context, "Errore generale gestione media")
    finally:
        for p in (comp_path, path):
            try:
                if p and os.path.exists(p) and os.path.isfile(p):
                    os.remove(p)
                    logger.info("Cleanup: file temporaneo rimosso -> %s", p)
            except Exception:
                logger.debug("Cleanup: rimozione file temp fallita", exc_info=True)

    # salva mapping per edit/delete
    try:
        if discord_id:
            edit_ts = int((msg.edit_date or msg.date).timestamp()) if (msg.edit_date or msg.date) else int(time.time())
            last_content = (content or "")[:2000]
            db_upsert_mapping(
                msg.chat_id, msg.message_id, edit_ts,
                discord_id, webhook_url or "", last_content,
                discord_ch, discord_thread
            )
    except Exception:
        logger.exception("Mapping: upsert fallito.")


async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FORWARD_EDITS:
        return
    logger.info("EDIT: messaggio editato, reinoltro come nuovo.")
    await on_message(update, context)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot attivo. Inoltro da chat sorgente verso Discord.")


# =========================
# Reconciliation job (ogni 10s sugli ultimi 10 msg)
# =========================
async def reconcile_last_messages(context: ContextTypes.DEFAULT_TYPE):
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        return
    try:
        recent = db_get_recent_mappings(TELEGRAM_SOURCE_CHAT_ID, limit=10)
        if not recent:
            return

        from pyrogram import Client
        from pyrogram.errors import RPCError, MessageIdInvalid

        async with Client(
            name=":memory:",
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
            session_string=TG_SESSION,
            no_updates=True,
            workdir=tempfile.gettempdir(),
        ) as app:
            try:
                await app.get_chat(TELEGRAM_SOURCE_CHAT_ID)
            except Exception:
                logger.debug("Reconcile: get_chat fallita; salto iterazione.")
                return

            for tg_message_id, discord_message_id, webhook_url, tg_edit_ts, last_content, discord_thread_id in recent:
                m = None
                try:
                    m = await app.get_messages(TELEGRAM_SOURCE_CHAT_ID, tg_message_id)
                except MessageIdInvalid:
                    m = None
                except RPCError as e:
                    logger.debug("Reconcile: RPCError per %s: %s", tg_message_id, e)
                    m = None
                except Exception as e:
                    logger.debug("Reconcile: errore %s su %s", e, tg_message_id)
                    m = None

                if m is None:
                    ok = delete_discord_message(webhook_url, discord_message_id, thread_id=discord_thread_id)
                    if ok:
                        db_mark_deleted(TELEGRAM_SOURCE_CHAT_ID, tg_message_id)
                        logger.info("Reconcile: cancellato su Discord (tg_id=%s -> dc_id=%s).",
                                    tg_message_id, discord_message_id)
                    continue

                plain = (m.caption or m.text or "").strip()
                new_content = plain[:2000]
                new_edit_ts = int((getattr(m, "edit_date", None) or getattr(m, "date", None)).timestamp()) if (getattr(m, "edit_date", None) or getattr(m, "date", None)) else tg_edit_ts

                if new_edit_ts > tg_edit_ts or (new_content and new_content != (last_content or "")):
                    ok = edit_discord_message(webhook_url, discord_message_id, new_content, thread_id=discord_thread_id)
                    if ok:
                        db_update_edit_ts_and_content(TELEGRAM_SOURCE_CHAT_ID, tg_message_id, new_edit_ts, new_content)
                        logger.info("Reconcile: edit sincronizzato (tg_id=%s).", tg_message_id)

    except Exception:
        logger.exception("Reconcile: errore nel job.")


# =========================
# Startup & wiring (polling con fallback JobQueue)
# =========================
async def _post_init(app: Application) -> None:
    global BOT_ID, BOT_USERNAME
    db_init()

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

    # JobQueue se disponibile, altrimenti loop asyncio
    if getattr(app, "job_queue", None):
        app.job_queue.run_repeating(reconcile_last_messages, interval=10, first=10)
    else:
        logger.warning('JobQueue assente: fallback a loop asyncio ogni 10s.')
        async def _reconcile_loop():
            while True:
                try:
                    await reconcile_last_messages(None)
                except Exception:
                    logger.exception("Reconcile loop: errore")
                await asyncio.sleep(10)
        app.create_task(_reconcile_loop())


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


