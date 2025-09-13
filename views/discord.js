// views/discord.js
import { Client, GatewayIntentBits, Partials } from 'discord.js';

export async function startDiscordBot(prisma, env) {
  const token = env.DISCORD_BOT_TOKEN;
  if (!token) {
    console.log('⚠️ DISCORD_BOT_TOKEN non impostato: avvio bot Discord saltato.');
    return {
      async createInviteAndSave() { return null; },
      async freeze() {},
      async unfreeze() {}
    };
  }

  const client = new Client({
    intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMembers],
    partials: [Partials.GuildMember]
  });

  client.on('clientReady', () => {
    console.log(`🤖 Discord bot online come ${client.user?.tag || 'Unknown'}`);
  });

  client.login(token).catch(err => {
    console.error('Errore login Discord:', err);
  });

  // Stubs (puoi integrarli con gestione ruoli se vuoi)
  async function createInviteAndSave(userId) {
    try {
      const guildId = env.DISCORD_GUILD_ID;
      const guild = await client.guilds.fetch(guildId);
      const channels = await guild.channels.fetch();
      const anyText = channels.find(ch => ch && ch.isTextBased && ch.createInvite);

      if (!anyText?.createInvite) return null;
      const invite = await anyText.createInvite({ maxAge: 86400, maxUses: 1, unique: true });
      const saved = await prisma.invite.create({
        data: { userId, code: invite.code, url: `https://discord.gg/${invite.code}` }
      });
      return saved.url;
    } catch (e) {
      console.error('createInvite error', e);
      return null;
    }
  }

  async function freeze(discordUserId) { /* opzionale: assegna ruolo frozen */ }
  async function unfreeze(discordUserId) { /* opzionale: rimuovi ruolo frozen */ }

  return { createInviteAndSave, freeze, unfreeze };
}

