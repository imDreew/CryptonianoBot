// views/discord.js
import { Client, GatewayIntentBits, Partials } from 'discord.js';

/*
  Richiede le seguenti env:
  - DISCORD_BOT_TOKEN
  - DISCORD_GUILD_ID
  - DISCORD_FROZEN_ROLE_ID

  Il bot deve avere privilegi per leggere membri e gestire ruoli sul server.
  Attiva gli intents:
  - GUILD_MEMBERS per cercare membri
  - GUILDS per accedere alla guild
*/

export async function startDiscordBot(prisma, env) {
  const token = env.DISCORD_BOT_TOKEN;
  const guildId = env.DISCORD_GUILD_ID;
  const frozenRoleId = env.DISCORD_FROZEN_ROLE_ID;

  if (!token) {
    console.log('⚠️ DISCORD_BOT_TOKEN non impostato: avvio bot Discord saltato.');
    return {
      async createInviteAndSave() { return null; },
      async freezeByDiscordNick() {},
      async unfreezeByDiscordNick() {}
    };
  }

  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMembers // necessario per fetch/search membri
    ],
    partials: [Partials.GuildMember, Partials.User]
  });

  client.on('ready', () => {
    console.log(`Discord bot online come ${client.user.tag}`);
  });

  await client.login(token);

  async function getGuild() {
    if (!guildId) throw new Error('DISCORD_GUILD_ID mancante');
    const g = await client.guilds.fetch(guildId);
    if (!g) throw new Error('Guild non trovata');
    return g;
  }

  async function findMemberByNick(nick) {
    // Prova ricerca "nickname/displayName/username" (richiede GUILD_MEMBERS)
    const guild = await getGuild();
    const q = String(nick || '').trim();
    // fetch con query (limite 5) — non sempre perfetto, ma spesso sufficiente
    const fetched = await guild.members.fetch({ query: q, limit: 5 }).catch(() => null);
    if (fetched && fetched.size > 0) {
      // prova match case-insensitive
      const lower = q.toLowerCase();
      const exact = fetched.find(m =>
        (m.nickname && m.nickname.toLowerCase() === lower) ||
        (m.user?.username && m.user.username.toLowerCase() === lower) ||
        (m.displayName && m.displayName.toLowerCase() === lower)
      );
      return exact || fetched.first();
    }

    // fallback: fetch tutti i membri (attenzione: server grandi → pesante)
    // Evita per server > 75k membri; qui tentiamo con cache/partial
    const cacheHit = guild.members.cache.find(m => {
      const lower = q.toLowerCase();
      return (m.nickname && m.nickname.toLowerCase() === lower) ||
             (m.user?.username && m.user.username.toLowerCase() === lower) ||
             (m.displayName && m.displayName.toLowerCase() === lower);
    });
    return cacheHit || null;
  }

  async function freezeByDiscordNick(discordNick) {
    if (!frozenRoleId) throw new Error('DISCORD_FROZEN_ROLE_ID mancante');
    const guild = await getGuild();
    const member = await findMemberByNick(discordNick);
    if (!member) throw new Error(`Membro non trovato per nick: ${discordNick}`);
    // assegna ruolo
    await member.roles.add(frozenRoleId, 'Abbonamento scaduto');
  }

  async function unfreezeByDiscordNick(discordNick) {
    if (!frozenRoleId) throw new Error('DISCORD_FROZEN_ROLE_ID mancante');
    const guild = await getGuild();
    const member = await findMemberByNick(discordNick);
    if (!member) throw new Error(`Membro non trovato per nick: ${discordNick}`);
    await member.roles.remove(frozenRoleId, 'Abbonamento riattivato');
  }

  // opzionale: inviti monouso (non richiesto per la tua domanda, lasciato per compatibilità)
  async function createInviteAndSave(userId) {
    if (!guildId) return null;
    try {
      const guild = await getGuild();
      const channels = await guild.channels.fetch();
      const channel = channels.find(c => c?.isTextBased?.());
      if (!channel) return null;
      const invite = await channel.createInvite({ maxAge: 3600, maxUses: 1, unique: true, reason: `Invite for user ${userId}` });
      const saved = await prisma.invite.create({
        data: { userId, code: invite.code, url: `https://discord.gg/${invite.code}` }
      });
      return saved.url;
    } catch (e) {
      console.error('createInvite error', e);
      return null;
    }
  }

  return { createInviteAndSave, freezeByDiscordNick, unfreezeByDiscordNick };
}


