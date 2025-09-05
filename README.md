# Abbonamenti — Railway


## Step 0 — Prepara i bot (solo se vuoi già lo Step 2)
**Telegram**
1. Crea bot con BotFather → prendi `TELEGRAM_BOT_TOKEN`.
2. Aggiungi il bot al tuo gruppo/supergruppo come **Amministratore**.
3. Invia `/groupid` nel gruppo o in privato al bot per leggere l'ID e copialo in `TELEGRAM_GROUP_ID`.
4. Scrivi al bot in privato `/start` e leggi il tuo ID con `/groupid` → `TELEGRAM_ADMIN_CHAT_ID`.


**Discord**
1. Crea un'app/bot sul [Developer Portal], abilita **MESSAGE CONTENT INTENT**.
2. Invita il bot nel tuo server.
3. Crea ruolo `FROZEN` (senza permessi) → copia `DISCORD_FROZEN_ROLE_ID`.
4. (Opzionale) crea ruolo `SUBSCRIBER` → `DISCORD_ACTIVE_ROLE_ID`.
5. Copia `DISCORD_GUILD_ID` dal server.


## Step 1 — Solo form + DB
1. Crea un nuovo repo su GitHub e incolla i file di questo progetto.
2. Su Railway: New Project → **Deploy from GitHub Repo**.
3. Aggiungi plugin **PostgreSQL** → Railway setta `DATABASE_URL` automaticamente.
4. (Opzionale) imposta `ADMIN_TOKEN`.
5. Deploy: al boot parte `prisma db push` che crea le tabelle.
6. Apri l’URL di Railway → compila il form → i dati finiscono nel DB e vedi il **codice di collegamento**.


## Step 2 — Abilita i controlli Discord/Telegram
1. Aggiungi su Railway le variabili `.env` della sezione Telegram/Discord.
2. Redeploy. I bot partiranno automaticamente.
3. Flow utente:
- Compila form → riceve **codice**.
- In Telegram: `/link CODICE` → salva il suo Telegram ID.
- In Discord: `!link CODICE` → salva il suo Discord ID.
4. Attiva l’abbonamento via API admin:
```bash
curl -X POST "$RAILWAY_URL/admin/set-plan" \
-H 'Content-Type: application/json' \
-H 'x-admin-token: TUA_CHIAVE' \
-d '{"email":"utente@dominio.com","plan":"MONTHLY","startDate":"2025-09-05"}'