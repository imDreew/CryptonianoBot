// views/index.js
import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';
import cron from 'node-cron';
import { startDiscordBot } from './discord.js';
import http from 'node:http';

const prisma = new PrismaClient();
const {
  TELEGRAM_BOT_TOKEN,
  TELEGRAM_GROUP_ID,    // gruppo/supergruppo dove applicare kick/unban
  TZ = 'Europe/Rome'
} = process.env;

if (!TELEGRAM_BOT_TOKEN) { console.error('Missing TELEGRAM_BOT_TOKEN'); process.exit(1); }

const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

// ===== VALIDAZIONI =====
const isPhone = (v) => /^\+[1-9]\d{7,14}$/.test((v||'').trim());             // E.164
const isTelegram = (v) => /^@[a-zA-Z0-9_]{5,32}$/.test((v||'').trim());
const isBitget = (v) => /^\d{10}$/.test((v||'').trim());
const isEmail = (v) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((v||'').trim());
const genCode = () => {
  const alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
  let out = ''; for (let i = 0; i < 8; i++) out += alphabet[Math.floor(Math.random()*alphabet.length)];
  return out;
};

// ===== WIZARD STEPS =====
const STEPS = ['phone', 'telegramNick', 'discordNick', 'bitgetUid', 'email', 'plan']; // plan con bottoni
const PROMPT = {
  phone:        '↘️ Inserisci il tuo **numero di telefono** con prefisso (es. `+39...`).',
  telegramNick: '↘️ Inserisci il tuo **nickname Telegram** iniziando con `@`.',
  discordNick:  '↘️ Inserisci il tuo **nickname Discord**.',
  bitgetUid:    '↘️ Inserisci il tuo **UID Bitget** (10 cifre).',
  email:        '↘️ Inserisci la tua **email**:',
  plan:         '↘️ Seleziona il **tipo di abbonamento**:'
};
const sessions = new Map(); // chatId -> { step, data }

function startFlow(chatId, user) {
  const name = user?.first_name || user?.username || 'amico';
  sessions.set(chatId, { step: 0, data: {} });
  bot.sendMessage(
    chatId,
    `Ciao ${name}! 👋\nProcedi a compilare le informazioni richieste per la registrazione al server *CRYPTONIANO VIP CLUB* 👑`,
    { parse_mode: 'Markdown' }
  ).then(() => bot.sendMessage(chatId, PROMPT.phone, { parse_mode: 'Markdown' }));
}

bot.onText(/^\/start$/, (msg) => startFlow(msg.chat.id));
bot.onText(/^\/restart$/, (msg) => { sessions.delete(msg.chat.id); startFlow(msg.chat.id); });

// STATUS rapido
bot.onText(/^\/status$/, async (msg) => {
  const tgId = String(msg.from.id);
  const s = await prisma.subscriber.findFirst({ where: { telegramUserId: tgId } });
  if (!s) return bot.sendMessage(msg.chat.id, 'Non risulti registrato. Usa /start');
  const scad = s.expiresAt ? new Date(s.expiresAt).toISOString().slice(0,10) : '-';
  bot.sendMessage(msg.chat.id, `📄 *Stato*\nEmail: ${s.email}\nPiano: ${s.plan ?? '-'}\nScade: ${scad}\nStato: ${s.status}\nCodice: \`${s.verifyCode}\``, { parse_mode: 'Markdown' });
});

// Gestione messaggi step-by-step
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  if (text.startsWith('/')) return;

  const s = sessions.get(chatId);
  if (!s) return;

  const key = STEPS[s.step];

  // validazioni
  if (key === 'phone' && !isPhone(text)) {
    return bot.sendMessage(chatId, '⚠️ Numero non valido.\n- Inizia con `+` (es. +39)\n- 8–15 cifre totali\nEsempio: `+393401234567`', { parse_mode: 'Markdown' });
  }
  if (key === 'telegramNick' && !isTelegram(text)) {
    return bot.sendMessage(chatId, '⚠️ Nick Telegram non valido.\n- Deve iniziare con `@`\n- 5–32 caratteri (lettere/numeri/underscore)\nEsempio: `@CryptoNiano`', { parse_mode: 'Markdown' });
  }
  if (key === 'bitgetUid' && !isBitget(text)) {
    return bot.sendMessage(chatId, '⚠️ UID Bitget non valido.\n- Deve avere **10 cifre** (es. `1234567890`)', { parse_mode: 'Markdown' });
  }
  if (key === 'email' && !isEmail(text)) {
    return bot.sendMessage(chatId, '⚠️ Email non valida. Esempio: `nome@dominio.com`', { parse_mode: 'Markdown' });
  }

  // salva e avanza
  if (key !== 'plan') {
    s.data[key] = text;
    s.step++;
  }

  // Se tocca scegliere il piano → inline keyboard
  if (STEPS[s.step] === 'plan') {
    const kb = { reply_markup: { inline_keyboard: [[
      { text: 'Mensile',  callback_data: 'PLAN:MONTHLY' },
      { text: 'Annuale',  callback_data: 'PLAN:ANNUAL'  }
    ]]}};
    return bot.sendMessage(chatId, PROMPT.plan, { ...kb, parse_mode: 'Markdown' });
  }

  // Altrimenti prompt successivo
  const nextKey = STEPS[s.step];
  bot.sendMessage(chatId, PROMPT[nextKey], { parse_mode: 'Markdown' });
});

