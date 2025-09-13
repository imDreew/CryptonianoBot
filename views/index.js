// views/index.js
import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import express from 'express';
import { PrismaClient } from '@prisma/client';
import cron from 'node-cron';
import Tesseract from 'tesseract.js';
import fetch from 'node-fetch';
import crypto from 'node:crypto';
import { startDiscordBot } from './discord.js';

const prisma = new PrismaClient();
const bot = new TelegramBot(process.env.TELEGRAM_BOT_TOKEN, {
  polling: {
    interval: 1000,
    params: { timeout: 50 }
  }
});
const TZ = process.env.TZ || 'Europe/Rome';

/* ============ VALIDAZIONI ============ */
const isPhone = v => /^\+[1-9]\d{7,14}$/.test((v || '').trim());
const isBitget = v => /^\d{10}$/.test((v || '').trim());
const isEmail = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((v || '').trim());
const isTelegramHandleNoAt = v => /^[a-zA-Z0-9_]{5,32}$/.test((v || '').trim());

/* ============ PREZZI E COSTANTI ============ */
// Prezzi Discord
const discordPrices = {
  NORMAL: {
    EUR:  { MONTHLY: 69, QUARTERLY: 199, ANNUAL: 699 },
    USDT: { MONTHLY: 85, QUARTERLY: 234, ANNUAL: 819 }
  },
  DISCOUNT: {
    EUR:  { MONTHLY: 59, QUARTERLY: 169, ANNUAL: 608 },
    USDT: { MONTHLY: 70, QUARTERLY: 198, ANNUAL: 714 }
  }
};

// Prezzi Telegram Lifetime per OCR
const telegramLifetimePrices = { EUR: [399, 499], USDT: [580, 585] };

// Destinatari validi (per OCR)
const validRecipients = {
  IBAN: ['BE76967156182995', 'LT943130010112907769'],
  PAYPAL: ['jonny.cecchi@outlook.it'],
  USDT: {
    TRC20: (process.env.USDT_TRC20 || '').toLowerCase(),
    ERC20: (process.env.USDT_ERC20 || '').toLowerCase(),
    BEP20: (process.env.USDT_BEP20 || '').toLowerCase()
  }
};

// Tolleranze
const TOL_EUR = 5;
const TOL_USDT = 5;

// Parole chiave che aumentano confidenza OCR
const POSITIVE_KEYS = [
  'bonifico eseguito', 'operazione eseguita',
  'transfer completed', 'successful', 'completed',
  'transaction hash', 'txid'
];

/* ============ UTILS ============ */
function addDuration(start, plan) {
  const d = new Date(start);
  if (plan === 'MONTHLY') d.setMonth(d.getMonth() + 1);
  if (plan === 'QUARTERLY') d.setMonth(d.getMonth() + 3);
  if (plan === 'ANNUAL') d.setFullYear(d.getFullYear() + 1);
  return d;
}

