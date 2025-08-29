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

# =========================
# Logging (pulito e chiaro)
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

bridge_log = logging.getLogger("bridge")
pyro_log = logging.getLogger("pyro")
discord_log = logging.getLogger("discord")

class CtxAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = " ".join(f"{k}={v}" for k, v in self.extra.items() if v is not None)
        return f"{msg} {extra}".strip(), kwargs

def with_ctx(logger, **ctx):
    return CtxAdapter(logger, ctx)

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

FORWARD_EDITS = env_bool("FORWARD_EDITS", False)      # True = edit realtime via Pyrogram
INCLUDE_AUTHOR = env_bool("INCLUDE_AUTHOR", False)    # aggiunge firma autore

DISCORD_WEBHOOK_SCALPING   = os.getenv("DISCORD_WEBHOOK_SCALPING", "").strip()
DISCORD_WEBHOOK_ALGORITMO  = os.getenv("DISCORD_WEBHOOK_ALGORITMO", "").strip()
DISCORD_WEBHOOK_FORMAZIONE = os.getenv("DISCORD_WEBHOOK_FORMAZIONE", "").strip()
DISCORD_WEBHOOK_DEFAULT    = os.getenv("DISCORD_WEBHOOK_DEFAULT", "").strip()

WEBHOOK_MAP = {
    "SCALPING": DISCORD_WEBHOOK_SCALPING,
    "ALGORITMO": DISCORD_WEBHOOK_ALGORITMO,
    "FORMAZIONE": DISCORD_WEBHOOK_FORMAZIONE,
}

BOT_API_LIMIT = 20 * 1024 * 1024        # 20MB Bot API
DISCORD_MAX_BYTES = 100 * 1024 * 1024   # 100MB Discord

# Pyrogram (user session)
TG_API_ID   = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION  = os.getenv("PYRO_SESSION", os.getenv("TG_SESSION", "")).strip()
TG_CHAT_INVITE = os.getenv("TG_CHAT_INVITE", "").strip()  # opzionale, meglio se permanente

if TG_SESSION:
    bridge_log.info("pyrogram=enabled (session string presente)")
else:
    bridge_log.info("pyrogram=disabled (manca session string)")

# HTTP base
PORT = int(os.getenv("PORT", "8080"))

# Cache/State
PYRO_KNOWN_CHATS: set[int] = set()
JOIN_BACKOFF: dict[int, float] = {}  # chat_id -> epoch per ritentare join
PYRO_ACCESS_OK: bool = False

# Stato bot
BOT_ID: Optional[int] = None
BOT_USERNAME: Optional[str] = None

# =========================
# DB (mapping Telegram<->Discord)
# =========================
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "map.sqlite")

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

def db_get_recent_mappings(tg_chat_id: int, limit: int = 20):
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

# =========================
# Utils
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
# Discord helpers (retry + wait)
# =========================
def _ensure_wait(url: str) -> str:
    return url + ("&wait=true" if "?" in url else "?wait=true")

def _post_with_retry(url: str, **kwargs) -> Response:
    last = None
    for attempt in range(5):
        r = requests.post(url, timeout=600, **kwargs)
        if r.status_code not in (429,) and r.status_code < 500:
            return r
        wait = min(2 ** attempt, 30)
        discord_log.warning("post_retry status=%s wait_s=%s", r.status_code, wait)
        time.sleep(wait)
        last = r
    return last

def _patch_with_retry(url: str, **kwargs) -> Response:
    last = None
    for attempt in range(5):
        r = requests.patch(url, timeout=600, **kwargs)
        if r.status_code not in (429,) and r.status_code < 500:
            return r
        wait = min(2 ** attempt, 30)
        discord_log.warning("patch_retry status=%s wait_s=%s", r.status_code, wait)
        time.sleep(wait)
        last = r
    return last

