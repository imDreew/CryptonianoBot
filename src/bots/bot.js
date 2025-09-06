// src/bots/bot.js
import 'dotenv/config';
import TelegramBot from 'node-telegram-bot-api';
import { PrismaClient } from '@prisma/client';
import cron from 'node-cron';
import { startDiscordBot } from './discord.js';

const prisma = new PrismaClient();
const {
  TELEGRAM_BOT_TOKEN,
  TELEGRAM_GROUP_ID,
  NODE_ENV
} = process.env;

if (!TELEGRAM_BOT_TOKEN) {
  console.error('Missing TELEGRAM_BOT_TOKEN');
  process.exit(1);
}

const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });

const STEPS = ['phone', 'discordNick', 'telegramNick', 'bitgetUid', 'email', 'plan'];
const prompts = {
  phone: '📞 Inserisci il tuo **numero di telefono**:',
  discordNick: '🎮 Inserisci il tuo **nickname Discord**:',
  telegramNick: '✈️ Inserisci il tuo **nickname Telegram**:',
  bitgetUid: '🪪 Inserisci il tuo **UID Bitget**:',
  email: '📧 Inserisci la tua **email**:',
  plan: '📦 Seleziona il **tipo di abbonamento**:'
};

const sessions = new Map();
let discord; // reference agli helpers Discord

// (… tutto il flusso di raccolta dati utente rimane uguale …)

// --- FREEZE/UNFREEZE job
async function telegramRestrict(userId) { /* invariato */ }
async function telegramUnrestrict(userId) { /* invariato */ }

cron.schedule('*/30 * * * *', async () => {
  const now = new Date();
  const subs = await prisma.subscriber.findMany();
  for (const s of subs) {
    if (!s.plan || !s.expiresAt) continue;
    const expired = now > new Date(s.expiresAt);

    if (expired && s.status !== 'FROZEN') {
      await telegramRestrict(s.telegramUserId);
      if (discord) await discord.freeze(s.discordUserId);
      await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'FROZEN' } });
    }

    if (!expired && s.status === 'FROZEN') {
      await telegramUnrestrict(s.telegramUserId);
      if (discord) await discord.unfreeze(s.discordUserId);
      await prisma.subscriber.update({ where: { id: s.id }, data: { status: 'ACTIVE' } });
    }
  }
});

// Avvio
(async () => {
  discord = await startDiscordBot(prisma, process.env);
  console.log(`🚀 Bot Telegram avviato (${NODE_ENV || 'prod'})`);
})();