async function fileBufferFromTelegram(fileId) {
  const file = await bot.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${process.env.TELEGRAM_BOT_TOKEN}/${file.file_path}`;
  const resp = await fetch(url);
  return Buffer.from(await resp.arrayBuffer());
}

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

async function ocrBuffer(buf) {
  const { data } = await Tesseract.recognize(buf, 'eng+ita');
  return data.text || '';
}

function parsePaymentData(text) {
  const amountMatches = text.match(/(\d+[.,]\d{2})/g) || [];
  const amounts = amountMatches.map(v => parseFloat(v.replace(',', '.')));
  const emails = (text.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}/g) || []).map(e => e.toLowerCase());
  const ibans = (text.match(/[A-Z]{2}\d{2}[A-Z0-9]{10,30}/g) || []).map(s => s.replace(/\s/g, ''));
  const tron = (text.match(/T[1-9A-HJ-NP-Za-km-z]{33}/g) || []).map(a => a.toLowerCase());
  const evm = (text.match(/0x[a-fA-F0-9]{40}/g) || []).map(a => a.toLowerCase());
  const addrs = [...tron, ...evm];
  return { amounts, emails, ibans, addrs, raw: text };
}

function closestUSDT(amount) {
  return telegramLifetimePrices.USDT.reduce((a, b) =>
    Math.abs(b - amount) < Math.abs(a - amount) ? b : a
  );
}

function scoreConfidence({ recipientOk, amountOk, text }) {
  let conf = 0;
  if (recipientOk) conf += 0.4;
  if (amountOk)    conf += 0.4;
  const lower = (text || '').toLowerCase();
  if (POSITIVE_KEYS.some(k => lower.includes(k))) conf += 0.2;
  return Math.min(1, conf);
}

function expectedDiscordPrice(hasLifetime, plan, currency) {
  const mode = hasLifetime ? 'DISCOUNT' : 'NORMAL';
  return discordPrices[mode][currency][plan];
}

async function ensureUserRecord(chatId, s) {
  const data = {
    telegramUserId: String(chatId),
    telegramHandle: s.data.telegramNick,
    discordUserId: s.data.discordNick,
    phone: s.data.phone,
    email: s.data.email,
    bitgetUid: s.data.bitgetUid
  };
  return prisma.user.upsert({
    where: { telegramUserId: String(chatId) },
    update: data,
    create: data
  });
}

function detectUsdtNet(text) {
  const low = (text||'').toLowerCase();
  if (low.includes('trc20')) return 'TRC20';
  if (low.includes('erc20')) return 'ERC20';
  if (low.includes('bep20')) return 'BEP20';
  return null;
}

/* ============ FLOW ============ */
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
  warning: '⚠️ *ATTENZIONE!!* Compila correttamente tutti i campi. Errori = sospensione account!',
  telegramNickNoAt: '✈️ Inserisci il tuo username Telegram (senza @)',
  tgSub: '📦 Che tipo di abbonamento *Telegram* hai?',
  tgLifetimeProof: '🖼️ Invia lo *screenshot* della ricevuta del pagamento **Telegram Lifetime** (399€/499€ o USDT equivalente)',
  phone: '📞 Inserisci il tuo numero di telefono con prefisso internazionale (+39...)',
  discordNick: '🎮 Inserisci il tuo nickname Discord',
  bitgetUid: '🪪 Inserisci il tuo UID Bitget (10 cifre)',
  email: '📧 Inserisci la tua email',
  discordPlan: '📦 Seleziona il piano **Discord**',
  payMethod: '💳 Seleziona il **metodo di pagamento**',
  payNetwork: '🌐 Seleziona la **rete USDT**',
  confirm: '📝 Confermi i dati inseriti?',
  paymentProof: '🖼️ Invia lo *screenshot* della ricevuta del **pagamento Discord**'
};

// Tastiere (tutte hanno INDIETRO)
const KB_BACK_ONLY = { reply_markup: { inline_keyboard: [[{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]] } };

const KB_TG = { reply_markup: { inline_keyboard: [
  [{ text: 'Semestrale', callback_data: 'TGSUB:SEMIANNUAL' }],
  [{ text: 'Annuale',    callback_data: 'TGSUB:ANNUAL' }],
  [{ text: 'Lifetime',   callback_data: 'TGSUB:LIFETIME' }],
  [{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]
] }};

const KB_PLAN = { reply_markup: { inline_keyboard: [
  [{ text: 'Mensile',      callback_data: 'DPLAN:MONTHLY' }],
  [{ text: 'Trimestrale',  callback_data: 'DPLAN:QUARTERLY' }],
  [{ text: 'Annuale',      callback_data: 'DPLAN:ANNUAL' }],
  [{ text: '⬅️ INDIETRO',  callback_data: 'BACK' }]
] }};

const KB_PAY = { reply_markup: { inline_keyboard: [
  [{ text: 'Bonifico', callback_data: 'PAY:BANK' }],
  [{ text: 'PayPal',   callback_data: 'PAY:PAYPAL' }],
  [{ text: 'USDT',     callback_data: 'PAY:USDT' }],
  [{ text: '⬅️ INDIETRO',  callback_data: 'BACK' }]
] }};

const KB_NET = { reply_markup: { inline_keyboard: [
  [{ text: 'TRC20', callback_data: 'NET:TRC20' }],
  [{ text: 'ERC20', callback_data: 'NET:ERC20' }],
  [{ text: 'BEP20', callback_data: 'NET:BEP20' }],
  [{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]
] }};

const KB_CONFIRM = { reply_markup: { inline_keyboard: [
  [{ text: '✅ Confermo',  callback_data: 'CONFIRM' }],
  [{ text: '❌ Ricomincia', callback_data: 'RESTART' }],
  [{ text: '⬅️ INDIETRO', callback_data: 'BACK' }]
] }};

function keyboardFor(step) {
  if (step === 'tgSub')        return KB_TG;
  if (step === 'discordPlan')  return KB_PLAN;
  if (step === 'payMethod')    return KB_PAY;
  if (step === 'payNetwork')   return KB_NET;
  if (step === 'confirm')      return KB_CONFIRM;
  return KB_BACK_ONLY; // per input testo/foto mostro almeno INDIETRO
}

const sessions = new Map();
const SESSION_TTL_MS = 60 * 60 * 1000;
function setSession(chatId, state) {
  const expireAt = Date.now() + SESSION_TTL_MS;
  sessions.set(chatId, { ...state, expireAt });
}
setInterval(() => {
  const now = Date.now();
  for (const [k, v] of sessions) if (v.expireAt && v.expireAt < now) sessions.delete(k);
}, 5 * 60 * 1000);

/* ============ START ============ */
function startFlow(chatId) {
  setSession(chatId, { step: 0, data: {}, hasLifetime: false });
  bot.sendMessage(chatId, PROMPT.warning, { parse_mode: 'Markdown' })
    .then(() => bot.sendMessage(chatId, PROMPT.telegramNickNoAt, keyboardFor('telegramNickNoAt')));
}
bot.onText(/^\/start/, (m) => startFlow(m.chat.id));

/* ============ CALLBACKS (bottoni) ============ */
bot.on('callback_query', async (q) => {
  const chatId = q.message.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;

  if (q.data === 'BACK') {
    if (s.step > 0) s.step -= 1;
    const prevKey = STEPS[s.step];
    return bot.sendMessage(chatId, PROMPT[prevKey], keyboardFor(prevKey));
  }

  if (q.data?.startsWith('TGSUB:')) {
    s.data.tgSub = q.data.split(':')[1]; // SEMIANNUAL | ANNUAL | LIFETIME
    if (s.data.tgSub === 'LIFETIME') {
      s.step = STEPS.indexOf('tgLifetimeProof');
      return bot.sendMessage(chatId, PROMPT.tgLifetimeProof, keyboardFor('tgLifetimeProof'));
    }
    s.step = STEPS.indexOf('phone');
    return bot.sendMessage(chatId, PROMPT.phone, keyboardFor('phone'));
  }

  if (q.data?.startsWith('DPLAN:')) {
    s.data.discordPlan = q.data.split(':')[1]; // MONTHLY | QUARTERLY | ANNUAL
    s.step = STEPS.indexOf('payMethod');
    return bot.sendMessage(chatId, PROMPT.payMethod, keyboardFor('payMethod'));
  }

  if (q.data?.startsWith('PAY:')) {
    s.data.payMethod = q.data.split(':')[1]; // BANK | PAYPAL | USDT
    if (s.data.payMethod === 'USDT') {
      s.step = STEPS.indexOf('payNetwork');
      return bot.sendMessage(chatId, PROMPT.payNetwork, keyboardFor('payNetwork'));
    }
    s.step = STEPS.indexOf('confirm');
    return showConfirmation(chatId, s);
  }

  if (q.data?.startsWith('NET:')) {
    s.data.usdtNetwork = q.data.split(':')[1]; // TRC20/ERC20/BEP20
    s.step = STEPS.indexOf('confirm');
    return showConfirmation(chatId, s);
  }

  if (q.data === 'CONFIRM') {
    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof, keyboardFor('paymentProof'));
  }

  if (q.data === 'RESTART') {
    sessions.delete(chatId);
    return startFlow(chatId);
  }
});

/* ============ MESSAGES (input testo) ============ */
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  const s = sessions.get(chatId);
  if (!s || text.startsWith('/')) return;

  const key = STEPS[s.step];

  if (key === 'telegramNickNoAt') {
    if (!isTelegramHandleNoAt(text)) {
      return bot.sendMessage(chatId, '⚠️ Username non valido. Solo lettere/numeri/_ (5–32), senza @', keyboardFor('telegramNickNoAt'));
    }
    s.data.telegramNick = '@' + text.toLowerCase();
    s.step = STEPS.indexOf('tgSub');
    return bot.sendMessage(chatId, PROMPT.tgSub, keyboardFor('tgSub'));
  }

  if (key === 'phone') {
    if (!isPhone(text)) {
      return bot.sendMessage(chatId, '⚠️ Numero non valido. Usa +prefisso e 8–15 cifre.', keyboardFor('phone'));
    }
    s.data.phone = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.discordNick, keyboardFor('discordNick'));
  }

  if (key === 'discordNick') {
    s.data.discordNick = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.bitgetUid, keyboardFor('bitgetUid'));
  }

  if (key === 'bitgetUid') {
    if (!isBitget(text)) {
      return bot.sendMessage(chatId, '⚠️ UID Bitget non valido (10 cifre).', keyboardFor('bitgetUid'));
    }
    s.data.bitgetUid = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.email, keyboardFor('email'));
  }

  if (key === 'email') {
    if (!isEmail(text)) {
      return bot.sendMessage(chatId, '⚠️ Email non valida.', keyboardFor('email'));
    }
    s.data.email = text;
    s.step = STEPS.indexOf('discordPlan');
    return bot.sendMessage(chatId, PROMPT.discordPlan, keyboardFor('discordPlan'));
  }
});

/* ============ PHOTOS (ricevute) ============ */
bot.on('photo', async (msg) => {
  const chatId = msg.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;
  const key = STEPS[s.step];
  const photo = msg.photo?.[msg.photo.length - 1];
  if (!photo?.file_id) return;

  // OCR Telegram Lifetime
  if (key === 'tgLifetimeProof') {
    try {
      const buf = await fileBufferFromTelegram(photo.file_id);
      const imgHash = sha256(buf);

      // anti-duplica
      const dup = await prisma.payment.findFirst({ where: { proofHash: imgHash }});
      if (dup) {
        return bot.sendMessage(chatId, '❌ Questa ricevuta risulta già usata.', keyboardFor('tgLifetimeProof'));
      }

      const text = await ocrBuffer(buf);
      const { amounts, emails, ibans, addrs } = parsePaymentData(text);

      const amountEURok  = amounts.some(a => [399,499].some(x => Math.abs(a - x) <= TOL_EUR));
      const amountUSDTok = amounts.some(a => [580,585].some(x => Math.abs(a - x) <= TOL_USDT));
      const currency = amountUSDTok ? 'USDT' : 'EUR';

      const recIbanOk   = ibans.some(i => validRecipients.IBAN.includes(i));
      const recPaypalOk = emails.some(e => validRecipients.PAYPAL.includes(e));
      const usdtSet = Object.values(validRecipients.USDT).filter(Boolean);
      const recUsdtOk   = usdtSet.length ? addrs.some(a => usdtSet.includes(a)) : false;
      const recipientOk = recIbanOk || recPaypalOk || recUsdtOk;

      const amountOk = amountEURok || amountUSDTok;
      const conf = scoreConfidence({ recipientOk, amountOk, text });

      const pay = await prisma.payment.create({
        data: {
          userId: (await ensureUserRecord(chatId, s)).id,
          service: 'TELEGRAM',
          method: amountUSDTok ? 'USDT' : (recPaypalOk ? 'PAYPAL' : 'BANK'),
          usdtNet: amountUSDTok ? (detectUsdtNet(text) || s.data.usdtNetwork || null) : null,
          proofFileId: photo.file_id,
          proofFileUid: photo.file_unique_id || null,
          proofHash: imgHash,
          ocrText: text,
          confidence: conf,
          recipientMatched: recipientOk,
          payFrom: null,
          payTo: recPaypalOk ? 'jonny.cecchi@outlook.it' : (recIbanOk ? 'IBAN_MATCH' : (recUsdtOk ? 'USDT_MATCH' : null)),
          amount: (amounts[0] || (amountUSDTok ? closestUSDT(582) : 499)),
          amountCurrency: currency,
          paidAt: new Date(),
          status: conf >= 0.8 ? 'AUTO_APPROVED' : 'PENDING_REVIEW'
        }
      });

      if (pay.status === 'AUTO_APPROVED') {
        // Lifetime: endAt molto lontana
        const startAt = new Date();
        const endAt = new Date();
        endAt.setFullYear(endAt.getFullYear() + 99);

        await prisma.subscription.create({
          data: {
            userId: pay.userId,
            type: 'TELEGRAM',
            plan: 'ANNUAL', // tecnicamente lifetime; gestito con endAt "lontana"
            status: 'ACTIVE',
            startAt,
            endAt
          }
        });

        s.hasLifetime = true;
        s.step = STEPS.indexOf('phone');
        return bot.sendMessage(chatId, '✅ Telegram Lifetime verificato. Procedi con i dati personali.', keyboardFor('phone'));
      } else {
        return bot.sendMessage(chatId, '🕓 Ricevuta inviata per verifica manuale. Ti aggiorniamo appena possibile.', keyboardFor('tgLifetimeProof'));
      }
    } catch (e) {
      console.error('tgLifetimeProof OCR error', e);
      return bot.sendMessage(chatId, '❌ Errore nell’elaborazione dell’immagine. Riprova.', keyboardFor('tgLifetimeProof'));
    }
  }

  // OCR pagamento Discord
  if (key === 'paymentProof') {
    try {
      const userExisting = await prisma.user.findUnique({ where: { telegramUserId: String(chatId) } });
      if (userExisting) {
        const lastSub = await prisma.subscription.findFirst({
          where: { userId: userExisting.id, type: 'DISCORD' },
          orderBy: { endAt: 'desc' }
        });
        if (lastSub?.endAt) {
          const now = new Date();
          const diff = Math.ceil((lastSub.endAt - now)/86400000);
          if (diff > 7) {
            return bot.sendMessage(
              chatId,
              `⚠️ Hai già un abbonamento attivo fino al ${lastSub.endAt.toISOString().slice(0,10)}.\n` +
              `Puoi rinnovare solo a partire da 7 giorni prima della scadenza.`,
              keyboardFor('paymentProof')
            );
          }
        }
      }

      const buf = await fileBufferFromTelegram(photo.file_id);
      const imgHash = sha256(buf);
      const dup = await prisma.payment.findFirst({ where: { proofHash: imgHash }});
      if (dup) return bot.sendMessage(chatId, '❌ Questa ricevuta risulta già usata.', keyboardFor('paymentProof'));

      const text = await ocrBuffer(buf);
      const { amounts, emails, ibans, addrs } = parsePaymentData(text);

      const recIbanOk   = ibans.some(i => validRecipients.IBAN.includes(i));
      const recPaypalOk = emails.some(e => validRecipients.PAYPAL.includes(e));
      const net = (s.data.usdtNetwork || '').toUpperCase();
      const expAddr = (validRecipients.USDT[net] || '').toLowerCase();
      const recUsdtOk = expAddr ? addrs.some(a => a === expAddr) : false;
      const recipientOk = recIbanOk || recPaypalOk || recUsdtOk;

      const plan = s.data.discordPlan; // MONTHLY | QUARTERLY | ANNUAL
      const currency = (s.data.payMethod === 'USDT') ? 'USDT' : 'EUR';
      const expected = expectedDiscordPrice(s.hasLifetime, plan, currency);
      const tol = currency === 'USDT' ? TOL_USDT : TOL_EUR;
      const amountOk = amounts.some(a => Math.abs(a - expected) <= tol);

      const conf = scoreConfidence({ recipientOk, amountOk, text });

      const pay = await prisma.payment.create({
        data: {
          userId: (await ensureUserRecord(chatId, s)).id,
          service: 'DISCORD',
          method: s.data.payMethod === 'USDT' ? 'USDT' : (recPaypalOk ? 'PAYPAL' : 'BANK'),
          usdtNet: s.data.usdtNetwork || null,
          proofFileId: photo.file_id,
          proofFileUid: photo.file_unique_id || null,
          proofHash: imgHash,
          ocrText: text,
          confidence: conf,
          recipientMatched: recipientOk,
          payFrom: null,
          payTo: recPaypalOk ? 'jonny.cecchi@outlook.it' : (recIbanOk ? 'IBAN_MATCH' : (recUsdtOk ? expAddr : null)),
          amount: (amounts[0] || expected),
          amountCurrency: currency,
          paidAt: new Date(),
          status: conf >= 0.8 ? 'AUTO_APPROVED' : 'PENDING_REVIEW'
        }
      });

      if (pay.status === 'AUTO_APPROVED') {
        const now = new Date();
        let startAt = now;

        const latest = await prisma.subscription.findFirst({
          where: { userId: pay.userId, type: 'DISCORD' },
          orderBy: { endAt: 'desc' }
        });

        if (latest?.endAt && latest.endAt > now && Math.ceil((latest.endAt - now)/86400000) <= 7) {
          startAt = new Date(latest.endAt);
        }

        const endAt = addDuration(startAt, plan);

        await prisma.subscription.create({
          data: {
            userId: pay.userId,
            type: 'DISCORD',
            plan,
            status: 'ACTIVE',
            startAt,
            endAt
          }
        });

        await bot.sendMessage(
          chatId,
          `✅ Pagamento verificato.\nPiano: *${plan}* — Inizio: *${startAt.toISOString().slice(0,10)}* — Fine: *${endAt.toISOString().slice(0,10)}*`,
          { parse_mode: 'Markdown' }
        );
      } else {
        await bot.sendMessage(chatId, '🕓 Ricevuta inviata per verifica manuale. Ti aggiorniamo appena possibile.', keyboardFor('paymentProof'));
      }

      sessions.delete(chatId);
    } catch (e) {
      console.error('paymentProof OCR error', e);
      return bot.sendMessage(chatId, '❌ Errore nell’elaborazione dell’immagine. Riprova.', keyboardFor('paymentProof'));
    }
  }
});

/* ============ CONFERMA ============ */
function showConfirmation(chatId, s) {
  const msg =
    `📝 *Riepilogo dati*\n\n` +
    `• Telegram: ${s.data.telegramNick || '-'}\n` +
    `• Telefono: ${s.data.phone || '-'}\n` +
    `• Discord: ${s.data.discordNick || '-'}\n` +
    `• UID Bitget: ${s.data.bitgetUid || '-'}\n` +
    `• Email: ${s.data.email || '-'}\n` +
    `• Piano Discord: ${s.data.discordPlan || '-'}\n` +
    `• Metodo: ${s.data.payMethod || '-'}${s.data.usdtNetwork ? ' (' + s.data.usdtNetwork + ')' : ''}\n\n` +
    `Confermi di procedere al pagamento?`;
  return bot.sendMessage(chatId, msg, { ...keyboardFor('confirm'), parse_mode: 'Markdown' });
}

/* ============ CRON: freeze/unfreeze ============ */
cron.schedule('0 12 * * *', async () => {
  try {
    const now = new Date();
    const subs = await prisma.subscription.findMany({
      where: { type: 'DISCORD' }
    });
    for (const s of subs) {
      if (s.status === 'ACTIVE' && s.endAt < now) {
        await prisma.subscription.update({ where: { id: s.id }, data: { status: 'FROZEN' } });
      }
      if (s.status === 'FROZEN' && s.endAt >= now) {
        await prisma.subscription.update({ where: { id: s.id }, data: { status: 'ACTIVE' } });
      }
    }
    console.log('Daily check subscriptions…');
  } catch (e) {
    console.error('cron error', e);
  }
}, { timezone: TZ });

/* ============ DISCORD ============ */
const discord = await startDiscordBot(prisma, process.env);

/* ============ HTTP (health) ============ */
const app = express();
app.get('/', (_req, res) => res.send('OK'));
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Health server on :${PORT}`));

/* ============ ERROR HANDLERS ============ */
bot.on('polling_error', (err) => {
  // filtra rumore da polling
  if (['ETELEGRAM', 'EFATAL'].includes(err?.code)) return;
  console.error('polling_error', err);
});


