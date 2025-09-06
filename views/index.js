import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';

const prisma = new PrismaClient();
const bot = new TelegramBot(process.env.TELEGRAM_BOT_TOKEN, { polling: true });

// === STEP FLOW ===
const STEPS = ['phone', 'telegramNick', 'discordNick', 'bitgetUid', 'email'];
const PROMPT = {
  phone:        '📞 Inserisci il tuo **numero di telefono** con prefisso (es. `+39...`).',
  telegramNick: '✈️ Inserisci il tuo **nickname Telegram** iniziando con `@`.',
  discordNick:  '🎮 Inserisci il tuo **nickname Discord**.',
  bitgetUid:    '🪪 Inserisci il tuo **UID Bitget** (10 cifre).',
  email:        '📧 Inserisci la tua **email**.'
};

const sessions = new Map();

// === VALIDAZIONI ===
// E.164: + prefisso e 8–15 cifre totali (adatta se vuoi più restrittivo)
const isPhone = (v) => /^\+[1-9]\d{7,14}$/.test(v.trim());
// Telegram @username: 5–32, lettere/numeri/underscore
const isTelegram = (v) => /^@[a-zA-Z0-9_]{5,32}$/.test(v.trim());
// Bitget UID: esattamente 10 cifre
const isBitget = (v) => /^\d{10}$/.test(v.trim());
// Email semplice ma robusta
const isEmail = (v) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v.trim());

// Codice di conferma: 8 caratteri alfanumerici maiuscoli (no 0/O, 1/I)
const genCode = () => {
  const alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
  let out = '';
  for (let i = 0; i < 8; i++) out += alphabet[Math.floor(Math.random() * alphabet.length)];
  return out;
};

function startFlow(chatId) {
  sessions.set(chatId, { step: 0, data: {} });
  bot.sendMessage(chatId, 'Ciao! 👋 Ti farò qualche domanda per registrarti.', { parse_mode: 'Markdown' })
    .then(() => bot.sendMessage(chatId, PROMPT.phone, { parse_mode: 'Markdown' }));
}

bot.onText(/^\/start$/, (msg) => startFlow(msg.chat.id));
bot.onText(/^\/restart$/, (msg) => { sessions.delete(msg.chat.id); startFlow(msg.chat.id); });

bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  if (text.startsWith('/')) return;

  const s = sessions.get(chatId);
  if (!s) return;

  const key = STEPS[s.step];

  // --- VALIDAZIONI PER CAMPO ---
  if (key === 'phone' && !isPhone(text)) {
    return bot.sendMessage(
      chatId,
      '⚠️ Numero non valido.\n- Deve iniziare con `+` e prefisso (es. +39)\n- Deve avere 8–15 cifre totali\nEsempio: `+393401234567`',
      { parse_mode: 'Markdown' }
    );
  }
  if (key === 'telegramNick' && !isTelegram(text)) {
    return bot.sendMessage(
      chatId,
      '⚠️ Nickname Telegram non valido.\n- Deve iniziare con `@`\n- 5–32 caratteri, solo lettere/numeri/underscore\nEsempio: `@CryptoNiano`',
      { parse_mode: 'Markdown' }
    );
  }
  if (key === 'bitgetUid' && !isBitget(text)) {
    return bot.sendMessage(
      chatId,
      '⚠️ UID Bitget non valido.\n- Deve essere composto da **10 cifre** (esempio: `1234567890`).',
      { parse_mode: 'Markdown' }
    );
  }
  if (key === 'email' && !isEmail(text)) {
    return bot.sendMessage(
      chatId,
      '⚠️ Email non valida. Esempio: `nome@dominio.com`',
      { parse_mode: 'Markdown' }
    );
  }

  // Salva risposta valida nello stato e passa allo step successivo
  s.data[key] = text;
  s.step++;

  if (s.step >= STEPS.length) {
    // Tutti i dati ok → genera codice e salva
    let verifyCode = genCode();
    for (let i = 0; i < 5; i++) {
      try {
        await prisma.subscriber.upsert({
          where: { email: s.data.email },
          update: { ...s.data, verifyCode },
          create: { ...s.data, verifyCode }
        });
        break;
      } catch (e) {
        // collisione rara sul codice: rigenera e riprova
        if (String(e).includes('verifyCode')) { verifyCode = genCode(); continue; }
        throw e;
      }
    }

    await bot.sendMessage(
      chatId,
      `✅ Registrazione completata!\n\n🔐 *Codice di conferma*: \`${verifyCode}\`\n\nConserva questo codice: ti servirà per il supporto.`,
      { parse_mode: 'Markdown' }
    );
    sessions.delete(chatId);
  } else {
    const nextKey = STEPS[s.step];
    bot.sendMessage(chatId, PROMPT[nextKey], { parse_mode: 'Markdown' });
  }
});

console.log('🚀 Telegram bot avviato con validazioni & codice di conferma');
