// views/index.js
import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';
import cron from 'node-cron';
import { startDiscordBot } from './discord.js';
import http from 'node:http';
import Tesseract from 'tesseract.js';

const prisma = new PrismaClient();
const {
  TELEGRAM_BOT_TOKEN,
  TZ = 'Europe/Rome',

  // Destinatari attesi
  BANK_DEST_IBANS = '',
  PAYPAL_DEST_EMAILS = '',
  USDT_DEST_TRC20 = '',
  USDT_DEST_ERC20 = '',
  USDT_DEST_BEP20 = ''
} = process.env;

if (!TELEGRAM_BOT_TOKEN) {
  console.error('❌ Missing TELEGRAM_BOT_TOKEN');
  process.exit(1);
}

const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

// ------------------- Utility -------------------

const parseCSV = s => (s || '').split(',').map(v => v.trim()).filter(Boolean);
const normIBAN = s => (s || '').replace(/[\s.]/g, '').toUpperCase();
const normEmail = s => (s || '').trim().toLowerCase();
const normAddr = s => (s || '').trim().toLowerCase();

// destinatari
const BANKS = parseCSV(BANK_DEST_IBANS).map(normIBAN);
const PAYPALS = parseCSV(PAYPAL_DEST_EMAILS).map(normEmail);
const USDT = {
  TRC20: normAddr(USDT_DEST_TRC20),
  ERC20: normAddr(USDT_DEST_ERC20),
  BEP20: normAddr(USDT_DEST_BEP20)
};

// Validazioni
const isPhone = v => /^\+[1-9]\d{7,14}$/.test((v || '').trim());
const isTelegram = v => /^@[a-zA-Z0-9_]{5,32}$/.test((v || '').trim());
const isBitget = v => /^\d{10}$/.test((v || '').trim());
const isEmail = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((v || '').trim());

// Codice verifica
const genCode = () => {
  const alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
  let out = '';
  for (let i = 0; i < 8; i++) {
    out += alphabet[Math.floor(Math.random() * alphabet.length)];
  }
  return out;
};

// Prezzi Discord in base a sub Telegram
function priceTable(tgSubType) {
  if (tgSubType === 'LIFETIME') {
    return {
      EUR: { MONTHLY: 59, QUARTERLY: 169, ANNUAL: 608 },
      USDT: { MONTHLY: 70, QUARTERLY: 198, ANNUAL: 714 }
    };
  }
  if (tgSubType === 'SEMIANNUAL' || tgSubType === 'ANNUAL') {
    return {
      EUR: { MONTHLY: 69, QUARTERLY: 199, ANNUAL: 699 },
      USDT: { MONTHLY: 85, QUARTERLY: 234, ANNUAL: 819 }
    };
  }
  return {
    EUR: { MONTHLY: 69, QUARTERLY: 199, ANNUAL: 699 },
    USDT: { MONTHLY: 85, QUARTERLY: 234, ANNUAL: 819 }
  };
}
const EARLY_PRICES = { EUR: { ANNUAL: 499 }, USDT: { ANNUAL: 585 } };

function addDuration(start, plan) {
  const d = new Date(start);
  if (plan === 'MONTHLY') d.setMonth(d.getMonth() + 1);
  if (plan === 'QUARTERLY') d.setMonth(d.getMonth() + 3);
  if (plan === 'ANNUAL') d.setFullYear(d.getFullYear() + 1);
  return d;
}

