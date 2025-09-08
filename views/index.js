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

  // EARLYACCESS: lista di @nick (CSV, con o senza @)
  EARLYACCESS_TELEGRAMS = '',

  // Destinatari attesi
  BANK_DEST_IBANS = '',      // CSV (con o senza spazi)
  PAYPAL_DEST_EMAILS = '',   // CSV
  USDT_DEST_TRC20 = '',
  USDT_DEST_ERC20 = '',
  USDT_DEST_BEP20 = ''
} = process.env;

if (!TELEGRAM_BOT_TOKEN) { console.error('Missing TELEGRAM_BOT_TOKEN'); process.exit(1); }
const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

// ---------- Utils & Config ----------
const parseCSV = s => (s||'').split(',').map(v=>v.trim()).filter(Boolean);
const normIBAN  = s => (s||'').replace(/[\s.]/g,'').toUpperCase();
const normEmail = s => (s||'').trim().toLowerCase();
const normAddr  = s => (s||'').trim().toLowerCase();

// EARLYACCESS
const EARLY = new Set(parseCSV(EARLYACCESS_TELEGRAMS).map(v => v.replace(/^@/,'').toLowerCase()));

// destinatari
const BANKS = parseCSV(BANK_DEST_IBANS).map(normIBAN);
const PAYPALS = parseCSV(PAYPAL_DEST_EMAILS).map(normEmail);
const USDT = {
  TRC20: normAddr(USDT_DEST_TRC20),
  ERC20: normAddr(USDT_DEST_ERC20),
  BEP20: normAddr(USDT_DEST_BEP20)
};

// Validazioni base
const isPhone = v => /^\+[1-9]\d{7,14}$/.test((v||'').trim());
const isTelegram = v => /^@[a-zA-Z0-9_]{5,32}$/.test((v||'').trim());
const isBitget = v => /^\d{10}$/.test((v||'').trim());
const isEmail = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((v||'').trim());

const genCode = () => {
  const alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
  let out = ''; for (let i=0;i<8;i++) out += alphabet[Math.floor(Math.random()*alphabet.length)];
  return out;
};

// Prezzi EUR/USDT
function priceTable(tgSubType) {
  // ritorna {EUR:{MONTHLY,QUARTERLY,ANNUAL}, USDT:{...}}
  if (tgSubType === 'LIFETIME') {
    return {
      EUR:   { MONTHLY: 59,  QUARTERLY: 169, ANNUAL: 608 },
      USDT:  { MONTHLY: 70,  QUARTERLY: 198, ANNUAL: 714 }
    };
  }
  // semestrale o annuale (normali)
  if (tgSubType === 'SEMIANNUAL' || tgSubType === 'ANNUAL') {
    return {
      EUR:   { MONTHLY: 69,  QUARTERLY: 199, ANNUAL: 699 },
      USDT:  { MONTHLY: 85,  QUARTERLY: 234, ANNUAL: 819 }
    };
  }
  // nessun abbonamento Telegram → usiamo i prezzi "normali" (stesso set dell'ultimo)
  return {
    EUR:   { MONTHLY: 69,  QUARTERLY: 199, ANNUAL: 699 },
    USDT:  { MONTHLY: 85,  QUARTERLY: 234, ANNUAL: 819 }
  };
}
const EARLY_PRICES = { EUR: { ANNUAL: 499 }, USDT: { ANNUAL: 585 } };

function addDuration(start, plan) {
  const d = new Date(start);
  if (plan === 'MONTHLY') d.setMonth(d.getMonth()+1);
  if (plan === 'QUARTERLY') d.setMonth(d.getMonth()+3);
  if (plan === 'ANNUAL') d.setFullYear(d.getFullYear()+1);
  return d;
}

