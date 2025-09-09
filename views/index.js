// views/index.js
import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';
import cron from 'node-cron';
import Tesseract from 'tesseract.js';
import fetch from 'node-fetch';
import http from 'node:http';
import { startDiscordBot } from './discord.js';

const prisma = new PrismaClient();
const bot = new TelegramBot(process.env.TELEGRAM_BOT_TOKEN, { polling: true });
const TZ = process.env.TZ || 'Europe/Rome';

// ================= UTILS =================
const isPhone = v => /^\+[1-9]\d{7,14}$/.test((v || '').trim());
const isBitget = v => /^\d{10}$/.test((v || '').trim());
const isEmail = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((v || '').trim());
const isTelegramHandleNoAt = v => /^[a-zA-Z0-9_]{5,32}$/.test((v || '').trim());

function addDuration(start, plan) {
  const d = new Date(start);
  if (plan === 'MONTHLY') d.setMonth(d.getMonth() + 1);
  if (plan === 'QUARTERLY') d.setMonth(d.getMonth() + 3);
  if (plan === 'ANNUAL') d.setFullYear(d.getFullYear() + 1);
  return d;
}

// Prezzi Discord
const discordPrices = {
  NORMAL: {
    EUR: { MONTHLY: 69, QUARTERLY: 199, ANNUAL: 699 },
    USDT: { MONTHLY: 85, QUARTERLY: 234, ANNUAL: 819 }
  },
  DISCOUNT: {
    EUR: { MONTHLY: 59, QUARTERLY: 169, ANNUAL: 608 },
    USDT: { MONTHLY: 70, QUARTERLY: 198, ANNUAL: 714 }
  }
};

// Prezzi Telegram Lifetime
const telegramLifetimePrices = { EUR: [399, 499], USDT: [580, 585] };

// Destinatari validi
const validRecipients = {
  IBAN: ['BE76967156182995', 'LT943130010112907769'],
  PAYPAL: ['jonny.cecchi@outlook.it'],
  USDT: {
    TRC20: process.env.USDT_TRC20 || '',
    ERC20: process.env.USDT_ERC20 || '',
    BEP20: process.env.USDT_BEP20 || ''
  }
};

