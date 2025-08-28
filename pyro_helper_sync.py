# pyro_helper_sync.py
from typing import Optional
import os
import inspect
from pyrogram import Client

def _run_if_awaitable(app: Client, value):
    return app.run(value) if inspect.isawaitable(value) else value

def download_media_via_pyro(
    api_id: int,
    api_hash: str,
    session_string: str,
    chat_id: int,
    message_id: int,
    download_dir: Optional[str] = None,
) -> str:
    """
    Scarica media da (chat_id, message_id) usando Pyrogram (session string).
    Ritorna il path del file scaricato (stringa).
    Compatibile con Pyrogram v2 (API async sotto, wrapper sync qui).
    """
    if not (api_id and api_hash and session_string):
        raise RuntimeError("Pyrogram non configurato (api_id/api_hash/session_string).")

    app = Client(
        name=":memory:",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        no_updates=True,
        workdir=download_dir or os.getcwd(),
    )

    # start/stop possono essere async: trattali allo stesso modo
    _run_if_awaitable(app, app.start())
    try:
        msg = _run_if_awaitable(app, app.get_messages(chat_id, message_id))
        path = _run_if_awaitable(app, app.download_media(msg, file_name=download_dir))
        if not path or not isinstance(path, str):
            raise RuntimeError(f"download_media ha restituito un valore non valido: {type(path)}")
        return path
    finally:
        _run_if_awaitable(app, app.stop())


