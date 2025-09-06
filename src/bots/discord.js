// src/bots/discord.js
import { Client, GatewayIntentBits } from 'discord.js';

export async function startDiscordBot(prisma, env) {
  const {
    DISCORD_BOT_TOKEN,
    DISCORD_GUILD_ID,
    DISCORD_FROZEN_ROLE_ID,
    DISCORD_ACTIVE_ROLE_ID
  } = env;

  if (!DISCORD_BOT_TOKEN || !DISCORD_GUILD_ID) {
    console.log('⚠️ Discord bot disabilitato (manca DISCORD_BOT_TOKEN o DISCORD_GUILD_ID)');
    return { freeze: async () => false, unfreeze: async () => false };
  }

  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMembers,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
    ]
  });

  client.once('ready', () => console.log(`🤖 Discord bot online come ${client.user.tag}`));

  // Comando !link CODICE
  client.on('messageCreate', async (message) => {
    if (message.author.bot) return;
    if (!message.guild || message.guild.id !== DISCORD_GUILD_ID) return;
    const m = message.content.trim();
    const match = m.match(/^!link\s+(\d{6})$/i);
    if (!match) return;

    const code = match[1];
    try {
      const sub = await prisma.subscriber.findUnique({ where: { verifyCode: code } });
      if (!sub) return void message.reply('❌ Codice non valido.');

      await prisma.subscriber.update({
        where: { id: sub.id },
        data: { discordUserId: message.author.id }
      });

      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const member = await guild.members.fetch(message.author.id).catch(() => null);
      if (member) {
        if (DISCORD_FROZEN_ROLE_ID) await member.roles.remove(DISCORD_FROZEN_ROLE_ID).catch(() => {});
        if (DISCORD_ACTIVE_ROLE_ID) await member.roles.add(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
      }

      await message.reply('✅ Discord collegato al tuo abbonamento!');
    } catch (e) {
      console.error('discord !link error', e);
      await message.reply('Errore interno, riprova più tardi.');
    }
  });

  await client.login(DISCORD_BOT_TOKEN);

  // Helpers freeze/unfreeze
  async function freeze(discordUserId) {
    if (!discordUserId || !DISCORD_FROZEN_ROLE_ID) return false;
    try {
      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const member = await guild.members.fetch(discordUserId);
      await member.roles.add(DISCORD_FROZEN_ROLE_ID).catch(() => {});
      if (DISCORD_ACTIVE_ROLE_ID) await member.roles.remove(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
      return true;
    } catch (e) {
      console.warn('freeze discord error', e.message || e);
      return false;
    }
  }

  async function unfreeze(discordUserId) {
    if (!discordUserId || !DISCORD_FROZEN_ROLE_ID) return false;
    try {
      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const member = await guild.members.fetch(discordUserId);
      await member.roles.remove(DISCORD_FROZEN_ROLE_ID).catch(() => {});
      if (DISCORD_ACTIVE_ROLE_ID) await member.roles.add(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
      return true;
    } catch (e) {
      console.warn('unfreeze discord error', e.message || e);
      return false;
    }
  }

  return { freeze, unfreeze };
}
