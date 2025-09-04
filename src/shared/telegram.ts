import TelegramBot from 'node-telegram-bot-api';


export const telegramBot = new TelegramBot(process.env.TELEGRAM_BOT_TOKEN!, { polling: false });


export async function alert(msg: string) {
const chatId = process.env.TELEGRAM_ALERT_CHAT_ID!;
try { await telegramBot.sendMessage(chatId, msg, { parse_mode: 'HTML' }); } catch {}
}


export async function freezeOnTelegram(telegramUserId?: number) {
const groupId = process.env.TELEGRAM_PRIVATE_GROUP_ID!;
if (!telegramUserId) return;
try {
await telegramBot.restrictChatMember(groupId, telegramUserId, {
can_send_messages: false,
can_send_media_messages: false,
can_send_other_messages: false,
can_add_web_page_previews: false
});
} catch {}
}