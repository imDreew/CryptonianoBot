// views/discord.js
import { Client, GatewayIntentBits } from 'discord.js';

export async function startDiscordBot(prisma, env) {
  const {
    DISCORD_BOT_TOKEN,
    DISCORD_GUILD_ID,
    DISCORD_FROZEN_ROLE_ID,
    DISCORD_ACTIVE_ROLE_ID,
    DISCORD_YOUNGTRADER_ROLE_ID,
    DISCORD_INVITE_CHANNEL_ID
  } = env;

  if (!DISCORD_BOT_TOKEN || !DISCORD_GUILD_ID) {
    console.log('⚠️ Discord bot non configurato.');
    return {
      freeze: async () => false,
      unfreeze: async () => false,
      createInviteAndSave: async () => null
    };
  }

  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMembers,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
      GatewayIntentBits.GuildInvites
    ]
  });

  const inviteUses = new Map();

  client.once('ready', async () => {
    console.log(`🤖 Discord bot online come ${client.user.tag}`);
    const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
    const invites = await guild.invites.fetch().catch(() => null);
    if (invites) invites.forEach(inv => inviteUses.set(inv.code, inv.uses ?? 0));
  });

  client.on('guildMemberAdd', async (member) => {
    if (member.guild.id !== DISCORD_GUILD_ID) return;

    const invites = await member.guild.invites.fetch().catch(() => null);
    if (!invites) return;
    let usedCode = null;
    invites.forEach(inv => {
      const prev = inviteUses.get(inv.code) ?? 0;
      if ((inv.uses ?? 0) > prev) usedCode = inv.code;
      inviteUses.set(inv.code, inv.uses ?? 0);
    });

    try {
      if (usedCode) {
        const sub = await prisma.subscriber.findFirst({
          where: { discordInviteCode: usedCode }
        });
        if (sub) {
          if (!sub.discordUserId) {
            await prisma.subscriber.update({
              where: { id: sub.id },
              data: { discordUserId: member.id, status: 'ACTIVE' }
            });
          }
          if (DISCORD_FROZEN_ROLE_ID) await member.roles.remove(DISCORD_FROZEN_ROLE_ID).catch(() => {});
          if (DISCORD_ACTIVE_ROLE_ID) await member.roles.add(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
          if (DISCORD_YOUNGTRADER_ROLE_ID) await member.roles.add(DISCORD_YOUNGTRADER_ROLE_ID).catch(() => {});
        }
      }
    } catch (e) {
      console.warn('guildMemberAdd error:', e.message || e);
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
    } catch (e) { return false; }
  }

  async function unfreeze(discordUserId) {
    if (!discordUserId) return false;
    try {
      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const member = await guild.members.fetch(discordUserId).catch(() => null);
      if (!member) return false;
      if (DISCORD_FROZEN_ROLE_ID) await member.roles.remove(DISCORD_FROZEN_ROLE_ID).catch(() => {});
      if (DISCORD_ACTIVE_ROLE_ID) await member.roles.add(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
      if (DISCORD_YOUNGTRADER_ROLE_ID) await member.roles.add(DISCORD_YOUNGTRADER_ROLE_ID).catch(() => {});
      return true;
    } catch (e) { return false; }
  }

  async function createInviteAndSave(subscriberId) {
    try {
      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const channel = DISCORD_INVITE_CHANNEL_ID
        ? await guild.channels.fetch(DISCORD_INVITE_CHANNEL_ID)
        : (await guild.channels.fetch()).find(c => c?.isTextBased?.());
      if (!channel) return null;

      const invite = await channel.createInvite({ maxUses: 1, unique: true });
      inviteUses.set(invite.code, invite.uses ?? 0);

      await prisma.subscriber.update({
        where: { id: subscriberId },
        data: { discordInviteCode: invite.code }
      });

      return invite.url;
    } catch (e) { return null; }
  }

  return { freeze, unfreeze, createInviteAndSave };
}

