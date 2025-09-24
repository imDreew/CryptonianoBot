// views/index.js
import 'dotenv/config';
import express from 'express';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';
import { parsePhoneNumberFromString } from 'libphonenumber-js';

const prisma = new PrismaClient();

/* ===========================
   TELEGRAM BOT (no hardcode)
=========================== */
const bot = new TelegramBot(process.env.TELEGRAM_BOT_TOKEN, {
  polling: { interval: 1000, autoStart: true }
});

/* ===========================
   UTILS: chat_id sicuro + HTML
=========================== */
function getChatId(update) {
  // message
  if (update?.chat?.id) return update.chat.id;
  if (update?.message?.chat?.id) return update.message.chat.id;
  // callback_query
  if (update?.message?.chat?.id) return update.message.chat.id;
  // my_chat_member / chat_member
  if (update?.my_chat_member?.chat?.id) return update.my_chat_member.chat.id;
  return null;
}

async function replyTo(update, text, opts = {}) {
  const chatId = getChatId(update);
  if (chatId == null) {
    console.warn('replyTo: chatId non trovato per update.');
    return null;
  }
  try {
    return await bot.sendMessage(chatId, text, { parse_mode: 'HTML', ...opts });
  } catch (err) {
    const desc = err?.response?.body?.description || err?.message || '';
    const code = err?.response?.statusCode || err?.code;
    console.error('sendMessage error', code, desc);
    return null;
  }
}

function escHTML(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function fmtDateTime(dt) {
  return new Date(dt).toLocaleString('it-IT', { hour12: false });
}

/* ===========================
   SESSIONE
=========================== */
const sessions = new Map(); // key = chatId, value = { step, data }

const STEPS = {
  START: 'START',
  TG_NICK: 'TG_NICK',
  DC_NICK: 'DC_NICK',
  BITGET: 'BITGET',
  EMAIL: 'EMAIL',
  PHONE: 'PHONE',
  PLAN: 'PLAN',
  PAYMENT: 'PAYMENT',
  END: 'END'
};

const PREV = {
  [STEPS.DC_NICK]: STEPS.TG_NICK,
  [STEPS.BITGET]:  STEPS.DC_NICK,
  [STEPS.EMAIL]:   STEPS.BITGET,
  [STEPS.PHONE]:   STEPS.EMAIL,
  [STEPS.PLAN]:    STEPS.PHONE,
  [STEPS.PAYMENT]: STEPS.PLAN
};

function getOrCreateSession(chatId) {
  if (!sessions.has(chatId)) sessions.set(chatId, { step: STEPS.START, data: {} });
  return sessions.get(chatId);
}

/* ===========================
   TASTI INLINE
=========================== */
const KB_BACK = { reply_markup: { inline_keyboard: [[{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]] } };

const KB_DC = {
  reply_markup: {
    inline_keyboard: [
      [{ text: '🆕 Non hai Discord?', callback_data: 'NO_DISCORD' }],
      [{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]
    ]
  }
};

const KB_BG = {
  reply_markup: {
    inline_keyboard: [
      [{ text: '🆕 Non ho Bitget', callback_data: 'NO_BITGET' }],
      [{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]
    ]
  }
};

const KB_PLAN = {
  reply_markup: {
    inline_keyboard: [
      [{ text: '📅 Mensile',     callback_data: 'PLAN:MONTHLY' }],
      [{ text: '📆 Trimestrale', callback_data: 'PLAN:QUARTERLY' }],
      [{ text: '📜 Annuale',     callback_data: 'PLAN:ANNUAL' }],
      [{ text: '⬅️ INDIETRO',    callback_data: 'BACK' }]
    ]
  }
};

const KB_PAYMENT = {
  reply_markup: {
    inline_keyboard: [
      [{ text: '🏦 Bonifico',         callback_data: 'PAY:BANK_TRANSFER' }],
      [{ text: '💸 PayPal',           callback_data: 'PAY:PAYPAL' }],
      [{ text: '🟡 USDT (BEP20)',     callback_data: 'PAY:USDT_BEP20' }],
      [{ text: '🟠 USDT (ERC20)',     callback_data: 'PAY:USDT_ERC20' }],
      [{ text: '🔵 USDT (TRC20)',     callback_data: 'PAY:USDT_TRC20' }],
      [{ text: '⬅️ INDIETRO',         callback_data: 'BACK' }]
    ]
  }
};

/* ===========================
   TESTI STEP
=========================== */
const STEPS_TEXT = {
  TG_NICK: '👋 <b>Benvenuto!</b>\n\n<b>1/7</b> — Inviami il tuo <b>nick Telegram</b> (con o senza @).',
  DC_NICK: '🎮 <b>2/7</b> — Inviami il tuo <b>nick Discord</b> (come appare su Discord).',
  BITGET:  '🏦 <b>3/7</b> — Inviami il tuo <b>UID Bitget</b> (solo cifre).',
  EMAIL:   '✉️ <b>4/7</b> — Inviami la tua <b>email</b>.',
  PHONE:   '📞 <b>5/7</b> — Inviami il tuo <b>numero di telefono con prefisso internazionale</b>.<br>Esempi: <code>+39 3331234567</code>, <code>+41 765432109</code>'
};

async function askStep(chatId, step) {
  const s = getOrCreateSession(chatId);
  s.step = step;

  if (step === STEPS.TG_NICK) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'HTML' });
  } else if (step === STEPS.DC_NICK) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'HTML', reply_markup: KB_DC.reply_markup });
  } else if (step === STEPS.BITGET) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'HTML', reply_markup: KB_BG.reply_markup });
  } else if (step === STEPS.EMAIL) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'HTML', reply_markup: KB_BACK.reply_markup });
  } else if (step === STEPS.PHONE) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'HTML', reply_markup: KB_BACK.reply_markup });
  } else {
    await bot.sendMessage(chatId, STEPS_TEXT[step] || 'Procedi.', { parse_mode: 'HTML', reply_markup: KB_BACK.reply_markup });
  }
}

