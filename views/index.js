// views/index.js
import 'dotenv/config';
import express from 'express';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';

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

const KB_PLAN = {
  reply_markup: {
    inline_keyboard: [
      [{ text: 'Mensile',     callback_data: 'PLAN:MONTHLY' }],
      [{ text: 'Trimestrale', callback_data: 'PLAN:QUARTERLY' }],
      [{ text: 'Annuale',     callback_data: 'PLAN:ANNUAL' }]
    ]
  }
};

const KB_PAYMENT = {
  reply_markup: {
    inline_keyboard: [
      [{ text: 'Bonifico Bancario', callback_data: 'PAY:BANK_TRANSFER' }],
      [{ text: 'PayPal',            callback_data: 'PAY:PAYPAL' }],
      [{ text: 'Transfer USDT',     callback_data: 'PAY:USDT_TRANSFER' }]
    ]
  }
};

/* ================== NORMALIZZAZIONI & VALIDAZIONI ================== */
function normTelegramNick(s) {
  // rimuove @ iniziale, lowercase, trim
  const t = String(s || '').trim();
  return t.replace(/^@/, '').toLowerCase();
}
function normDiscordNick(s) {
  // lowercase, trim (Discord modern: username senza #, ma lasciamo libero come testo)
  return String(s || '').trim().toLowerCase();
}
function normEmail(s) {
  return String(s || '').trim().toLowerCase();
}

function isValidTelegramNick(s) {
  // Telegram username: 5-32, alfanumerico + underscore, con o senza @
  const t = String(s || '').trim();
  return /^@?[A-Za-z0-9_]{5,32}$/.test(t);
}

function isValidDiscordNick(s) {
  // Permettiamo 2-32 caratteri, alfanum + . _ - (no @ obbligatorio)
  const t = String(s || '').trim();
  return /^[A-Za-z0-9._-]{2,32}$/.test(t);
}

function isValidBitgetUID(s) {
  // Solo cifre, 5-20 (range prudenziale)
  const t = String(s || '').trim();
  return /^\d{5,20}$/.test(t);
}

function isValidEmail(s) {
  // valida robusta
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(s || '').trim());
}

function parsePhoneWithPrefix(s) {
  // E.164 light: +CC N..., normalizziamo togliendo spazi
  const raw = String(s || '').replace(/\s+/g, '');
  const m = raw.match(/^\+(\d{1,4})(\d{5,15})$/);
  if (!m) return null;
  return { country: `+${m[1]}`, number: m[2] };
}

function planHuman(plan) {
  return plan === 'MONTHLY' ? 'Mensile'
    : plan === 'QUARTERLY' ? 'Trimestrale'
    : plan === 'ANNUAL' ? 'Annuale'
    : plan;
}
function paymentHuman(pm) {
  return pm === 'BANK_TRANSFER' ? 'Bonifico Bancario'
    : pm === 'PAYPAL' ? 'PayPal'
    : pm === 'USDT_TRANSFER' ? 'Transfer USDT'
    : pm;
}

/* ================== CHECK DUPLICATI (DB) ================== */
async function existsTelegramUserId(telegramUserId) {
  if (!telegramUserId) return false;
  const found = await prisma.intake.findFirst({ where: { telegramUserId } });
  return !!found;
}
async function existsTelegramNick(nickNorm) {
  const found = await prisma.intake.findFirst({ where: { telegramNickNorm: nickNorm } });
  return !!found;
}
async function existsDiscordNick(nickNorm) {
  const found = await prisma.intake.findFirst({ where: { discordNickNorm: nickNorm } });
  return !!found;
}
async function existsBitgetUID(uid) {
  const found = await prisma.intake.findFirst({ where: { bitgetUID: uid } });
  return !!found;
}
async function existsEmail(emailNorm) {
  const found = await prisma.intake.findFirst({ where: { emailNorm } });
  return !!found;
}
async function existsPhone(country, number) {
  const found = await prisma.intake.findFirst({
    where: { phoneCountryCode: country, phoneNumber: number }
  });
  return !!found;
}

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

