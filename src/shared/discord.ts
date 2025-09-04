import { Client, GatewayIntentBits } from 'discord.js';


const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMembers] });
let ready = false;
client.once('ready', () => { ready = true; });
client.login(process.env.DISCORD_BOT_TOKEN);


export async function freezeOnDiscord(discordUserId?: string) {
if (!discordUserId) return;
if (!ready) await new Promise(r => client.once('ready', r));
const guild = await client.guilds.fetch(process.env.DISCORD_GUILD_ID!);
const member = await guild.members.fetch(discordUserId).catch(() => null);
if (!member) return;
const activeRole = guild.roles.cache.find(r => r.name === (process.env.DISCORD_ROLE_ACTIVE || 'Abbonato'));
const frozenRole = guild.roles.cache.find(r => r.name === (process.env.DISCORD_ROLE_FROZEN || 'Frozen'));
if (activeRole) await member.roles.remove(activeRole).catch(()=>{});
if (frozenRole) await member.roles.add(frozenRole).catch(()=>{});
}
