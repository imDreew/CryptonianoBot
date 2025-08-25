import re
import html
import asyncio
import logging
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# =========================
# 🔧 Conversione HTML → Discord
# =========================
def telegram_html_to_discord(text: str) -> str:
    if not text:
        return ""

    # Decodifica entità HTML (&quot; → ", &amp; → &)
    text = html.unescape(text)

    # Grassetto
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)

    # Corsivo
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)

    # Sottolineato
    text = re.sub(r"<u>(.*?)</u>", r"__\1__", text, flags=re.DOTALL)

    # Barrato
    text = re.sub(r"<s>(.*?)</s>", r"~~\1~~", text, flags=re.DOTALL)
    text = re.sub(r"<strike>(.*?)</strike>", r"~~\1~~", text, flags=re.DOTALL)
    text = re.sub(r"<del>(.*?)</del>", r"~~\1~~", text, flags=re.DOTALL)

    # Inline code
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)

    # Blocco di codice <pre>
    text = re.sub(r"<pre.*?>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)

    # Link: <a href="URL">testo</a>
    text = re.sub(r'<a href="(.*?)">(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)

    # Spoiler <tg-spoiler>...</tg-spoiler>
    text = re.sub(r"<tg-spoiler>(.*?)</tg-spoiler>", r"||\1||", text, flags=re.DOTALL)

    # Citazioni <blockquote>
    text = re.sub(r"<blockquote>(.*?)</blockquote>", r"> \1", text, flags=re.DOTALL)

    # Rimuove eventuali tag rimasti
    text = re.sub(r"<.*?>", "", text)

    return text.strip()


# =========================
# HANDLER MESSAGGI
# =========================
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    # Prende contenuto con formattazione HTML
    content = message.text_html or message.caption_html or ""
    # 🔄 Converti in formato compatibile Discord
    content = telegram_html_to_discord(content)

    if not content:
        return

    # Qui prendi l’hashtag per decidere a quale webhook mandarlo
    webhook_url = "TUO_WEBHOOK_DISCORD"  # <-- sostituisci con la mappatura corretta

    payload = {"content": content}
    try:
        requests.post(webhook_url, json=payload)
    except Exception as e:
        logging.error(f"Errore nell'inoltro a Discord: {e}")


# =========================
# MAIN
# =========================
async def main():
    app = Application.builder().token("YOUR_TELEGRAM_BOT_TOKEN").build()

    # Ascolta i messaggi di testo/caption
    app.add_handler(MessageHandler(filters.TEXT | filters.Caption, forward_message))
    # Avvia il bot
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