function buildFinalSummary(data) {
  return [
    '✅ *Riepilogo dati*',
    `• Nick Telegram: ${data.telegramNick}`,
    `• Nick Discord: ${data.discordNick}`,
    `• UID Bitget: ${data.bitgetUID}`,
    `• Email: ${data.email}`,
    `• Telefono: ${data.phoneCountryCode} ${data.phoneNumber}`,
    `• Abbonamento Discord: ${planHuman(data.plan)}`,
    `• Metodo di pagamento: ${paymentHuman(data.payment)}`,
    '',
    '➡️ *Invia questo messaggio direttamente a **Jonny** in chat privata.*'
  ].join('\n');
}

/* ================== FLOW HELPERS ================== */
const STEPS_TEXT = {
  TG_NICK: '1/7 — Inviami il tuo **nick Telegram** (con o senza @).',
  DC_NICK: '2/7 — Inviami il tuo **nick Discord** (come appare su Discord).',
  BITGET:  '3/7 — Inviami il tuo **UID Bitget** (solo cifre).',
  EMAIL:   '4/7 — Inviami la tua **email**.',
  PHONE:   '5/7 — Inviami il tuo **numero di telefono con prefisso**.\nEsempio: `+39 3331234567`',
};

async function askStep(chatId, step, extra='') {
  const s = getOrCreateSession(chatId);
  s.step = step;
  const text = STEPS_TEXT[step] || '';
  await bot.sendMessage(chatId, [text, extra].filter(Boolean).join('\n'), { parse_mode: 'Markdown' });
}

/* ================== SESSION ================== */
function getOrCreateSession(chatId) {
  if (!sessions.has(chatId)) sessions.set(chatId, { step: STEPS.START, data: {} });
  return sessions.get(chatId);
}

/* ================== COMANDI ================== */
bot.onText(/^\/start$/i, async (msg) => {
  const chatId = msg.chat.id;
  const s = getOrCreateSession(chatId);
  s.data = { telegramUserId: String(msg.from?.id || '') };

  // Controlla che lo stesso utente non abbia già inviato un intake
  if (await existsTelegramUserId(s.data.telegramUserId)) {
    await bot.sendMessage(chatId,
      '⚠️ Risulta già una registrazione associata al tuo account Telegram. ' +
      'Se pensi sia un errore, contatta **Jonny** in privato.',
      { parse_mode: 'Markdown' }
    );
    return;
  }
  const hint = msg.from?.username ? ` (es.: @${msg.from.username})` : '';
  await askStep(chatId, STEPS.TG_NICK, `Suggerimento${hint}`);
});

bot.onText(/^\/restart$/i, async (msg) => {
  const chatId = msg.chat.id;
  sessions.delete(chatId);
  await bot.sendMessage(chatId, '🔁 Flusso azzerato.');
  await askStep(chatId, STEPS.TG_NICK);
});