def _delete_with_retry(url: str, **kwargs) -> Response:
    last = None
    for attempt in range(5):
        r = requests.delete(url, timeout=600, **kwargs)
        if r.status_code not in (429,) and r.status_code < 500:
            return r
        wait = min(2 ** attempt, 30)
        discord_log.warning("delete_retry status=%s wait_s=%s", r.status_code, wait)
        time.sleep(wait)
        last = r
    return last

def send_discord_text(webhook_url: str, content: str):
    if not webhook_url:
        discord_log.info("send_text skip=no_webhook")
        return False, None, None, None
    url = _ensure_wait(webhook_url)
    r = _post_with_retry(url, json={"content": content})
    ok, msg_id, ch_id, th_id = False, None, None, None
    if r is not None and 200 <= r.status_code < 300:
        try:
            j = r.json()
            msg_id = j.get("id")
            ch_id = j.get("channel_id")
            th_id = ch_id
            ok = True
        except Exception:
            ok = True
    discord_log.log(logging.INFO if ok else logging.ERROR, "send_text status=%s", getattr(r, "status_code", "NA"))
    return ok, msg_id, ch_id, th_id

def send_discord_file_bytes(webhook_url: str, file_bytes: bytes, filename: str, content: Optional[str] = None):
    if not webhook_url:
        discord_log.info("send_file skip=no_webhook")
        return False, None, None, None
    url = _ensure_wait(webhook_url)
    files = {"file": (filename, io.BytesIO(file_bytes))}
    data = {"content": content} if content else {}
    r = _post_with_retry(url, files=files, data=data)
    ok, msg_id, ch_id, th_id = False, None, None, None
    if r is not None and 200 <= r.status_code < 300:
        try:
            j = r.json()
            msg_id = j.get("id")
            ch_id = j.get("channel_id")
            th_id = ch_id
            ok = True
        except Exception:
            ok = True
    discord_log.log(logging.INFO if ok else logging.ERROR, "send_file status=%s filename=%s", getattr(r, "status_code", "NA"), filename)
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
    r = _patch_with_retry(url, json={"content": new_content})
    ok = r is not None and 200 <= r.status_code < 300
    discord_log.log(logging.INFO if ok else logging.ERROR, "edit status=%s", getattr(r, "status_code", "NA"))
    return ok

def delete_discord_message(webhook_url: str, message_id: str, thread_id: Optional[str] = None) -> bool:
    url = f"{webhook_url}/messages/{message_id}"
    if thread_id:
        url += f"?thread_id={thread_id}"
    r = _delete_with_retry(url)
    if r is not None and (200 <= r.status_code < 300 or r.status_code == 404):
        discord_log.info("delete status=%s", r.status_code)
        return True
    discord_log.warning("delete fail status=%s -> tombstone", getattr(r, "status_code", "NA"))
    return edit_discord_message(webhook_url, message_id, "*(eliminato su Telegram)*", thread_id=thread_id)

# =========================
# Bot-side: autoinvite (fallback HTTP, se serve)
# =========================
def _http_create_invite_link(chat_id: int) -> Optional[str]:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/createChatInviteLink"
        r = requests.post(url, json={
            "chat_id": chat_id, "name": "bridge-autoinvite",
            "creates_join_request": False, "member_limit": 0
        }, timeout=30)
        if r.ok and r.json().get("ok"):
            bridge_log.info("autoinvite=created via Bot API")
            return r.json()["result"]["invite_link"]
        bridge_log.warning("autoinvite=create_fail status=%s", r.status_code)
    except Exception as e:
        bridge_log.warning("autoinvite=exception err=%s", e)
    return None

# =========================
# Pyrogram client persistente
# =========================
pyro_client = None  # istanza Client