// OCR utils
async function ocrTelegramFile(fileId) {
  const file = await bot.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file.file_path}`;
  const resp = await fetch(url);
  const buf = Buffer.from(await resp.arrayBuffer());
  const { data } = await Tesseract.recognize(buf, 'ita+eng');
  return data.text || '';
}
function parseDateAny(str) {
  const s = str.replace(/\s+/g, ' ');
  const dmy = s.match(/(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})(?:[ T](\d{1,2}):(\d{2}))?/);
  if (dmy) {
    const [_, dd, mm, yy, HH = '12', MM = '00'] = dmy;
    const y = yy.length === 2 ? 2000 + parseInt(yy, 10) : parseInt(yy, 10);
    return new Date(y, parseInt(mm, 10) - 1, parseInt(dd, 10), parseInt(HH, 10), parseInt(MM, 10));
  }
  const ymd = s.match(/(\d{4})[\/.\-](\d{1,2})[\/.\-](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?/);
  if (ymd) {
    const [_, y, m, d, HH = '12', MM = '00'] = ymd;
    return new Date(parseInt(y, 10), parseInt(m, 10) - 1, parseInt(d, 10), parseInt(HH, 10), parseInt(MM, 10));
  }
  return null;
}
function parseMoney(str) {
  const m = str.replace(',', '.').match(/(\d{1,4}(?:\.\d{3})*(?:\.\d{2})?|\d+(?:\.\d+)?)/);
  return m ? parseFloat(m[1].replace(/\.(?=\d{3}(\D|$))/g, '')) : NaN;
}

// Domande step-by-step
const STEPS = [
  'tgSub', 'phone', 'telegramNick', 'discordNick', 'email', 'bitgetUid',
  'discordPlanOrSkip', 'payMethod', 'payNetwork', 'paymentProof'
];
const PROMPT = {
  tgSub: '🤖 Seleziona il tuo abbonamento *Telegram*:',
  phone: '📞 Inserisci il tuo **numero di telefono** con prefisso (+39...).',
  telegramNick: '✈️ Inserisci il tuo **nickname Telegram** (es: @nome).',
  discordNick: '🎮 Inserisci il tuo **nickname Discord**.',
  email: '📧 Inserisci la tua **email**:',
  bitgetUid: '🪪 Inserisci il tuo **UID Bitget** (10 cifre).',
  discordPlanOrSkip: '📦 Seleziona il piano Discord:',
  payMethod: '💳 Seleziona il metodo di pagamento:',
  payNetwork: '🌐 Seleziona la rete USDT:',
  paymentProof: '🖼️ Invia lo screenshot della ricevuta come immagine.'
};
const KB_TG = { reply_markup: { inline_keyboard: [[
  { text: 'Lifetime', callback_data: 'TGSUB:LIFETIME' },
  { text: 'Semestrale', callback_data: 'TGSUB:SEMIANNUAL' },
  { text: 'Annuale', callback_data: 'TGSUB:ANNUAL' }
]] }};

const KB_PLAN = { reply_markup: { inline_keyboard: [[
  { text: 'Mensile', callback_data: 'DPLAN:MONTHLY' },
  { text: 'Trimestrale', callback_data: 'DPLAN:QUARTERLY' },
  { text: 'Annuale', callback_data: 'DPLAN:ANNUAL' }
]] }};

const KB_PAY = { reply_markup: { inline_keyboard: [[
  { text: 'Bonifico', callback_data: 'PAY:BANK' },
  { text: 'PayPal', callback_data: 'PAY:PAYPAL' },
  { text: 'USDT', callback_data: 'PAY:USDT' }
]] }};

const KB_NET = { reply_markup: { inline_keyboard: [[
  { text: 'TRC20', callback_data: 'NET:TRC20' },
  { text: 'ERC20', callback_data: 'NET:ERC20' },
  { text: 'BEP20', callback_data: 'NET:BEP20' }
]] }};

const sessions = new Map();

function startFlow(chatId, user) {
  const name = user?.first_name || user?.username || 'amico';
  sessions.set(chatId, { step: 0, data: { tgSub: 'NONE' }, isEA: false });
  bot.sendMessage(chatId, `Ciao ${name}! 👋`, { parse_mode: 'Markdown' })
    .then(() => bot.sendMessage(chatId, PROMPT.tgSub, { ...KB_TG, parse_mode: 'Markdown' }));
}

bot.onText(/^\/start$/, (m) => startFlow(m.chat.id, m.from));

// ====== CALLBACK BUTTONS ======
bot.on('callback_query', async (q) => {
  const chatId = q.message.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;

  // 1) Abbonamento Telegram
  if (q.data?.startsWith('TGSUB:')) {
    s.data.tgSub = q.data.split(':')[1]; // LIFETIME | SEMIANNUAL | ANNUAL
    await bot.answerCallbackQuery(q.id, { text: `Telegram: ${s.data.tgSub}` });
    await bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: q.message.message_id });
    s.step = 1; // vai a telefono
    return bot.sendMessage(chatId, PROMPT.phone, { parse_mode: 'Markdown' });
  }

  // 3) Piano Discord (solo se NON Early Access)
  if (q.data?.startsWith('DPLAN:')) {
    s.data.discordPlan = q.data.split(':')[1]; // MONTHLY | QUARTERLY | ANNUAL
    await bot.answerCallbackQuery(q.id, { text: `Discord: ${s.data.discordPlan}` });
    await bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: q.message.message_id });
    s.step = STEPS.indexOf('payMethod');
    return bot.sendMessage(chatId, PROMPT.payMethod, { ...KB_PAY, parse_mode: 'Markdown' });
  }

  // 4) Metodo Pagamento (+ rete)
  if (q.data?.startsWith('PAY:')) {
    s.data.payMethod = q.data.split(':')[1]; // BANK | PAYPAL | USDT
    await bot.answerCallbackQuery(q.id, { text: `Metodo: ${s.data.payMethod}` });
    await bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: q.message.message_id });

    if (s.data.payMethod === 'USDT') {
      s.step = STEPS.indexOf('payNetwork');
      return bot.sendMessage(chatId, PROMPT.payNetwork, { ...KB_NET, parse_mode: 'Markdown' });
    }

    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof, { parse_mode: 'Markdown' });
  }

  if (q.data?.startsWith('NET:')) {
    s.data.usdtNetwork = q.data.split(':')[1]; // TRC20 | ERC20 | BEP20
    await bot.answerCallbackQuery(q.id, { text: `Rete: ${s.data.usdtNetwork}` });
    await bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: q.message.message_id });
    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof, { parse_mode: 'Markdown' });
  }
});

// ====== MESSAGGI TESTUALI ======
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  if (text.startsWith('/')) return;
  const s = sessions.get(chatId);
  if (!s) return;

  const key = STEPS[s.step];

  if (key === 'phone') {
    if (!isPhone(text)) {
      return bot.sendMessage(chatId, '⚠️ Numero non valido (+ prefisso, 8–15 cifre).', { parse_mode: 'Markdown' });
    }
    s.data.phone = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.telegramNick, { parse_mode: 'Markdown' });
  }

  if (key === 'telegramNick') {
    if (!isTelegram(text)) {
      return bot.sendMessage(chatId, '⚠️ Nick Telegram non valido (deve iniziare con `@`).', { parse_mode: 'Markdown' });
    }
    s.data.telegramNick = text;

    // EARLY ACCESS dal DB (tabella EarlyAccess)
    const handle = text.replace(/^@/, '').toLowerCase();
    const ea = await prisma.earlyAccess.findUnique({ where: { telegramHandle: handle } }).catch(() => null);
    s.isEA = !!ea;

    s.step++;
    return bot.sendMessage(chatId, PROMPT.discordNick, { parse_mode: 'Markdown' });
  }

  if (key === 'discordNick') {
    s.data.discordNick = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.email, { parse_mode: 'Markdown' });
  }

  if (key === 'email') {
    if (!isEmail(text)) {
      return bot.sendMessage(chatId, '⚠️ Email non valida.', { parse_mode: 'Markdown' });
    }
    s.data.email = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.bitgetUid, { parse_mode: 'Markdown' });
  }

  if (key === 'bitgetUid') {
    if (!isBitget(text)) {
      return bot.sendMessage(chatId, '⚠️ UID Bitget non valido (10 cifre).', { parse_mode: 'Markdown' });
    }
    s.data.bitgetUid = text;

    // SE EARLY ACCESS → piano forzato Annuale, vai al pagamento
    if (s.isEA) {
      s.data.discordPlan = 'ANNUAL';
      s.step = STEPS.indexOf('payMethod');
      return bot.sendMessage(
        chatId,
        '🟡 *Early Access* rilevato: piano **Annuale** con prezzo dedicato.\n\n' + PROMPT.payMethod,
        { ...KB_PAY, parse_mode: 'Markdown' }
      );
    }

    // Altrimenti chiedi il piano Discord
    s.step = STEPS.indexOf('discordPlanOrSkip');
    return bot.sendMessage(chatId, PROMPT.discordPlanOrSkip, { ...KB_PLAN, parse_mode: 'Markdown' });
  }
});

// ====== OCR/PARSING E VALIDAZIONE ======

// Estrazione campi dalla ricevuta in base al metodo
function extractByMethod({ method, network, text }) {
  let fromField = '', toField = '', currency = 'EUR', amount = NaN, paidAt = null;
  if (method === 'USDT') currency = 'USDT';

  // Importo
  const amt = text.match(/(?:importo|amount|totale|total)[^\d]*([\d\.,]+)/i) || text.match(/([\d\.,]+)\s*(?:EUR|€|USDT)/i);
  if (amt) amount = parseMoney(amt[1]);

  // Data (con ora se presente)
  const dateLine = text.match(/(?:data|date|payment date)[:\s-]*([^\n]+)/i)?.[1] || text;
  paidAt = parseDateAny(dateLine) || new Date();

  // Destinatario / Mittente
  if (method === 'BANK') {
    const ibanRegex = /[A-Z]{2}\d{2}[A-Z0-9]{10,30}/g;
    const ibans = Array.from(new Set((text.match(ibanRegex) || []).map(x => x.trim())));
    toField = ibans.find(i => BANKS.includes(normIBAN(i))) || (ibans[0] || '');
    fromField = ibans.find(i => !BANKS.includes(normIBAN(i))) || '';
  } else if (method === 'PAYPAL') {
    const emailRegex = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig;
    const emails = Array.from(new Set((text.match(emailRegex) || []).map(e => e.toLowerCase())));
    toField = emails.find(e => PAYPALS.includes(normEmail(e))) || emails[0] || '';
    fromField = emails.find(e => !PAYPALS.includes(normEmail(e))) || '';
  } else if (method === 'USDT') {
    const tron = /T[1-9A-HJ-NP-Za-km-z]{33}/g;
    const evm = /0x[a-fA-F0-9]{40}/g;
    const addrs = Array.from(new Set([...(text.match(tron) || []), ...(text.match(evm) || [])]));
    const exp = (USDT[network] || '');
    toField = addrs.find(a => normAddr(a) === exp) || '';
    fromField = addrs.find(a => normAddr(a) !== exp) || '';
  }

  return { fromField, toField, amount, currency, paidAt };
}

// deduci piano dal prezzo (EARLY = annuale 499€/585USDT)
function inferPlan({ amount, currency, tgSubType, isEA }) {
  if (isEA) {
    const target = currency === 'USDT' ? 585 : 499;
    if (isFinite(amount) && Math.abs(amount - target) < 0.01) return 'ANNUAL';
    return null;
  }
  const tbl = priceTable(tgSubType);
  const T = currency === 'USDT' ? tbl.USDT : tbl.EUR;
  for (const plan of ['MONTHLY', 'QUARTERLY', 'ANNUAL']) {
    if (isFinite(amount) && Math.abs(amount - T[plan]) < 0.01) return plan;
  }
  return null;
}

// ====== FOTO (ricevuta) ======
let discord; // helpers (set sotto)

bot.on('photo', async (msg) => {
  const chatId = msg.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;
  if (STEPS[s.step] !== 'paymentProof') return;

  try {
    const photo = msg.photo?.[msg.photo.length - 1];
    if (!photo?.file_id) {
      return bot.sendMessage(chatId, '⚠️ Invia uno *screenshot come immagine*, non come file.', { parse_mode: 'Markdown' });
    }

    // OCR
    const text = await ocrTelegramFile(photo.file_id);

    // Parsing ricevuta
    const method = s.data.payMethod;
    const network = s.data.usdtNetwork;
    const parsed = extractByMethod({ method, network, text });
    const { fromField, toField, amount, currency, paidAt } = parsed;

    // Validazione destinatario
    let validDest = false;
    if (method === 'BANK')   validDest = !!toField && BANKS.includes(normIBAN(toField));
    if (method === 'PAYPAL') validDest = !!toField && PAYPALS.includes(normEmail(toField));
    if (method === 'USDT')   validDest = !!toField && USDT[network] && normAddr(toField) === USDT[network];

    // Deduzione piano da importo/currency
    const curr = (method === 'USDT') ? 'USDT' : 'EUR';
    const inferredPlan = inferPlan({ amount: amount ?? NaN, currency: curr, tgSubType: s.data.tgSub, isEA: s.isEA });

    if (!validDest || !inferredPlan) {
      let reasons = '';
      if (!validDest) reasons += `• Destinatario non valido o non configurato.\n`;
      if (!inferredPlan) {
        if (s.isEA) {
          reasons += `• Importo non corrisponde a *EARLY* Annuale (499 EUR / 585 USDT). Rilevato: ${isFinite(amount) ? amount : '—'} ${curr}\n`;
        } else {
          const P = priceTable(s.data.tgSub)[curr];
          reasons += `• Importo non corrisponde a nessun piano.\n  Valori: M=${P.MONTHLY}, T=${P.QUARTERLY}, A=${P.ANNUAL} ${curr}\n  Rilevato: ${isFinite(amount) ? amount : '—'} ${curr}\n`;
        }
      }
      await bot.sendMessage(chatId, `❌ Ricevuta non valida.\n${reasons}Riprova con uno screenshot più chiaro.`, { parse_mode: 'Markdown' });
      return;
    }

    // Inizio/Fine dall'istante pagamento
    const start = paidAt || new Date();
    const plan = s.isEA ? 'ANNUAL' : inferredPlan;
    const end = addDuration(start, plan);

    // Upsert Subscriber + snapshot pagamento
    const sub = await prisma.subscriber.upsert({
      where: { email: s.data.email },
      update: {
        phone: s.data.phone,
        telegramNick: s.data.telegramNick,
        discordNick: s.data.discordNick,
        bitgetUid: s.data.bitgetUid,
        isEarlyAccess: s.isEA,
        tgSub: s.data.tgSub,

        discordPlan: plan,
        discordStartDate: start,
        discordEndDate: end,
        status: 'ACTIVE',

        payMethod: method,
        usdtNetwork: network ?? null,
        proofFileId: photo.file_id,
        payFrom: fromField ?? null,
        payTo: toField ?? null,
        amount: amount ?? null,
        amountCurrency: curr
      },
      create: {
        phone: s.data.phone,
        telegramNick: s.data.telegramNick,
        discordNick: s.data.discordNick,
        bitgetUid: s.data.bitgetUid,
        email: s.data.email,
        verifyCode: genCode(),
        isEarlyAccess: s.isEA,
        tgSub: s.data.tgSub,

        discordPlan: plan,
        discordStartDate: start,
        discordEndDate: end,
        status: 'ACTIVE',

        payMethod: method,
        usdtNetwork: network ?? null,
        proofFileId: photo.file_id,
        payFrom: fromField ?? null,
        payTo: toField ?? null,
        amount: amount ?? null,
        amountCurrency: curr
      }
    });

    // Log pagamento in tabella Payment (target = DISCORD)
    await prisma.payment.create({
      data: {
        subscriberId: sub.id,
        target: 'DISCORD',
        method,
        usdtNetwork: network ?? null,
        fileId: photo.file_id,
        rawText: text,
        fromField: fromField ?? null,
        toField: toField ?? null,
        amount: amount ?? null,
        currency: curr,
        paidAt: start,
        validDest: true,
        validAmount: true,
        expectedDest: (() => {
          if (method === 'BANK') return BANKS.join(', ');
          if (method === 'PAYPAL') return PAYPALS.join(', ');
          if (method === 'USDT') return USDT[network] || '';
          return '';
        })(),
        expectedAmt: (() => {
          if (s.isEA) return curr === 'USDT' ? 585 : 499;
          const P = priceTable(s.data.tgSub)[curr];
          return P[plan];
        })()
      }
    });

    // Invito Discord univoco e messaggio di conferma
    const inviteUrl = await discord.createInviteAndSave?.(sub.id);
    await bot.sendMessage(
      chatId,
      `✅ Pagamento verificato.\n` +
      `Piano: *${plan}* — Inizio: *${start.toISOString().slice(0, 10)}* — Fine: *${end.toISOString().slice(0, 10)}*\n` +
      (inviteUrl
        ? `🔗 Entra su Discord: ${inviteUrl}\n\n➡️ Al primo ingresso ti verrà assegnato automaticamente il ruolo *YoungTrader*.`
        : '⚠️ Non sono riuscito a creare l’invito. Contatta il supporto.'),
      { parse_mode: 'Markdown' }
    );

    sessions.delete(chatId);
  } catch (e) {
    console.error('payment/ocr error', e);
    await bot.sendMessage(chatId, '❌ Errore nell’analisi della ricevuta. Riprova con uno screenshot più chiaro.', { parse_mode: 'Markdown' });
  }
});

// ====== AVVIO DISCORD + CRON SCADENZE ======
discord = await startDiscordBot(prisma, process.env);

// Freeze/unfreeze solo lato Discord, ogni giorno alle 12:00 Europe/Rome
cron.schedule('0 12 * * *', async () => {
  const now = new Date();
  const subs = await prisma.subscriber.findMany({ where: { discordPlan: { not: null } } });
  for (const s of subs) {
    const expired = s.discordEndDate ? now > new Date(s.discordEndDate) : false;
    if (expired && s.status !== 'FROZEN') {
      await discord.freeze?.(s.discordUserId);
      await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'FROZEN' } });
    } else if (!expired && s.status === 'FROZEN') {
      await discord.unfreeze?.(s.discordUserId);
      await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'ACTIVE' } });
    }
  }
}, { timezone: TZ });

// ====== HEALTH SERVER ======
http.createServer((_, res) => { res.writeHead(200); res.end('OK'); })
  .listen(process.env.PORT || 3000, () => console.log('Health server on /'));
