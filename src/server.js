import 'dotenv/config';
where: { email },
update: { phone, discordNick, telegramNick, bitgetUid },
create: { phone, discordNick, telegramNick, bitgetUid, email, verifyCode },
});
break;
} catch (e) {
// codice duplicato? rigenera
if (String(e).includes('verifyCode')) verifyCode = await genCode();
else throw e;
}
}


const html = `<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Codice di collegamento</title><style>body{font-family:system-ui;background:#0b1220;color:#e6e6e6;padding:32px} .card{max-width:680px;margin:auto;background:#121a2b;border:1px solid #1f2a44;border-radius:16px;padding:24px} code{font-size:2rem;background:#0e1526;border:1px solid #2a3554;border-radius:12px;padding:10px 14px;display:inline-block;margin:.5rem 0}</style></head><body><div class="card"><h1>Grazie! Ecco il tuo codice</h1><p>Usa questo codice per collegare il tuo account ai bot:</p><p><code>${verifyCode}</code></p><h2>Come usarlo</h2><ol><li><strong>Telegram</strong>: invia al bot <code>/link ${verifyCode}</code>.</li><li><strong>Discord</strong>: manda un messaggio <code>!link ${verifyCode}</code> nel server dove il bot è invitato.</li></ol><p>Una volta collegato, quando attiveremo il piano vedrai automaticamente i permessi corretti.</p></div></body></html>`;
res.status(201).send(html);
});


// Admin API: imposta piano e data inizio (calcola scadenza)
app.post('/admin/set-plan', async (req, res) => {
const token = req.headers['x-admin-token'];
if (!process.env.ADMIN_TOKEN || token !== process.env.ADMIN_TOKEN) {
return res.status(401).json({ error: 'Unauthorized' });
}
const { email, plan, startDate } = req.body; // plan: MONTHLY | ANNUAL
if (!email || !plan) return res.status(400).json({ error: 'email e plan obbligatori' });


const start = startDate ? new Date(startDate) : new Date();
const expires = new Date(start);
if (plan === 'MONTHLY') expires.setMonth(expires.getMonth() + 1);
else if (plan === 'ANNUAL') expires.setFullYear(expires.getFullYear() + 1);


const sub = await prisma.subscriber.update({
where: { email },
data: { plan, startDate: start, expiresAt: expires, status: 'ACTIVE' },
}).catch(() => null);


if (!sub) return res.status(404).json({ error: 'Subscriber non trovato' });
res.json({ ok: true, expiresAt: sub.expiresAt });
});


app.get('/healthz', (_req, res) => res.send('ok'));


const PORT = process.env.PORT || 3000;
app.listen(PORT, async () => {
console.log(`Server online :${PORT}`);


// avvia bot (solo se sono presenti i token/ID)
const telegramHelpers = startTelegramBot(prisma);
const discordHelpers = await startDiscordBot(prisma);


// passiamo i helpers al job scheduler
setTelegramHelpers(telegramHelpers);
setDiscordHelpers(discordHelpers);
scheduleChecks(prisma);


if (!process.env.DATABASE_URL) {
console.warn('⚠️ DATABASE_URL non impostata');
}
});