async def _pyro_ensure_access() -> None:
    """
    Tenta di 'vedere' la chat. Join SOLO se necessario e non in backoff.
    Aggiorna PYRO_ACCESS_OK / PYRO_KNOWN_CHATS.
    """
    global PYRO_ACCESS_OK
    if not pyro_client:
        return
    from pyrogram.errors import FloodWait

    # Se già ok, verifica veloce
    if PYRO_ACCESS_OK or (TELEGRAM_SOURCE_CHAT_ID in PYRO_KNOWN_CHATS):
        try:
            await pyro_client.get_chat(TELEGRAM_SOURCE_CHAT_ID)
            PYRO_ACCESS_OK = True
            return
        except Exception:
            PYRO_ACCESS_OK = False  # ricade nel flusso

    # 1) warm-up dialoghi
    try:
        found = False
        async for d in pyro_client.get_dialogs():
            if getattr(d.chat, "id", None) == TELEGRAM_SOURCE_CHAT_ID:
                found = True
                break
        if found:
            PYRO_KNOWN_CHATS.add(TELEGRAM_SOURCE_CHAT_ID)
            PYRO_ACCESS_OK = True
            bridge_log.info("access source=dialogs esito=ok")
            return
    except Exception as e:
        bridge_log.info("access warmup=fail err=%s", e)

    # 2) get_chat diretto
    try:
        await pyro_client.get_chat(TELEGRAM_SOURCE_CHAT_ID)
        PYRO_KNOWN_CHATS.add(TELEGRAM_SOURCE_CHAT_ID)
        PYRO_ACCESS_OK = True
        bridge_log.info("access source=get_chat esito=ok")
        return
    except Exception as e:
        bridge_log.info("access get_chat=fail err=%s", e)

    # 3) join solo se disponibile invite + no backoff
    invite = TG_CHAT_INVITE or _http_create_invite_link(TELEGRAM_SOURCE_CHAT_ID)
    if not invite:
        PYRO_ACCESS_OK = False
        bridge_log.info("access join=skip reason=no_invite")
        return

    now = time.time()
    until = JOIN_BACKOFF.get(TELEGRAM_SOURCE_CHAT_ID, 0)
    if now < until:
        bridge_log.info("access join=skip reason=backoff wait_s=%s", int(until - now))
        PYRO_ACCESS_OK = False
        return

    try:
        await pyro_client.join_chat(invite)
        bridge_log.info("access join=ok")
    except FloodWait as fw:
        JOIN_BACKOFF[TELEGRAM_SOURCE_CHAT_ID] = time.time() + fw.value + 5
        PYRO_ACCESS_OK = False
        bridge_log.warning("access join=flood_wait wait_s=%s", fw.value)
        return
    except Exception as e:
        if "USER_ALREADY_PARTICIPANT" in str(e).upper():
            bridge_log.info("access join=already_participant")
        else:
            JOIN_BACKOFF[TELEGRAM_SOURCE_CHAT_ID] = time.time() + 60
            PYRO_ACCESS_OK = False
            bridge_log.warning("access join=fail backoff_s=60 err=%s", e)
            return

    # 4) verifica finale
    try:
        async for _ in pyro_client.get_dialogs():
            pass
        await pyro_client.get_chat(TELEGRAM_SOURCE_CHAT_ID)
        PYRO_KNOWN_CHATS.add(TELEGRAM_SOURCE_CHAT_ID)
        PYRO_ACCESS_OK = True
        bridge_log.info("access verify=ok")
    except Exception as e:
        PYRO_ACCESS_OK = False
        bridge_log.warning("access verify=fail err=%s", e)

async def _pyro_on_deleted(_, messages):
    """Realtime: quando Telegram cancella, togli anche su Discord."""
    try:
        ids = getattr(messages, "ids", []) or []
        for mid in ids:
            row = db_get_mapping(TELEGRAM_SOURCE_CHAT_ID, mid)
            if not row:
                continue
            _, discord_id, webhook, *_ , thread_id = row
            ok = delete_discord_message(webhook, discord_id, thread_id=thread_id)
            if ok:
                db_mark_deleted(TELEGRAM_SOURCE_CHAT_ID, mid)
            bridge_log.info("delete op=realtime tg_msg=%s esito=%s", mid, "ok" if ok else "fail")
    except Exception as e:
        bridge_log.exception("delete realtime error=%s", e)

