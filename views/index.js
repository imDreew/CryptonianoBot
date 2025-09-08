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
const isTelegram= v => /^@[a-zA-Z0-9_]{5,32}$/.test((v || '').trim());
const isBitget  = v => /^\d{10}$/.test((v || '').trim());
const isEmail   = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((v || '').trim());

function addDuration(start, plan) {
  const d = new Date(start);
  if (plan === 'MONTHLY')   d.setMonth(d.getMonth() + 1);
  if (plan === 'QUARTERLY') d.setMonth(d.getMonth() + 3);
  if (plan === 'ANNUAL')    d.setFullYear(d.getFullYear() + 1);
  return d;
}

// Prezzi
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
const EARLY_PRICES = { EUR: { ANNUAL: 499 }, USDT: { ANNUAL: 585 } };

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
    const target = currency==='USDT'?585:499;
    if (Math.abs(amount-target)<0.01) return 'ANNUAL';
    return null;
  }
  const tbl=priceTable(tgSubType);
  const T=currency==='USDT'?tbl.USDT:tbl.EUR;
  for (const plan of ['MONTHLY','QUARTERLY','ANNUAL']){
    if (Math.abs(amount-T[plan])<0.01) return plan;
  }
  return null;
}

// ---------- Renew helper ----------
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
const STEPS=['tgSub','phone','telegramNick','discordNick','email','bitgetUid','discordPlanOrSkip','payMethod','payNetwork','paymentProof'];
const PROMPT={ tgSub:'🤖 Seleziona il tuo abbonamento Telegram:', phone:'📞 Inserisci il tuo numero di telefono (+39...)', telegramNick:'✈️ Nick Telegram (@...)', discordNick:'🎮 Nick Discord', email:'📧 Email:', bitgetUid:'🪪 UID Bitget (10 cifre)', discordPlanOrSkip:'📦 Seleziona piano Discord:', payMethod:'💳 Metodo di pagamento:', payNetwork:'🌐 Rete USDT:', paymentProof:'🖼️ Invia screenshot pagamento' };
const KB_TG={ reply_markup:{ inline_keyboard:[[ {text:'Lifetime',callback_data:'TGSUB:LIFETIME'},{text:'Semestrale',callback_data:'TGSUB:SEMIANNUAL'},{text:'Annuale',callback_data:'TGSUB:ANNUAL'} ]] }};
const KB_PLAN={ reply_markup:{ inline_keyboard:[[ {text:'Mensile',callback_data:'DPLAN:MONTHLY'},{text:'Trimestrale',callback_data:'DPLAN:QUARTERLY'},{text:'Annuale',callback_data:'DPLAN:ANNUAL'} ]] }};
const KB_PAY={ reply_markup:{ inline_keyboard:[[ {text:'Bonifico',callback_data:'PAY:BANK'},{text:'PayPal',callback_data:'PAY:PAYPAL'},{text:'USDT',callback_data:'PAY:USDT'} ]] }};
const KB_NET={ reply_markup:{ inline_keyboard:[[ {text:'TRC20',callback_data:'NET:TRC20'},{text:'ERC20',callback_data:'NET:ERC20'},{text:'BEP20',callback_data:'NET:BEP20'} ]] }};
const sessions=new Map();

function startFlow(chatId,user){
  const name=user?.first_name||user?.username||'amico';
  sessions.set(chatId,{ step:0,data:{tgSub:'NONE'},isEA:false });
  bot.sendMessage(chatId,`Ciao ${name}! 👋`,{parse_mode:'Markdown'}).then(()=>bot.sendMessage(chatId,PROMPT.tgSub,{...KB_TG}));
}
bot.onText(/^\/start$/,(m)=>startFlow(m.chat.id,m.from));

bot.on('callback_query',async(q)=>{
  const chatId=q.message.chat.id;
  const s=sessions.get(chatId); if(!s)return;
  if(q.data.startsWith('TGSUB:')){s.data.tgSub=q.data.split(':')[1];s.step=1;return bot.sendMessage(chatId,PROMPT.phone);}
  if(q.data.startsWith('DPLAN:')){s.data.discordPlan=q.data.split(':')[1];s.step=STEPS.indexOf('payMethod');return bot.sendMessage(chatId,PROMPT.payMethod,{...KB_PAY});}
  if(q.data.startsWith('PAY:')){s.data.payMethod=q.data.split(':')[1];if(s.data.payMethod==='USDT'){s.step=STEPS.indexOf('payNetwork');return bot.sendMessage(chatId,PROMPT.payNetwork,{...KB_NET});}s.step=STEPS.indexOf('paymentProof');return bot.sendMessage(chatId,PROMPT.paymentProof);}
  if(q.data.startsWith('NET:')){s.data.usdtNetwork=q.data.split(':')[1];s.step=STEPS.indexOf('paymentProof');return bot.sendMessage(chatId,PROMPT.paymentProof);}
});

