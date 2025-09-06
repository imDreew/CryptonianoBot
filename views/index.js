import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';
import http from 'node:http';

const prisma = new PrismaClient();
const { TELEGRAM_BOT_TOKEN } = process.env;

if (!TELEGRAM_BOT_TOKEN) {
  console.error('Missing TELEGRAM_BOT_TOKEN'); process.exit(1);
}

const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

// === WIZARD ===
const STEPS = ['phone', 'telegramNick', 'discordNick', 'bitgetUid', 'email'];
const PROMPT = {
  phone:        '📞 Inserisci il tuo **numero di telefono** con prefisso internazionale (es. `+39XXXXXXXXX`):',
  telegramNick: '✈️ Inserisci il tuo **nickname Telegram** (deve iniziare con `@`):',
  discordNick:  '🎮 Inserisci il tuo **nickname Discord**:',
  bitgetUid:    '🪪 Inserisci il tuo **UID Bitget** (esattamente 10 cifre):',
  email:        '📧 Inserisci la tua **email**:'
};

// === VALIDATORS ===
const isE164Phone = v => /^\+[1-9]\d{7,14}$/.test(String(v).trim());                 // +[country][number], 8–15 cifre totali
const isTelegramHandle = v => /^@[a-zA-Z0-9_]{5,32}$/.test(String(v).trim());        // @username
const isBitgetUid = v => /^\d{10}$/.test(String(v).trim());                           // 10 cifre
const isEmail = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(v).trim());             // semplice ma robusto

// === SUPPORT CODE ===
const genSupportCode = () => Math.random().toString(36).slice(2, 10).toUpperCase();  // 8 chars A-Z0-9

const sessions = new Map(); // chatId -> { step, data }

function startFlow(chatId) {
  sessions.set(chatId, { step: 0, data: {} });
  bot.sendMessage(chatId,
    'Ciao! 👋 Ti farò qualche domanda per registrarti. Se sbagli qualcosa, te lo segnalerò subito.',
    { parse_mode: 'Markdown' }
  ).then(() => bot.sendMessage(chatId, PROMPT.phone, { parse_mode: 'Markdown' }));
}

bot.onText(/^\/start$/, (msg) => startFlow(msg.chat.id));
bot.onText(/^\/restart$/, (msg) => { sessions.delete(msg.chat.id); startFlow(msg.chat.id); });

// === MESSAGES HANDLER ===
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  if (text.startsWith('/')) return;

  const s = sessions.get(chatId);
  if (!s) return;

  const step = STEPS[s.step];

  // Validazioni step-by-step
  if (step === 'phone' && !isE164Phone(text)) {
    return bot.sendMessage(chatId, '⚠️ Formato non valido. Usa il prefisso internazionale, es. `+39XXXXXXXXX`.', { parse_mode: 'Markdown' });
  }
  if (step === 'telegramNick' && !isTelegramHandle(text)) {
    return bot.sendMessage(chatId, '⚠️ Il nickname Telegram deve iniziare con `@` e avere 5–32 caratteri alfanumerici/underscore.', { parse_mode: 'Markdown' });
  }
  if (step === 'bitgetUid' && !isBitgetUid(text)) {
    return bot.sendMessage(chatId, '⚠️ L’UID Bitget deve essere **esattamente 10 cifre**.', { parse_mode: 'Markdown' });
  }
  if (step === 'email' && !isEmail(text)) {
    return bot.sendMessage(chatId, '⚠️ Email non valida. Esempio valido: `nome@dominio.com`.', { parse_mode: 'Markdown' });
  }

  // Salva il valore valido e passa avanti
  s.data[step] = text;
  s.step++;

  if (s.step >= STEPS.length) {
    // Genera supportCode (unico)
    let supportCode = genSupportCode();
    for (let i = 0; i < 5; i++) {
      try {
        const payload = {
          phone: s.data.phone,
          telegramNick: s.data.telegramNick,
          discordNick: s.data.discordNick,
          bitgetUid: s.data.bitgetUid,
          email: s.data.email,
          supportCode
        };
        await prisma.subscriber.upsert({
          where: { email: s.data.email },
          update: payload,
          create: payload
        });
        break; // ok
      } catch (e) {
        // collisione (rarissima) su supportCode
        if (String(e).includes('Unique') || String(e).includes('supportCode')) {
          supportCode = genSupportCode();
          continue;
        }
        console.error('DB save error', e);
        await bot.sendMessage(chatId, '❌ Errore interno nel salvataggio. Riprova più tardi.');
        sessions.delete(chatId);
        return;
      }
    }

    await bot.sendMessage(
      chatId,
      `✅ Registrazione completata!\n\n` +
      `🔐 *Codice di conferma* (da comunicare al supporto):\n\`\`\`\n${supportCode}\n\`\`\`\n` +
      `📩 Email: ${s.data.email}\n` +
      `📞 Telefono: ${s.data.phone}\n` +
      `✈️ Telegram: ${s.data.telegramNick}\n` +
      `🎮 Discord: ${s.data.discordNick}\n` +
      `🪪 Bitget UID: ${s.data.bitgetUid}`,
      { parse_mode: 'Markdown' }
    );

    sessions.delete(chatId);
  } else {
    const next = STEPS[s.step];
    bot.sendMessage(chatId, PROMPT[next], { parse_mode: 'Markdown' });
  }
});

// Healthcheck HTTP (facoltativo, utile per Railway)
http.createServer((_, res) => { res.writeHead(200); res.end('OK - subs-collector-bot'); })
  .listen(process.env.PORT || 3000, () => console.log('Health server on /'));
