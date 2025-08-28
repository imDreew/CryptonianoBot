from typing import Optional
import os
from pyrogram import Client

async def download_media_via_pyro_async(
    api_id: int,
    api_hash: str,
    session_string: str,
    chat_id: int,
    message_id: int,
    download_dir: Optional[str] = None,
) -> str:
    """
    Scarica media da (chat_id, message_id) usando Pyrogram (async).
    Ritorna il path del file scaricato (stringa).
    """
    if not (api_id and api_hash and session_string):
        raise RuntimeError("Pyrogram non configurato (api_id/api_hash/session_string).")

    async with Client(
        name=":memory:",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        no_updates=True,
        workdir=download_dir or os.getcwd(),
    ) as app:
        msg = await app.get_messages(chat_id, message_id)
        path = await app.download_media(msg, file_name=download_dir)
        if not path or not isinstance(path, str):
            raise RuntimeError("download_media ha restituito un valore non valido.")
        return path

