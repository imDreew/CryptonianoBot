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

  // Early Access CSV
  EA1_PHONES = '', EA1_TELEGRAMS = '', EA1_EMAILS = '',
  EA2_PHONES = '', EA2_TELEGRAMS = '', EA2_EMAILS = '',

  // Destinatari attesi
  BANK_DEST_IBAN = '',
  PAYPAL_DEST_EMAIL = '',
  USDT_DEST_TRC20 = '',
  USDT_DEST_ERC20 = '',
  USDT_DEST_BEP20 = ''
} = process.env;

if (!TELEGRAM_BOT_TOKEN) { console.error('Missing TELEGRAM_BOT_TOKEN'); process.exit(1); }
const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

// ---------- Utils ----------
const parseCSV = s => (s||'').split(',').map(v=>v.trim()).filter(Boolean).map(v=>v.toLowerCase());
const ea1Phones = parseCSV(EA1_PHONES), ea1Telegr = parseCSV(EA1_TELEGRAMS), ea1Emails = parseCSV(EA1_EMAILS);
const ea2Phones = parseCSV(EA2_PHONES), ea2Telegr = parseCSV(EA2_TELEGRAMS), ea2Emails = parseCSV(EA2_EMAILS);

const isPhone = (v) => /^\+[1-9]\d{7,14}$/.test((v||'').trim());
const isTelegram = (v) => /^@[a-zA-Z0-9_]{5,32}$/.test((v||'').trim());
const isBitget = (v) => /^\d{10}$/.test((v||'').trim());
const isEmail = (v) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((v||'').trim());

const genCode = () => {
  const alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
  let out = ''; for (let i=0;i<8;i++) out += alphabet[Math.floor(Math.random()*alphabet.length)];
  return out;
};

// Prezzi richiesti (EUR) al netto di Early Access / Telegram sub
function priceEUR({ earlyTier, tgSubType, plan }) {
  if (earlyTier === 'EA1') return 499;
  if (earlyTier === 'EA2') return 699;

  if (tgSubType === 'LIFETIME') {
    if (plan === 'MONTHLY') return 69;
    if (plan === 'QUARTERLY') return 169;
    if (plan === 'ANNUAL') return 608;
  }
  if (tgSubType === 'SEMIANNUAL' || tgSubType === 'ANNUAL') {
    if (plan === 'MONTHLY') return 69;
    if (plan === 'QUARTERLY') return 199;
    if (plan === 'ANNUAL') return 699;
  }
  if (plan === 'MONTHLY') return 69;
  if (plan === 'QUARTERLY') return 199;
  if (plan === 'ANNUAL') return 699;
  return 0;
}

function expectedRecipient({ method, network }) {
  if (method === 'BANK') return BANK_DEST_IBAN;
  if (method === 'PAYPAL') return PAYPAL_DEST_EMAIL;
  if (method === 'USDT') {
    if (network === 'TRC20') return USDT_DEST_TRC20;
    if (network === 'ERC20') return USDT_DEST_ERC20;
    if (network === 'BEP20') return USDT_DEST_BEP20;
  }
  return '';
}

// Durate piani
function addDuration(date, planOrTg) {
  const d = new Date(date);
  if (planOrTg === 'MONTHLY') d.setMonth(d.getMonth() + 1);
  else if (planOrTg === 'QUARTERLY') d.setMonth(d.getMonth() + 3);
  else if (planOrTg === 'ANNUAL') d.setFullYear(d.getFullYear() + 1);
  else if (planOrTg === 'SEMIANNUAL') d.setMonth(d.getMonth() + 6);
  else if (planOrTg === 'LIFETIME') return null; // senza scadenza
  return d;
}

