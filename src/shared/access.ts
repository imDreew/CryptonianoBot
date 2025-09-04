import { freezeOnTelegram, alert } from './telegram.js';
import { freezeOnDiscord } from './discord.js';


export async function freezeAccess(subscriber: any) {
await Promise.all([
freezeOnTelegram(subscriber.telegram_user_id ?? undefined),
freezeOnDiscord(subscriber.discord_user_id ?? undefined)
]);
await alert(`⚠️ Accesso congelato per <b>${subscriber.email}</b>`);
}