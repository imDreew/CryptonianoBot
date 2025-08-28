# pyro_helper_sync.py
from typing import Optional
import os
import logging
import tempfile
import mimetypes

from pyrogram import Client
from pyrogram.errors import (
    RPCError,
    UserAlreadyParticipant,
    InviteHashExpired,
    InviteHashInvalid,
    InviteRequestSent,
)

logger = logging.getLogger("pyro-helper")

# Fallback opzionale (se il bot non può creare autoinvite o vuoi forzare un link statico)
FALLBACK_INVITE = (
    os.getenv("TG_CHAT_INVITE")
    or os.getenv("TELEGRAM_SOURCE_INVITE_LINK")
    or None
)


def _guess_name_and_suffix(msg, message_id: int):
    """
    Deriva un nome/estensione sensata per il file scaricato.
    """
    name, suffix = f"media_{message_id}", ".bin"
    mt = None

    if getattr(msg, "video", None):
        v = msg.video
        name = v.file_name or name
        mt = getattr(v, "mime_type", None)
    elif getattr(msg, "document", None):
        d = msg.document
        name = d.file_name or name
        mt = getattr(d, "mime_type", None)
    elif getattr(msg, "photo", None):
        name = f"photo_{message_id}"
        mt = "image/jpeg"
    elif getattr(msg, "audio", None):
        a = msg.audio
        name = a.file_name or name
        mt = getattr(a, "mime_type", None)
    elif getattr(msg, "voice", None):
        name, mt = f"voice_{message_id}", "audio/ogg"
    elif getattr(msg, "animation", None):
        a = msg.animation
        name = a.file_name or name
        mt = getattr(a, "mime_type", None)

    base, ext = os.path.splitext(name)
    if ext:
        suffix = ext
        name = base
    elif mt:
        guess = mimetypes.guess_extension(mt) or ""
        if guess:
            suffix = guess

    # Discord preferisce estensioni standard per i video
    if suffix.lower() in (".bin", "") and mt and mt.startswith("video/"):
        suffix = ".mp4"

    return name, suffix


async def download_media_via_pyro_async(
    api_id: int,
    api_hash: str,
    session_string: str,
    chat_id: int,
    message_id: int,
    download_dir: Optional[str] = None,
    invite_link: Optional[str] = None,   # passato da bridge (autoinvite creato dal bot)
) -> str:
    """
    Scarica media da (chat_id, message_id) usando Pyrogram (async).
    Flusso:
      - tenta get_chat(id)
      - se fallisce e ho un invite -> join + (get_chat(invite) + warm-up dialoghi) + retry get_chat(id)
      - se ancora nulla e c'è FALLBACK_INVITE -> join + warm-up + retry
      - get_messages + download in memoria + salvataggio in file temporaneo
    Ritorna: percorso FILE (string) pronto per open(..., 'rb').
    """
    if not (api_id and api_hash and session_string):
        raise RuntimeError("Pyrogram non configurato (api_id/api_hash/session_string).")

    async with Client(
        name=":memory:",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,  # deve essere una sessione UTENTE, non bot
        no_updates=True,
        workdir=download_dir or os.getcwd(),
    ) as app:
        logger.info("Pyro: connesso. Verifico accesso a chat_id=%s", chat_id)

        async def ensure_access_with(inv_link: Optional[str]) -> bool:
            # 1) prova a risolvere direttamente per id
            try:
                await app.get_chat(chat_id)
                logger.info("Pyro: get_chat(id) OK (già membro/visibile).")
                return True
            except (RPCError, ValueError) as e:
                logger.info("Pyro: get_chat(id) fallita: %s", e)

            # 2) se ho un invito, provo a joinare
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

                # 2a) prova a risolvere via stringa (invite) per popolare lo storage
                try:
                    chat_via_inv = await app.get_chat(inv_link)
                    logger.info("Pyro: get_chat(invite) OK -> id=%s", getattr(chat_via_inv, "id", None))
                except Exception as e:
                    logger.info("Pyro: get_chat(invite) non disponibile: %s", e)

                # 2b) warm-up dialoghi (popola cache peer) e cerca l'id
                try:
                    logger.info("Pyro: warm-up dialoghi…")
                    async for dlg in app.get_dialogs():
                        if getattr(dlg.chat, "id", None) == chat_id:
                            logger.info("Pyro: trovato peer nei dialoghi.")
                            break
                except Exception as e:
                    logger.info("Pyro: get_dialogs fallito/skip: %s", e)

                # 2c) retry finale su get_chat(id)
                try:
                    await app.get_chat(chat_id)
                    logger.info("Pyro: get_chat(id) OK dopo join/warm-up.")
                    return True
                except (RPCError, ValueError) as e:
                    logger.info("Pyro: get_chat(id) ancora fallita: %s", e)
                    return False

            logger.info("Pyro: nessun invite disponibile in questo tentativo.")
            return False

        # 1) usa l’autoinvite del bot, se presente
        ok = await ensure_access_with(invite_link)
        # 2) fallback opzionale da env
        if not ok and FALLBACK_INVITE:
            logger.info("Pyro: uso FALLBACK_INVITE da env…")
            ok = await ensure_access_with(FALLBACK_INVITE)

        if not ok:
            raise RuntimeError("Pyro: impossibile accedere al peer. Nessun invite usabile e ID non risolvibile.")

        # Recupera messaggio e scarica il media in memoria
        logger.info("Pyro: recupero messaggio message_id=%s", message_id)
        msg = await app.get_messages(chat_id, message_id)
        logger.info("Pyro: messaggio ottenuto, avvio download_media in memoria…")

        bio = await app.download_media(msg, in_memory=True)
        if not bio:
            raise RuntimeError("Pyro: download_media ha restituito un buffer vuoto.")

        name, suffix = _guess_name_and_suffix(msg, message_id)
        tmp_dir = download_dir or tempfile.gettempdir()
        os.makedirs(tmp_dir, exist_ok=True)

        with tempfile.NamedTemporaryFile(prefix=f"{name}_", suffix=suffix, dir=tmp_dir, delete=False) as fh:
            fh.write(bio.getbuffer())
            out_path = fh.name

        logger.info("Pyro: download completato. Path=%s", out_path)
        return out_path