bot.on('message',async(msg)=>{
  const chatId=msg.chat.id; const text=(msg.text||'').trim(); if(text.startsWith('/'))return;
  const s=sessions.get(chatId); if(!s)return;
  const key=STEPS[s.step];
  if(key==='phone'){ if(!isPhone(text))return bot.sendMessage(chatId,'⚠️ Numero non valido'); s.data.phone=text; s.step++; return bot.sendMessage(chatId,PROMPT.telegramNick);}
  if(key==='telegramNick'){ if(!isTelegram(text))return bot.sendMessage(chatId,'⚠️ Nick non valido'); s.data.telegramNick=text; const handle=text.replace(/^@/,'').toLowerCase(); const ea=await prisma.earlyAccess.findUnique({where:{telegram_id:handle}}).catch(()=>null); s.isEA=!!ea; s.step++; return bot.sendMessage(chatId,PROMPT.discordNick);}
  if(key==='discordNick'){ s.data.discordNick=text; s.step++; return bot.sendMessage(chatId,PROMPT.email);}
  if(key==='email'){ if(!isEmail(text))return bot.sendMessage(chatId,'⚠️ Email non valida'); s.data.email=text; s.step++; return bot.sendMessage(chatId,PROMPT.bitgetUid);}
  if(key==='bitgetUid'){ if(!isBitget(text))return bot.sendMessage(chatId,'⚠️ UID non valido'); s.data.bitgetUid=text; if(s.isEA){s.data.discordPlan='ANNUAL';s.step=STEPS.indexOf('payMethod');return bot.sendMessage(chatId,'🟡 Early Access: piano Annuale.\n'+PROMPT.payMethod,{...KB_PAY});} s.step=STEPS.indexOf('discordPlanOrSkip'); return bot.sendMessage(chatId,PROMPT.discordPlanOrSkip,{...KB_PLAN});}
});

// Foto ricevuta
bot.on('photo',async(msg)=>{
  const chatId=msg.chat.id; const s=sessions.get(chatId); if(!s||STEPS[s.step]!=='paymentProof')return;
  try{
    const photo=msg.photo?.[msg.photo.length-1]; if(!photo?.file_id)return;
    const text=await ocrTelegramFile(photo.file_id);
    const parsed=extractByMethod({method:s.data.payMethod,network:s.data.usdtNetwork,text});
    const {fromField,toField,amount,currency,paidAt}=parsed;
    const plan=inferPlan({amount,currency,tgSubType:s.data.tgSub,isEA:s.isEA});
    if(!plan) return bot.sendMessage(chatId,'❌ Importo non valido o piano non riconosciuto.');

    // Upsert utente
    const user=await prisma.user.upsert({
      where:{ email:s.data.email },
      update:{ telegramHandle:s.data.telegramNick, phone:s.data.phone, bitgetUid:s.data.bitgetUid },
      create:{ telegramUserId:chatId.toString(), telegramHandle:s.data.telegramNick, phone:s.data.phone, email:s.data.email, bitgetUid:s.data.bitgetUid }
    });

    // Payment
    await prisma.payment.create({data:{ userId:user.id, method:s.data.payMethod, usdtNet:s.data.usdtNetwork||null, proofFileId:photo.file_id, payFrom:fromField||null, payTo:toField||null, amount:amount||0, amountCurrency:currency, paidAt }});

    // Subscription con rinnovo
    const { startAt, endAt, extended }=await renewDiscordSubscription(prisma,user.id,plan,paidAt);

    const inviteUrl=await discord.createInviteAndSave?.(user.id);
    await bot.sendMessage(chatId,`✅ Pagamento verificato.\n${extended?'🔁 Rinnovo':'🆕 Nuova attivazione'}\nPiano: *${plan}*\nInizio: ${startAt.toISOString().slice(0,10)}\nFine: ${endAt.toISOString().slice(0,10)}\n${inviteUrl?`🔗 Discord: ${inviteUrl}`:'⚠️ Invito non generato'}`,{parse_mode:'Markdown'});
    sessions.delete(chatId);
  }catch(e){console.error(e);bot.sendMessage(chatId,'❌ Errore analisi ricevuta.');}
});

// ---------- Discord + CRON ----------
const discord=await startDiscordBot(prisma,process.env);
cron.schedule('0 12 * * *',async()=>{
  const now=new Date();
  const subs=await prisma.subscription.findMany({where:{type:'discord',status:'ACTIVE'}});
  for(const sub of subs){ if(sub.endAt<now){await discord.freeze?.(sub.user?.discordUserId); await prisma.subscription.update({where:{id:sub.id},data:{status:'FROZEN'}});} }
},{timezone:TZ});

// ---------- Health ----------
http.createServer((_,res)=>{res.writeHead(200);res.end('OK');}).listen(process.env.PORT||3000,()=>console.log('Health server on /'));