/* ===========================
   NORMALIZZAZIONI & VALIDAZIONI
=========================== */
function normTelegramNick(s) { return String(s||'').trim().replace(/^@/, '').toLowerCase(); }
function normDiscordNick(s)  { return String(s||'').trim().toLowerCase(); }
function normEmail(s)        { return String(s||'').trim().toLowerCase(); }

function isValidTelegramNick(s) { return /^@?[A-Za-z0-9_]{5,32}$/.test(String(s||'').trim()); }
function isValidDiscordNick(s)  { return /^[A-Za-z0-9._-]{2,32}$/.test(String(s||'').trim()); }
function isValidBitgetUID(s)    { return /^\d{5,20}$/.test(String(s||'').trim()); }
function isValidEmailFmt(s)     { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(s||'').trim()); }

function parsePhoneWithPrefix(input) {
  const cleaned = String(input || '').replace(/[^\d+]/g, '');
  if (!cleaned.startsWith('+')) return null;
  const phone = parsePhoneNumberFromString(cleaned);
  if (!phone || !phone.isValid()) return null;
  return {
    country: `+${phone.countryCallingCode}`,
    number: phone.nationalNumber,
    e164: phone.number,
    iso2: phone.country
  };
}

function planHuman(plan) {
  return plan === 'MONTHLY' ? 'Mensile'
    : plan === 'QUARTERLY' ? 'Trimestrale'
    : plan === 'ANNUAL' ? 'Annuale'
    : plan;
}
function paymentHuman(pm) {
  return pm === 'BANK_TRANSFER' ? 'Bonifico'
    : pm === 'PAYPAL' ? 'PayPal'
    : pm === 'USDT_BEP20' ? 'USDT (BEP20)'
    : pm === 'USDT_ERC20' ? 'USDT (ERC20)'
    : pm === 'USDT_TRC20' ? 'USDT (TRC20)'
    : pm;
}
function planDurationDays(plan) {
  if (plan === 'MONTHLY') return 30;
  if (plan === 'QUARTERLY') return 90;
  if (plan === 'ANNUAL') return 365;
  return 30;
}

