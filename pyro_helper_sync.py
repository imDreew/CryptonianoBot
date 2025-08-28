from typing import Optional
import os
from pyrogram import Client

def download_media_via_pyro(
    api_id: int,
    api_hash: str,
    session_string: str,
    chat_id: int,
    message_id: int,
    download_dir: Optional[str] = None,
) -> str:
    """
    Scarica media da (chat_id, message_id) usando Pyrogram con session string.
    Ritorna il path del file scaricato.
    """
    if not (api_id and api_hash and session_string):
        raise RuntimeError("Pyrogram non configurato (api_id/api_hash/session_string).")

    # Usa una session in-memory per non creare file sul disco
    app = Client(
        name=":memory:",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        no_updates=True,
        workdir=download_dir or os.getcwd(),
    )
    app.start()
    try:
        msg = app.get_messages(chat_id, message_id)
        path = app.download_media(msg, file_name=download_dir)
        if not path:
            raise RuntimeError("download_media ha restituito None.")
        return path
    finally:
        app.stop()