async function ocrTelegramFile(fileId) {
  const file = await bot.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${process.env.TELEGRAM_BOT_TOKEN}/${file.file_path}`;
  const resp = await fetch(url);
  const buf = Buffer.from(await resp.arrayBuffer());
  const { data } = await Tesseract.recognize(buf, 'eng+ita');
  return data.text || '';
}

function parsePaymentData(text) {
  const amountMatches = text.match(/(\d+[.,]\d{2})/g) || [];
  const amounts = amountMatches.map(v => parseFloat(v.replace(',', '.')));
  const emails = (text.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}/g) || []).map(e => e.toLowerCase());
  const ibans = text.match(/[A-Z]{2}\d{2}[A-Z0-9]{10,30}/g) || [];
  return { amounts, emails, ibans, raw: text };
}

function closestUSDT(amount) {
  return telegramLifetimePrices.USDT.reduce((a, b) => {
    return Math.abs(b - amount) < Math.abs(a - amount) ? b : a;
  });
}

// ================= FLOW =================
const STEPS = [
  'telegramNickNoAt',
  'tgSub',
  'tgLifetimeProof',
  'phone',
  'discordNick',
  'bitgetUid',
  'email',
  'discordPlan',
  'payMethod',
  'payNetwork',
  'confirm',
  'paymentProof'
];

const PROMPT = {
  warning: '⚠️ *ATTENZIONE!!* Compilare correttamente tutti i campi. Errori = sospensione account!',
  telegramNickNoAt: '✈️ Inserisci il tuo username Telegram (senza @)',
  tgSub: '📦 Che tipo di abbonamento Telegram hai?',
  tgLifetimeProof: '🖼️ Invia lo screenshot della ricevuta del pagamento Lifetime (399€/499€ o USDT equivalente)',
  phone: '📞 Inserisci il tuo numero di telefono con prefisso internazionale (+39...)',
  discordNick: '🎮 Inserisci il tuo nickname Discord',
  bitgetUid: '🪪 Inserisci il tuo UID Bitget (10 cifre)',
  email: '📧 Inserisci la tua email',
  discordPlan: '📦 Seleziona il piano Discord',
  payMethod: '💳 Seleziona il metodo di pagamento',
  payNetwork: '🌐 Seleziona la rete USDT',
  confirm: '📝 Confermi i dati inseriti?',
  paymentProof: '🖼️ Invia lo screenshot della ricevuta del pagamento Discord'
};

const KB_TG = { reply_markup: { inline_keyboard: [
  [{ text: 'Semestrale', callback_data: 'TGSUB:SEMIANNUAL' }],
  [{ text: 'Annuale', callback_data: 'TGSUB:ANNUAL' }],
  [{ text: 'Lifetime', callback_data: 'TGSUB:LIFETIME' }]
]}};

const KB_PLAN = { reply_markup: { inline_keyboard: [
  [{ text: 'Mensile', callback_data: 'DPLAN:MONTHLY' }],
  [{ text: 'Trimestrale', callback_data: 'DPLAN:QUARTERLY' }],
  [{ text: 'Annuale', callback_data: 'DPLAN:ANNUAL' }]
]}};

const KB_PAY = { reply_markup: { inline_keyboard: [
  [{ text: 'Bonifico', callback_data: 'PAY:BANK' }],
  [{ text: 'PayPal', callback_data: 'PAY:PAYPAL' }],
  [{ text: 'USDT', callback_data: 'PAY:USDT' }]
]}};

const KB_NET = { reply_markup: { inline_keyboard: [
  [{ text: 'TRC20', callback_data: 'NET:TRC20' }],
  [{ text: 'ERC20', callback_data: 'NET:ERC20' }],
  [{ text: 'BEP20', callback_data: 'NET:BEP20' }]
]}};

const KB_CONFIRM = { reply_markup: { inline_keyboard: [
  [{ text: '✅ Confermo', callback_data: 'CONFIRM' }],
  [{ text: '❌ Ricomincia', callback_data: 'RESTART' }]
]}};

const sessions = new Map();

// ================= START =================
function startFlow(chatId, user) {
  sessions.set(chatId, { step: 0, data: {}, hasLifetime: false });
  bot.sendMessage(chatId, PROMPT.warning, { parse_mode: 'Markdown' })
    .then(() => bot.sendMessage(chatId, PROMPT.telegramNickNoAt));
}

bot.onText(/^\/start/, (m) => startFlow(m.chat.id, m.from));

// ================= CALLBACKS =================
bot.on('callback_query', async (q) => {
  const chatId = q.message.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;

  if (q.data?.startsWith('TGSUB:')) {
    s.data.tgSub = q.data.split(':')[1];
    if (s.data.tgSub === 'LIFETIME') {
      s.step = STEPS.indexOf('tgLifetimeProof');
      return bot.sendMessage(chatId, PROMPT.tgLifetimeProof);
    }
    s.step = STEPS.indexOf('phone');
    return bot.sendMessage(chatId, PROMPT.phone);
  }

  if (q.data?.startsWith('DPLAN:')) {
    s.data.discordPlan = q.data.split(':')[1];
    s.step = STEPS.indexOf('payMethod');
    return bot.sendMessage(chatId, PROMPT.payMethod, KB_PAY);
  }

  if (q.data?.startsWith('PAY:')) {
    s.data.payMethod = q.data.split(':')[1];
    if (s.data.payMethod === 'USDT') {
      s.step = STEPS.indexOf('payNetwork');
      return bot.sendMessage(chatId, PROMPT.payNetwork, KB_NET);
    }
    s.step = STEPS.indexOf('confirm');
    return showConfirmation(chatId, s);
  }

  if (q.data?.startsWith('NET:')) {
    s.data.usdtNetwork = q.data.split(':')[1];
    s.step = STEPS.indexOf('confirm');
    return showConfirmation(chatId, s);
  }

  if (q.data === 'CONFIRM') {
    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof);
  }
  if (q.data === 'RESTART') {
    sessions.delete(chatId);
    return startFlow(chatId, q.from);
  }
});

// ================= MESSAGES =================
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  const s = sessions.get(chatId);
  if (!s || text.startsWith('/')) return;

  const key = STEPS[s.step];

  if (key === 'telegramNickNoAt') {
    if (!isTelegramHandleNoAt(text)) return bot.sendMessage(chatId, '⚠️ Username non valido.');
    s.data.telegramNick = '@' + text.toLowerCase();
    s.step = STEPS.indexOf('tgSub');
    return bot.sendMessage(chatId, PROMPT.tgSub, KB_TG);
  }
  if (key === 'phone') {
    if (!isPhone(text)) return bot.sendMessage(chatId, '⚠️ Numero non valido.');
    s.data.phone = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.discordNick);
  }
  if (key === 'discordNick') {
    s.data.discordNick = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.bitgetUid);
  }
  if (key === 'bitgetUid') {
    if (!isBitget(text)) return bot.sendMessage(chatId, '⚠️ UID non valido.');
    s.data.bitgetUid = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.email);
  }
  if (key === 'email') {
    if (!isEmail(text)) return bot.sendMessage(chatId, '⚠️ Email non valida.');
    s.data.email = text;
    s.step = STEPS.indexOf('discordPlan');
    return bot.sendMessage(chatId, PROMPT.discordPlan, KB_PLAN);
  }
});

// ================= PHOTOS =================
bot.on('photo', async (msg) => {
  const chatId = msg.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;
  const key = STEPS[s.step];
  const photo = msg.photo?.[msg.photo.length - 1];
  if (!photo?.file_id) return;

  // OCR Lifetime Telegram
  if (key === 'tgLifetimeProof') {
    const text = await ocrTelegramFile(photo.file_id);
    const { amounts } = parsePaymentData(text);
    const validEUR = amounts.some(a => telegramLifetimePrices.EUR.includes(Math.round(a)));
    const validUSDT = amounts.some(a => Math.abs(closestUSDT(a) - a) <= 5);
    if (!validEUR && !validUSDT) {
      return bot.sendMessage(chatId, '❌ Ricevuta non valida. Lifetime 399€/499€ o equivalente USDT richiesto.');
    }
    s.hasLifetime = true;
    s.step = STEPS.indexOf('phone');
    return bot.sendMessage(chatId, PROMPT.phone);
  }

  // OCR Discord payment
  if (key === 'paymentProof') {
    // 🔒 Controllo rinnovi
    const user = await prisma.user.findUnique({ where: { telegramUserId: String(chatId) } });
    if (user) {
      const sub = await prisma.subscription.findFirst({
        where: { userId: user.id, type: 'DISCORD' },
        orderBy: { endAt: 'desc' }
      });
      if (sub && sub.endAt) {
        const now = new Date();
        const diffDays = Math.ceil((sub.endAt - now) / (1000 * 60 * 60 * 24));
        if (diffDays > 7) {
          return bot.sendMessage(chatId,
            `⚠️ Abbonamento attivo fino al ${sub.endAt.toISOString().slice(0,10)}.\n` +
            `Puoi rinnovare solo 7 giorni prima della scadenza.`);
        }
      }
    }

    const text = await ocrTelegramFile(photo.file_id);
    const { amounts, emails, ibans } = parsePaymentData(text);

    // check recipient
    const validRecipient = ibans.some(i => validRecipients.IBAN.includes(i.replace(/\s/g,''))) ||
                           emails.some(e => validRecipients.PAYPAL.includes(e));
    if (!validRecipient) {
      return bot.sendMessage(chatId, '❌ Destinatario non valido.');
    }

    // importo atteso
    const plan = s.data.discordPlan;
    const mode = s.hasLifetime ? 'DISCOUNT' : 'NORMAL';
    const payCurrency = (s.data.payMethod === 'USDT') ? 'USDT' : 'EUR';
    const expected = discordPrices[mode][payCurrency][plan];
    const ok = amounts.some(a => Math.abs(a - expected) <= 5);

    if (!ok) return bot.sendMessage(chatId, `❌ Importo non corrisponde al piano scelto (${expected} ${payCurrency}).`);

    // salva DB
    const start = new Date();
    const end = addDuration(start, plan);
    const userData = {
      telegramUserId: String(chatId),
      telegramHandle: s.data.telegramNick,
      discordUserId: s.data.discordNick,
      bitgetUid: s.data.bitgetUid,
      email: s.data.email
    };
    const userSaved = await prisma.user.upsert({
      where: { telegramUserId: String(chatId) },
      update: userData,
      create: userData
    });
    await prisma.subscription.create({
      data: {
        userId: userSaved.id,
        type: 'DISCORD',
        plan,
        startAt: start,
        endAt: end
      }
    });

    bot.sendMessage(chatId, `✅ Pagamento verificato.\nPiano ${plan}, attivo fino al ${end.toISOString().slice(0,10)}`);
    sessions.delete(chatId);
  }
});

// ================= CONFIRMATION =================
function showConfirmation(chatId, s) {
  const msg = `📝 Riepilogo:\nTelegram: ${s.data.telegramNick}\nTel: ${s.data.phone}\nDiscord: ${s.data.discordNick}\nUID: ${s.data.bitgetUid}\nEmail: ${s.data.email}\nPiano Discord: ${s.data.discordPlan}\nMetodo: ${s.data.payMethod}${s.data.usdtNetwork ? ' ('+s.data.usdtNetwork+')' : ''}`;
  return bot.sendMessage(chatId, msg, KB_CONFIRM);
}

// ================= CRON =================
cron.schedule('0 12 * * *', async () => {
  console.log('Daily check subscriptions...');
}, { timezone: TZ });

// ================= DISCORD =================
const discord = await startDiscordBot(prisma, process.env);

// ================= HEALTH =================
http.createServer((_,res)=>{res.writeHead(200);res.end('OK');})
  .listen(process.env.PORT||3000,()=>console.log('Health server on /'));