/* ================== MESSAGGI LIBERI ================== */
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;

  // ignora callback / comandi / non-testuali
  if (msg.data || msg.text?.startsWith('/')) return;
  const text = (msg.text || '').trim();
  if (!text) return;

  const s = getOrCreateSession(chatId);

  try {
    switch (s.step) {
      case STEPS.TG_NICK: {
        if (!isValidTelegramNick(text)) {
          await bot.sendMessage(chatId,
            '❌ Nick Telegram non valido. Deve essere 5–32 caratteri alfanumerici/underscore, con o senza @.\n' +
            'Esempi validi: `@crypto_user`, `crypto_user`',
            { parse_mode: 'Markdown' }
          );
          return;
        }
        const nickNorm = normTelegramNick(text);
        if (await existsTelegramNick(nickNorm)) {
          await bot.sendMessage(chatId,
            '⚠️ Questo nick Telegram risulta già registrato. Inserisci un nick diverso oppure contatta **Jonny**.',
            { parse_mode: 'Markdown' }
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
            '❌ Nick Discord non valido. Usa 2–32 caratteri (lettere, numeri, punto, trattino, underscore).',
          );
          return;
        }
        const nickNorm = normDiscordNick(text);
        if (await existsDiscordNick(nickNorm)) {
          await bot.sendMessage(chatId,
            '⚠️ Questo nick Discord risulta già registrato. Inserisci un nick diverso oppure contatta **Jonny**.',
            { parse_mode: 'Markdown' }
          );
          return;
        }
        s.data.discordNick = text;
        await askStep(chatId, STEPS.BITGET);
        break;
      }

      case STEPS.BITGET: {
        if (!isValidBitgetUID(text)) {
          await bot.sendMessage(chatId,
            '❌ UID Bitget non valido. Inserisci solo cifre (5–20).'
          );
          return;
        }
        if (await existsBitgetUID(text)) {
          await bot.sendMessage(chatId,
            '⚠️ Questo UID Bitget risulta già registrato. Verifica e reinserisci, oppure contatta **Jonny**.',
            { parse_mode: 'Markdown' }
          );
          return;
        }
        s.data.bitgetUID = text;
        await askStep(chatId, STEPS.EMAIL);
        break;
      }

      case STEPS.EMAIL: {
        if (!isValidEmail(text)) {
          await bot.sendMessage(chatId,
            '❌ Email non valida. Esempio: `nome@dominio.it`',
            { parse_mode: 'Markdown' }
          );
          return;
        }
        const emailNorm = normEmail(text);
        if (await existsEmail(emailNorm)) {
          await bot.sendMessage(chatId,
            '⚠️ Questa email risulta già registrata. Usa un’altra email o contatta **Jonny**.',
            { parse_mode: 'Markdown' }
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
            '❌ Numero non valido. Usa il formato: `+39 3331234567`',
            { parse_mode: 'Markdown' }
          );
          return;
        }
        if (await existsPhone(parsed.country, parsed.number)) {
          await bot.sendMessage(chatId,
            '⚠️ Questo numero di telefono risulta già registrato. Inserisci un altro numero o contatta **Jonny**.',
            { parse_mode: 'Markdown' }
          );
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
    await bot.sendMessage(chatId, '⚠️ Errore temporaneo. Riprova tra poco.');
  }
});

/* ================== BOTTONI (PIANO/PAGAMENTO) ================== */
async function askPlan(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PLAN;
  await bot.sendMessage(chatId, '6/7 — Che tipo di abbonamento per il server Discord hai acquistato?', KB_PLAN);
}
async function askPayment(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PAYMENT;
  await bot.sendMessage(chatId, '7/7 — Che tipo di pagamento hai utilizzato?', KB_PAYMENT);
}

async function finishFlow(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.END;

  try {
    await saveIntake(s.data);
  } catch (e) {
    console.error('saveIntake_error', e);
    let msg = '⚠️ Errore nel salvataggio.';
    // in caso di race, Prisma può lanciare unique constraint violation:
    if (String(e.message || '').toLowerCase().includes('unique')) {
      msg += ' Alcuni dati risultano già registrati. Verifica i campi oppure contatta **Jonny**.';
    }
    await bot.sendMessage(chatId, msg, { parse_mode: 'Markdown' });
    return;
  }

  await bot.sendMessage(chatId, buildFinalSummary(s.data), { parse_mode: 'Markdown' });
  sessions.delete(chatId);
}

bot.on('callback_query', async (query) => {
  const chatId = query.message.chat.id;
  const data = String(query.data || '');
  const s = getOrCreateSession(chatId);

  try {
    if (data.startsWith('PLAN:') && s.step === STEPS.PLAN) {
      const plan = data.split(':')[1];
      if (!['MONTHLY', 'QUARTERLY', 'ANNUAL'].includes(plan)) {
        await bot.answerCallbackQuery(query.id, { text: 'Selezione non valida.' });
        return;
      }
      s.data.plan = plan;
      await bot.answerCallbackQuery(query.id, { text: `Hai scelto: ${planHuman(plan)}` });
      await askPayment(chatId);
      return;
    }

    if (data.startsWith('PAY:') && s.step === STEPS.PAYMENT) {
      const pay = data.split(':')[1];
      if (!['BANK_TRANSFER', 'PAYPAL', 'USDT_TRANSFER'].includes(pay)) {
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