/* ===========================
   CHECK DUPLICATI (DB)
=========================== */
async function existsTelegramUserId(telegramUserId) { if (!telegramUserId) return false; return !!(await prisma.intake.findFirst({ where: { telegramUserId } })); }
async function existsTelegramNick(nickNorm)        { return !!(await prisma.intake.findFirst({ where: { telegramNickNorm: nickNorm } })); }
async function existsDiscordNick(nickNorm)         { return !!(await prisma.intake.findFirst({ where: { discordNickNorm: nickNorm } })); }
async function existsBitgetUID(uid)                { return !!(await prisma.intake.findFirst({ where: { bitgetUID: uid } })); }
async function existsEmail(emailNorm)              { return !!(await prisma.intake.findFirst({ where: { emailNorm } })); }
async function existsPhone(country, number)        { return !!(await prisma.intake.findFirst({ where: { phoneCountryCode: country, phoneNumber: number } })); }

/* ===========================
   PERSISTENZA
=========================== */
async function saveIntake(data) {
  return prisma.intake.create({
    data: {
      telegramUserId: data.telegramUserId ?? null,
      telegramNick: data.telegramNick,
      telegramNickNorm: normTelegramNick(data.telegramNick),
      discordNick: data.discordNick,
      discordNickNorm: normDiscordNick(data.discordNick),
      bitgetUID: data.bitgetUID,
      email: data.email,
      emailNorm: normEmail(data.email),
      phoneCountryCode: data.phoneCountryCode,
      phoneNumber: data.phoneNumber,
      plan: data.plan,
      payment: data.payment
    }
  });
}

function buildFinalSummary(record, data) {
  const when = fmtDateTime(record.createdAt);
  const ends = new Date(record.createdAt); ends.setDate(ends.getDate() + planDurationDays(data.plan));
  const expiry = fmtDateTime(ends);

  const tg  = escHTML(data.telegramNick);
  const dc  = escHTML(data.discordNick);
  const uid = escHTML(data.bitgetUID);
  const em  = escHTML(data.email);
  const ph  = `${escHTML(data.phoneCountryCode)} ${escHTML(data.phoneNumber)}`;

  return [
    '✅ <b>Riepilogo dati</b>',
    `• 📨 Nick Telegram: <b>${tg}</b>`,
    `• 🕹️ Nick Discord: <b>${dc}</b>`,
    `• 🆔 UID Bitget: <b>${uid}</b>`,
    `• ✉️ Email: <b>${em}</b>`,
    `• 📱 Telefono: <b>${ph}</b>`,
    `• 🏷️ Abbonamento Discord: <b>${planHuman(data.plan)}</b>`,
    `• 💳 Metodo di pagamento: <b>${paymentHuman(data.payment)}</b>`,
    `• 🕒 Registrazione: <b>${when}</b>`,
    `• ⏰ Scadenza stimata: <b>${expiry}</b>`,
    '',
    '➡️ <b>Invia questo messaggio direttamente a Jonny in chat privata.</b>'
  ].join('\n');
}

/* ===========================
   COMANDI
=========================== */
bot.onText(/^\/start$/i, async (msg) => {
  const chatId = getChatId(msg);
  const s = getOrCreateSession(chatId);
  s.data = { telegramUserId: String(msg.from?.id || '') };

  if (await existsTelegramUserId(s.data.telegramUserId)) {
    await replyTo(msg, '⚠️ <b>Attenzione</b>: risulta già una registrazione associata al tuo account Telegram.<br>Se pensi sia un errore, contatta <b>Jonny</b> in privato.');
    return;
  }
  await askStep(chatId, STEPS.TG_NICK);
});

bot.onText(/^\/restart$/i, async (msg) => {
  const chatId = getChatId(msg);
  sessions.delete(chatId);
  await replyTo(msg, '🔁 <b>Flusso azzerato.</b><br>Ripartiamo da capo!');
  await askStep(chatId, STEPS.TG_NICK);
});

// utile per sapere il tuo chat_id
bot.onText(/^\/whoami$/i, async (msg) => {
  await replyTo(msg, `👤 <b>whoami</b>\nusername: <code>@${escHTML(msg.from.username || '—')}</code>\nchat_id: <code>${escHTML(msg.chat.id)}</code>`);
});

