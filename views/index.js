// views/index.js
import 'dotenv/config';
import express from 'express';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';
import { parsePhoneNumberFromString } from 'libphonenumber-js';

const prisma = new PrismaClient();

/* ================== TELEGRAM BOT ================== */
const bot = new TelegramBot(process.env.TELEGRAM_BOT_TOKEN, {
  polling: { interval: 1000, autoStart: true }
});

/* ================== STATO CONVERSAZIONI ================== */
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

// Mappa step precedente (per INDIETRO)
const PREV = {
  [STEPS.DC_NICK]: STEPS.TG_NICK,
  [STEPS.BITGET]:  STEPS.DC_NICK,
  [STEPS.EMAIL]:   STEPS.BITGET,
  [STEPS.PHONE]:   STEPS.EMAIL,
  [STEPS.PLAN]:    STEPS.PHONE,
  [STEPS.PAYMENT]: STEPS.PLAN
};

/* ================== TASTI ================== */

// back generico
const KB_BACK = { reply_markup: { inline_keyboard: [[{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]] } };

// step Discord: back + “Non hai Discord?”
const KB_DC = {
  reply_markup: {
    inline_keyboard: [
      [{ text: '🆕 Non hai Discord?', callback_data: 'NO_DISCORD' }],
      [{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]
    ]
  }
};

// step Bitget: back + “Non ho Bitget”
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

/* ================== NORMALIZZAZIONI & VALIDAZIONI ================== */
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

/* ================== CHECK DUPLICATI (DB) ================== */
async function existsTelegramUserId(telegramUserId) { if (!telegramUserId) return false; return !!(await prisma.intake.findFirst({ where: { telegramUserId } })); }
async function existsTelegramNick(nickNorm)        { return !!(await prisma.intake.findFirst({ where: { telegramNickNorm: nickNorm } })); }
async function existsDiscordNick(nickNorm)         { return !!(await prisma.intake.findFirst({ where: { discordNickNorm: nickNorm } })); }
async function existsBitgetUID(uid)                { return !!(await prisma.intake.findFirst({ where: { bitgetUID: uid } })); }
async function existsEmail(emailNorm)              { return !!(await prisma.intake.findFirst({ where: { emailNorm } })); }
async function existsPhone(country, number)        { return !!(await prisma.intake.findFirst({ where: { phoneCountryCode: country, phoneNumber: number } })); }

/* ================== PERSISTENZA ================== */
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
function fmtDateTime(dt) { return new Date(dt).toLocaleString('it-IT', { hour12: false }); }
function buildFinalSummary(record, data) {
  const when = fmtDateTime(record.createdAt);
  const ends = new Date(record.createdAt); ends.setDate(ends.getDate() + planDurationDays(data.plan));
  const expiry = fmtDateTime(ends);
  return [
    '✅ *Riepilogo dati*',
    `• 📨 Nick Telegram: *${data.telegramNick}*`,
    `• 🕹️ Nick Discord: *${data.discordNick}*`,
    `• 🆔 UID Bitget: *${data.bitgetUID}*`,
    `• ✉️ Email: *${data.email}*`,
    `• 📱 Telefono: *${data.phoneCountryCode} ${data.phoneNumber}*`,
    `• 🏷️ Abbonamento Discord: *${planHuman(data.plan)}*`,
    `• 💳 Metodo di pagamento: *${paymentHuman(data.payment)}*`,
    `• 🕒 Registrazione: *${when}*`,
    `• ⏰ Scadenza stimata: *${expiry}*`,
    '',
    '➡️ *Invia questo messaggio direttamente a **Il Cryptoniano** in chat privata.*'
  ].join('\n');
}

/* ================== FLOW HELPERS ================== */
const STEPS_TEXT = {
  TG_NICK: '👋 *Benvenuto!*\n\n*1/7* — Inviami il tuo **nick Telegram** (con o senza @).',
  DC_NICK: '🎮 *2/7* — Inviami il tuo **nick Discord** (come appare su Discord).',
  BITGET:  '🏦 *3/7* — Inviami il tuo **UID Bitget** (solo cifre).',
  EMAIL:   '✉️ *4/7* — Inviami la tua **email**.',
  PHONE:   '📞 *5/7* — Inviami il tuo **numero di telefono con prefisso internazionale**.\nEsempi: `+39 3331234567`, `+41 765432109`'
};

function getOrCreateSession(chatId) {
  if (!sessions.has(chatId)) sessions.set(chatId, { step: STEPS.START, data: {} });
  return sessions.get(chatId);
}

async function askStep(chatId, step) {
  const s = getOrCreateSession(chatId);
  s.step = step;

  // Per ogni step (tranne il primo) mostriamo il bottone INDIETRO; per Discord/Bitget anche i bottoni di aiuto
  if (step === STEPS.TG_NICK) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'HTML' });
  } else if (step === STEPS.DC_NICK) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'Markdown', reply_markup: KB_DC.reply_markup });
  } else if (step === STEPS.BITGET) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'Markdown', reply_markup: KB_BG.reply_markup });
  } else if (step === STEPS.EMAIL) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'Markdown', reply_markup: KB_BACK.reply_markup });
  } else if (step === STEPS.PHONE) {
    await bot.sendMessage(chatId, STEPS_TEXT[step], { parse_mode: 'Markdown', reply_markup: KB_BACK.reply_markup });
  } else {
    await bot.sendMessage(chatId, STEPS_TEXT[step] || 'Procedi.', { parse_mode: 'Markdown', reply_markup: KB_BACK.reply_markup });
  }
}