async def _pyro_on_edited(_, message):
    """Realtime: modifica su Telegram -> patch su Discord (solo se FORWARD_EDITS=True)."""
    try:
        if not FORWARD_EDITS:
            return
        mid = message.id
        row = db_get_mapping(TELEGRAM_SOURCE_CHAT_ID, mid)
        if not row:
            return
        _, discord_id, webhook, *_ , thread_id = row
        text_md = tg_html_to_discord_md(message.caption_html or message.text_html or "")
        ok = edit_discord_message(webhook, discord_id, text_md[:2000], thread_id=thread_id)
        if ok:
            ts = int((getattr(message, "edit_date", None) or getattr(message, "date", None)).timestamp())
            db_update_edit_ts_and_content(TELEGRAM_SOURCE_CHAT_ID, mid, ts, text_md[:2000])
        bridge_log.info("edit op=realtime tg_msg=%s esito=%s", mid, "ok" if ok else "fail")
    except Exception:
        bridge_log.exception("edit realtime error")

async def _start_pyrogram_and_handlers(app: Application):
    """Avvia Pyrogram persistente + handlers realtime + task ensure_access periodico."""
    global pyro_client
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        bridge_log.info("pyrogram=start skip (config mancante)")
        return

    from pyrogram import Client, filters
    from pyrogram.handlers import DeletedMessagesHandler, EditedMessageHandler

    pyro_client = Client(
        name=":bridge:",
        api_id=TG_API_ID,
        api_hash=TG_API_HASH,
        session_string=TG_SESSION,
        no_updates=False,
        workdir=tempfile.gettempdir(),
    )
    await pyro_client.start()
    pyro_log.info("connected=1")

    await _pyro_ensure_access()

    pyro_client.add_handler(DeletedMessagesHandler(_pyro_on_deleted, filters.chat(TELEGRAM_SOURCE_CHAT_ID)))
    if FORWARD_EDITS:
        pyro_client.add_handler(EditedMessageHandler(_pyro_on_edited, filters.chat(TELEGRAM_SOURCE_CHAT_ID)))
    pyro_log.info("handlers registered deleted=%s edited=%s", True, FORWARD_EDITS)

    async def _periodic_access():
        while True:
            try:
                await _pyro_ensure_access()
            except Exception:
                bridge_log.exception("ensure_access periodic error")
            await asyncio.sleep(60)
    app.create_task(_periodic_access())

# =========================
# FFmpeg (compress >100MB)
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
        out = subprocess.check_output(
            ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",input_path],
            stderr=subprocess.STDOUT, timeout=30
        )
        return float(out.decode().strip())
    except Exception:
        return None

def compress_video_to_limit(input_path: str, max_bytes: int = DISCORD_MAX_BYTES) -> Optional[str]:
    if not ffmpeg_available():
        bridge_log.warning("ffmpeg=absent")
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
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            bridge_log.warning("ffmpeg return=%s", proc.returncode)
            return None
        return out_path if file_size(out_path) <= max_bytes else None

    for args in attempts:
        res = run_once(*args)
        if res:
            return res
    bridge_log.warning("compress fail=over_limit target=%s", human_size(max_bytes))
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
            bridge_log.info("route tag=%s webhook=%s", tag, bool(url))
            break
    if not chosen:
        chosen = DISCORD_WEBHOOK_DEFAULT or None
        bridge_log.info("route default=%s", bool(chosen))
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
            bridge_log.exception("notify_admin fail")

# =========================
# Pyro download helper (usa client persistente)
# =========================
async def pyro_download_by_ids(chat_id: int, message_id: int) -> Optional[str]:
    if not (TG_API_ID and TG_API_HASH and TG_SESSION and pyro_client):
        bridge_log.error("pyro_download skip reason=config_or_client_missing")
        return None
    # assicurati accesso
    if not PYRO_ACCESS_OK:
        await _pyro_ensure_access()
        if not PYRO_ACCESS_OK:
            bridge_log.error("pyro_download access=not_ready")
            return None
    try:
        m = await pyro_client.get_messages(chat_id, message_id)
        if not m:
            bridge_log.error("pyro_download get_messages=None")
            return None
        suffix = ".bin"
        name = getattr(getattr(m, "document", None), "file_name", None) or \
               getattr(getattr(m, "video", None), "file_name", None) or \
               "file.bin"
        _, ext = os.path.splitext(name)
        if ext: suffix = ext
        path = await m.download(file_name=os.path.join(tempfile.gettempdir(), f"tg_{message_id}_{int(time.time())}{suffix}"))
        PYRO_KNOWN_CHATS.add(chat_id)
        bridge_log.info("pyro_download ok path=%s", path)
        return path
    except Exception as e:
        bridge_log.exception("pyro_download fail err=%s", e)
        return None

