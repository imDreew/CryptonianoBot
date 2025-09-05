import { Client, GatewayIntentBits } from 'discord.js';


client = new Client({ intents: [
GatewayIntentBits.Guilds,
GatewayIntentBits.GuildMembers,
GatewayIntentBits.GuildMessages,
GatewayIntentBits.MessageContent,
]});


client.on('ready', () => console.log(`Discord bot loggato come ${client.user.tag}`));


client.on('messageCreate', async (message) => {
if (message.author.bot) return;
const m = message.content.trim();
const match = m.match(/^!link\s+(\d{6})$/);
if (!match) return;
const code = match[1];
const userId = message.author.id;


try {
const sub = await prisma.subscriber.findUnique({ where: { verifyCode: code } });
if (!sub) return void message.reply('Codice non valido.');
await prisma.subscriber.update({ where: { id: sub.id }, data: { discordUserId: String(userId) } });
await message.reply('✅ Discord collegato!');
} catch (e) {
await message.reply('Errore interno, riprova più tardi.');
}
});


await client.login(token);


helpers.freeze = async (discordUserId) => {
if (!FROZEN_ROLE_ID) return false;
try {
const guild = await client.guilds.fetch(GUILD_ID);
const member = await guild.members.fetch(discordUserId);
await member.roles.add(FROZEN_ROLE_ID).catch(() => {});
if (ACTIVE_ROLE_ID) await member.roles.remove(ACTIVE_ROLE_ID).catch(() => {});
return true;
} catch (e) {
console.warn('freeze discord error', e.message || e);
return false;
}
};


helpers.unfreeze = async (discordUserId) => {
if (!FROZEN_ROLE_ID) return false;
try {
const guild = await client.guilds.fetch(GUILD_ID);
const member = await guild.members.fetch(discordUserId);
await member.roles.remove(FROZEN_ROLE_ID).catch(() => {});
if (ACTIVE_ROLE_ID) await member.roles.add(ACTIVE_ROLE_ID).catch(() => {});
return true;
} catch (e) {
console.warn('unfreeze discord error', e.message || e);
return false;
}
};


return helpers;
}


export function setDiscordHelpers(h) { helpers = { ...helpers, ...h }; }
export const discordFreeze = async (id) => helpers.freeze(id);
export const discordUnfreeze = async (id) => helpers.unfreeze(id);