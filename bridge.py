import os
import logging
import asyncio
import discord
from discord.ext import commands
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from pyrogram import Client as PyroClient

# =========================
# CONFIG
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
PYRO_SESSION = "helper_session"   # sarà salvato in locale (su Railway puoi montare volume persistente)

# =========================
# LOGGER
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =========================
# DISCORD CLIENT
# =========================
discord_intents = discord.Intents.default()
discord_client = commands.Bot(command_prefix="!", intents=discord_intents)

@discord_client.event
async def on_ready():
    logger.info(f"✅ Discord connesso come {discord_client.user}")

# =========================
# PYROGRAM CLIENT
# =========================
pyro = PyroClient(PYRO_SESSION, api_id=TG_API_ID, api_hash=TG_API_HASH)

async def send_big_file_to_discord(chat_id: int, message_id: int):
    """Scarica un file grande da Telegram con Pyrogram e lo invia su Discord"""
    async with pyro:
        msg = await pyro.get_messages(chat_id, message_id)
        file_path = await msg.download()
    
    channel = discord_client.get_channel(DISCORD_CHANNEL_ID)
    await channel.send(file=discord.File(file_path))
    os.remove(file_path)
    logger.info(f"✅ File grande inviato su Discord: {file_path}")

# =========================
# TELEGRAM HANDLER
# =========================
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inoltra messaggi e media da Telegram a Discord"""
    if not update.message:
        return
    
    try:
        # --- Caso TESTO ---
        if update.message.text:
            channel = discord_client.get_channel(DISCORD_CHANNEL_ID)
            await channel.send(update.message.text)
            logger.info(f"✅ Testo inoltrato: {update.message.text[:30]}...")

        # --- Caso FOTO / VIDEO / FILE ---
        elif update.message.photo or update.message.video or update.message.document:
            file_id = None
            if update.message.photo:
                file_id = update.message.photo[-1].file_id
            elif update.message.video:
                file_id = update.message.video.file_id
            elif update.message.document:
                file_id = update.message.document.file_id

            try:
                # Scarico con Bot API (limite 20MB)
                file = await context.bot.get_file(file_id)
                file_path = await file.download_to_drive()

                channel = discord_client.get_channel(DISCORD_CHANNEL_ID)
                await channel.send(file=discord.File(file_path))
                os.remove(file_path)
                logger.info(f"✅ Media inoltrato (Bot API): {file_id}")

            except Exception as e:
                # Se file troppo grande, fallback Pyrogram
                if "File is too big" in str(e):
                    logger.warning(f"⚠️ File troppo grande, passo a Pyrogram: {file_id}")
                    await send_big_file_to_discord(update.message.chat_id, update.message.message_id)
                else:
                    logger.error(f"❌ Errore inoltro media: {e}")

    except Exception as e:
        logger.error(f"❌ Errore generico handler: {e}")

# =========================
# AVVIO BRIDGE
# =========================
async def main():
    # Telegram
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, forward_message))

    # Avvio entrambi in parallelo
    await asyncio.gather(
        app.run_polling(poll_interval=5),
        discord_client.start(DISCORD_TOKEN)
    )

if __name__ == "__main__":
    asyncio.run(main())
