import os
from pyrogram import Client
import discord
import asyncio

# --- TELEGRAM ---
TELEGRAM_API_ID = int(os.getenv("TG_API_ID"))
TELEGRAM_API_HASH = os.getenv("TG_API_HASH"))
SESSION_NAME = "helper_session"

# --- DISCORD ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

# Discord client
discord_client = discord.Client(intents=discord.Intents.default())

# Pyrogram client
pyro = Client(SESSION_NAME, api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH)

async def download_and_send(chat_id: int, message_id: int):
    """Scarica un media da Telegram e lo invia su Discord."""
    async with pyro:
        msg = await pyro.get_messages(chat_id, message_id)
        file_path = await msg.download()
    
    channel = discord_client.get_channel(DISCORD_CHANNEL_ID)
    await channel.send(file=discord.File(file_path))
    os.remove(file_path)
    print(f"✅ Video inviato su Discord: {file_path}")

# Event Discord ready
@discord_client.event
async def on_ready():
    print(f"✅ Discord connesso come {discord_client.user}")

# Avvio Discord
discord_client.run(DISCORD_TOKEN)
