# bridge.py
import os
import io
import re
import time
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

# ========= Logging =========
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("bridge")

# ========= Env / Config =========
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

DISCORD_WEBHOOK_SCALPING   = os.getenv("DISCORD_WEBHOOK_SCALPING", "").strip()
DISCORD_WEBHOOK_ALGORITMO  = os.getenv("DISCORD_WEBHOOK_ALGORITMO", "").strip()
DISCORD_WEBHOOK_FORMAZIONE = os.getenv("DISCORD_WEBHOOK_FORMAZIONE", "").strip()
DISCORD_WEBHOOK_DEFAULT    = os.getenv("DISCORD_WEBHOOK_DEFAULT", "").strip()

WEBHOOK_MAP = {
    "SCALPING": DISCORD_WEBHOOK_SCALPING,
    "ALGORITMO": DISCORD_WEBHOOK_ALGORITMO,
    "FORMAZIONE": DISCORD_WEBHOOK_FORMAZIONE,
}

BOT_API_LIMIT = 20 * 1024 * 1024  # 20MB
DISCORD_MAX_BYTES = 100 * 1024 * 1024  # 100MB

# Pyrogram (user session)
TG_API_ID   = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION  = os.getenv("PYRO_SESSION", os.getenv("TG_SESSION", "")).strip()
TG_CHAT_INVITE = os.getenv("TG_CHAT_INVITE", "").strip()  # opzionale, se vuoi fissarlo
if TG_SESSION:
    logger.info("Pyrogram abilitato (session string presente).")
else:
    logger.info("Pyrogram non configurato: niente eventi realtime cancellazione, niente download >20MB.")

PUBLIC_BASE = os.getenv("PUBLIC_BASE", os.getenv("RAILWAY_STATIC_URL", "")).strip()
PORT = int(os.getenv("PORT", "8080"))

# Cache
PYRO_KNOWN_CHATS: set[int] = set()
JOIN_BACKOFF: dict[int, float] = {}  # chat_id -> epoch quando poter ritentare il join

# Stato bot
BOT_ID: Optional[int] = None
BOT_USERNAME: Optional[str] = None

# DB mapping
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "map.sqlite")

# ========= DB: Telegram↔Discord =========
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

def db_get_recent_mappings(tg_chat_id: int, limit: int = 10):
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

def db_get_mapping(tg_chat_id: int, tg_message_id: int):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("""
          SELECT tg_message_id, discord_message_id, webhook_url, tg_edit_ts, last_content, discord_thread_id
          FROM map
          WHERE tg_chat_id=? AND tg_message_id=? AND deleted=0
        """, (tg_chat_id, tg_message_id))
        return cur.fetchone()
    finally:
        con.close()

# ========= Utils =========
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

# ========= HTML TG -> Markdown Discord =========
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

# ========= Discord helpers =========
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

def send_discord_text(webhook_url: str, content: str):
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
            th_id = ch_id
            ok = True
        except Exception:
            ok = True
    logger.log(logging.INFO if ok else logging.ERROR,
               "Discord: risposta invio testo -> %s (%s)", r.status_code, r.text[:200])
    return ok, msg_id, ch_id, th_id

def send_discord_file_bytes(webhook_url: str, file_bytes: bytes, filename: str, content: Optional[str] = None):
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

def send_discord_file_path(webhook_url: str, path: str, content: Optional[str] = None):
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
    return edit_discord_message(webhook_url, message_id, "*(eliminato su Telegram)*", thread_id=thread_id)

