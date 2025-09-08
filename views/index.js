// views/index.js
import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';
import cron from 'node-cron';
import { startDiscordBot } from './discord.js';
import http from 'node:http';
import Tesseract from 'tesseract.js';

// ---------- Prisma ----------
const prisma = new PrismaClient();

// ---------- ENV ----------
const {
  TELEGRAM_BOT_TOKEN,
  TZ = 'Europe/Rome',
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

// ---------- Telegram ----------
const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

// disattiva eventuale webhook rimasto
try {
  await bot.deleteWebHook({ drop_pending_updates: false });
  console.log('Telegram webhook disattivato (polling attivo).');
} catch {}

// log errori polling/webhook
bot.on('polling_error', (err) => console.error('polling_error:', err?.message || err));
bot.on('webhook_error', (err) => console.error('webhook_error:', err?.message || err));

// ---------- Utils ----------
const parseCSV = s => (s || '').split(',').map(v => v.trim()).filter(Boolean);
const normIBAN  = s => (s || '').replace(/[\s.]/g, '').toUpperCase();
const normEmail = s => (s || '').trim().toLowerCase();
const normAddr  = s => (s || '').trim().toLowerCase();

const BANKS   = parseCSV(BANK_DEST_IBANS).map(normIBAN);
const PAYPALS = parseCSV(PAYPAL_DEST_EMAILS).map(normEmail);
const USDT = {
  TRC20: normAddr(USDT_DEST_TRC20),
  ERC20: normAddr(USDT_DEST_ERC20),
  BEP20: normAddr(USDT_DEST_BEP20)
};

const isPhone   = v => /^\+[1-9]\d{7,14}$/.test((v || '').trim());
const isTelegramHandleNoAt = v => /^[a-zA-Z0-9_]{5,32}$/.test((v || '').trim());
const isBitget  = v => /^\d{10}$/.test((v || '').trim());
const isEmail   = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((v || '').trim());

function addDuration(start, plan) {
  const d = new Date(start);
  if (plan === 'MONTHLY')   d.setMonth(d.getMonth() + 1);
  if (plan === 'QUARTERLY') d.setMonth(d.getMonth() + 3);
  if (plan === 'ANNUAL')    d.setFullYear(d.getFullYear() + 1);
  return d;
}

// Prezzi (non-EA)
function priceTable(tgSubType) {
  if (tgSubType === 'LIFETIME') {
    return {
      EUR:  { MONTHLY: 59, QUARTERLY: 169, ANNUAL: 608 },
      USDT: { MONTHLY: 70, QUARTERLY: 198, ANNUAL: 714 }
    };
  }
  if (tgSubType === 'SEMIANNUAL' || tgSubType === 'ANNUAL') {
    return {
      EUR:  { MONTHLY: 69, QUARTERLY: 199, ANNUAL: 699 },
      USDT: { MONTHLY: 85, QUARTERLY: 234, ANNUAL: 819 }
    };
  }
  return {
    EUR:  { MONTHLY: 69, QUARTERLY: 199, ANNUAL: 699 },
    USDT: { MONTHLY: 85, QUARTERLY: 234, ANNUAL: 819 }
  };
}

// OCR
async function ocrTelegramFile(fileId) {
  const file = await bot.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file.file_path}`;
  const resp = await fetch(url);
  const buf = Buffer.from(await resp.arrayBuffer());
  const { data } = await Tesseract.recognize(buf, 'ita+eng');
  return data.text || '';
}

function parseDateAny(str) {
  const s = (str || '').replace(/\s+/g, ' ');
  const dmyhm = s.match(/(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})[ T](\d{1,2}):(\d{2})/);
  if (dmyhm) {
    const [_, dd, mm, yy, HH, MM] = dmyhm;
    const y = yy.length===2 ? 2000+parseInt(yy,10) : parseInt(yy,10);
    return new Date(y, parseInt(mm,10)-1, parseInt(dd,10), parseInt(HH,10), parseInt(MM,10));
  }
  // fallback
  return new Date();
}

function parseMoney(str) {
  const m = (str || '').replace(',', '.').match(/(\d+(\.\d+)?)/);
  return m ? parseFloat(m[1]) : NaN;
}

function extractByMethod({ method, network, text }) {
  let fromField='', toField='', currency='EUR', amount=NaN, paidAt=null;
  if (method === 'USDT') currency='USDT';
  const amt = text.match(/([\d\.,]+)\s*(?:EUR|€|USDT)?/i);
  if (amt) amount = parseMoney(amt[1]);
  paidAt = parseDateAny(text);

  if (method === 'BANK') {
    const ibanRegex=/[A-Z]{2}\d{2}[A-Z0-9]{10,30}/g;
    const ibans = Array.from(new Set((text.match(ibanRegex)||[])));
    toField   = ibans.find(i => BANKS.includes(normIBAN(i)))||'';
    fromField = ibans.find(i => !BANKS.includes(normIBAN(i)))||'';
  } else if (method==='PAYPAL') {
    const emails=(text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig)||[]).map(e=>e.toLowerCase());
    toField   = emails.find(e => PAYPALS.includes(normEmail(e)))||'';
    fromField = emails.find(e => !PAYPALS.includes(normEmail(e)))||'';
  } else if (method==='USDT') {
    const tron=(text.match(/T[1-9A-HJ-NP-Za-km-z]{33}/g)||[]);
    const evm=(text.match(/0x[a-fA-F0-9]{40}/g)||[]);
    const addrs=[...tron,...evm];
    const exp=(USDT[network]||'');
    toField   = addrs.find(a => normAddr(a)===exp)||'';
    fromField = addrs.find(a => normAddr(a)!==exp)||'';
  }
  return { fromField,toField,amount,currency,paidAt };
}

function inferPlan({ amount, currency, tgSubType, isEA }) {
  if (isEA) {
    const target = 499; // EUR only per tua richiesta
    if (currency === 'EUR' && Math.abs(amount - target) < 0.01) return 'ANNUAL';
    return null;
  }
  const tbl=priceTable(tgSubType);
  const T=currency==='USDT'?tbl.USDT:tbl.EUR;
  for (const plan of ['MONTHLY','QUARTERLY','ANNUAL']){
    if (Math.abs(amount-T[plan])<0.01) return plan;
  }
  return null;
}

// Renew helper
async function renewDiscordSubscription(prisma, userId, plan, paidAt) {
  const latest = await prisma.subscription.findFirst({
    where:{ userId, type:'discord' },
    orderBy:{ endAt:'desc' }
  });
  const baseStart=(latest && latest.endAt && latest.endAt>paidAt)? new Date(latest.endAt):new Date(paidAt);
  const newEnd=addDuration(baseStart,plan);

  if (latest && latest.endAt && latest.endAt>=paidAt){
    const updated=await prisma.subscription.update({
      where:{ id:latest.id },
      data:{ plan,status:'ACTIVE',endAt:newEnd }
    });
    return { startAt:updated.startAt, endAt:updated.endAt, extended:true };
  }
  const created=await prisma.subscription.create({
    data:{ userId,type:'discord',plan,status:'ACTIVE',startAt:paidAt,endAt:addDuration(paidAt,plan) }
  });
  return { startAt:created.startAt, endAt:created.endAt, extended:false };
}

// ---------- Flow ----------
const STEPS = [
  'telegramNickNoAt',  // 1) username telegram senza @
  'tgSub',             // 2) (solo non-EA) tipo abbonamento telegram
  'phone',             // 3)
  'discordNick',       // 4)
  'bitgetUid',         // 5)
  'email',             // 6)
  'discordPlan',       // 7)
  'payMethod',         // 8)
  'payNetwork',        // 9)
  'paymentProof'       // 10)
];

const PROMPT = {
  telegramNickNoAt: '✈️ Inserisci il tuo **username Telegram** (senza @).',
  tgSub: '🤖 Seleziona il tuo abbonamento *Telegram*:',
  phone: '📞 Inserisci il tuo **numero di telefono** con prefisso (+39...).',
  discordNick: '🎮 Inserisci il tuo **nickname Discord**.',
  bitgetUid: '🪪 Inserisci il tuo **UID Bitget** (10 cifre).',
  email: '📧 Inserisci la tua **email**:',
  discordPlan: '📦 Seleziona il **piano Discord**:',
  payMethod: '💳 Seleziona il **metodo di pagamento**:',
  payNetwork: '🌐 Seleziona la **rete USDT**:',
  paymentProof: '🖼️ Invia lo **screenshot della ricevuta** come immagine.'
};

const KB_TG = { reply_markup: { inline_keyboard: [[
  { text:'Lifetime',   callback_data:'TGSUB:LIFETIME' },
  { text:'Semestrale', callback_data:'TGSUB:SEMIANNUAL' },
  { text:'Annuale',    callback_data:'TGSUB:ANNUAL' }
]] }};

const KB_PLAN = { reply_markup: { inline_keyboard: [[
  { text:'Mensile',     callback_data:'DPLAN:MONTHLY' },
  { text:'Trimestrale', callback_data:'DPLAN:QUARTERLY' },
  { text:'Annuale',     callback_data:'DPLAN:ANNUAL' }
]] }};

const KB_PAY = { reply_markup: { inline_keyboard: [[
  { text:'Bonifico', callback_data:'PAY:BANK' },
  { text:'PayPal',   callback_data:'PAY:PAYPAL' },
  { text:'USDT',     callback_data:'PAY:USDT' }
]] }};

const KB_NET = { reply_markup: { inline_keyboard: [[
  { text:'TRC20', callback_data:'NET:TRC20' },
  { text:'ERC20', callback_data:'NET:ERC20' },
  { text:'BEP20', callback_data:'NET:BEP20' }
]] }};

const sessions=new Map();

function startFlow(chatId,user){
  const name=user?.first_name||user?.username||'amico';
  sessions.set(chatId,{ step:0, data:{ tgSub:'NONE' }, isEA:false });
  bot.sendMessage(chatId,`Ciao ${name}! 👋`,{parse_mode:'Markdown'})
    .then(()=>bot.sendMessage(chatId,PROMPT.telegramNickNoAt,{parse_mode:'Markdown'}));
}

// /start compatibile con gruppi e payload
bot.onText(/^\/start(?:@[\w_]+)?(?:\s+.*)?$/i, (m) => startFlow(m.chat.id, m.from));

// Callbacks (bottoni)
bot.on('callback_query', async (q) => {
  const chatId = q.message.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;

  if (q.data?.startsWith('TGSUB:')) {
    s.data.tgSub = q.data.split(':')[1];
    await bot.answerCallbackQuery(q.id, { text: `Telegram: ${s.data.tgSub}` });
    await bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: q.message.message_id });
    s.step = STEPS.indexOf('phone');
    return bot.sendMessage(chatId, PROMPT.phone, { parse_mode: 'Markdown' });
  }

  if (q.data?.startsWith('DPLAN:')) {
    s.data.discordPlan = q.data.split(':')[1];
    await bot.answerCallbackQuery(q.id, { text: `Discord: ${s.data.discordPlan}` });
    await bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: q.message.message_id });
    s.step = STEPS.indexOf('payMethod');
    return bot.sendMessage(chatId, PROMPT.payMethod, { ...KB_PAY, parse_mode: 'Markdown' });
  }

  if (q.data?.startsWith('PAY:')) {
    s.data.payMethod = q.data.split(':')[1];
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
    s.data.usdtNetwork = q.data.split(':')[1];
    await bot.answerCallbackQuery(q.id, { text: `Rete: ${s.data.usdtNetwork}` });
    await bot.editMessageReplyMarkup({ inline_keyboard: [] }, { chat_id: chatId, message_id: q.message.message_id });
    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof, { parse_mode: 'Markdown' });
  }
});

// Messaggi testuali (nuovo ordine)
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  if (text.startsWith('/')) return;
  const s = sessions.get(chatId);
  if (!s) return;

  const key = STEPS[s.step];

  if (key === 'telegramNickNoAt') {
    if (!isTelegramHandleNoAt(text)) {
      return bot.sendMessage(chatId, '⚠️ Username non valido. Usa solo lettere/numeri/_ (5–32), **senza @**.');
    }
    const handle = text.toLowerCase();
    s.data.telegramNick = '@' + handle;

    // Early Access lookup
    const ea = await prisma.earlyAccess.findUnique({ where: { telegram_id: handle } }).catch(() => null);
    s.isEA = !!ea;

    if (s.isEA) {
      s.data.discordPlan = 'ANNUAL';
      s.step = STEPS.indexOf('paymentProof');
      return bot.sendMessage(
        chatId,
        '🟡 *Early Access* rilevato.\n' +
        '➡️ Invia subito lo **screenshot della ricevuta**. Il pagamento deve essere di **499 €** (EUR, non USDT).\n' +
        'La ricevuta deve mostrare: *destinatario*, *mittente*, *importo*, *data/ora*.',
        { parse_mode: 'Markdown' }
      );
    }

    s.step = STEPS.indexOf('tgSub');
    return bot.sendMessage(chatId, PROMPT.tgSub, { ...KB_TG, parse_mode: 'Markdown' });
  }

  if (key === 'phone') {
    if (!isPhone(text)) return bot.sendMessage(chatId, '⚠️ Numero non valido (+ prefisso, 8–15 cifre).');
    s.data.phone = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.discordNick, { parse_mode: 'Markdown' });
  }

  if (key === 'discordNick') {
    s.data.discordNick = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.bitgetUid, { parse_mode: 'Markdown' });
  }

  if (key === 'bitgetUid') {
    if (!isBitget(text)) return bot.sendMessage(chatId, '⚠️ UID Bitget non valido (10 cifre).');
    s.data.bitgetUid = text;
    s.step++;
    return bot.sendMessage(chatId, PROMPT.email, { parse_mode: 'Markdown' });
  }

  if (key === 'email') {
    if (!isEmail(text)) return bot.sendMessage(chatId, '⚠️ Email non valida.');
    s.data.email = text;
    s.step = STEPS.indexOf('discordPlan');
    return bot.sendMessage(chatId, PROMPT.discordPlan, { ...KB_PLAN, parse_mode: 'Markdown' });
  }
});

// Ricevuta (OCR + validazioni + salvataggio + rinnovo)
bot.on('photo', async (msg) => {
  const chatId = msg.chat.id;
  const s = sessions.get(chatId);
  if (!s || STEPS[s.step] !== 'paymentProof') return;

  try {
    const photo = msg.photo?.[msg.photo.length - 1];
    if (!photo?.file_id) {
      return bot.sendMessage(chatId, '⚠️ Invia lo *screenshot come immagine*, non come file.', { parse_mode: 'Markdown' });
    }

    const text = await ocrTelegramFile(photo.file_id);

    // EA: forziamo EUR 499, no USDT
    let method = s.data.payMethod;
    let network = s.data.usdtNetwork;

    if (s.isEA) {
      if (/[A-Z]{2}\d{2}[A-Z0-9]{10,30}/.test(text)) method = 'BANK';
      else if (/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i.test(text)) method = 'PAYPAL';
      else if (/T[1-9A-HJ-NP-Za-km-z]{33}|0x[a-fA-F0-9]{40}/.test(text)) method = 'USDT';

      if (method === 'USDT') {
        return bot.sendMessage(chatId, '❌ Early Access accetta solo **EUR 499**. Invia ricevuta bonifico/PayPal.', { parse_mode: 'Markdown' });
      }
    }

    const parsed = extractByMethod({ method, network, text });
    const { fromField, toField, amount, currency, paidAt } = parsed;

    // validazione destinatario
    let validDest = false;
    if (method === 'BANK')   validDest = !!toField && BANKS.includes(normIBAN(toField));
    if (method === 'PAYPAL') validDest = !!toField && PAYPALS.includes(normEmail(toField));
    if (method === 'USDT')   validDest = !!toField && USDT[network] && normAddr(toField) === USDT[network];

    if (!validDest) {
      return bot.sendMessage(chatId, `❌ Destinatario non valido o non configurato.\nRilevato: ${toField || '—'}`, { parse_mode: 'Markdown' });
    }

    // piano
    let plan = s.data.discordPlan;
    if (s.isEA) {
      if (!(currency === 'EUR' && isFinite(amount) && Math.abs(amount - 499) < 0.01)) {
        return bot.sendMessage(chatId, `❌ Early Access: importo non valido. Atteso **499 €**, rilevato: ${isFinite(amount) ? amount : '—'} ${currency || ''}`);
      }
      plan = 'ANNUAL';
    } else {
      const curr = method === 'USDT' ? 'USDT' : 'EUR';
      const inferred = inferPlan({ amount: amount ?? NaN, currency: curr, tgSubType: s.data.tgSub, isEA: false });
      if (!inferred) {
        const P = priceTable(s.data.tgSub)[curr];
        return bot.sendMessage(chatId,
          `❌ Importo non corrisponde a nessun piano.\n` +
          `Valori: M=${P.MONTHLY}, T=${P.QUARTERLY}, A=${P.ANNUAL} ${curr}\n` +
          `Rilevato: ${isFinite(amount) ? amount : '—'} ${curr}`, { parse_mode: 'Markdown' });
      }
      plan = inferred;
    }

    const startAt = paidAt || new Date();

    // Upsert utente (EA potrebbe non avere email: uso placeholder)
    const emailForUpsert = s.data.email || `${chatId}@placeholder.local`;
    const user = await prisma.user.upsert({
      where: { email: emailForUpsert },
      update: {
        telegramHandle: s.data.telegramNick,
        phone: s.data.phone || null,
        bitgetUid: s.data.bitgetUid || null
      },
      create: {
        telegramUserId: chatId.toString(),
        telegramHandle: s.data.telegramNick,
        phone: s.data.phone || null,
        email: emailForUpsert,
        bitgetUid: s.data.bitgetUid || null
      }
    });

    // Payment audit
    await prisma.payment.create({
      data: {
        userId: user.id,
        method: method || 'BANK',
        usdtNet: network || null,
        proofFileId: photo.file_id,
        payFrom: fromField || null,
        payTo: toField || null,
        amount: amount ?? 0,
        amountCurrency: s.isEA ? 'EUR' : (method === 'USDT' ? 'USDT' : 'EUR'),
        paidAt: startAt
      }
    });

    // Crea o rinnova subscription
    const { startAt: subStart, endAt: subEnd, extended } =
      await renewDiscordSubscription(prisma, user.id, plan, startAt);

    // Invito Discord
    const inviteUrl = await discord.createInviteAndSave?.(user.id);
    await bot.sendMessage(
      chatId,
      `✅ Pagamento verificato.\n` +
      (extended ? '🔁 Rinnovo effettuato.\n' : '🆕 Nuova attivazione.\n') +
      `Piano: *${plan}* — Inizio: *${subStart.toISOString().slice(0, 10)}* — Fine: *${subEnd.toISOString().slice(0, 10)}*\n` +
      (inviteUrl ? `🔗 Entra su Discord: ${inviteUrl}` : '⚠️ Invito non generato, contatta il supporto.'),
      { parse_mode: 'Markdown' }
    );

    sessions.delete(chatId);
  } catch (e) {
    console.error('payment/ocr error', e);
    await bot.sendMessage(chatId, '❌ Errore nell’analisi della ricevuta. Riprova con uno screenshot più chiaro.', { parse_mode: 'Markdown' });
  }
});

// ---------- Discord + CRON ----------
const discord = await startDiscordBot(prisma, process.env);

// Ogni giorno alle 12:00 Europe/Rome → freeze se sub discord scaduta
cron.schedule('0 12 * * *', async () => {
  const now = new Date();
  const latestPerUser = await prisma.subscription.groupBy({
    by: ['userId'],
    _max: { endAt: true }
  });

  for (const row of latestPerUser) {
    const last = await prisma.subscription.findFirst({
      where: { userId: row.userId, type: 'discord' },
      orderBy: { endAt: 'desc' },
      include: { user: true }
    });
    if (!last) continue;

    if (last.status === 'ACTIVE' && last.endAt < now) {
      try { await discord.freeze?.(last.user?.discordUserId); } catch {}
      await prisma.subscription.update({ where: { id: last.id }, data: { status: 'FROZEN' } });
    } else if (last.status === 'FROZEN' && last.endAt >= now) {
      try { await discord.unfreeze?.(last.user?.discordUserId); } catch {}
      await prisma.subscription.update({ where: { id: last.id }, data: { status: 'ACTIVE' } });
    }
  }
}, { timezone: TZ });

// ---------- Health ----------
http.createServer((_,res)=>{res.writeHead(200);res.end('OK');})
  .listen(process.env.PORT||3000,()=>console.log('Health server on /'));