/* ===========================
   MESSAGGI (INPUT UTENTE)
=========================== */
bot.on('message', async (msg) => {
  if (msg.data || msg.text?.startsWith('/')) return;

  const chatId = getChatId(msg);
  const text = (msg.text || '').trim();
  if (!text) return;

  const s = getOrCreateSession(chatId);

  try {
    switch (s.step) {
      case STEPS.TG_NICK: {
        if (!isValidTelegramNick(text)) {
          await replyTo(msg, '❌ <b>Nick Telegram non valido.</b><br>Deve essere 5–32 caratteri alfanumerici/underscore, con o senza @.<br>Esempi: <code>@crypto_user</code>, <code>crypto_user</code>');
          return;
        }
        const nickNorm = normTelegramNick(text);
        if (await existsTelegramNick(nickNorm)) {
          await replyTo(msg, '⚠️ Questo <b>nick Telegram</b> risulta già registrato. Inserisci un nick diverso oppure contatta <b>Jonny</b>.');
          return;
        }
        s.data.telegramNick = text;
        await askStep(chatId, STEPS.DC_NICK);
        break;
      }

      case STEPS.DC_NICK: {
        if (!isValidDiscordNick(text)) {
          await replyTo(msg, '❌ <b>Nick Discord non valido.</b> Usa 2–32 caratteri (lettere, numeri, punto, trattino, underscore).');
          return;
        }
        const nickNorm = normDiscordNick(text);
        if (await existsDiscordNick(nickNorm)) {
          await replyTo(msg, '⚠️ Questo <b>nick Discord</b> risulta già registrato. Inserisci un nick diverso oppure contatta <b>Jonny</b>.');
          return;
        }
        s.data.discordNick = text;
        await askStep(chatId, STEPS.BITGET);
        break;
      }

      case STEPS.BITGET: {
        if (!isValidBitgetUID(text)) {
          await replyTo(msg, '❌ <b>UID Bitget non valido.</b> Inserisci solo cifre (5–20).');
          return;
        }
        if (await existsBitgetUID(text)) {
          await replyTo(msg, '⚠️ Questo <b>UID Bitget</b> risulta già registrato. Verifica e reinserisci, oppure contatta <b>Jonny</b>.');
          return;
        }
        s.data.bitgetUID = text;
        await askStep(chatId, STEPS.EMAIL);
        break;
      }

      case STEPS.EMAIL: {
        if (!isValidEmailFmt(text)) {
          await replyTo(msg, '❌ <b>Email non valida.</b> Esempio: <code>nome@dominio.it</code>');
          return;
        }
        const emailNorm = normEmail(text);
        if (await existsEmail(emailNorm)) {
          await replyTo(msg, '⚠️ <b>Email</b> già registrata. Usa un’altra email o contatta <b>Jonny</b>.');
          return;
        }
        s.data.email = text;
        await askStep(chatId, STEPS.PHONE);
        break;
      }

      case STEPS.PHONE: {
        const parsed = parsePhoneWithPrefix(text);
        if (!parsed) {
          await replyTo(msg, '❌ <b>Numero non valido.</b> Usa il formato <b>internazionale</b> con “+”.<br>Esempi: <code>+39 3331234567</code>, <code>+41 765432109</code>');
          return;
        }
        if (await existsPhone(parsed.country, parsed.number)) {
          await replyTo(msg, '⚠️ <b>Numero di telefono</b> già registrato. Inserisci un altro numero o contatta <b>Jonny</b>.');
          return;
        }
        s.data.phoneCountryCode = parsed.country;
        s.data.phoneNumber = parsed.number;
        await askPlan(chatId);
        break;
      }

      default:
        // fuori flusso: ignora
        break;
    }
  } catch (err) {
    console.error('flow_error', err);
    await replyTo(msg, '⚠️ <b>Errore temporaneo.</b> Riprova tra poco.');
  }
});

/* ===========================
   CALLBACK (BOTTONI)
=========================== */
async function askPlan(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PLAN;
  await bot.sendMessage(chatId, '🧾 <b>6/7</b> — Che tipo di <b>abbonamento Discord</b> hai acquistato?', { parse_mode: 'HTML', reply_markup: KB_PLAN.reply_markup });
}
async function askPayment(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PAYMENT;
  await bot.sendMessage(chatId, '💳 <b>7/7</b> — Che tipo di <b>pagamento</b> hai utilizzato?', { parse_mode: 'HTML', reply_markup: KB_PAYMENT.reply_markup });
}

