from typing import Optional
import os
from pyrogram import Client
from pyrogram.errors import RPCError

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
    - Se il peer non è risolvibile, prova a risolvere con get_chat(...)
    - Se ancora fallisce e sono presenti variabili d'ambiente per il join,
      prova a joinare e ripete il tentativo.
    Ritorna il path del file scaricato (stringa).
    """
    if not (api_id and api_hash and session_string):
        raise RuntimeError("Pyrogram non configurato (api_id/api_hash/session_string).")

    target_handle = os.getenv("TG_CHAT_USERNAME", "").strip()  # es. @nomecanale
    invite_link   = os.getenv("TG_CHAT_INVITE", "").strip()    # es. https://t.me/+xxxx

    async with Client(
        name=":memory:",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        no_updates=True,
        workdir=download_dir or os.getcwd(),
    ) as app:

        async def _try_get_message(_chat_id: int) -> str:
            msg = await app.get_messages(_chat_id, message_id)
            path = await app.download_media(msg, file_name=download_dir)
            if not path or not isinstance(path, str):
                raise RuntimeError("download_media ha restituito un valore non valido.")
            return path

        # 1) tentativo diretto con chat_id numerico
        try:
            return await _try_get_message(chat_id)
        except (ValueError, KeyError, RPCError):
            # Peer non risolto/inesistente nella cache: prova a risolvere e ripeti
            pass

        # 2) prova a risolvere in cache con get_chat(chat_id) e ripeti
        try:
            _ = await app.get_chat(chat_id)  # popola cache se possibile
            return await _try_get_message(chat_id)
        except (ValueError, KeyError, RPCError):
            pass

        # 3) tenta il join se fornisci handle o invite nelle env
        joined = False
        try:
            if invite_link:
                await app.join_chat(invite_link)
                joined = True
            elif target_handle:
                await app.join_chat(target_handle)
                joined = True
        except RPCError:
            # join fallito (invito invalido/già membro/privilegi insufficienti)
            pass

        if joined:
            # Se abbiamo un handle, possiamo anche ricavare l'id “vero” e riprovare
            try:
                target = target_handle or invite_link
                if target:
                    ch = await app.get_chat(target)
                    chat_id = ch.id  # allinea l'id e ritenta
            except RPCError:
                pass

            # riprova dopo join/resolve
            return await _try_get_message(chat_id)

        # 4) ultimo tentativo: se conosci un handle ma non hai potuto joinare,
        # prova comunque a risolvere il peer via handle (canali pubblici)
        if target_handle:
            try:
                _ = await app.get_chat(target_handle)
                return await _try_get_message(target_handle)  # pyrogram accetta handle
            except RPCError:
                pass

        raise RuntimeError(
            "Impossibile risolvere il peer Telegram: l'account della session string "
            "non è membro del canale/supergruppo sorgente e non è stato possibile joinare. "
            "Imposta TG_CHAT_USERNAME=@handle o TG_CHAT_INVITE=https://t.me/+invite nelle env."
        )


