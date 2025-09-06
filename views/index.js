import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';

const prisma = new PrismaClient();
const { TELEGRAM_BOT_TOKEN } = process.env;

if (!TELEGRAM_BOT_TOKEN) {
  console.error('Missing TELEGRAM_BOT_TOKEN'); process.exit(1);
}

const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

// semplice wizard a step
const STEPS = ['phone', 'telegramNick', 'discordNick', 'bitgetUid', 'email'];
const PROMPT = {
  phone:        '📞 Inserisci il tuo **numero di telefono**:',
  telegramNick: '✈️ Inserisci il tuo **nickname Telegram** (es. @username):',
  discordNick:  '🎮 Inserisci il tuo **nickname Discord**:',
  bitgetUid:    '🪪 Inserisci il tuo **UID Bitget**:',
  email:        '📧 Inserisci la tua **email**:'
};

const sessions = new Map(); // chatId -> { step, data }

function startFlow(chatId) {
  sessions.set(chatId, { step: 0, data: {} });
  bot.sendMessage(chatId, 'Ciao! 👋 Ti farò qualche domanda per registrarti.', { parse_mode: 'Markdown' })
    .then(() => bot.sendMessage(chatId, PROMPT.phone, { parse_mode: 'Markdown' }));
}

bot.onText(/^\/start$/, (msg) => startFlow(msg.chat.id));
bot.onText(/^\/restart$/, (msg) => { sessions.delete(msg.chat.id); startFlow(msg.chat.id); });

const isEmail = v => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v || '');

bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  if (text.startsWith('/')) return;

  const s = sessions.get(chatId);
  if (!s) return;

  const key = STEPS[s.step];
  if (key === 'email' && !isEmail(text)) {
    return bot.sendMessage(chatId, '⚠️ Email non valida. Riprova:', { parse_mode: 'Markdown' });
  }
  if (key === 'phone' && text.length < 6) {
    return bot.sendMessage(chatId, '⚠️ Numero di telefono troppo corto. Riprova:', { parse_mode: 'Markdown' });
  }

  s.data[key] = text;
  s.step++;

  if (s.step >= STEPS.length) {
    try {
      await prisma.subscriber.upsert({
        where: { email: s.data.email },
        update: { ...s.data },
        create: { ...s.data }
      });
      await bot.sendMessage(chatId, '✅ Registrazione completata! Grazie.', { parse_mode: 'Markdown' });
    } catch (e) {
      console.error('DB save error', e);
      await bot.sendMessage(chatId, '❌ Errore interno nel salvataggio. Riprova più tardi.');
    } finally {
      sessions.delete(chatId);
    }
  } else {
    const nextKey = STEPS[s.step];
    bot.sendMessage(chatId, PROMPT[nextKey], { parse_mode: 'Markdown' });
  }
});

console.log('🚀 Telegram bot avviato');

<p class="hint">Dopo l'invio vedrai un codice da usare per collegare i bot.</p>
</form>
</body>
</html>