// ---------- OCR & PARSING ----------
async function ocrTelegramFile(fileId) {
  // 1) ottieni link pubblico del file
  const file = await bot.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file.file_path}`;
  // 2) scarica come ArrayBuffer
  const resp = await fetch(url);
  const buf = Buffer.from(await resp.arrayBuffer());
  // 3) OCR (ita+eng)
  const { data } = await Tesseract.recognize(buf, 'ita+eng', { logger: ()=>{} });
  return data.text || '';
}

// date tipiche: 01/09/2025, 2025-09-01, 1 set 2025, 1 Sep 2025, 01.09.2025
function parseDate(str) {
  const s = str.replace(/\s+/g,' ');
  const dmy = s.match(/(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})/);
  if (dmy) {
    const [_, dd, mm, yy] = dmy;
    const year = yy.length===2 ? (2000 + parseInt(yy,10)) : parseInt(yy,10);
    return new Date(year, parseInt(mm,10)-1, parseInt(dd,10));
  }
  const ymd = s.match(/(\d{4})[\/.\-](\d{1,2})[\/.\-](\d{1,2})/);
  if (ymd) return new Date(parseInt(ymd[1],10), parseInt(ymd[2],10)-1, parseInt(ymd[3],10));
  const mon = { jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11,
                gen:0,febbr:1,marz:2,apr:3,mag:4,giu:5, lug:6, ago:7, sett:8, set:8, ott:9, nov:10, dic:11 };
  const mmm = s.match(/(\d{1,2})\s+([A-Za-zÀ-ÿ]{3,})\.?\s+(\d{4})/);
  if (mmm) {
    const dd = parseInt(mmm[1],10);
    const mmk = mmm[2].toLowerCase().slice(0,3);
    const year = parseInt(mmm[3],10);
    const mi = mon[mmk];
    if (mi!=null) return new Date(year, mi, dd);
  }
  return null;
}

function parseMoney(str) {
  const m = str.replace(',', '.').match(/(\d{1,4}(?:\.\d{3})*(?:\.\d{2})?|\d+(?:\.\d+)?)/);
  return m ? parseFloat(m[1].replace(/\.(?=\d{3}(\D|$))/g,'')) : NaN;
}

function extractByMethod({ method, network, text }) {
  const lower = text.toLowerCase();

  // candidate fields
  let fromField = '', toField = '', amount = NaN, currency = 'EUR', paidAt = null;

  // importi / valute
  if (method === 'USDT') currency = 'USDT';

  // generic amount lines
  const amtLine = text.match(/(?:importo|amount|totale|total)[^\d]*([\d\.,]+)/i) || text.match(/([\d\.,]+)\s*(?:eur|€|usdt)/i);
  if (amtLine) amount = parseMoney(amtLine[1]);

  // dates
  const dateCandidate = text.match(/(?:data|date|payment date)[:\s-]*([^\n]+)/i)?.[1] || text;
  paidAt = parseDate(dateCandidate);

  if (method === 'BANK') {
    // IBAN: IT.. + alfanum
    const ibanRegex = /[A-Z]{2}\d{2}[A-Z0-9]{10,30}/g;
    const ibans = Array.from(new Set((text.match(ibanRegex) || []).map(x=>x.trim())));
    // eur importi col simbolo €
    if (Number.isNaN(amount)) {
      const euroAmt = text.match(/€\s*([\d\.,]+)/) || text.match(/([\d\.,]+)\s*€/);
      if (euroAmt) amount = parseMoney(euroAmt[1]);
    }
    // heuristica: destinatario atteso noto
    toField = ibans.find(i => i.toUpperCase() === (BANK_DEST_IBAN||'').toUpperCase()) || (ibans[0]||'');
    fromField = ibans.find(i => i.toUpperCase() !== (BANK_DEST_IBAN||'').toUpperCase()) || '';
  }

  if (method === 'PAYPAL') {
    const emailRegex = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig;
    const emails = Array.from(new Set((text.match(emailRegex) || []).map(e=>e.toLowerCase())));
    toField = emails.find(e => e === (PAYPAL_DEST_EMAIL||'').toLowerCase()) || emails[0] || '';
    fromField = emails.find(e => e !== (PAYPAL_DEST_EMAIL||'').toLowerCase()) || '';
  }

  if (method === 'USDT') {
    // TRC20 (T...), EVM 0x...
    const tron = /T[1-9A-HJ-NP-Za-km-z]{33}/g; // base58-ish length 34
    const evm = /0x[a-fA-F0-9]{40}/g;
    const addrs = Array.from(new Set([...(text.match(tron)||[]), ...(text.match(evm)||[])]));
    const dest = expectedRecipient({ method: 'USDT', network });
    toField = addrs.find(a => a.toLowerCase() === (dest||'').toLowerCase()) || '';
    fromField = addrs.find(a => a.toLowerCase() !== (dest||'').toLowerCase()) || '';
    // amount usdt
    if (Number.isNaN(amount)) {
      const usdtAmt = text.match(/([\d\.,]+)\s*USDT/i);
      if (usdtAmt) amount = parseMoney(usdtAmt[1]);
    }
  }

  return { fromField, toField, amount, currency, paidAt };
}

// ---------- Flow & UI ----------
const STEPS = [
  'phone','telegramNick','discordNick','bitgetUid','email',
  'tgSubType',        // NONE/LIFETIME/SEMIANNUAL/ANNUAL
  'discordPlan',      // MONTHLY/QUARTERLY/ANNUAL (se EA → solo ANNUAL)
  'paymentTarget',    // TELEGRAM o DISCORD (questa ricevuta a cosa si riferisce?)
  'payMethod',        // BANK/PAYPAL/USDT
  'payNetwork',       // TRC20/ERC20/BEP20 (solo se USDT)
  'paymentProof',     // invio immagine
];

const PROMPT = {
  phone:'📞 Inserisci il tuo **numero di telefono** con prefisso (es. `+39...`).',
  telegramNick:'✈️ Inserisci il tuo **nickname Telegram** iniziando con `@`.',
  discordNick:'🎮 Inserisci il tuo **nickname Discord**.',
  bitgetUid:'🪪 Inserisci il tuo **UID Bitget** (10 cifre).',
  email:'📧 Inserisci la tua **email**:',

  tgSubType:'🤖 Abbonamento attivo su *Telegram*? Seleziona:',
  discordPlan:'📦 Seleziona il **piano Discord**:',
  paymentTarget:'📄 Questa *ricevuta* si riferisce a:',
  payMethod:'💳 Seleziona il **metodo di pagamento** usato:',
  payNetwork:'🌐 Seleziona la **rete USDT**:',
  paymentProof:'🖼️ Invia **lo screenshot della ricevuta** come *immagine* (non file).'
};

const sessions = new Map();

const kbTgSub = { reply_markup: { inline_keyboard: [[
  { text:'Nessuno', callback_data:'TGSUB:NONE' },
  { text:'Lifetime', callback_data:'TGSUB:LIFETIME' }
],[{ text:'Semestrale', callback_data:'TGSUB:SEMIANNUAL' },
   { text:'Annuale', callback_data:'TGSUB:ANNUAL' }]] }};

const kbPlan = (eaOnly=false)=>({ reply_markup:{ inline_keyboard:[ eaOnly ? [
  { text:'Annuale', callback_data:'DPLAN:ANNUAL' }
] : [
  { text:'Mensile', callback_data:'DPLAN:MONTHLY' },
  { text:'Trimestrale', callback_data:'DPLAN:QUARTERLY' },
  { text:'Annuale', callback_data:'DPLAN:ANNUAL' }
] ] }});

const kbTarget = { reply_markup:{ inline_keyboard:[[ 
  { text:'Canale Telegram', callback_data:'TARGET:TELEGRAM' },
  { text:'Server Discord',  callback_data:'TARGET:DISCORD' }
]] }};

const kbPay = { reply_markup:{ inline_keyboard:[[
  { text:'Bonifico', callback_data:'PAY:BANK' },
  { text:'PayPal',   callback_data:'PAY:PAYPAL' },
  { text:'USDT',     callback_data:'PAY:USDT' }
]] }};

const kbNet = { reply_markup:{ inline_keyboard:[[
  { text:'TRC20', callback_data:'NET:TRC20' },
  { text:'ERC20', callback_data:'NET:ERC20' },
  { text:'BEP20', callback_data:'NET:BEP20' }
]] }};

function startFlow(chatId, user){
  const name = user?.first_name || user?.username || 'amico';
  sessions.set(chatId, { step:0, data:{ tgSubType:'NONE' }, earlyTier:'NONE' });
  bot.sendMessage(chatId, `Ciao ${name}! 👋\nProcedi a compilare le informazioni richieste per la registrazione al server *CRYPTONIANO VIP CLUB* 👑`, { parse_mode:'Markdown' })
    .then(()=> bot.sendMessage(chatId, PROMPT.phone, { parse_mode:'Markdown' }));
}

bot.onText(/^\/start$/, (m)=> startFlow(m.chat.id, m.from));
bot.onText(/^\/restart$/, (m)=> { sessions.delete(m.chat.id); startFlow(m.chat.id, m.from); });

// messaggi testuali
bot.on('message', async (msg)=>{
  const chatId = msg.chat.id;
  const text = (msg.text||'').trim();
  if (text.startsWith('/')) return;
  const s = sessions.get(chatId); if (!s) return;

  const key = STEPS[s.step];

  if (key === 'phone'){
    if (!isPhone(text)) return bot.sendMessage(chatId,'⚠️ Numero non valido (usa `+39...`, 8–15 cifre).',{parse_mode:'Markdown'});
    s.data.phone = text;
    const low = text.toLowerCase();
    if (ea1Phones.includes(low)) s.earlyTier='EA1';
    else if (ea2Phones.includes(low)) s.earlyTier='EA2';
    s.step++; return bot.sendMessage(chatId, PROMPT.telegramNick, { parse_mode:'Markdown' });
  }
  if (key === 'telegramNick'){
    if (!isTelegram(text)) return bot.sendMessage(chatId,'⚠️ Nick Telegram non valido. Deve iniziare con `@`.',{parse_mode:'Markdown'});
    s.data.telegramNick = text;
    const low = text.toLowerCase();
    if (ea1Telegr.includes(low)) s.earlyTier='EA1';
    else if (ea2Telegr.includes(low)) s.earlyTier='EA2';
    s.step++; return bot.sendMessage(chatId, PROMPT.discordNick, { parse_mode:'Markdown' });
  }
  if (key === 'discordNick'){
    s.data.discordNick = text;
    s.step++; return bot.sendMessage(chatId, PROMPT.bitgetUid, { parse_mode:'Markdown' });
  }
  if (key === 'bitgetUid'){
    if (!isBitget(text)) return bot.sendMessage(chatId,'⚠️ UID Bitget non valido. 10 cifre.',{parse_mode:'Markdown'});
    s.data.bitgetUid = text;
    s.step++; return bot.sendMessage(chatId, PROMPT.email, { parse_mode:'Markdown' });
  }
  if (key === 'email'){
    if (!isEmail(text)) return bot.sendMessage(chatId,'⚠️ Email non valida. Esempio: `nome@dominio.com`',{parse_mode:'Markdown'});
    s.data.email = text;
    const low = text.toLowerCase();
    if (ea1Emails.includes(low)) s.earlyTier='EA1';
    else if (ea2Emails.includes(low)) s.earlyTier='EA2';
    s.step++; return bot.sendMessage(chatId, PROMPT.tgSubType, { ...kbTgSub, parse_mode:'Markdown' });
  }
});

// callback bottoni
bot.on('callback_query', async (q)=>{
  const chatId = q.message.chat.id;
  const s = sessions.get(chatId);
  if (!s) { try { await bot.answerCallbackQuery(q.id,{text:'Sessione scaduta. /start'});}catch{} return; }

  if (q.data?.startsWith('TGSUB:')){
    s.data.tgSubType = q.data.split(':')[1]; // NONE/LIFETIME/SEMIANNUAL/ANNUAL
    await bot.answerCallbackQuery(q.id,{text:`Abbonamento Telegram: ${s.data.tgSubType}`});
    await bot.editMessageReplyMarkup({ inline_keyboard:[] },{chat_id:chatId, message_id:q.message.message_id});

    const eaOnly = (s.earlyTier==='EA1'||s.earlyTier==='EA2');
    return bot.sendMessage(chatId, PROMPT.discordPlan, { ...kbPlan(eaOnly), parse_mode:'Markdown' });
  }

  if (q.data?.startsWith('DPLAN:')){
    const plan = q.data.split(':')[1]; // MONTHLY/QUARTERLY/ANNUAL
    if ((s.earlyTier==='EA1'||s.earlyTier==='EA2') && plan!=='ANNUAL') {
      return bot.answerCallbackQuery(q.id,{ text:'Early Access: solo Annuale consentito.' });
    }
    s.data.discordPlan = plan;
    await bot.answerCallbackQuery(q.id,{ text:`Piano Discord: ${plan}` });
    await bot.editMessageReplyMarkup({ inline_keyboard:[] },{chat_id:chatId, message_id:q.message.message_id});
    s.step++; // paymentTarget
    return bot.sendMessage(chatId, PROMPT.paymentTarget, { ...kbTarget, parse_mode:'Markdown' });
  }

  if (q.data?.startsWith('TARGET:')){
    s.data.paymentTarget = q.data.split(':')[1]; // TELEGRAM/DISCORD
    await bot.answerCallbackQuery(q.id,{ text:`Ricevuta per: ${s.data.paymentTarget}` });
    await bot.editMessageReplyMarkup({ inline_keyboard:[] },{chat_id:chatId, message_id:q.message.message_id});
    s.step++; // payMethod
    return bot.sendMessage(chatId, PROMPT.payMethod, { ...kbPay, parse_mode:'Markdown' });
  }

  if (q.data?.startsWith('PAY:')){
    s.data.payMethod = q.data.split(':')[1]; // BANK/PAYPAL/USDT
    await bot.answerCallbackQuery(q.id,{ text:`Metodo: ${s.data.payMethod}` });
    await bot.editMessageReplyMarkup({ inline_keyboard:[] },{chat_id:chatId, message_id:q.message.message_id});
    if (s.data.payMethod==='USDT'){
      s.step++; // payNetwork
      return bot.sendMessage(chatId, PROMPT.payNetwork, { ...kbNet, parse_mode:'Markdown' });
    }
    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof, { parse_mode:'Markdown' });
  }

  if (q.data?.startsWith('NET:')){
    s.data.usdtNetwork = q.data.split(':')[1]; // TRC20/ERC20/BEP20
    await bot.answerCallbackQuery(q.id,{ text:`Rete: ${s.data.usdtNetwork}` });
    await bot.editMessageReplyMarkup({ inline_keyboard:[] },{chat_id:chatId, message_id:q.message.message_id});
    s.step = STEPS.indexOf('paymentProof');
    return bot.sendMessage(chatId, PROMPT.paymentProof, { parse_mode:'Markdown' });
  }
});

// foto: OCR + creazione Payment + validazione + aggiornamento abbonamenti
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

    // Estrai campi dalla ricevuta
    const parsed = extractByMethod({
      method: s.data.payMethod,
      network: s.data.usdtNetwork,
      text
    });

    const expDest = expectedRecipient({ method: s.data.payMethod, network: s.data.usdtNetwork });
    const validDest = expDest ? (parsed.toField||'').toLowerCase() === expDest.toLowerCase() : true;

    // prezzo atteso (sempre EUR per BANK/PAYPAL; USDT per USDT)
    const planRef = s.data.paymentTarget === 'DISCORD' ? s.data.discordPlan : s.data.tgSubType;
    const requiredEUR = priceEUR({ earlyTier: s.earlyTier, tgSubType: s.data.tgSubType, plan: s.data.discordPlan });
    const expectedAmt = s.data.payMethod==='USDT' ? requiredEUR /* se vuoi tariffa in USDT = EUR 1:1 */ : requiredEUR;
    const validAmount = !Number.isNaN(parsed.amount) && Math.abs(parsed.amount - expectedAmt) < 0.01;

    // Data pagamento
    const paidAt = parsed.paidAt || new Date(); // fallback oggi se non si riesce a leggere

    // registra Payment
    const payment = await prisma.payment.create({
      data: {
        subscriber: { connectOrCreate: {
          where: { email: s.data.email },
          create: {
            phone: s.data.phone, telegramNick: s.data.telegramNick, discordNick: s.data.discordNick,
            bitgetUid: s.data.bitgetUid, email: s.data.email, verifyCode: genCode()
          }
        }},
        target: s.data.paymentTarget,
        method: s.data.payMethod,
        usdtNetwork: s.data.usdtNetwork || null,
        fileId: photo.file_id,
        rawText: text,
        fromField: parsed.fromField || null,
        toField: parsed.toField || null,
        amount: isFinite(parsed.amount) ? parsed.amount : null,
        currency: parsed.currency || (s.data.payMethod==='USDT' ? 'USDT' : 'EUR'),
        paidAt,
        validDest,
        validAmount,
        expectedDest: expDest || null,
        expectedAmt: expectedAmt
      }
    });

    // Se tutto ok → aggiorna l'abbonamento corrispondente
    if (validDest && validAmount) {
      const start = paidAt;
      let end;
      if (s.data.paymentTarget === 'DISCORD') {
        end = addDuration(start, s.data.discordPlan);
        await prisma.subscriber.update({
          where: { email: s.data.email },
          data: {
            discordPlan: s.data.discordPlan,
            discordStartDate: start,
            discordEndDate: end,
            status: 'ACTIVE',
            // snapshot pagamento
            payMethod: s.data.payMethod,
            usdtNetwork: s.data.usdtNetwork || null,
            proofFileId: photo.file_id,
            payFrom: parsed.fromField || null,
            payTo: parsed.toField || null,
            amount: parsed.amount || null,
            amountCurrency: parsed.currency || (s.data.payMethod==='USDT' ? 'USDT' : 'EUR')
          }
        });

        // invito Discord univoco
        const invite = await discord.createInvite?.();
        await bot.sendMessage(chatId,
          `✅ Pagamento verificato.\n*Discord* → Piano: ${s.data.discordPlan}\nInizio: ${start.toISOString().slice(0,10)}\nFine: ${end ? end.toISOString().slice(0,10) : '—'}\n` + (invite ? `🔗 Invito: ${invite}` : '⚠️ Non sono riuscito a creare un invito.'),
          { parse_mode:'Markdown' }
        );

      } else { // TELEGRAM
        end = addDuration(start, s.data.tgSubType);
        await prisma.subscriber.update({
          where: { email: s.data.email },
          data: {
            tgSubType: s.data.tgSubType,
            telegramStartDate: start,
            telegramEndDate: end,
            // snapshot pagamento
            payMethod: s.data.payMethod,
            usdtNetwork: s.data.usdtNetwork || null,
            proofFileId: photo.file_id,
            payFrom: parsed.fromField || null,
            payTo: parsed.toField || null,
            amount: parsed.amount || null,
            amountCurrency: parsed.currency || (s.data.payMethod==='USDT' ? 'USDT' : 'EUR')
          }
        });

        await bot.sendMessage(chatId,
          `✅ Pagamento verificato.\n*Telegram* → Tipo: ${s.data.tgSubType}\nInizio: ${start.toISOString().slice(0,10)}\nFine: ${end ? end.toISOString().slice(0,10) : '—'}`,
          { parse_mode:'Markdown' }
        );
      }

      sessions.delete(chatId);
    } else {
      // feedback errore
      let reasons = '';
      if (!validDest) reasons += `• Destinatario non corrisponde a quello atteso.\n`;
      if (!validAmount) reasons += `• Importo atteso: ${expectedAmt} ${s.data.payMethod==='USDT'?'USDT':'EUR'}.\n`;
      await bot.sendMessage(chatId, `❌ Dati non coerenti con la configurazione.\n${reasons}Controlla e reinvia la ricevuta.`, { parse_mode:'Markdown' });
      // manteniamo la sessione allo step paymentProof per nuovo invio
    }

  } catch (e) {
    console.error('OCR/payment error', e);
    await bot.sendMessage(chatId, '❌ Errore nell’analisi della ricevuta. Riprova inviando uno *screenshot* chiaro.', { parse_mode:'Markdown' });
  }
});

// Cron: FREEZE SOLO SU DISCORD
const discord = await startDiscordBot(prisma, process.env);

cron.schedule('0 12 * * *', async ()=>{
  const now = new Date();
  const subs = await prisma.subscriber.findMany({ where: { discordPlan: { not: null } }});
  for (const s of subs) {
    const expired = s.discordEndDate ? now > new Date(s.discordEndDate) : false;
    if (expired && s.status !== 'FROZEN') {
      await discord.freeze?.(s.discordUserId);
      await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'FROZEN' } });
    }
    if (!expired && s.status === 'FROZEN') {
      await discord.unfreeze?.(s.discordUserId);
      await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'ACTIVE' } });
    }
  }
}, { timezone: TZ });

// Health
http.createServer((_,res)=>{res.writeHead(200);res.end('OK');})
  .listen(process.env.PORT||3000, ()=>console.log('Health server on /'));

