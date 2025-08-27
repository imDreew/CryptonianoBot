import os
import asyncio
import logging
from pyrogram import Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PyroHelper")

# Config
api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")
session_name = os.getenv("TG_SESSION", "bridge")

# Client globale
pyro = Client(session_name, api_id=api_id, api_hash=api_hash)

async def _download_media_async(message_link: str, dest: str = "downloads/") -> str:
    """
    Scarica un media da un link pubblico/privato di Telegram (message_link).
    """
    await pyro.start()
    try:
        chat_id, msg_id = message_link.split("/")[-2], int(message_link.split("/")[-1])
        msg = await pyro.get_messages(int(chat_id), msg_id)
        file_path = await pyro.download_media(msg, file_name=dest)
        return file_path
    finally:
        await pyro.stop()

def download_media(message_link: str, dest: str = "downloads/") -> str:
    """
    Wrapper sincrono: scarica un media da Telegram e restituisce il percorso locale.
    """
    return asyncio.run(_download_media_async(message_link, dest))
