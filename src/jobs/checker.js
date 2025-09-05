import cron from 'node-cron';
import { notifyAdmin, telegramRestrict, telegramUnrestrict } from '../bots/telegram.js';
import { discordFreeze, discordUnfreeze } from '../bots/discord.js';


export function scheduleChecks(prisma) {
// ogni 30 minuti
cron.schedule('*/30 * * * *', async () => {
const now = new Date();
const subs = await prisma.subscriber.findMany();


for (const s of subs) {
// calcola expiresAt se plan presente ma expires mancante
if (s.plan && !s.expiresAt) {
const start = s.startDate || s.createdAt;
const expires = new Date(start);
if (s.plan === 'MONTHLY') expires.setMonth(expires.getMonth() + 1);
if (s.plan === 'ANNUAL') expires.setFullYear(expires.getFullYear() + 1);
await prisma.subscriber.update({ where: { id: s.id }, data: { startDate: start, expiresAt: expires } });
s.expiresAt = expires;
}


if (!s.expiresAt || !s.plan) continue; // non ancora attivato


const expired = now > new Date(s.expiresAt);


if (expired && s.status !== 'FROZEN') {
// F R E E Z E
const actions = [];
if (s.discordUserId) actions.push(discordFreeze(s.discordUserId)); else actions.push(Promise.resolve(false));
if (s.telegramUserId) actions.push(telegramRestrict(s.telegramUserId)); else actions.push(Promise.resolve(false));
const [dOk, tOk] = await Promise.all(actions);
await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'FROZEN' } });
await notifyAdmin(`⛔️ Freeze ${s.email} (D:${dOk?'ok':'no'} T:${tOk?'ok':'no'})`);
}


if (!expired && s.status === 'FROZEN') {
// U N F R E E Z E
const actions = [];
if (s.discordUserId) actions.push(discordUnfreeze(s.discordUserId)); else actions.push(Promise.resolve(false));
if (s.telegramUserId) actions.push(telegramUnrestrict(s.telegramUserId)); else actions.push(Promise.resolve(false));
const [dOk, tOk] = await Promise.all(actions);
await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'ACTIVE' } });
await notifyAdmin(`✅ Unfreeze ${s.email} (D:${dOk?'ok':'no'} T:${tOk?'ok':'no'})`);
}


// Avvisi per dati mancanti
if (s.plan && s.status !== 'FROZEN' && (!s.discordUserId || !s.telegramUserId)) {
await notifyAdmin(`⚠️ ${s.email} ha piano attivo ma manca link: Discord=${!!s.discordUserId} Telegram=${!!s.telegramUserId}`);
}
}
});
}