// views/discord.js
import { Client, GatewayIntentBits, PermissionFlagsBits } from 'discord.js';

export async function startDiscordBot(prisma, env) {
  const {
    DISCORD_BOT_TOKEN,
    DISCORD_GUILD_ID,
    DISCORD_FROZEN_ROLE_ID,
    DISCORD_ACTIVE_ROLE_ID,
    DISCORD_INVITE_CHANNEL_ID, // canale su cui creare inviti unici
  } = env;

  if (!DISCORD_BOT_TOKEN || !DISCORD_GUILD_ID) {
    console.log('⚠️ Discord bot disabilitato: manca DISCORD_BOT_TOKEN o DISCORD_GUILD_ID');
    return { freeze: async () => false, unfreeze: async () => false, createInvite: async () => null };
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
  client.once('clientReady', () => console.log(`🤖 Discord bot (clientReady) ${client.user.tag}`));

  // !link <CODICE> → collega Discord all'abbonamento
  client.on('messageCreate', async (message) => {
    if (message.author.bot) return;
    if (!message.guild || message.guild.id !== DISCORD_GUILD_ID) return;
    const m = message.content.trim();
    const match = m.match(/^!link\s+([A-Z2-9]{8}|\d{6})$/i);
    if (!match) return;

    const code = match[1].toUpperCase();
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

  async function freeze(discordUserId) {
    if (!discordUserId || !DISCORD_FROZEN_ROLE_ID) return false;
    try {
      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const member = await guild.members.fetch(discordUserId).catch(() => null);
      if (!member) return false;
      await member.roles.add(DISCORD_FROZEN_ROLE_ID).catch(() => {});
      if (DISCORD_ACTIVE_ROLE_ID) await member.roles.remove(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
      return true;
    } catch (e) { console.warn('freeze discord error', e.message || e); return false; }
  }

  async function unfreeze(discordUserId) {
    try {
      if (!discordUserId) return false;
      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const member = await guild.members.fetch(discordUserId).catch(() => null);
      if (!member) return false;
      if (process.env.DISCORD_FROZEN_ROLE_ID) await member.roles.remove(DISCORD_FROZEN_ROLE_ID).catch(() => {});
      if (process.env.DISCORD_ACTIVE_ROLE_ID) await member.roles.add(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
      return true;
    } catch (e) { console.warn('unfreeze discord error', e.message || e); return false; }
  }

  async function createInvite() {
    try {
      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const channelId = DISCORD_INVITE_CHANNEL_ID || (await guild.channels.fetch()).find(c => c?.isTextBased?.())?.id;
      if (!channelId) return null;
      const channel = await guild.channels.fetch(channelId);
      if (!channel) return null;

      // invito usa e getta (1 uso), senza scadenza temporale (puoi mettere maxAge)
      const invite = await channel.createInvite({
        maxUses: 1,
        unique: true,
        reason: 'Registrazione CRYPTONIANO VIP CLUB',
      }, 'create invite');
      return invite.url;
    } catch (e) {
      console.warn('createInvite error', e.message || e);
      return null;
    }
  }

  return { freeze, unfreeze, createInvite };
}


  return { freeze, unfreeze, createInvite };
}