# ========= Bot-side: autoinvite =========
async def _get_autoinvite_for_pyro(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Optional[str]:
    try:
        link = await context.bot.create_chat_invite_link(
            chat_id=chat_id, name="bridge-autoinvite",
            creates_join_request=False, expire_date=None, member_limit=0
        )
        logger.info("Autoinvite: creato via Bot API.")
        return link.invite_link
    except Exception as e:
        logger.warning("Autoinvite: NON creato (permessi mancanti?). %s", e)
        return None

def _http_create_invite_link(chat_id: int) -> Optional[str]:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/createChatInviteLink"
        r = requests.post(url, json={
            "chat_id": chat_id, "name": "bridge-autoinvite",
            "creates_join_request": False, "member_limit": 0
        }, timeout=30)
        if r.ok and r.json().get("ok"):
            return r.json()["result"]["invite_link"]
        logger.warning("Autoinvite HTTP: fallito %s %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("Autoinvite HTTP: eccezione: %s", e)
    return None

# ========= Pyrogram helpers =========
async def pyro_download_by_ids(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> Optional[str]:
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        logger.error("Pyrogram: configurazione mancante.")
        return None
    try:
        from pyro_helper_sync import download_media_via_pyro_async
    except Exception as e:
        logger.exception("Pyrogram: import helper fallito: %s", e)
        return None

    invite_link = None
    if chat_id not in PYRO_KNOWN_CHATS:
        invite_link = TG_CHAT_INVITE or await _get_autoinvite_for_pyro(context, chat_id)
        logger.info("Pyrogram: invite_link=%s", (invite_link[:24] + "…") if invite_link else "None")

    logger.info("Pyrogram: avvio download (chat_id=%s, message_id=%s)", chat_id, message_id)
    try:
        path = await download_media_via_pyro_async(
            api_id=TG_API_ID, api_hash=TG_API_HASH, session_string=TG_SESSION,
            chat_id=chat_id, message_id=message_id,
            download_dir=tempfile.gettempdir(), invite_link=invite_link
        )
        PYRO_KNOWN_CHATS.add(chat_id)
        logger.info("Pyrogram: download completato. Path: %s", path)
        return path
    except Exception as e:
        logger.exception("Pyrogram: errore nel download: %s", e)
        return None

# ========= FFmpeg =========
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

def is_video_filename(name: str) -> bool:
    name = (name or "").lower()
    return any(name.endswith(ext) for ext in (".mp4",".mov",".mkv",".webm",".avi",".m4v"))

def probe_duration_seconds(input_path: str) -> Optional[float]:
    if not ffmpeg_available():
        return None
    try:
        out = subprocess.check_output(
            ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",input_path],
            stderr=subprocess.STDOUT, timeout=30
        )
        return float(out.decode().strip())
    except Exception as e:
        logger.warning("FFprobe: durata non disponibile: %s", e)
        return None

def compress_video_to_limit(input_path: str, max_bytes: int = DISCORD_MAX_BYTES) -> Optional[str]:
    if not ffmpeg_available():
        logger.warning("ffmpeg non disponibile.")
        return None
    duration = probe_duration_seconds(input_path)
    if not duration or duration <= 0:
        return None

    base_dir = os.path.dirname(input_path) or tempfile.gettempdir()
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    safety = 0.95
    target_bits_total = max_bytes * 8 * safety
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

    def run_once(suffix, max_h, v_factor, a_kbps, preset):
        v_kbps = max(200, int(base_video_kbps * v_factor))
        maxrate_kbps = int(v_kbps * 1.10)
        bufsize_kbps = max(int(v_kbps * 1.5), 300)
        out_path = os.path.join(base_dir, f"{base_name}{suffix}.mp4")
        try:
            if os.path.exists(out_path): os.remove(out_path)
        except Exception:
            pass
        cmd = [
            "ffmpeg","-y","-nostdin","-threads","1","-filter_threads","1",
            "-i", input_path,
            "-vf", f"scale='min(1920,iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease",
            "-c:v","libx264","-preset",preset,
            "-b:v",f"{v_kbps}k","-maxrate",f"{maxrate_kbps}k","-bufsize",f"{bufsize_kbps}k",
            "-c:a","aac","-b:a",f"{a_kbps}k",
            "-movflags","+faststart","-max_muxing_queue_size","1024",
            out_path
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc.returncode != 0:
                logger.warning("FFmpeg: ritorno %s.", proc.returncode)
                return None
        except Exception as e:
            logger.exception("FFmpeg: errore: %s", e)
            return None
        return out_path if file_size(out_path) <= max_bytes else None

    for args in attempts:
        res = run_once(*args)
        if res: return res
    logger.warning("FFmpeg: impossibile scendere sotto %s.", human_size(max_bytes))
    return None

# ========= Routing & helpers =========
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
        logger.info("Routing: nessun tag trovato -> default=%s", bool(chosen))
    return chosen

def author_suffix(msg: Message) -> str:
    if not INCLUDE_AUTHOR or not msg.from_user:
        return ""
    u = msg.from_user
    handle = f"@{u.username}" if u.username else ""
    name = u.full_name or handle or str(u.id)
    return f"\n\n— {name} {handle}".strip()

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if TELEGRAM_ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(TELEGRAM_ADMIN_CHAT_ID, text[:4000])
        except Exception:
            logger.exception("notify_admin fallito")

# ========= Handlers PTB =========
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if not msg:
        return
    if msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    logger.info("MSG: message_id=%s chat_id=%s", msg.message_id, msg.chat_id)

    text_html = msg.caption_html or msg.text_html or ""
    text_md = tg_html_to_discord_md(text_html)
    webhook_url = pick_webhook_from_text(text_md or "")
    content = (text_md or "").strip() + author_suffix(msg)

    has_media = any([msg.photo, msg.video, msg.document, msg.animation, msg.voice, msg.audio, msg.sticker])

    discord_id = None
    discord_ch = None
    discord_thread = None
    path = None
    comp_path = None
    try:
        if not has_media:
            ok, discord_id, discord_ch, discord_thread = send_discord_text(webhook_url, content)
            if not ok: await notify_admin(context, "Errore invio testo a Discord.")
        else:
            telegram_file_id = None
            file_name = "file.bin"
            size_tg = 0
            is_video = False

            if msg.photo:
                p = msg.photo[-1]
                telegram_file_id = p.file_id
                file_name = "image.jpg"
                size_tg = p.file_size or 0
            elif msg.video:
                telegram_file_id = msg.video.file_id
                file_name = msg.video.file_name or "video.mp4"
                size_tg = msg.video.file_size or 0
                is_video = True
            elif msg.document:
                telegram_file_id = msg.document.file_id
                file_name = msg.document.file_name or "document.bin"
                size_tg = msg.document.file_size or 0
                is_video = is_video_filename(file_name)
            elif msg.animation:
                telegram_file_id = msg.animation.file_id
                file_name = msg.animation.file_name or "animation.mp4"
                size_tg = msg.animation.file_size or 0
                is_video = True
            elif msg.audio:
                telegram_file_id = msg.audio.file_id
                file_name = msg.audio.file_name or "audio.mp3"
                size_tg = msg.audio.file_size or 0
            elif msg.voice:
                telegram_file_id = msg.voice.file_id
                file_name = "voice.ogg"
                size_tg = msg.voice.file_size or 0
            elif msg.sticker:
                telegram_file_id = msg.sticker.file_id
                file_name = "sticker.webp"
                size_tg = msg.sticker.file_size or 0

            if size_tg and size_tg < BOT_API_LIMIT:
                f = await context.bot.get_file(telegram_file_id)
                resp = requests.get(f.file_path, timeout=600)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(prefix="tg_dl_", suffix=os.path.splitext(file_name)[1] or ".bin", delete=False) as fh:
                    fh.write(resp.content)
                    path = fh.name
            else:
                path = await pyro_download_by_ids(context, msg.chat_id, msg.message_id)
                if not path:
                    await notify_admin(context, "Download via Pyrogram fallito o non configurato.")
                    return

            if is_video and file_size(path) > DISCORD_MAX_BYTES:
                comp_path = compress_video_to_limit(path, DISCORD_MAX_BYTES)
                if comp_path and file_size(comp_path) <= DISCORD_MAX_BYTES:
                    ok, discord_id, discord_ch, discord_thread = send_discord_file_path(webhook_url, comp_path, content if content.strip() else None)
                    if not ok: await notify_admin(context, "Errore invio file compresso a Discord.")
                else:
                    warn = (
                        f"⚠️ Video oltre 100MB.\n"
                        f"- Originale: {human_size(file_size(path))}\n"
                        f"- Compressione: non disponibile/insufficiente\n"
                        f"Carica su host esterno oppure aumenta risorse."
                    )
                    ok, discord_id, discord_ch, discord_thread = send_discord_text(webhook_url, (content + "\n\n" + warn).strip())
            else:
                ok, discord_id, discord_ch, discord_thread = send_discord_file_path(webhook_url, path, content if content.strip() else None)
                if not ok: await notify_admin(context, "Errore invio file a Discord.")
    except Exception:
        logger.exception("Gestione media fallita.")
        await notify_admin(context, "Errore generale gestione media")
    finally:
        for p in (comp_path, path):
            try:
                if p and os.path.exists(p) and os.path.isfile(p):
                    os.remove(p)
                    logger.info("Cleanup: %s rimosso", p)
            except Exception:
                pass

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
        logger.exception("Mapping upsert fallito.")

async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not FORWARD_EDITS:
        return
    await on_message(update, context)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot attivo. Inoltro messaggi e sync cancellazioni in tempo reale.")

# ========= Reconcile (solo EDIT come default; delete è realtime) =========
async def reconcile_last_messages(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Reconcile(Edit): tick")
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        return
    try:
        recent = db_get_recent_mappings(TELEGRAM_SOURCE_CHAT_ID, limit=10)
        if not recent:
            return
        from pyrogram import Client
        from pyrogram.errors import RPCError, MessageIdInvalid
        async with Client(
            name=":memory:", api_id=TG_API_ID, api_hash=TG_API_HASH,
            session_string=TG_SESSION, no_updates=True, workdir=tempfile.gettempdir()
        ) as app:
            # niente join qui: delete è realtime, qui ci interessa solo chi riusciamo a leggere
            try:
                await app.get_chat(TELEGRAM_SOURCE_CHAT_ID)
            except Exception:
                # se non riesce, saltiamo il tick (evitiamo flood)
                logger.info("Reconcile(Edit): get_chat fallita, salto tick.")
                return

            for tg_message_id, discord_message_id, webhook_url, tg_edit_ts, last_content, thread_id in recent:
                m = None
                try:
                    m = await app.get_messages(TELEGRAM_SOURCE_CHAT_ID, tg_message_id)
                except (MessageIdInvalid, RPCError):
                    m = None
                except Exception:
                    m = None
                if not m:
                    # delete realtime gestisce già, ma nel dubbio fai tombstone
                    logger.debug("Reconcile(Edit): msg %s non leggibile (prob. cancellato).", tg_message_id)
                    continue
                plain = (m.caption or m.text or "").strip()
                new_content = plain[:2000]
                new_edit_ts = int((getattr(m, "edit_date", None) or getattr(m, "date", None)).timestamp()) if (getattr(m, "edit_date", None) or getattr(m, "date", None)) else tg_edit_ts
                if new_edit_ts > tg_edit_ts or (new_content and new_content != (last_content or "")):
                    ok = edit_discord_message(webhook_url, discord_message_id, new_content, thread_id=thread_id)
                    if ok:
                        db_update_edit_ts_and_content(TELEGRAM_SOURCE_CHAT_ID, tg_message_id, new_edit_ts, new_content)
                        logger.info("Reconcile(Edit): sync edit tg_id=%s", tg_message_id)
    except Exception:
        logger.exception("Reconcile(Edit): errore")

# ========= Pyrogram client persistente (Delete realtime) =========
pyro_client = None  # sarà istanziato in _post_init

async def _pyro_ensure_access_once() -> None:
    """
    All'avvio: prova a 'vedere' la chat. Se serve e consentito, fai join UNA VOLTA con backoff FloodWait.
    """
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        return
    from pyrogram.errors import FloodWait
    # 1) warm-up dialoghi
    try:
        found = False
        async for d in pyro_client.get_dialogs():
            try:
                if getattr(d.chat, "id", None) == TELEGRAM_SOURCE_CHAT_ID:
                    found = True
                    break
            except Exception:
                continue
        if found:
            PYRO_KNOWN_CHATS.add(TELEGRAM_SOURCE_CHAT_ID)
            logger.info("Pyro(start): canale presente nei dialoghi (no join).")
            return
    except Exception as e:
        logger.info("Pyro(start): warm-up dialoghi fallito (non blocca): %s", e)

    # 2) prova get_chat
    try:
        await pyro_client.get_chat(TELEGRAM_SOURCE_CHAT_ID)
        PYRO_KNOWN_CHATS.add(TELEGRAM_SOURCE_CHAT_ID)
        logger.info("Pyro(start): get_chat OK senza join.")
        return
    except Exception as e:
        logger.info("Pyro(start): get_chat fallita: %s", e)

    # 3) join solo se abbiamo un invite e non in backoff
    invite = TG_CHAT_INVITE or _http_create_invite_link(TELEGRAM_SOURCE_CHAT_ID)
    if not invite:
        logger.info("Pyro(start): nessun invite disponibile, skip join (riceverai cancellazioni solo se già membro).")
        return

    now = time.time()
    if now < JOIN_BACKOFF.get(TELEGRAM_SOURCE_CHAT_ID, 0):
        logger.info("Pyro(start): join in backoff, skip.")
        return

    try:
        await pyro_client.join_chat(invite)
        logger.info("Pyro(start): join effettuato.")
    except FloodWait as fw:
        JOIN_BACKOFF[TELEGRAM_SOURCE_CHAT_ID] = time.time() + fw.value + 5
        logger.warning("Pyro(start): FLOOD_WAIT %ss, riproverò solo dopo backoff.", fw.value)
        return
    except Exception as e:
        if "USER_ALREADY_PARTICIPANT" in str(e).upper():
            logger.info("Pyro(start): già partecipante.")
        else:
            logger.warning("Pyro(start): join fallita: %s", e)
            return

    # 4) dopo join, warm-up e verifica
    try:
        async for _ in pyro_client.get_dialogs():
            pass
        await pyro_client.get_chat(TELEGRAM_SOURCE_CHAT_ID)
        PYRO_KNOWN_CHATS.add(TELEGRAM_SOURCE_CHAT_ID)
        logger.info("Pyro(start): accesso OK dopo join+warm-up.")
    except Exception as e:
        logger.warning("Pyro(start): accesso KO anche dopo join: %s", e)

async def _pyro_on_deleted(_, messages):
    """
    Handler realtime: quando Telegram cancella, rimuoviamo il mirror su Discord.
    """
    try:
        ids = getattr(messages, "ids", []) or []
        if not ids:
            return
        for mid in ids:
            row = db_get_mapping(TELEGRAM_SOURCE_CHAT_ID, mid)
            if not row:
                continue
            _, discord_id, webhook, _, _, thread_id = row
            ok = delete_discord_message(webhook, discord_id, thread_id=thread_id)
            if ok:
                db_mark_deleted(TELEGRAM_SOURCE_CHAT_ID, mid)
                logger.info("Delete realtime: tg_id=%s rimosso su Discord.", mid)
    except Exception:
        logger.exception("Delete realtime: errore handler")

async def _start_pyrogram_and_handlers(app: Application):
    """
    Avvia un client Pyrogram persistente e registra DeletedMessagesHandler.
    """
    global pyro_client
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        logger.info("Pyrogram disabilitato.")
        return

    from pyrogram import Client, filters
    from pyrogram.handlers import DeletedMessagesHandler

    pyro_client = Client(
        name=":bridge:",
        api_id=TG_API_ID,
        api_hash=TG_API_HASH,
        session_string=TG_SESSION,
        no_updates=False,              # vogliamo ricevere gli update
        workdir=tempfile.gettempdir(),
    )

    await pyro_client.start()
    logger.info("Pyrogram: connesso.")
    # ensure access una volta (senza flood)
    await _pyro_ensure_access_once()

    # handler cancellazioni solo per la chat sorgente
    pyro_client.add_handler(DeletedMessagesHandler(_pyro_on_deleted, filters.chat(TELEGRAM_SOURCE_CHAT_ID)))
    logger.info("Pyrogram: DeletedMessagesHandler registrato.")

# ========= Startup & wiring =========
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
    logger.info("Pyrogram enabled=%s (API_ID=%s, SESSION=%s)", bool(TG_SESSION), bool(TG_API_ID and TG_API_HASH), bool(TG_SESSION))

    # Avvia Pyrogram persistente + handler delete
    app.create_task(_start_pyrogram_and_handlers(app))

    # Scheduler: solo reconcile EDIT (delete è realtime)
    if getattr(app, "job_queue", None):
        logger.info("Scheduler: JobQueue PTB ogni 10s (EDIT).")
        app.job_queue.run_repeating(reconcile_last_messages, interval=10, first=10)
    else:
        logger.warning("Scheduler: nessun JobQueue -> loop asyncio (EDIT) ogni 10s.")
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