// Piano selezionato
bot.on('callback_query', async (q) => {
  const chatId = q.message.chat.id;
  const s = sessions.get(chatId);
  if (!s) return bot.answerCallbackQuery(q.id, { text: 'Sessione scaduta. /start' });

  if (!q.data?.startsWith('PLAN:')) return;
  const plan = q.data.split(':')[1]; // MONTHLY | ANNUAL

  // calcola date
  const start = new Date();
  const expires = new Date(start);
  if (plan === 'MONTHLY') expires.setMonth(expires.getMonth() + 1);
  if (plan === 'ANNUAL')  expires.setFullYear(expires.getFullYear() + 1);

  // genera codice unico
  let verifyCode = genCode();
  for (let i = 0; i < 5; i++) {
    try {
      const payload = {
        phone: s.data.phone,
        telegramNick: s.data.telegramNick,
        discordNick: s.data.discordNick,
        bitgetUid: s.data.bitgetUid,
        email: s.data.email,
        verifyCode,
        plan,
        startDate: start,
        expiresAt: expires,
        status: 'ACTIVE',
        telegramUserId: String(q.from.id)
      };
      await prisma.subscriber.upsert({
        where: { email: s.data.email },
        update: payload,
        create: payload
      });
      break;
    } catch (e) {
      if (String(e).includes('verifyCode')) { verifyCode = genCode(); continue; }
      console.error('save error', e); throw e;
    }
  }

  await bot.answerCallbackQuery(q.id, { text: `Piano: ${plan === 'MONTHLY' ? 'Mensile' : 'Annuale'}` });
  await bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: q.message.message_id });

  await bot.sendMessage(chatId,
    `✅ Registrazione completata!\n\n🔐 *Codice conferma*: \`${verifyCode}\`\n- Piano: ${plan}\n- Scadenza: ${expires.toISOString().slice(0,10)}`,
    { parse_mode: 'Markdown' }
  );
  sessions.delete(chatId);
});

// ===== Telegram helpers: kick/unban + invite link =====
async function tgKick(userId) {
  if (!TELEGRAM_GROUP_ID || !userId) return false;
  try { await bot.banChatMember(TELEGRAM_GROUP_ID, Number(userId)); return true; }
  catch { return false; }
}
async function tgUnban(userId) {
  if (!TELEGRAM_GROUP_ID || !userId) return false;
  try { await bot.unbanChatMember(TELEGRAM_GROUP_ID, Number(userId), { only_if_banned: true }); return true; }
  catch { return false; }
}
async function tgInviteLink() {
  if (!TELEGRAM_GROUP_ID) return null;
  try {
    // crea link di invito (richiede bot admin)
    const link = await bot.createChatInviteLink(TELEGRAM_GROUP_ID, { member_limit: 1 });
    return link?.invite_link || null;
  } catch { return null; }
}

// ===== Avvio Discord + cron =====
const discord = await startDiscordBot(prisma, process.env);

// ogni giorno alle 12:00 Europe/Rome
cron.schedule('0 12 * * *', async () => {
  const now = new Date();
  const subs = await prisma.subscriber.findMany();
  for (const s of subs) {
    if (!s.plan || !s.expiresAt) continue;
    const expired = now > new Date(s.expiresAt);

    if (expired && s.status !== 'FROZEN') {
      // Discord: FROZEN
      await discord.freeze?.(s.discordUserId);
      // Telegram: kick
      await tgKick(s.telegramUserId);
      await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'FROZEN' } });

      // Avvisa in DM (se vuoi): il bot non può DM se utente non ha mai scritto al bot in privato
      try { await bot.sendMessage(Number(s.telegramUserId), '❌ Abbonamento scaduto: accesso revocato. Rinnova per riottenere l’accesso.'); } catch {}
    }

    if (!expired && s.status === 'FROZEN') {
      // Discord: ACTIVE
      await discord.unfreeze?.(s.discordUserId);
      // Telegram: unban + link invito (i bot NON possono “ri-aggiungere” utenti direttamente)
      await tgUnban(s.telegramUserId);
      const link = await tgInviteLink();
      if (link) {
        try { await bot.sendMessage(Number(s.telegramUserId), `✅ Abbonamento riattivato.\nEntra di nuovo nel gruppo: ${link}`); } catch {}
      }
      await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'ACTIVE' } });
    }
  }
}, { timezone: TZ });

// mini health server
http.createServer((_, res) => { res.writeHead(200); res.end('OK'); })
  .listen(process.env.PORT || 3000, () => console.log('Health server on /'));