async function finishFlow(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.END;
  try {
    const saved = await saveIntake(s.data);
    await bot.sendMessage(chatId, buildFinalSummary(saved, s.data), { parse_mode: 'HTML' });
    sessions.delete(chatId);
  } catch (e) {
    console.error('saveIntake_error', e);
    let msg = '⚠️ <b>Errore nel salvataggio.</b>';
    if (String(e.message || '').toLowerCase().includes('unique')) {
      msg += ' Alcuni dati risultano già registrati. Verifica i campi oppure contatta <b>Jonny</b>.';
    }
    await bot.sendMessage(chatId, msg, { parse_mode: 'HTML' });
  }
}

bot.on('callback_query', async (query) => {
  const chatId = getChatId(query);
  const data = String(query.data || '');
  const s = getOrCreateSession(chatId);

  try {
    // INDIETRO
    if (data === 'BACK') {
      const prev = PREV[s.step];
      if (!prev) {
        await bot.answerCallbackQuery(query.id, { text: 'Non puoi tornare indietro da qui.' });
        return;
      }
      await bot.answerCallbackQuery(query.id);
      await askStep(chatId, prev);
      return;
    }

    // Discord helper
    if (data === 'NO_DISCORD' && s.step === STEPS.DC_NICK) {
      await bot.answerCallbackQuery(query.id);
      await bot.sendMessage(chatId, '⬇️ Scarica Discord da qui:\nhttps://discord.com/download');
      await bot.sendMessage(chatId, '✅ <b>Scarica Discord, registrati ed inserisci il tuo nickname Discord.</b>', { parse_mode: 'HTML' });
      await askStep(chatId, STEPS.DC_NICK);
      return;
    }

    // Bitget helper
    if (data === 'NO_BITGET' && s.step === STEPS.BITGET) {
      await bot.answerCallbackQuery(query.id);
      await bot.sendMessage(chatId, '⬇️ Scarica Bitget da qui:\nhttps://bonus.bitget.com/KZZRD3');
      await bot.sendMessage(chatId, '✅ <b>Scarica Bitget, registrati, ed inserisci il tuo UID.</b>', { parse_mode: 'HTML' });
      await askStep(chatId, STEPS.BITGET);
      return;
    }

    // PLAN
    if (data.startsWith('PLAN:') && s.step === STEPS.PLAN) {
      const plan = data.split(':')[1];
      if (!['MONTHLY', 'QUARTERLY', 'ANNUAL'].includes(plan)) {
        await bot.answerCallbackQuery(query.id, { text: 'Selezione non valida.' });
        return;
      }
      s.data.plan = plan;
      await bot.answerCallbackQuery(query.id, { text: `Scelto: ${planHuman(plan)}` });
      await askPayment(chatId);
      return;
    }

    // PAYMENT (incluse reti USDT)
    if (data.startsWith('PAY:') && s.step === STEPS.PAYMENT) {
      const pay = data.split(':')[1];
      if (!['BANK_TRANSFER', 'PAYPAL', 'USDT_BEP20', 'USDT_ERC20', 'USDT_TRC20'].includes(pay)) {
        await bot.answerCallbackQuery(query.id, { text: 'Selezione non valida.' });
        return;
      }
      s.data.payment = pay;
      await bot.answerCallbackQuery(query.id, { text: `Pagamento: ${paymentHuman(pay)}` });
      await finishFlow(chatId);
      return;
    }

    await bot.answerCallbackQuery(query.id, { text: 'Azione non valida per questo step.' });
  } catch (e) {
    console.error('callback_error', e);
    try { await bot.answerCallbackQuery(query.id, { text: 'Errore, riprova.' }); } catch {}
  }
});

/* ===========================
   HEALTHCHECK / ERROR HANDLERS
=========================== */
const app = express();
app.get('/', (_req, res) => res.send('OK'));
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Health server on :${PORT}`));

bot.on('polling_error', (err) => {
  if (['ETELEGRAM', 'EFATAL'].includes(err?.code)) return;
  console.error('polling_error', err);
});