/* ================== COMANDI ================== */
bot.onText(/^\/start$/i, async (msg) => {
  const chatId = msg.chat.id;
  const s = getOrCreateSession(chatId);
  s.data = { telegramUserId: String(msg.from?.id || '') };

  if (await existsTelegramUserId(s.data.telegramUserId)) {
    await bot.sendMessage(chatId,
      '⚠️ *Attenzione*: risulta già una registrazione associata al tuo account Telegram.\nSe pensi sia un errore, contatta **Il Cryptoniano** in privato.',
      { parse_mode: 'HTML' }
    );
    return;
  }
  await askStep(chatId, STEPS.TG_NICK);
});

bot.onText(/^\/restart$/i, async (msg) => {
  const chatId = msg.chat.id;
  sessions.delete(chatId);
  await bot.sendMessage(chatId, '🔁 *Flusso azzerato.*\nRipartiamo da capo!', { parse_mode: 'HTML' });
  await askStep(chatId, STEPS.TG_NICK);
});

/* ================== MESSAGGI LIBERI ================== */
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  if (msg.data || msg.text?.startsWith('/')) return;

  const text = (msg.text || '').trim();
  if (!text) return;

  const s = getOrCreateSession(chatId);

  try {
    switch (s.step) {
      case STEPS.TG_NICK: {
        if (!isValidTelegramNick(text)) {
          await bot.sendMessage(chatId,
            '❌ *Nick Telegram non valido.*\nDeve essere 5–32 caratteri alfanumerici/underscore, con o senza @.\nEsempi: `@crypto_user`, `crypto_user`',
            { parse_mode: 'HTML' }
          );
          return;
        }
        const nickNorm = normTelegramNick(text);
        if (await existsTelegramNick(nickNorm)) {
          await bot.sendMessage(chatId,
            '⚠️ Questo *nick Telegram* risulta già registrato. Inserisci un nick diverso oppure contatta **Il Cryptoniano**.',
            { parse_mode: 'HTML' }
          );
          return;
        }
        s.data.telegramNick = text;
        await askStep(chatId, STEPS.DC_NICK);
        break;
      }

      case STEPS.DC_NICK: {
        if (!isValidDiscordNick(text)) {
          await bot.sendMessage(chatId,
            '❌ *Nick Discord non valido.* Usa 2–32 caratteri (lettere, numeri, punto, trattino, underscore).',
            { parse_mode: 'HTML' }
          );
          return;
        }
        const nickNorm = normDiscordNick(text);
        if (await existsDiscordNick(nickNorm)) {
          await bot.sendMessage(chatId,
            '⚠️ Questo *nick Discord* risulta già registrato. Inserisci un nick diverso oppure contatta **Il Cryptoniano**.',
            { parse_mode: 'HTML' }
          );
          return;
        }
        s.data.discordNick = text;
        await askStep(chatId, STEPS.BITGET);
        break;
      }

      case STEPS.BITGET: {
        if (!isValidBitgetUID(text)) {
          await bot.sendMessage(chatId, '❌ *UID Bitget non valido.* Inserisci solo cifre (5–20).', { parse_mode: 'HTML' });
          return;
        }
        if (await existsBitgetUID(text)) {
          await bot.sendMessage(chatId,
            '⚠️ Questo *UID Bitget* risulta già registrato. Verifica e reinserisci, oppure contatta **Il Cryptoniano**.',
            { parse_mode: 'HTML' }
          );
          return;
        }
        s.data.bitgetUID = text;
        await askStep(chatId, STEPS.EMAIL);
        break;
      }

      case STEPS.EMAIL: {
        if (!isValidEmailFmt(text)) {
          await bot.sendMessage(chatId, '❌ *Email non valida.* Esempio: `nome@dominio.it`', { parse_mode: 'HTML' });
          return;
        }
        const emailNorm = normEmail(text);
        if (await existsEmail(emailNorm)) {
          await bot.sendMessage(chatId,
            '⚠️ *Email* già registrata. Usa un’altra email o contatta **Il Cryptoniano**.',
            { parse_mode: 'HTML' }
          );
          return;
        }
        s.data.email = text;
        await askStep(chatId, STEPS.PHONE);
        break;
      }

      case STEPS.PHONE: {
        const parsed = parsePhoneWithPrefix(text);
        if (!parsed) {
          await bot.sendMessage(chatId,
            '❌ *Numero non valido.* Usa il formato **internazionale** con prefisso “+”.\nEsempi: `+39 3331234567`, `+41 765432109`',
            { parse_mode: 'HTML' }
          );
          return;
        }
        if (await existsPhone(parsed.country, parsed.number)) {
          await bot.sendMessage(chatId,
            '⚠️ *Numero di telefono* già registrato. Inserisci un altro numero o contatta **Il Cryptoniano**.',
            { parse_mode: 'HTML' }
          );
          return;
        }
        s.data.phoneCountryCode = parsed.country;
        s.data.phoneNumber = parsed.number;
        await askPlan(chatId);
        break;
      }

      default: break;
    }
  } catch (err) {
    console.error('flow_error', err);
    await bot.sendMessage(chatId, '⚠️ *Errore temporaneo.* Riprova tra poco.', { parse_mode: 'HTML' });
  }
});