# =========================
# Handlers PTB
# =========================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    if not msg or msg.chat_id != TELEGRAM_SOURCE_CHAT_ID:
        return

    log = with_ctx(bridge_log, chat=msg.chat_id, tg_msg=msg.message_id)

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
            log.info("forward op=send_text esito=%s", "ok" if ok else "fail")
            if not ok:
                await notify_admin(context, "Errore invio testo a Discord.")
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

            # Download
            if size_tg and size_tg < BOT_API_LIMIT:
                f = await context.bot.get_file(telegram_file_id)
                resp = requests.get(f.file_path, timeout=600)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(prefix="tg_dl_", suffix=os.path.splitext(file_name)[1] or ".bin", delete=False) as fh:
                    fh.write(resp.content)
                    path = fh.name
                log.info("download method=bot_api size=%s path=%s", human_size(len(resp.content)), path)
            else:
                path = await pyro_download_by_ids(msg.chat_id, msg.message_id)
                if not path:
                    await notify_admin(context, "Download via Pyrogram fallito/non pronto.")
                    return
                log.info("download method=pyrogram size=%s path=%s", human_size(file_size(path)), path)

            # Compress se serve
            if is_video and file_size(path) > DISCORD_MAX_BYTES:
                log.info("discord_limit action=compress input=%s", human_size(file_size(path)))
                comp_path = compress_video_to_limit(path, DISCORD_MAX_BYTES)
                if comp_path and file_size(comp_path) <= DISCORD_MAX_BYTES:
                    ok, discord_id, discord_ch, discord_thread = send_discord_file_path(webhook_url, comp_path, content if content.strip() else None)
                    log.info("forward op=send_file compressed=%s esito=%s", os.path.basename(comp_path), "ok" if ok else "fail")
                    if not ok: await notify_admin(context, "Errore invio file compresso a Discord.")
                else:
                    warn = (
                        f"⚠️ Video oltre 100MB.\n"
                        f"- Originale: {human_size(file_size(path))}\n"
                        f"- Compressione: non disponibile/insufficiente\n"
                        f"Carica su host esterno oppure aumenta risorse."
                    )
                    ok, discord_id, discord_ch, discord_thread = send_discord_text(webhook_url, (content + "\n\n" + warn).strip())
                    log.info("forward op=send_text reason=too_large esito=%s", "ok" if ok else "fail")
            else:
                ok, discord_id, discord_ch, discord_thread = send_discord_file_path(webhook_url, path, content if content.strip() else None)
                log.info("forward op=send_file name=%s esito=%s", os.path.basename(path), "ok" if ok else "fail")
                if not ok: await notify_admin(context, "Errore invio file a Discord.")
    except Exception:
        bridge_log.exception("forward error")
        await notify_admin(context, "Errore generale gestione media")
    finally:
        for p in (comp_path, path):
            try:
                if p and os.path.exists(p) and os.path.isfile(p):
                    os.remove(p)
                    bridge_log.info("cleanup path=%s", p)
            except Exception:
                pass

    # Mapping
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
        bridge_log.exception("mapping upsert fail")

async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Se non vuoi realtime via Pyrogram, abilita questa per convertire edit via Bot API.
    if FORWARD_EDITS:
        return
    await on_message(update, context)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot attivo. Inoltro messaggi, cancellazioni realtime e reconcile soft.")

