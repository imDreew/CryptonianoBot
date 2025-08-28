# pyro_helper_sync.py
from typing import Optional
import os
import logging
from pyrogram import Client
from pyrogram.errors import (
    RPCError,
    UserAlreadyParticipant,
    InviteHashExpired,
    InviteHashInvalid,
    InviteRequestSent,
)

logger = logging.getLogger("pyro-helper")

# Fallback opzionale (se il bot non può creare autoinvite)
FALLBACK_INVITE = (
    os.getenv("TG_CHAT_INVITE")
    or os.getenv("TELEGRAM_SOURCE_INVITE_LINK")
    or None
)

async def download_media_via_pyro_async(
    api_id: int,
    api_hash: str,
    session_string: str,
    chat_id: int,
    message_id: int,
    download_dir: Optional[str] = None,
    invite_link: Optional[str] = None,   # passato da bridge
) -> str:
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
        logger.info("Pyro: connesso. Verifico accesso a chat_id=%s", chat_id)

        async def ensure_access_with(inv_link: Optional[str]) -> bool:
            # 1) tenta direttamente get_chat(id)
            try:
                await app.get_chat(chat_id)
                logger.info("Pyro: get_chat(id) OK (già membro/visibile).")
                return True
            except (RPCError, ValueError) as e:
                logger.info("Pyro: get_chat(id) fallita: %s", e)

            # 2) se ho un invito, provo JOIN
            if inv_link:
                logger.info("Pyro: provo join via invite=%s", inv_link)
                try:
                    await app.join_chat(inv_link)
                    logger.info("Pyro: join riuscito (o già partecipante).")
                except UserAlreadyParticipant:
                    logger.info("Pyro: già partecipante.")
                except InviteRequestSent:
                    raise RuntimeError(
                        "Pyro: invite richiede approvazione. Approva l'account della sessione dal canale."
                    )
                except (InviteHashExpired, InviteHashInvalid):
                    raise RuntimeError("Pyro: invite non valido/scaduto.")
                except RPCError as e:
                    raise RuntimeError(f"Pyro: join fallito: {type(e).__name__}: {e}")
                # dopo join, riprova get_chat
                await app.get_chat(chat_id)
                logger.info("Pyro: get_chat(id) OK dopo join.")
                return True

            logger.info("Pyro: nessun invite disponibile in questo tentativo.")
            return False

        # 1) usa l’autoinvite del bot
        ok = await ensure_access_with(invite_link)
        # 2) fallback env opzionale
        if not ok and FALLBACK_INVITE:
            logger.info("Pyro: uso FALLBACK_INVITE da env…")
            ok = await ensure_access_with(FALLBACK_INVITE)

        if not ok:
            raise RuntimeError("Pyro: impossibile accedere al peer. Nessun invite usabile e ID non risolvibile.")

        logger.info("Pyro: recupero messaggio message_id=%s", message_id)
        msg = await app.get_messages(chat_id, message_id)
        logger.info("Pyro: messaggio ottenuto, avvio download_media…")
        path = await app.download_media(msg, file_name=download_dir)
        if not path or not isinstance(path, str):
            raise RuntimeError("Pyro: download_media ha restituito un valore non valido.")
        logger.info("Pyro: download completato. Path=%s", path)
        return path
