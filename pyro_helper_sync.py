# pyro_helper_sync.py
from typing import Optional
import os
from pyrogram import Client
from pyrogram.errors import (
    RPCError,
    UserAlreadyParticipant,
    InviteHashExpired,
    InviteHashInvalid,
    InviteRequestSent,
)

async def download_media_via_pyro_async(
    api_id: int,
    api_hash: str,
    session_string: str,
    chat_id: int,
    message_id: int,
    download_dir: Optional[str] = None,
    invite_link: Optional[str] = None,   # <--- nuovo
) -> str:
    """
    Scarica media da (chat_id, message_id) usando Pyrogram (async).
    Se non conosce il peer, prova a usare invite_link per joinare.
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
        async def ensure_access() -> None:
            try:
                await app.get_chat(chat_id)
                return
            except RPCError:
                pass
            if invite_link:
                try:
                    await app.join_chat(invite_link)
                except UserAlreadyParticipant:
                    pass
                except InviteRequestSent:
                    raise RuntimeError(
                        "L'invito richiede approvazione manuale. Approva l'account della sessione dal canale."
                    )
                except (InviteHashExpired, InviteHashInvalid):
                    raise RuntimeError("Invite link non valido/scaduto generato dal bot.")
                # dopo il join, il get_chat per id deve riuscire
                await app.get_chat(chat_id)
                return
            raise RuntimeError("Peer non risolvibile e nessun invite_link fornito.")

        await ensure_access()
        msg = await app.get_messages(chat_id, message_id)
        path = await app.download_media(msg, file_name=download_dir)
        if not path or not isinstance(path, str):
            raise RuntimeError("download_media ha restituito un valore non valido.")
        return path
