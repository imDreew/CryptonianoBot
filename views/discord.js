// views/discord.js
import { Client, GatewayIntentBits } from 'discord.js';

export async function startDiscordBot(prisma, env) {
  const {
    DISCORD_BOT_TOKEN,
    DISCORD_GUILD_ID,
    DISCORD_FROZEN_ROLE_ID,
    DISCORD_ACTIVE_ROLE_ID,
    DISCORD_YOUNGTRADER_ROLE_ID,      // ruolo da assegnare all’ingresso
    DISCORD_INVITE_CHANNEL_ID         // canale dove creare gli inviti
  } = env;

  if (!DISCORD_BOT_TOKEN || !DISCORD_GUILD_ID) {
    console.log('⚠️ Discord bot non configurato (token/server ID mancante)');
    return {
      freeze: async () => false,
      unfreeze: async () => false,
      createInvite: async () => null
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

  // cache usi degli inviti per capire quale è stato usato
  const inviteUses = new Map(); // code -> uses

  client.once('ready', async () => {
    console.log(`🤖 Discord bot online come ${client.user.tag}`);
    const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
    const invites = await guild.invites.fetch().catch(() => null);
    if (invites) invites.forEach(inv => inviteUses.set(inv.code, inv.uses ?? 0));
  });

  // Assegna ruolo YoungTrader quando entra qualcuno con un nostro invito univoco
  client.on('guildMemberAdd', async (member) => {
    if (member.guild.id !== DISCORD_GUILD_ID) return;

    // Rileva quale invito ha incrementato gli "uses"
    const invites = await member.guild.invites.fetch().catch(() => null);
    if (!invites) return;

    let usedCode = null;
    invites.forEach(inv => {
      const prev = inviteUses.get(inv.code) ?? 0;
      if ((inv.uses ?? 0) > prev) usedCode = inv.code;
      inviteUses.set(inv.code, inv.uses ?? 0);
    });

    try {
      // Se abbiamo salvato l’invito su Subscriber, lo usiamo per trovare l’utente
      if (usedCode) {
        const sub = await prisma.subscriber.findFirst({
          where: { discordInviteCode: usedCode }
        });
        if (sub) {
          // collega l’ID Discord se non presente
          if (!sub.discordUserId) {
            await prisma.subscriber.update({
              where: { id: sub.id },
              data: { discordUserId: member.id, status: 'ACTIVE' }
            });
          }
          // ruoli: togli FROZEN, metti ACTIVE + YoungTrader
          if (DISCORD_FROZEN_ROLE_ID) await member.roles.remove(DISCORD_FROZEN_ROLE_ID).catch(() => {});
          if (DISCORD_ACTIVE_ROLE_ID) await member.roles.add(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
          if (DISCORD_YOUNGTRADER_ROLE_ID) await member.roles.add(DISCORD_YOUNGTRADER_ROLE_ID).catch(() => {});
        }
      }
    } catch (e) {
      console.warn('guildMemberAdd handling error:', e.message || e);
    }
  });

  // Comando manuale fallback: !link <verifyCode>
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
        data: { discordUserId: message.author.id, status: 'ACTIVE' }
      });

      const member = await message.guild.members.fetch(message.author.id).catch(() => null);
      if (member) {
        if (DISCORD_FROZEN_ROLE_ID) await member.roles.remove(DISCORD_FROZEN_ROLE_ID).catch(() => {});
        if (DISCORD_ACTIVE_ROLE_ID) await member.roles.add(DISCORD_ACTIVE_ROLE_ID).catch(() => {});
        if (DISCORD_YOUNGTRADER_ROLE_ID) await member.roles.add(DISCORD_YOUNGTRADER_ROLE_ID).catch(() => {});
      }
      await message.reply('✅ Collegato e ruoli assegnati!');
    } catch (e) {
      console.error('!link error', e);
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
    } catch (e) { console.warn('freeze error', e.message || e); return false; }
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
    } catch (e) { console.warn('unfreeze error', e.message || e); return false; }
  }

  // Crea invito univoco (1 uso) e salva il code nei Subscriber
  async function createInviteAndSave(subscriberId) {
    try {
      const guild = await client.guilds.fetch(DISCORD_GUILD_ID);
      const channel = DISCORD_INVITE_CHANNEL_ID
        ? await guild.channels.fetch(DISCORD_INVITE_CHANNEL_ID)
        : (await guild.channels.fetch()).find(c => c?.isTextBased?.());
      if (!channel) return null;

      const invite = await channel.createInvite({ maxUses: 1, unique: true });
      // aggiorna cache
      inviteUses.set(invite.code, invite.uses ?? 0);

      await prisma.subscriber.update({
        where: { id: subscriberId },
        data: { discordInviteCode: invite.code }
      });

      return invite.url;
    } catch (e) { console.warn('createInvite error', e.message || e); return null; }
  }

  return { freeze, unfreeze, createInviteAndSave };
}

