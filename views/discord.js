// views/discord.js
import { Client, GatewayIntentBits, Partials } from 'discord.js';

export async function startDiscordBot(prisma, env) {
  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMembers
    ],
    partials: [Partials.GuildMember]
  });

  const GUILD_ID = env.DISCORD_GUILD_ID;
  const TOKEN = env.DISCORD_BOT_TOKEN;
  const ROLE_FROZEN = env.DISCORD_FROZEN_ROLE_ID;
  const ROLE_ACTIVE = env.DISCORD_ACTIVE_ROLE_ID;

  if (!TOKEN || !GUILD_ID) {
    console.error('❌ Missing DISCORD_BOT_TOKEN or DISCORD_GUILD_ID');
    return {};
  }

  client.once('ready', () => {
    console.log(`🤖 Discord bot online come ${client.user.tag}`);
  });

  await client.login(TOKEN);

  async function createInviteAndSave(userId) {
    try {
      const guild = await client.guilds.fetch(GUILD_ID);
      const channel =
        guild.systemChannel ||
        guild.channels.cache.find(c => c.isTextBased()) ||
        (await guild.channels.fetch()).find(c => c.isTextBased());

      if (!channel) return null;

      const invite = await channel.createInvite({
        maxUses: 1,
        unique: true,
        maxAge: 60 * 60 * 24 // 24h
      });

      await prisma.invite.create({
        data: {
          userId,
          code: invite.code,
          url: `https://discord.gg/${invite.code}`
        }
      });

      return `https://discord.gg/${invite.code}`;
    } catch (err) {
      console.error('Errore createInvite:', err);
      return null;
    }
  }

  async function freeze(discordUserId) {
    if (!discordUserId) return;
    try {
      const guild = await client.guilds.fetch(GUILD_ID);
      const member = await guild.members.fetch(discordUserId);
      if (!member) return;

      if (ROLE_ACTIVE) await member.roles.remove(ROLE_ACTIVE).catch(() => {});
      if (ROLE_FROZEN) await member.roles.add(ROLE_FROZEN).catch(() => {});
      console.log(`❄️ Utente ${discordUserId} freezato`);
    } catch (err) {
      console.error('Errore freeze:', err);
    }
  }

  async function unfreeze(discordUserId) {
    if (!discordUserId) return;
    try {
      const guild = await client.guilds.fetch(GUILD_ID);
      const member = await guild.members.fetch(discordUserId);
      if (!member) return;

      if (ROLE_FROZEN) await member.roles.remove(ROLE_FROZEN).catch(() => {});
      if (ROLE_ACTIVE) await member.roles.add(ROLE_ACTIVE).catch(() => {});
      console.log(`🔥 Utente ${discordUserId} riattivato`);
    } catch (err) {
      console.error('Errore unfreeze:', err);
    }
  }

  return { createInviteAndSave, freeze, unfreeze };
}
