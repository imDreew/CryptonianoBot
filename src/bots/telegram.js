import TelegramBot from 'node-telegram-bot-api';


helpers.notifyAdmin = async (text) => {
if (ADMIN_CHAT_ID) {
try { await bot.sendMessage(ADMIN_CHAT_ID, text); } catch {}
}
};


helpers.restrict = async (telegramUserId) => {
if (!GROUP_ID || !telegramUserId) return false;
try {
await bot.restrictChatMember(GROUP_ID, Number(telegramUserId), {
can_send_messages: false,
can_send_audios: false,
can_send_documents: false,
can_send_photos: false,
can_send_videos: false,
can_send_video_notes: false,
can_send_voice_notes: false,
can_send_polls: false,
can_add_web_page_previews: false,
can_change_info: false,
can_invite_users: false,
can_pin_messages: false,
});
return true;
} catch (e) {
await helpers.notifyAdmin(`❗️Errore restrict Telegram per user ${telegramUserId}: ${e.message || e}`);
return false;
}
};


helpers.unrestrict = async (telegramUserId) => {
if (!GROUP_ID || !telegramUserId) return false;
try {
// Permessi "default" (puoi adattarli)
await bot.restrictChatMember(GROUP_ID, Number(telegramUserId), {
can_send_messages: true,
can_send_audios: true,
can_send_documents: true,
can_send_photos: true,
can_send_videos: true,
can_send_video_notes: true,
can_send_voice_notes: true,
can_send_polls: true,
can_add_web_page_previews: true,
can_change_info: false,
can_invite_users: true,
can_pin_messages: false,
});
return true;
} catch (e) {
await helpers.notifyAdmin(`❗️Errore unrestrict Telegram per user ${telegramUserId}: ${e.message || e}`);
return false;
}
};


return helpers;
}


export function setTelegramHelpers(h) { helpers = { ...helpers, ...h }; }
export const notifyAdmin = async (text) => helpers.notifyAdmin(text);
export const telegramRestrict = async (id) => helpers.restrict(id);
export const telegramUnrestrict = async (id) => helpers.unrestrict(id);