# =========================
# Reconcile soft (safety net)
# =========================
async def reconcile_last_messages(context: ContextTypes.DEFAULT_TYPE):
    start = time.time()
    if not (TG_API_ID and TG_API_HASH and TG_SESSION) or not pyro_client:
        return
    if not PYRO_ACCESS_OK:
        bridge_log.info("reconcile skip reason=access_not_ready")
        return

    changed = deleted = 0
    try:
        recent = db_get_recent_mappings(TELEGRAM_SOURCE_CHAT_ID, limit=20)
        if not recent:
            return

        # quick check
        try:
            await pyro_client.get_chat(TELEGRAM_SOURCE_CHAT_ID)
        except Exception:
            bridge_log.info("reconcile skip reason=get_chat_fail")
            return

        from pyrogram.errors import RPCError, MessageIdInvalid
        for tg_message_id, discord_message_id, webhook_url, tg_edit_ts, last_content, thread_id in recent:
            m = None
            try:
                m = await pyro_client.get_messages(TELEGRAM_SOURCE_CHAT_ID, tg_message_id)
            except (MessageIdInvalid, RPCError):
                m = None
            except Exception:
                m = None

            if not m:
                # delete realtime dovrebbe averlo già gestito; safety:
                ok = delete_discord_message(webhook_url, discord_message_id, thread_id=thread_id)
                if ok:
                    db_mark_deleted(TELEGRAM_SOURCE_CHAT_ID, tg_message_id)
                    deleted += 1
                continue

            plain = (m.caption or m.text or "")
            new_content = plain[:2000]
            new_edit_ts = int((getattr(m, "edit_date", None) or getattr(m, "date", None)).timestamp() or tg_edit_ts)
            if new_edit_ts > tg_edit_ts or (new_content and new_content != (last_content or "")):
                ok = edit_discord_message(webhook_url, discord_message_id, new_content, thread_id=thread_id)
                if ok:
                    db_update_edit_ts_and_content(TELEGRAM_SOURCE_CHAT_ID, tg_message_id, new_edit_ts, new_content)
                    changed += 1
    except Exception:
        bridge_log.exception("reconcile error")
    finally:
        dur = int((time.time() - start) * 1000)
        bridge_log.info("reconcile summary batch=%s changed=%s deleted=%s duration_ms=%s",
                        len(recent) if 'recent' in locals() else 0, changed, deleted, dur)

# =========================
# Startup & wiring
# =========================
async def _post_init(app: Application) -> None:
    global BOT_ID, BOT_USERNAME
    db_init()

    me = await app.bot.get_me()
    BOT_ID = me.id
    BOT_USERNAME = f"@{me.username}" if me.username else None

    bridge_log.info("== start ==")
    bridge_log.info("bot id=%s username=%s", BOT_ID, BOT_USERNAME)
    bridge_log.info("source_chat=%s admin_notify=%s", TELEGRAM_SOURCE_CHAT_ID, TELEGRAM_ADMIN_CHAT_ID)
    bridge_log.info("flags forward_edits=%s include_author=%s", FORWARD_EDITS, INCLUDE_AUTHOR)
    bridge_log.info(
        "webhooks scalping=%s algoritmo=%s formazione=%s default=%s",
        bool(DISCORD_WEBHOOK_SCALPING), bool(DISCORD_WEBHOOK_ALGORITMO),
        bool(DISCORD_WEBHOOK_FORMAZIONE), bool(DISCORD_WEBHOOK_DEFAULT)
    )

    # Avvia Pyrogram persistente + handlers + ensure_access periodico
    app.create_task(_start_pyrogram_and_handlers(app))

    # Scheduler asincrono leggero (no job-queue extra necessario)
    async def _reconcile_loop():
        while True:
            try:
                await reconcile_last_messages(None)
            except Exception:
                bridge_log.exception("reconcile loop error")
            await asyncio.sleep(180)   # ogni 3 minuti
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
    bridge_log.info("mode=polling")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

# =========================
# Main
# =========================
if __name__ == "__main__":
    application = build_application()
    add_handlers(application)
    run_polling(application)