/* ================== BOTTONI (CALLBACK) ================== */
async function askPlan(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PLAN;
  await bot.sendMessage(chatId, '🧾 *6/7* — Che tipo di **abbonamento Discord** hai acquistato?', KB_PLAN);
}
async function askPayment(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PAYMENT;
  await bot.sendMessage(chatId, '💳 *7/7* — Che tipo di **pagamento** hai utilizzato?', KB_PAYMENT);
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
    let msg = '⚠️ *Errore nel salvataggio.*';
    if (String(e.message || '').toLowerCase().includes('unique')) {
      msg += ' Alcuni dati risultano già registrati. Verifica i campi oppure contatta **Il Cryptoniano**.';
    }
    await bot.sendMessage(chatId, msg, { parse_mode: 'HTML' });
  }
}

bot.on('callback_query', async (query) => {
  const chatId = query.message.chat.id;
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
      await bot.sendMessage(chatId, '✅ *Scarica Discord, registrati ed inserisci il tuo nickname Discord.*', { parse_mode: 'HTML' });
      await askStep(chatId, STEPS.DC_NICK);
      return;
    }

    // Bitget helper
    if (data === 'NO_BITGET' && s.step === STEPS.BITGET) {
      await bot.answerCallbackQuery(query.id);
      await bot.sendMessage(chatId, '⬇️ Scarica Bitget da qui:\nhttps://bonus.bitget.com/KZZRD3');
      await bot.sendMessage(chatId, '✅ *Scarica Bitget, registrati, ed inserisci il tuo UID.*', { parse_mode: 'HTML' });
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

    // PAYMENT
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

/* ================== HEALTH / ERROR ================== */
const app = express();
app.get('/', (_req, res) => res.send('OK'));
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Health server on :${PORT}`));

bot.on('polling_error', (err) => {
  if (['ETELEGRAM', 'EFATAL'].includes(err?.code)) return;
  console.error('polling_error', err);
});


