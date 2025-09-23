// views/index.js
import 'dotenv/config';
import express from 'express';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';

const prisma = new PrismaClient();

/* ================== TELEGRAM BOT ================== */
const bot = new TelegramBot(process.env.TELEGRAM_BOT_TOKEN, {
  polling: {
    interval: 1000,
    autoStart: true
  }
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

/* ================== UTIL ================== */
function getOrCreateSession(chatId) {
  if (!sessions.has(chatId)) {
    sessions.set(chatId, {
      step: STEPS.START,
      data: {}
    });
  }
  return sessions.get(chatId);
}

function isValidEmail(s) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(s).trim());
}

function parsePhoneWithPrefix(s) {
  // Accetta: "+39 3331234567" oppure "+393331234567"
  const raw = String(s).replace(/\s+/g, '');
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

async function saveIntake(data) {
  return prisma.intake.create({
    data: {
      telegramUserId: data.telegramUserId ?? null,
      telegramNick: data.telegramNick,
      discordNick: data.discordNick,
      bitgetUID: data.bitgetUID,
      email: data.email,
      phoneCountryCode: data.phoneCountryCode,
      phoneNumber: data.phoneNumber,
      plan: data.plan,          // enum Plan
      payment: data.payment     // enum PaymentMethod
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

/* ================== FLOW ================== */

async function askTelegramNick(chatId, msg) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.TG_NICK;

  const suggested = msg?.from?.username ? ` (es.: @${msg.from.username})` : '';
  await bot.sendMessage(
    chatId,
    `Ciao! Iniziamo l’acquisizione dati.\n\n1/7 — Inviami il tuo **nick Telegram**${suggested}.\nSe non hai il simbolo @, scrivi comunque il tuo nickname come lo usi su Telegram.`,
    { parse_mode: 'Markdown' }
  );
}

async function askDiscordNick(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.DC_NICK;
  await bot.sendMessage(
    chatId,
    `2/7 — Inviami il tuo **nick Discord** (come appare su Discord).`,
    { parse_mode: 'Markdown' }
  );
}

async function askBitgetUID(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.BITGET;
  await bot.sendMessage(
    chatId,
    `3/7 — Inviami il tuo **UID Bitget**.`,
    { parse_mode: 'Markdown' }
  );
}

async function askEmail(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.EMAIL;
  await bot.sendMessage(
    chatId,
    `4/7 — Inviami la tua **email**.`,
    { parse_mode: 'Markdown' }
  );
}

async function askPhone(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PHONE;
  await bot.sendMessage(
    chatId,
    `5/7 — Inviami il tuo **numero di telefono con prefisso**.\nEsempio: \`+39 3331234567\``,
    { parse_mode: 'Markdown' }
  );
}

async function askPlan(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PLAN;
  await bot.sendMessage(
    chatId,
    `6/7 — Che tipo di abbonamento per il server Discord hai acquistato?`,
    KB_PLAN
  );
}

async function askPayment(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.PAYMENT;
  await bot.sendMessage(
    chatId,
    `7/7 — Che tipo di pagamento hai utilizzato?`,
    KB_PAYMENT
  );
}

async function finishFlow(chatId) {
  const s = getOrCreateSession(chatId);
  s.step = STEPS.END;

  try {
    await saveIntake(s.data);
  } catch (e) {
    console.error('saveIntake_error', e);
    await bot.sendMessage(chatId, '⚠️ Errore nel salvataggio dei dati. Riprova con /start tra qualche istante.');
    return;
  }

  await bot.sendMessage(chatId, buildFinalSummary(s.data), { parse_mode: 'Markdown' });
  sessions.delete(chatId); // reset
}

/* ================== COMANDI ================== */

bot.onText(/^\/start$/i, async (msg) => {
  const chatId = msg.chat.id;
  const s = getOrCreateSession(chatId);
  s.data = {
    telegramUserId: String(msg.from?.id || '')
  };
  await askTelegramNick(chatId, msg);
});

bot.onText(/^\/restart$/i, async (msg) => {
  const chatId = msg.chat.id;
  sessions.delete(chatId);
  await bot.sendMessage(chatId, '🔁 Flusso azzerato.');
  await askTelegramNick(chatId, msg);
});

/* ================== MESSAGGI LIBERI ================== */

bot.on('message', async (msg) => {
  const chatId = msg.chat.id;

  if (msg.data || msg.text?.startsWith('/')) return;

  const text = (msg.text || '').trim();
  if (!text) return;

  const s = getOrCreateSession(chatId);

  switch (s.step) {
    case STEPS.TG_NICK: {
      s.data.telegramNick = text;
      await askDiscordNick(chatId);
      break;
    }
    case STEPS.DC_NICK: {
      s.data.discordNick = text;
      await askBitgetUID(chatId);
      break;
    }
    case STEPS.BITGET: {
      s.data.bitgetUID = text;
      await askEmail(chatId);
      break;
    }
    case STEPS.EMAIL: {
      if (!isValidEmail(text)) {
        await bot.sendMessage(chatId, 'L’email non sembra valida. Riprova (es.: nome@dominio.it).');
        return;
      }
      s.data.email = text;
      await askPhone(chatId);
      break;
    }
    case STEPS.PHONE: {
      const parsed = parsePhoneWithPrefix(text);
      if (!parsed) {
        await bot.sendMessage(chatId, 'Il numero non sembra valido. Usa questo formato: `+39 3331234567`', { parse_mode: 'Markdown' });
        return;
      }
      s.data.phoneCountryCode = parsed.country;
      s.data.phoneNumber = parsed.number;
      await askPlan(chatId);
      break;
    }
    default:
      break;
  }
});

/* ================== CALLBACK (BOTTONI) ================== */

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