// OCR helpers
async function ocrTelegramFile(fileId) {
  const file = await bot.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file.file_path}`;
  const resp = await fetch(url);
  const buf = Buffer.from(await resp.arrayBuffer());
  const { data } = await Tesseract.recognize(buf, 'ita+eng');
  return data.text || '';
}
function parseDateAny(str) {
  const s = str.replace(/\s+/g,' ');
  const dmy = s.match(/(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})(?:[ T](\d{1,2}):(\d{2}))?/);
  if (dmy) {
    const [_, dd, mm, yy, HH='12', MM='00'] = dmy;
    const y = yy.length===2 ? 2000+parseInt(yy,10) : parseInt(yy,10);
    return new Date(y, parseInt(mm,10)-1, parseInt(dd,10), parseInt(HH,10), parseInt(MM,10));
  }
  const ymd = s.match(/(\d{4})[\/.\-](\d{1,2})[\/.\-](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?/);
  if (ymd) {
    const [_, y, m, d, HH='12', MM='00'] = ymd;
    return new Date(parseInt(y,10), parseInt(m,10)-1, parseInt(d,10), parseInt(HH,10), parseInt(MM,10));
  }
  return null;
}
function parseMoney(str) {
  const m = str.replace(',', '.').match(/(\d{1,4}(?:\.\d{3})*(?:\.\d{2})?|\d+(?:\.\d+)?)/);
  return m ? parseFloat(m[1].replace(/\.(?=\d{3}(\D|$))/g,'')) : NaN;
}
function extractByMethod({ method, network, text }) {
  let fromField='', toField='', currency='EUR', amount=NaN, paidAt=null;
  if (method === 'USDT') currency = 'USDT';

  // importo
  const amt = text.match(/(?:importo|amount|totale|total)[^\d]*([\d\.,]+)/i) || text.match(/([\d\.,]+)\s*(?:EUR|€|USDT)/i);
  if (amt) amount = parseMoney(amt[1]);

  // data
  const dateLine = text.match(/(?:data|date|payment date)[:\s-]*([^\n]+)/i)?.[1] || text;
  paidAt = parseDateAny(dateLine) || new Date();

  if (method === 'BANK') {
    const ibanRegex = /[A-Z]{2}\d{2}[A-Z0-9]{10,30}/g;
    const ibans = Array.from(new Set((text.match(ibanRegex)||[]).map(x=>x.trim())));
    toField = ibans.find(i => BANKS.includes(normIBAN(i))) || (ibans[0]||'');
    fromField = ibans.find(i => !BANKS.includes(normIBAN(i))) || '';
  } else if (method === 'PAYPAL') {
    const emailRegex = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig;
    const emails = Array.from(new Set((text.match(emailRegex)||[]).map(e=>e.toLowerCase())));
    toField = emails.find(e => PAYPALS.includes(normEmail(e))) || emails[0] || '';
    fromField = emails.find(e => !PAYPALS.includes(normEmail(e))) || '';
  } else if (method === 'USDT') {
    const tron = /T[1-9A-HJ-NP-Za-km-z]{33}/g;
    const evm  = /0x[a-fA-F0-9]{40}/g;
    const addrs = Array.from(new Set([...(text.match(tron)||[]), ...(text.match(evm)||[])]));
    const exp = (USDT[network]||'');
    toField = addrs.find(a => normAddr(a) === exp) || '';
    fromField = addrs.find(a => normAddr(a) !== exp) || '';
  }
  return { fromField, toField, amount, currency, paidAt };
}

// calcola piano in base all'importo/currency
function inferPlan({ amount, currency, tgSubType, isEA }) {
  if (isEA) {
    if (currency==='USDT' && Math.abs(amount - (EARLY_PRICES.USDT.ANNUAL)) < 0.01) return 'ANNUAL';
    if (currency!=='USDT' && Math.abs(amount - (EARLY_PRICES.EUR.ANNUAL)) < 0.01) return 'ANNUAL';
    return null;
  }
  const tables = priceTable(tgSubType);
  const T = currency==='USDT' ? tables.USDT : tables.EUR;
  for (const plan of ['MONTHLY','QUARTERLY','ANNUAL']) {
    if (Math.abs(amount - T[plan]) < 0.01) return plan;
  }
  return null; // nessun match esatto
}

// ---------- FLOW ----------
const STEPS = [
  'tgSub',               // 1) che abbonamento Telegram hai?
  'phone','telegramNick','discordNick','email','bitgetUid', // 2) dati
  'discordPlanOrSkip',   // 3) se EARLYACCESS → skip, altrimenti chiedi piano
  'payMethod',           // 4) metodo + rete (se USDT)
  'payNetwork',
  'paymentProof'
];

const PROMPT = {
  tgSub: '🤖 Seleziona il tuo abbonamento *Telegram*:',
  phone: '📞 Inserisci il tuo **numero di telefono** con prefisso (es. `+39...`).',
  telegramNick: '✈️ Inserisci il tuo **nickname Telegram** iniziando con `@`.',
  discordNick: '🎮 Inserisci il tuo **nickname Discord**.',
  email: '📧 Inserisci la tua **email**:',
  bitgetUid: '🪪 Inserisci il tuo **UID Bitget** (10 cifre).',
  discordPlanOrSkip: '📦 Seleziona il **piano Discord**:',
  payMethod: '💳 Seleziona il **metodo di pagamento**:',
  payNetwork: '🌐 Seleziona la **rete USDT**:',
  paymentProof: '🖼️ Invia **lo screenshot della ricevuta** come *immagine* (non file).'
};

const KB_TG = { reply_markup: { inline_keyboard: [[
  { text:'Lifetime', callback_data:'TGSUB:LIFETIME' },
  { text:'Semestrale', callback_data:'TGSUB:SEMIANNUAL' },
  { text:'Annuale', callback_data:'TGSUB:ANNUAL' }
]] }};

const KB_PLAN = { reply_markup: { inline_keyboard: [[
  { text:'Mensile', callback_data:'DPLAN:MONTHLY' },
  { text:'Trimestrale', callback_data:'DPLAN:QUARTERLY' },
  { text:'Annuale', callback_data:'DPLAN:ANNUAL' }
]] }};

const KB_PAY = { reply_markup: { inline_keyboard: [[
  { text:'Bonifico', callback_data:'PAY:BANK' },
  { text:'PayPal', callback_data:'PAY:PAYPAL' },
  { text:'USDT', callback_data:'PAY:USDT' }
]] }};

const KB_NET = { reply_markup: { inline_keyboard: [[
  { text:'TRC20', callback_data:'NET:TRC20' },
  { text:'ERC20', callback_data:'NET:ERC20' },
  { text:'BEP20', callback_data:'NET:BEP20' }
]] }};

const sessions = new Map();

function startFlow(chatId, user){
  const name = user?.first_name || user?.username || 'amico';
  sessions.set(chatId, { step:0, data:{ tgSub:'NONE' }, isEA:false });
  bot.sendMessage(chatId, `Ciao ${name}! 👋\nPartiamo.`, { parse_mode:'Markdown' })
    .then(()=> bot.sendMessage(chatId, PROMPT.tgSub, { ...KB_TG, parse_mode:'Markdown' }));
}

bot.onText(/^\/start$/, (m)=> startFlow(m.chat.id, m.from));
bot.onText(/^\/restart$/, (m)=> { sessions.delete(m.chat.id); startFlow(m.chat.id, m.from); });

// callbacks bottoni
bot.on('callback_query', async (q)=>{
  const chatId = q.message.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;

  if (q.data?.startsWith('TGSUB:')) {
    s.data.tgSub = q.data.split(':')[1]; // LIFETIME/SEMIANNUAL/ANNUAL
    await bot.answerCallbackQuery(q.id, { text:`Telegram: ${s.data.tgSub}` });
    await bot.editMessageReplyMarkup({ inline_keyboard:[] }, { chat_id:chatId, message_id:q.message.message_id });
    s.step = 1; // avanti ai dati
    return bot.sendMessage(chatId, PROMPT.phone, { parse_mode:'Markdown' });
  }

  if (q.data?.startsWith('DPLAN:')) {
    s.data.discordPlan = q.data.split(':')[1];
    await bot.answerCallbackQuery(q.id, { text:`Discord: ${s.data.discordPlan}` });
    await bot.editMessageReplyMarkup({ inline_keyboard:[] }, { chat_id:chatId, message_id:q.message.message_id });
    s.step = STEPS.indexOf('payMethod');
    return bot.sendMessage(chatId, PROMPT.payMethod, { ...KB_PAY, parse_mode:'Markdown' });
  }

  if (q.data?.startsWith('PAY:')) {
    s.data.payMethod = q.data.split(':')[1]; // BANK/PAYPAL/USDT
    await bot.answerCallbackQuery(q.id, { text:`Metodo: ${s.data.payMethod}` });
    await bot.editMessageReplyMarkup({ inline_keyboard:[] }, { chat_id:chatId, message_id:q.message.message_id });
    if (s.data.payMethod === 'USDT') {
      s.step = STEPS.indexOf('payNetwork');
      return bot.sendMessage(chatId, PROMPT.payNetwork, { ...KB_NET, parse_mode:'Markdown' });
    }
    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof, { parse_mode:'Markdown' });
  }

  if (q.data?.startsWith('NET:')) {
    s.data.usdtNetwork = q.data.split(':')[1]; // TRC20/ERC20/BEP20
    await bot.answerCallbackQuery(q.id, { text:`Rete: ${s.data.usdtNetwork}` });
    await bot.editMessageReplyMarkup({ inline_keyboard:[] }, { chat_id:chatId, message_id:q.message.message_id });
    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof, { parse_mode:'Markdown' });
  }
});

// messaggi testuali
bot.on('message', async (msg)=>{
  const chatId = msg.chat.id;
  const text = (msg.text||'').trim();
  if (text.startsWith('/')) return;
  const s = sessions.get(chatId); if (!s) return;

  const key = STEPS[s.step];

  if (key === 'phone') {
    if (!isPhone(text)) return bot.sendMessage(chatId,'⚠️ Numero non valido (+ prefisso, 8–15 cifre).',{parse_mode:'Markdown'});
    s.data.phone = text; s.step++; return bot.sendMessage(chatId, PROMPT.telegramNick, { parse_mode:'Markdown' });
  }
  if (key === 'telegramNick') {
    if (!isTelegram(text)) return bot.sendMessage(chatId,'⚠️ Nick Telegram non valido (deve iniziare con `@`).',{parse_mode:'Markdown'});
    s.data.telegramNick = text;

    // EARLYACCESS SOLO sul nick Telegram
    const handle = text.replace(/^@/,'').toLowerCase();
    s.isEA = EARLY.has(handle);
    s.step++; return bot.sendMessage(chatId, PROMPT.discordNick, { parse_mode:'Markdown' });
  }
  if (key === 'discordNick') {
    s.data.discordNick = text; s.step++; return bot.sendMessage(chatId, PROMPT.email, { parse_mode:'Markdown' });
  }
  if (key === 'email') {
    if (!isEmail(text)) return bot.sendMessage(chatId,'⚠️ Email non valida.',{parse_mode:'Markdown'});
    s.data.email = text; s.step++; return bot.sendMessage(chatId, PROMPT.bitgetUid, { parse_mode:'Markdown' });
  }
  if (key === 'bitgetUid') {
    if (!isBitget(text)) return bot.sendMessage(chatId,'⚠️ UID Bitget non valido (10 cifre).',{parse_mode:'Markdown'});
    s.data.bitgetUid = text;

    // 3) Se EARLYACCESS → salta scelta piano (forzato Annuale), vai subito al pagamento
    if (s.isEA) {
      s.data.discordPlan = 'ANNUAL';
      s.step = STEPS.indexOf('payMethod');
      return bot.sendMessage(chatId, '🟡 *Early Access* rilevato: piano **Annuale** a prezzo dedicato.\n\n' + PROMPT.payMethod, { ...KB_PAY, parse_mode:'Markdown' });
    }

    // altrimenti chiedi piano Discord (prezzi dipendono da tgSub)
    s.step = STEPS.indexOf('discordPlanOrSkip');
    return bot.sendMessage(chatId, PROMPT.discordPlanOrSkip, { ...KB_PLAN, parse_mode:'Markdown' });
  }
});

// FOTO: OCR + validazione + inferenza piano + DB + invito
let discord; // helpers

bot.on('photo', async (msg)=>{
  const chatId = msg.chat.id;
  const s = sessions.get(chatId);
  if (!s) return;
  if (STEPS[s.step] !== 'paymentProof') return;

  try {
    const photo = msg.photo?.[msg.photo.length-1];
    if (!photo?.file_id) return bot.sendMessage(chatId,'⚠️ Invia uno *screenshot come immagine*, non come file.',{parse_mode:'Markdown'});

    const text = await ocrTelegramFile(photo.file_id);
    const method = s.data.payMethod;
    const network = s.data.usdtNetwork;

    // 4) estrai campi
    const parsed = extractByMethod({ method, network, text });
    const { fromField, toField, amount, currency, paidAt } = parsed;

    // destinatario valido?
    let validDest = false;
    if (method === 'BANK')   validDest = !!toField && BANKS.includes(normIBAN(toField));
    if (method === 'PAYPAL') validDest = !!toField && PAYPALS.includes(normEmail(toField));
    if (method === 'USDT')   validDest = !!toField && USDT[network] && normAddr(toField) === USDT[network];

    // 3bis) Se non è EARLY: controlliamo che il piano scelto esista per il prezzo pagato;
    // In ogni caso *inferiamo* il piano dall'importo (questo è il requisito #4).
    const EURorUSDT = (method === 'USDT') ? 'USDT' : 'EUR';
    const inferred = inferPlan({
      amount: amount ?? NaN,
      currency: EURorUSDT,
      tgSubType: s.data.tgSub,
      isEA: s.isEA
    });

    if (!validDest || !inferred) {
      let reasons = '';
      if (!validDest) reasons += `• Destinatario non valido o non configurato.\n`;
      if (!inferred) {
        const tbl = s.isEA
          ? `EARLYACCESS: Annuale = ${EURorUSDT==='USDT'?EARLY_PRICES.USDT.ANNUAL:EARLY_PRICES.EUR.ANNUAL} ${EURorUSDT}`
          : (()=>{ const P = priceTable(s.data.tgSub)[EURorUSDT]; return `Valori validi: M=${P.MONTHLY}, T=${P.QUARTERLY}, A=${P.ANNUAL} ${EURorUSDT}`; })();
        reasons += `• Importo non corrisponde a nessun piano.\n  ${tbl}\n  Rilevato: ${isFinite(amount)?amount:'—'} ${EURorUSDT}\n`;
      }
      await bot.sendMessage(chatId, `❌ Ricevuta non valida.\n${reasons}Riprova con uno screenshot più chiaro.`, { parse_mode:'Markdown' });
      return;
    }

    // calcolo fine abbonamento da data pagamento
    const start = paidAt || new Date();
    const plan = s.isEA ? 'ANNUAL' : inferred;
    const end = addDuration(start, plan);

    // upsert Subscriber + snapshot pagamento
    const sub = await prisma.subscriber.upsert({
      where: { email: s.data.email },
      update: {
        phone: s.data.phone,
        telegramNick: s.data.telegramNick,
        discordNick: s.data.discordNick,
        bitgetUid: s.data.bitgetUid,
        verifyCode: { set: undefined }, // mantieni esistente
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
        amountCurrency: EURorUSDT
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
        amountCurrency: EURorUSDT
      }
    });

    // log Payment per audit
    await prisma.payment.create({
      data: {
        subscriberId: sub.id,
        target: 'DISCORD', // questa ricevuta è per il server Discord (da requisito)
        method,
        usdtNetwork: network ?? null,
        fileId: photo.file_id,
        rawText: text,
        fromField: fromField ?? null,
        toField: toField ?? null,
        amount: amount ?? null,
        currency: EURorUSDT,
        paidAt: start,
        validDest: true,
        validAmount: true,
        expectedDest: (() => {
          if (method==='BANK') return BANKS.join(', ');
          if (method==='PAYPAL') return PAYPALS.join(', ');
          if (method==='USDT') return USDT[network] || '';
          return '';
        })(),
        expectedAmt: (() => {
          if (s.isEA) return (EURorUSDT==='USDT'? EARLY_PRICES.USDT.ANNUAL : EARLY_PRICES.EUR.ANNUAL);
          const P = priceTable(s.data.tgSub)[EURorUSDT];
          return P[plan];
        })()
      }
    });

    // crea invito univoco e invialo
    const inviteUrl = await discord.createInviteAndSave?.(sub.id);
    await bot.sendMessage(
      chatId,
      `✅ Pagamento verificato.\n` +
      `Piano: *${plan}* — Inizio: *${start.toISOString().slice(0,10)}* — Fine: *${end.toISOString().slice(0,10)}*\n` +
      (inviteUrl ? `🔗 Entra su Discord: ${inviteUrl}\n\n➡️ Al primo ingresso ti verrà assegnato *YoungTrader* automaticamente.` : '⚠️ Non sono riuscito a creare l’invito. Contatta il supporto.'),
      { parse_mode:'Markdown' }
    );

    sessions.delete(chatId);
  } catch (e) {
    console.error('payment/ocr error', e);
    await bot.sendMessage(chatId, '❌ Errore nell’analisi della ricevuta. Riprova con uno screenshot più chiaro.', { parse_mode:'Markdown' });
  }
});

// Avvio Discord + CRON freeze/unfreeze solo Discord
const discord = await startDiscordBot(prisma, process.env);

cron.schedule('0 12 * * *', async ()=>{
  const now = new Date();
  const subs = await prisma.subscriber.findMany({ where: { discordPlan: { not: null } }});
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

// Health
http.createServer((_,res)=>{res.writeHead(200);res.end('OK');})
  .listen(process.env.PORT||3000, ()=>console.log('Health server on /'));

