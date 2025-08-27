# pyro_helper.py
import os
import asyncio
import tempfile
import logging
from pathlib import Path
from typing import Optional

import requests
from pyrogram import Client
from pyrogram.errors import RPCError

logger = logging.getLogger(__name__)

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_STRING = os.getenv("PYRO_SESSION", None)  # preferibile: string session per headless deploy
SESSION_NAME = os.getenv("PYRO_SESSION_NAME", "pyro_session")

_client: Optional[Client] = None

def _make_client() -> Client:
    global _client
    if SESSION_STRING:
        # crea client usando session string (headless)
        _client = Client(
            name="pyro-client",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=SESSION_STRING,
            workdir="."
        )
    else:
        _client = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, workdir=".")
    return _client

async def init_client():
    """Avvia il client Pyrogram (chiamare una volta all'avvio del bridge)."""
    global _client
    if _client is None:
        _make_client()
        try:
            await _client.start()
            logger.info("✅ Pyrogram client avviato.")
        except Exception as e:
            logger.exception("Impossibile avviare Pyrogram client: %s", e)
            raise

async def stop_client():
    global _client
    if _client is not None:
        try:
            await _client.stop()
            logger.info("Pyrogram client fermato.")
        except Exception as e:
            logger.exception("Errore nello stop di Pyrogram: %s", e)

async def download_and_forward(chat_id: int, message_id: int, webhook_url: str, caption: str = "", media_type: str = None):
    """
    Scarica il media dal messaggio Telegram (usando Pyrogram) e lo invia a Discord tramite webhook_url.
    Restituisce il JSON della risposta Discord se va a buon fine.
    """
    if _client is None:
        await init_client()

    # download
    file_path = None
    try:
        msg = await _client.get_messages(chat_id, message_id)
        if not msg:
            raise RuntimeError("Messaggio non trovato via Pyrogram")

        # scarica il media in una cartella temporanea
        tmpdir = tempfile.mkdtemp(prefix="pyro_dl_")
        # file_name None -> lascia Pyrogram scegliere nome
        file_path = await _client.download_media(msg, file_name=str(Path(tmpdir) / "media"))
        if not file_path:
            raise RuntimeError("Nessun file scaricato dal messaggio")

        logger.info("File scaricato via Pyrogram: %s", file_path)

        # 1) invia prima la caption come embed (se video e/o se vuoi che il testo sia in alto)
        if caption:
            embed_payload = {"embeds": [{"description": caption}]}
            # requests è sincrono: esegui in thread per non bloccare event loop
            await asyncio.to_thread(requests.post, webhook_url, json=embed_payload)

        # 2) invia il file come attachment (così Discord mostra il player per i video)
        def _post_file():
            with open(file_path, "rb") as f:
                files = {"file": (Path(file_path).name, f)}
                r = requests.post(webhook_url, files=files)
                r.raise_for_status()
                try:
                    return r.json()
                except Exception:
                    return {"status_code": r.status_code}

        discord_resp = await asyncio.to_thread(_post_file)
        logger.info("File inviato a Discord via webhook (resp): %s", discord_resp)
        return discord_resp

    except RPCError as e:
        logger.exception("Errore RPC Pyrogram: %s", e)
        raise
    except Exception as e:
        logger.exception("Errore download_and_forward: %s", e)
        raise
    finally:
        # cleanup
        try:
            if file_path and Path(file_path).exists():
                Path(file_path).unlink()
            # rimuovi tmp dir se vuota
            if 'tmpdir' in locals() and Path(tmpdir).exists():
                try:
                    Path(tmpdir).rmdir()
                except OSError:
                    pass
        except Exception:
            pass
