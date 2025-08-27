# pyro_helper_sync.py
import os
import tempfile
import logging
import requests
from pathlib import Path
from pyrogram import Client

logger = logging.getLogger(__name__)

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_NAME = os.getenv("PYRO_SESSION_NAME", "pyro_sync")

_pyro_client = None


def init_client_sync():
    """Avvia Pyrogram in modalità sync"""
    global _pyro_client
    _pyro_client = Client(
        SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        workdir="."
    )
    _pyro_client.start()
    logger.info("✅ Pyrogram sync avviato")


def stop_client_sync():
    """Chiudi connessione Pyrogram"""
    global _pyro_client
    if _pyro_client:
        try:
            _pyro_client.stop()
            logger.info("🛑 Pyrogram sync fermato")
        except Exception as e:
            logger.exception("Errore stop Pyrogram: %s", e)


def download_and_forward_sync(chat_id: int, message_id: int, webhook_url: str, caption: str = ""):
    """
    Scarica media con Pyrogram sync e manda a Discord via webhook
    (blocking: va chiamata in asyncio.to_thread)
    """
    global _pyro_client
    if not _pyro_client:
        raise RuntimeError("Pyrogram non inizializzato")

    msg = _pyro_client.get_messages(chat_id, message_id)
    if not msg or not msg.media:
        raise RuntimeError("Messaggio/media non trovato")

    tmpdir = tempfile.mkdtemp(prefix="pyro_dl_")
    file_path = _pyro_client.download_media(msg, file_name=str(Path(tmpdir) / "media"))

    # invio caption come embed
    if caption:
        try:
            requests.post(webhook_url, json={"embeds": [{"description": caption}]})
        except Exception as e:
            logger.warning("Errore invio caption a Discord: %s", e)

    # invio file
    with open(file_path, "rb") as f:
        files = {"file": (Path(file_path).name, f)}
        r = requests.post(webhook_url, files=files)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code}
